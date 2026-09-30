"""Durable, attempt-bound assembly of role results without one large model reply."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import threading
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError

from agentflow.common import DomainError, canonical_digest, canonical_json
from agentflow.runtime.workspace import _real_directory

NAMESPACE = '.role_output'
CHECKPOINT = 'role_output_checkpoint.json'
MAX_RESULT_BYTES = 1024 * 1024
MAX_CHECKPOINT_BYTES = 16 * 1024 * 1024
MAX_BUILDERS = 4
MAX_STREAMS = 128
MAX_CHUNKS = 4096
RESULT_REF_SCHEMA = {'type': 'object', 'properties': {
    'id': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
    'revision': {'type': 'integer', 'minimum': 0},
    'digest': {'type': 'string', 'pattern': '^sha256:[0-9a-f]{64}$'}},
    'required': ['id', 'revision', 'digest'], 'additionalProperties': False}


def _get(task, name):
    return task.get(name) if isinstance(task, dict) else getattr(task, name)


def result_identity(task) -> dict:
    identity = {key: _get(task, key) for key in ('attempt_id', 'run_id', 'iteration_id', 'work_item_id',
                                                'fencing_token', 'input_fingerprint')}
    identity['schema_digest'] = canonical_digest(_get(task, 'output_schema'))
    return identity


def _failure(code, message):
    raise DomainError(code, message, 409)


def role_output_limits(output_tokens):
    """Keep request headroom while using the task's frozen model allowance."""
    if type(output_tokens) is not int or output_tokens < 1:
        _failure('result_budget_missing', 'Staged output requires the frozen positive output-token limit')
    return {
        'max_chunk_bytes': max(1, min(MAX_RESULT_BYTES, output_tokens // 2)),
        'max_direct_result_bytes': min(MAX_RESULT_BYTES, max(512, output_tokens * 4)),
    }


def _serialized(value, *, ascii_only=False):
    try:
        return json.dumps(value, ensure_ascii=ascii_only, allow_nan=False, separators=(',', ':')).encode()
    except (TypeError, ValueError) as error:
        raise DomainError('invalid_result_chunk', 'Result chunks must contain finite JSON values', 422) from error


def _directory(path, anchor=None):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        _failure('result_checkpoint_corrupt', 'Output storage requires a canonical absolute directory')
    anchor = Path(anchor) if anchor is not None else Path(path.anchor)
    _real_directory(anchor)
    if not path.is_relative_to(anchor):
        _failure('result_checkpoint_corrupt', 'Output storage escaped its authorized artifact root')
    # The OS sandbox grants this root, not directory listings of its ancestors.
    # Descendants are still opened individually without following any links.
    descriptor = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.relative_to(anchor).parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as error:
        os.close(descriptor)
        raise DomainError('result_checkpoint_corrupt', 'Output storage contains an unsafe directory', 409) from error


def _bytes_at(directory, name, maximum):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_mode & 0o077
                or before.st_uid != os.getuid() or before.st_size > maximum):
            _failure('result_checkpoint_corrupt', 'Output checkpoint is not a private bounded regular file')
        raw = os.read(descriptor, maximum + 1)
        after = os.fstat(descriptor)
        keys = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if len(raw) != before.st_size or any(getattr(before, key) != getattr(after, key) for key in keys):
            _failure('result_checkpoint_corrupt', 'Output checkpoint changed while being read')
        return raw
    finally:
        os.close(descriptor)


def _private_json(path, *, maximum=65536, anchor=None):
    directory = _directory(path.parent, anchor)
    try:
        value = json.loads(_bytes_at(directory, path.name, maximum))
        if not isinstance(value, dict):
            _failure('result_checkpoint_corrupt', 'Output checkpoint must be a JSON object')
        return value
    finally:
        os.close(directory)


def _write(path, value, *, exclusive=False, anchor=None):
    encoded = canonical_json(value).encode()
    directory = _directory(path.parent, anchor)
    temporary = '.result-' + uuid4().hex
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            try:
                os.link(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                if _bytes_at(directory, path.name, MAX_CHECKPOINT_BYTES) != encoded:
                    _failure('result_chunk_conflict', 'A committed chunk identity already has different content')
        else:
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory)


def _checkpoint_bytes(path, anchor=None):
    directory = _directory(path.parent, anchor)
    try:
        return _bytes_at(directory, path.name, MAX_CHECKPOINT_BYTES)
    finally:
        os.close(directory)


def _pointer(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        _failure('invalid_result_field', 'Select a bounded result field or JSON pointer')
    if not value.startswith('/'):
        value = '/' + value.replace('~', '~0').replace('/', '~1')
    if re.search(r'~(?![01])', value):
        _failure('invalid_result_field', 'Invalid JSON pointer escape')
    parts = [part.replace('~1', '/').replace('~0', '~') for part in value[1:].split('/')]
    if len(parts) > 32:
        _failure('invalid_result_field', 'Result field nesting exceeds the supported bound')
    return value, parts


def _slot(fields, pointer):
    _, parts = _pointer(pointer)
    node = fields
    for part in parts[:-1]:
        if isinstance(node, list) and re.fullmatch(r'0|[1-9][0-9]*', part) and int(part) < len(node):
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            _failure('invalid_result_field', 'Initialize parent containers before declaring a nested stream')
    key = parts[-1]
    if isinstance(node, list):
        if not re.fullmatch(r'0|[1-9][0-9]*', key) or int(key) >= len(node):
            _failure('invalid_result_field', 'Result array index is not present')
        return node, int(key)
    if not isinstance(node, dict):
        _failure('invalid_result_field', 'Result field parent must be an object or array')
    return node, key


def _result_size(fields):
    # Match the complete canonical artifact's default JSON encoding, including spaces.
    try:
        size = len(json.dumps(fields, ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError) as error:
        raise DomainError('invalid_result_chunk', 'Result must be finite JSON', 422) from error
    if size > MAX_RESULT_BYTES:
        _failure('result_requires_partition', 'Complete role result exceeds the existing 1 MiB context limit; partition the work')
    return size


def _progress_digest(states):
    # Rebinding opaque draft IDs can change directory order. Identical copies
    # and order changes are not new output progress across recovery attempts.
    contents = {canonical_digest({'fields': state['core']['fields'], 'streams': state['core']['streams']}) for state in states}
    return canonical_digest(sorted(contents))


class ResultBuilderStore:
    def __init__(self, task, artifact_root=None):
        self.task = task
        self.identity = result_identity(task)
        self.artifact_root = Path(artifact_root if artifact_root is not None else _get(task, 'artifact_dir'))
        self.root = self.artifact_root / NAMESPACE
        self.lock = threading.RLock()
        output_tokens = _get(task, 'max_output_tokens')
        limits = role_output_limits(output_tokens)
        self.max_chunk_bytes = limits['max_chunk_bytes']
        self.max_direct_result_bytes = limits['max_direct_result_bytes']

    def limits(self):
        return {'max_chunk_bytes': self.max_chunk_bytes, 'max_result_bytes': MAX_RESULT_BYTES,
                'max_direct_result_bytes': self.max_direct_result_bytes,
                'max_builders': MAX_BUILDERS, 'max_streams': MAX_STREAMS}

    def _budget(self, value):
        if len(_serialized(value, ascii_only=True)) > self.max_chunk_bytes:
            _failure('result_chunk_too_large',
                     f'Submit a smaller chunk: escaped JSON arguments must fit {self.max_chunk_bytes} bytes')

    def _namespace(self, create=False):
        _real_directory(self.artifact_root)
        if not self.root.exists() and not self.root.is_symlink():
            if not create:
                return False
            self.root.mkdir(mode=0o700)
            _write(self.root / 'owner.json', {'version': 2, 'identity': self.identity}, exclusive=True, anchor=self.artifact_root)
            from .text_pages import save_source_history, source_history
            save_source_history(self, source_history(self.identity), exclusive=True)
        _real_directory(self.root)
        owner = _private_json(self.root / 'owner.json', anchor=self.artifact_root)
        if (type(owner.get('version')) is not int or owner not in (
                {'version': 1, 'identity': self.identity}, {'version': 2, 'identity': self.identity})):
            _failure('result_identity_mismatch', 'Staged output belongs to another frozen task')
        return True

    @staticmethod
    def _wrap(core, operations):
        value = {'version': 1, 'core': core, 'core_digest': canonical_digest(core), 'operations': operations}
        return {**value, 'storage_digest': canonical_digest(value)}

    def _validate(self, state):
        if not isinstance(state, dict) or not isinstance(state.get('core'), dict):
            _failure('result_checkpoint_corrupt', 'Staged output manifest has an invalid shape')
        expected = {key: value for key, value in state.items() if key != 'storage_digest'}
        core = state.get('core', {})
        if (state.get('version') != 1 or state.get('storage_digest') != canonical_digest(expected)
                or state.get('core_digest') != canonical_digest(core) or core.get('identity') != self.identity
                or not isinstance(core.get('id'), str) or not re.fullmatch('[0-9a-f]{64}', core['id'])
                or not isinstance(core.get('fields'), dict) or not isinstance(core.get('streams'), dict)
                or len(core['streams']) > MAX_STREAMS
                or type(core.get('revision')) is not int or core['revision'] < 0
                or type(core.get('inherited_chunks', 0)) is not int or core.get('inherited_chunks', 0) < 0
                or not isinstance(state.get('operations'), dict)
                or len(state['operations']) + core.get('inherited_chunks', 0) > MAX_CHUNKS):
            _failure('result_checkpoint_corrupt', 'Staged output identity or content cannot be verified')
        _result_size(core['fields'])
        for pointer, stream in core['streams'].items():
            parent, key = _slot(core['fields'], pointer)
            if not isinstance(stream, dict) or (isinstance(parent, dict) and key not in parent):
                _failure('result_checkpoint_corrupt', 'Staged stream metadata does not match its field')
            value = parent[key]
            if (stream.get('kind') not in {'string', 'array'} or type(stream.get('sealed')) is not bool
                    or not isinstance(value, str if stream['kind'] == 'string' else list)
                    or stream.get('offset') != len(value)):
                _failure('result_checkpoint_corrupt', 'Staged stream cursor is inconsistent')
        return state

    def _load(self, reference, *, exact=True):
        try:
            Draft202012Validator(RESULT_REF_SCHEMA).validate(reference)
        except ValidationError:
            _failure('invalid_result_reference',
                     'Use the exact result_ref object returned by the result tools; omit it or use null to list drafts')
        self._namespace()
        directory = self.root / reference['id']
        _real_directory(directory)
        state = self._validate(_private_json(directory / 'manifest.json', maximum=MAX_CHECKPOINT_BYTES, anchor=self.artifact_root))
        if state['core'].get('id') != reference['id']:
            _failure('result_identity_mismatch', 'Result reference does not match its manifest')
        if exact and self._reference(state) != reference:
            _failure('result_revision_conflict', 'Use result_status to obtain the latest acknowledged revision')
        return state

    @staticmethod
    def _reference(state):
        return {'id': state['core']['id'], 'revision': state['core']['revision'], 'digest': state['core_digest']}

    def _summary(self, state):
        return {'result_ref': self._reference(state), 'streams': copy.deepcopy(state['core']['streams']),
                'all_streams_sealed': all(stream['sealed'] for stream in state['core']['streams'].values()),
                'result_bytes': _result_size(state['core']['fields']),
                'committed_chunks': len(state['operations']) + state['core'].get('inherited_chunks', 0),
                'limits': self.limits()}

    def _states(self):
        states = []
        for path in sorted(self.root.iterdir()):
            if not re.fullmatch('[0-9a-f]{64}', path.name):
                continue
            _real_directory(path)
            manifest = path / 'manifest.json'
            if not manifest.exists() and not manifest.is_symlink():
                intent = _private_json(path / 'begin.json', maximum=MAX_CHECKPOINT_BYTES, anchor=self.artifact_root)
                request = intent.get('request', {})
                expected_id = canonical_digest({'task': self.identity, 'request_id': request.get('request_id')}).split(':')[1]
                if (intent.get('identity') != self.identity or intent.get('begin_fingerprint') != canonical_digest(request)
                        or expected_id != path.name):
                    _failure('result_checkpoint_corrupt', 'Pending initialization is not bound to this task')
                chunks = path / 'chunks'
                if chunks.exists() or chunks.is_symlink():
                    _real_directory(chunks)
                    if any(chunks.iterdir()):
                        _failure('result_checkpoint_corrupt', 'A draft with persisted chunks lost its manifest')
                # Initialization never acknowledged a result_ref. Preserve its
                # intent for begin retry without hiding other committed drafts.
                continue
            state = self._validate(_private_json(manifest, maximum=MAX_CHECKPOINT_BYTES, anchor=self.artifact_root))
            if state['core'].get('id') != path.name:
                _failure('result_identity_mismatch', 'Result directory does not match its manifest')
            states.append(state)
        return states

    def begin(self, fields, streamed_fields, request_id):
        request = {'fields': fields, 'streamed_fields': streamed_fields, 'request_id': request_id}
        self._budget(request)
        if (not isinstance(fields, dict) or not isinstance(streamed_fields, dict) or len(streamed_fields) > MAX_STREAMS
                or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
            _failure('invalid_result_begin', 'Provide small initial fields, declared string/array streams and a stable request_id')
        prepared, streams = copy.deepcopy(fields), {}
        for field, kind in streamed_fields.items():
            pointer, _ = _pointer(field)
            if pointer in streams or kind not in {'string', 'array'}:
                _failure('invalid_result_field', 'Declare each stream once as string or array')
            parent, key = _slot(prepared, pointer)
            if isinstance(parent, dict) and key not in parent:
                parent[key] = '' if kind == 'string' else []
            if not isinstance(parent[key], str if kind == 'string' else list):
                _failure('invalid_result_field', 'Initial stream value has the wrong type')
            streams[pointer] = {'kind': kind, 'offset': len(parent[key]), 'sealed': False}
        _result_size(prepared)
        with self.lock:
            self._namespace(create=True)
            identity = canonical_digest({'task': self.identity, 'request_id': request_id}).split(':')[1]
            fingerprint = canonical_digest(request)
            directory = self.root / identity
            intent = {'identity': self.identity, 'begin_fingerprint': fingerprint, 'request': request}
            if directory.exists() or directory.is_symlink():
                _real_directory(directory)
                if _private_json(directory / 'begin.json', maximum=MAX_CHECKPOINT_BYTES, anchor=self.artifact_root) != intent:
                    _failure('result_begin_conflict', 'This request_id already initialized different fields')
                manifest = directory / 'manifest.json'
                if manifest.exists() or manifest.is_symlink():
                    state = self._validate(_private_json(manifest, maximum=MAX_CHECKPOINT_BYTES, anchor=self.artifact_root))
                    if state['core']['begin_fingerprint'] != fingerprint:
                        _failure('result_begin_conflict', 'This request_id already initialized different fields')
                    return self._summary(state)
                chunks = directory / 'chunks'
                if chunks.exists() and any(chunks.iterdir()):
                    _failure('result_checkpoint_corrupt', 'A draft with persisted chunks lost its manifest')
            else:
                if len([path for path in self.root.iterdir() if path.is_dir()]) >= MAX_BUILDERS:
                    _failure('result_builder_limit', 'Continue an existing builder instead of creating more drafts')
                directory.mkdir(mode=0o700)
                _write(directory / 'begin.json', intent, exclusive=True, anchor=self.artifact_root)
            core = {'id': identity, 'identity': self.identity, 'begin_fingerprint': fingerprint,
                    'revision': 0, 'fields': prepared, 'streams': streams}
            state = self._wrap(core, {})
            (directory / 'chunks').mkdir(mode=0o700, exist_ok=True)
            _real_directory(directory / 'chunks')
            _write(directory / 'manifest.json', state, anchor=self.artifact_root)
            return self._summary(state)

    def append(self, result_ref, field, chunk_id, expected_offset, value, final=False):
        request = {'result_ref': result_ref, 'field': field, 'chunk_id': chunk_id,
                   'expected_offset': expected_offset, 'value': value, 'final': final}
        self._budget(request)
        if not isinstance(chunk_id, str) or not 1 <= len(chunk_id) <= 128 or type(expected_offset) is not int or type(final) is not bool:
            _failure('invalid_result_chunk', 'Use a stable chunk_id, integer cursor and explicit boolean final flag')
        with self.lock:
            state = self._load(result_ref, exact=False)
            request_digest = canonical_digest(request)
            previous = state['operations'].get(chunk_id)
            if previous:
                if previous['request_digest'] != request_digest:
                    _failure('result_chunk_conflict', 'The chunk_id was already committed with different arguments')
                return copy.deepcopy(previous['receipt'])
            if self._reference(state) != result_ref:
                _failure('result_revision_conflict', 'Use the latest acknowledged result_ref')
            if len(state['operations']) + state['core'].get('inherited_chunks', 0) >= MAX_CHUNKS:
                _failure('result_chunk_limit', 'Draft chunk count reached its bound; preserved content was not changed')
            pointer, _ = _pointer(field)
            core = copy.deepcopy(state['core'])
            stream = core['streams'].get(pointer)
            if not stream or stream['sealed'] or any(parent != pointer and pointer.startswith(parent + '/') and part['sealed']
                                                    for parent, part in core['streams'].items()):
                _failure('result_stream_closed', 'Append only to a declared, unsealed stream')
            parent, key = _slot(core['fields'], pointer)
            if expected_offset != len(parent[key]):
                _failure('result_offset_conflict', 'The chunk cursor does not match the acknowledged content')
            if not isinstance(value, str if stream['kind'] == 'string' else list):
                _failure('invalid_result_chunk', 'Append string text or a list of complete JSON items matching the stream type')
            if final and any(child.startswith(pointer + '/') and not part['sealed'] for child, part in core['streams'].items()):
                _failure('result_incomplete', 'Seal nested streams before sealing their parent')
            parent[key] += copy.deepcopy(value)
            stream.update(offset=len(parent[key]), sealed=final)
            core['revision'] += 1
            _result_size(core['fields'])
            interim = self._wrap(core, state['operations'])
            receipt = {'result_ref': self._reference(interim), 'field': pointer,
                       'next_offset': stream['offset'], 'sealed': final}
            operations = {**state['operations'], chunk_id: {'request_digest': request_digest, 'receipt': receipt}}
            updated = self._wrap(core, operations)
            directory = self.root / core['id']
            _write(directory / 'chunks' / (canonical_digest(chunk_id).split(':')[1] + '.json'),
                   {'identity': self.identity, 'request': request, 'request_digest': request_digest}, exclusive=True, anchor=self.artifact_root)
            _write(directory / 'manifest.json', updated, anchor=self.artifact_root)
            return receipt

    def status(self, result_ref=None, field=None, offset=0, limit=12000):
        with self.lock:
            if result_ref is not None:
                state = self._load(result_ref, exact=False)
                if field is None:
                    return self._summary(state)
                if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 24000:
                    _failure('invalid_result_range', 'Use nonnegative offset and a limit from 1 to 24000')
                parent, key = _slot(state['core']['fields'], field)
                value = parent[key]
                if not isinstance(value, (str, list)):
                    _failure('invalid_result_range', 'Select a string or array field to inspect its saved content')
                selected = value[offset:offset + limit]
                if len(_serialized(selected)) > 96 * 1024:
                    _failure('result_read_too_large', 'Use a smaller limit or a nested field pointer')
                return {'result_ref': self._reference(state), 'field': _pointer(field)[0], 'value': selected,
                        'next_offset': min(offset + limit, len(value)), 'has_more': offset + limit < len(value)}
            if field is not None:
                _failure('invalid_result_reference', 'Select a result_ref before reading a saved field')
            if not self._namespace():
                return {'drafts': [], 'limits': self.limits()}
            return {'drafts': [self._summary(state) for state in self._states()], 'limits': self.limits()}

    def preserve_rejected_planning(self, result):
        """Retain an ordinary rejected finish at shutdown, without marking it complete."""
        Draft202012Validator(_get(self.task, 'output_schema')).validate(result)
        _result_size(result)
        with self.lock:
            self._namespace(create=True)
            identity = canonical_digest({'task': self.identity, 'rejected_planning': result}).split(':')[1]
            directory = self.root / identity
            if directory.exists() or directory.is_symlink():
                _real_directory(directory)
                return
            if len([p for p in self.root.iterdir() if p.is_dir()]) >= MAX_BUILDERS:
                _failure('result_builder_limit', 'Draft count reached its bound')
            directory.mkdir(mode=0o700)
            (directory / 'chunks').mkdir(mode=0o700)
            core = dict(id=identity, identity=self.identity, revision=0, fields=copy.deepcopy(result),
                        streams={}, inherited_chunks=1, rejected_planning=True)
            _write(directory / 'manifest.json', self._wrap(core, {}), anchor=self.artifact_root)

    def revise_parallel_work(self, result_ref, parallel_work, request_id, validate):
        """Derive a bounded draft; preserve prose, source receipt and consumed chunks."""
        request = dict(result_ref=result_ref, parallel_work=parallel_work, request_id=request_id)
        self._budget(request)
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            _failure('invalid_result_begin', 'Provide a bounded stable request_id')
        with self.lock:
            source = self._load(result_ref)
            fields = self.resolve(result_ref)
            if 'parallel_work' not in fields:
                _failure('invalid_result_field', 'Source is not a planning result')
            inherited = len(source['operations']) + source['core'].get('inherited_chunks', 0) + 1
            if inherited > MAX_CHUNKS:
                _failure('result_chunk_limit', 'Derived drafts preserve the original chunk limit')
            fields['parallel_work'] = [] if parallel_work is None else copy.deepcopy(parallel_work)
            if parallel_work is not None:
                fields = validate(fields)
                Draft202012Validator(_get(self.task, 'output_schema')).validate(fields)
            _result_size(fields)
            identity = canonical_digest({'task': self.identity, 'request_id': request_id}).split(':')[1]
            fingerprint = canonical_digest(request)
            directory = self.root / identity
            intent = {'identity': self.identity, 'begin_fingerprint': fingerprint, 'request': request}
            if directory.exists() or directory.is_symlink():
                _real_directory(directory)
                if _private_json(directory / 'begin.json', maximum=MAX_CHECKPOINT_BYTES, anchor=self.artifact_root) != intent:
                    _failure('result_begin_conflict', 'This request_id already initialized different fields')
                manifest = directory / 'manifest.json'
                if manifest.exists() or manifest.is_symlink():
                    return self._summary(self._load(self._reference(self._validate(_private_json(manifest, maximum=MAX_CHECKPOINT_BYTES, anchor=self.artifact_root)))))
            else:
                if len([p for p in self.root.iterdir() if p.is_dir()]) >= MAX_BUILDERS:
                    _failure('result_builder_limit', 'Draft count reached its bound')
                directory.mkdir(mode=0o700)
                _write(directory / 'begin.json', intent, exclusive=True, anchor=self.artifact_root)
            streams = {'/parallel_work': {'kind': 'array', 'offset': 0, 'sealed': False}} if parallel_work is None else {}
            core = dict(id=identity, identity=self.identity, begin_fingerprint=fingerprint, revision=0,
                        fields=fields, streams=streams, inherited_chunks=inherited, source_result_ref=copy.deepcopy(result_ref))
            state = self._wrap(core, {})
            (directory / 'chunks').mkdir(mode=0o700, exist_ok=True)
            _real_directory(directory / 'chunks')
            _write(directory / 'manifest.json', state, anchor=self.artifact_root)
            return self._summary(state)

    def resolve(self, result_ref):
        with self.lock:
            state = self._load(result_ref)
            if any(not stream['sealed'] for stream in state['core']['streams'].values()):
                _failure('result_incomplete', 'Every declared stream must be explicitly sealed before finish')
            result = copy.deepcopy(state['core']['fields'])
            Draft202012Validator(_get(self.task, 'output_schema')).validate(result)
            return result


def export_partial(artifact_root: Path, *, expected_identity: dict) -> dict | None:
    from .text_pages import load_source_history
    root = Path(artifact_root)
    namespace = root / NAMESPACE
    if not namespace.exists() and not namespace.is_symlink():
        return None
    task = {**expected_identity, 'output_schema': {}, 'max_output_tokens': 8192}
    manager = ResultBuilderStore(task, root)
    manager.identity = expected_identity
    manager._namespace()
    states = manager._states()
    if not states:
        return None
    progress = _progress_digest(states)
    history = load_source_history(manager)
    payload = {'version': 2 if history is not None else 1, 'kind': 'role_output_checkpoint', 'source_identity': expected_identity,
               'schema_digest': expected_identity['schema_digest'], 'progress_digest': progress, 'drafts': states}
    if history is not None:
        payload['source_reads'] = history
    if len(_serialized(payload)) > MAX_CHECKPOINT_BYTES:
        _failure('result_requires_partition', 'Preserved draft checkpoint exceeds its bounded export size')
    path = root / CHECKPOINT
    _write(path, payload, anchor=root)
    digest = canonical_digest(payload)
    return {'version': 1, 'kind': 'role_output_checkpoint', 'role_output_checkpoint_id': digest, 'digest': digest,
            'path': str(path), 'source_identity': expected_identity, 'schema_digest': expected_identity['schema_digest'],
            'progress_digest': progress, 'draft_count': len(states),
            'committed_chunks': sum(len(state['operations']) + state['core'].get('inherited_chunks', 0) for state in states),
            'result_bytes': sum(_result_size(state['core']['fields']) for state in states)}


def import_partial(target_task, *, artifact_root: Path, source_directory: Path,
                   expected_digest: str, expected_source_identity: dict) -> dict:
    """Controller-only: caller proves recovery authorization before passing these inputs."""
    from .text_pages import (
        RESTART_REVIEW_REASON,
        import_source_history,
        load_source_history,
        validate_source_history,
    )
    source = Path(source_directory)
    _real_directory(source)
    path = source / CHECKPOINT
    raw = _checkpoint_bytes(path, source)
    payload = json.loads(raw)
    if (not isinstance(payload, dict) or 'sha256:' + hashlib.sha256(raw).hexdigest() != expected_digest
            or type(payload.get('version')) is not int or payload['version'] not in {1, 2}
            or payload.get('kind') != 'role_output_checkpoint'
            or payload.get('source_identity') != expected_source_identity
            or payload.get('schema_digest') != expected_source_identity.get('schema_digest')):
        _failure('result_checkpoint_corrupt', 'Authorized output checkpoint does not match its digest or source identity')
    target = ResultBuilderStore(target_task, Path(artifact_root))
    if any(target.identity[key] != expected_source_identity[key] for key in ('run_id', 'work_item_id', 'schema_digest')):
        _failure('result_identity_mismatch', 'Output checkpoint is incompatible with the target work or schema')
    states = payload.get('drafts')
    if not isinstance(states, list) or not 1 <= len(states) <= MAX_BUILDERS:
        _failure('result_checkpoint_corrupt', 'Checkpoint draft list is invalid')
    verifier = ResultBuilderStore(target_task, source)
    verifier.identity = expected_source_identity
    for state in states:
        verifier._validate(state)
    progress = _progress_digest(states)
    if progress != payload.get('progress_digest'):
        _failure('result_checkpoint_corrupt', 'Checkpoint progress digest does not match its contents')
    legacy = payload['version'] == 1 and 'source_reads' not in payload
    history = None if legacy else validate_source_history(payload.get('source_reads'), expected_source_identity)
    restart = _get(target_task, 'role') == 'review' and (legacy or not history['known'])
    if not target.artifact_root.exists() and not target.artifact_root.is_symlink():
        _real_directory(target.artifact_root.parent)
        target.artifact_root.mkdir(mode=0o700)
    target._namespace(create=True)
    current_history = load_source_history(target)
    if (restart and current_history is not None and expected_digest not in current_history['imports']
            and target.status()['drafts']):
        _failure('result_checkpoint_conflict', 'Legacy review restart requires an independent target without drafts')
    import_source_history(target, history, expected_digest, restart=restart)
    if restart:
        return {'role_output_checkpoint_id': expected_digest, 'digest': expected_digest,
                'source_identity': expected_source_identity, 'target_identity': target.identity,
                'progress_digest': progress, 'builders': [], 'mode': 'restart_review', 'reason': RESTART_REVIEW_REASON}
    for state in states:
        identity = canonical_digest({'target': target.identity, 'source': expected_digest, 'draft': state['core']['id']}).split(':')[1]
        directory = target.root / identity
        core = {**copy.deepcopy(state['core']), 'id': identity, 'identity': target.identity, 'revision': 0,
                'source_checkpoint_id': expected_digest, 'source_identity': expected_source_identity,
                'source_revision': state['core']['revision'],
                'inherited_chunks': state['core'].get('inherited_chunks', 0) + len(state['operations'])}
        imported = target._wrap(core, {})
        if not directory.exists():
            directory.mkdir(mode=0o700)
            (directory / 'chunks').mkdir(mode=0o700)
            _write(directory / 'manifest.json', imported, anchor=target.artifact_root)
        else:
            _real_directory(directory)
            existing = target._validate(_private_json(directory / 'manifest.json', maximum=MAX_CHECKPOINT_BYTES, anchor=target.artifact_root))
            if existing['core'].get('source_checkpoint_id') != expected_digest:
                _failure('result_checkpoint_conflict', 'Destination draft belongs to another recovery checkpoint')
    return {'role_output_checkpoint_id': expected_digest, 'digest': expected_digest,
            'source_identity': expected_source_identity, 'target_identity': target.identity,
            'progress_digest': progress, 'builders': target.status()['drafts']}


def import_planning_final(target_task, *, artifact_root, result, source_identity, source_digest):
    """Controller-only import after source receipt authorization; never publishes a result."""
    target = ResultBuilderStore(target_task, Path(artifact_root))
    if (not isinstance(source_identity, dict)
            or any(target.identity[k] != source_identity.get(k) for k in ('run_id', 'work_item_id'))
            or target.identity['attempt_id'] == source_identity.get('attempt_id')
            or not isinstance(result, dict) or 'parallel_work' not in result
            or canonical_digest(result) != source_digest):
        _failure('result_identity_mismatch', 'Planning result does not match its authorized source')
    Draft202012Validator(_get(target_task, 'output_schema')).validate(result)
    _result_size(result)
    if not target.artifact_root.exists() and not target.artifact_root.is_symlink():
        _real_directory(target.artifact_root.parent)
        target.artifact_root.mkdir(mode=0o700)
    target._namespace(create=True)
    identity = canonical_digest({'target': target.identity, 'source': source_identity, 'digest': source_digest}).split(':')[1]
    directory = target.root / identity
    core = dict(id=identity, identity=target.identity, revision=0, fields=copy.deepcopy(result), streams={},
                source_checkpoint_id=source_digest, source_identity=copy.deepcopy(source_identity),
                begin_fingerprint=source_digest, inherited_chunks=1)
    imported = target._wrap(core, {})
    if directory.exists() or directory.is_symlink():
        _real_directory(directory)
        existing = target._validate(_private_json(directory / 'manifest.json', maximum=MAX_CHECKPOINT_BYTES, anchor=target.artifact_root))
        if existing != imported:
            _failure('result_checkpoint_conflict', 'Destination draft differs from the authorized planning result')
    else:
        if len([p for p in target.root.iterdir() if p.is_dir()]) >= MAX_BUILDERS:
            _failure('result_builder_limit', 'Draft count reached its bound')
        directory.mkdir(mode=0o700)
        (directory / 'chunks').mkdir(mode=0o700)
        _write(directory / 'manifest.json', imported, anchor=target.artifact_root)
    return dict(progress_digest=canonical_digest(result), source_identity=copy.deepcopy(source_identity),
                target_identity=target.identity, builders=target.status()['drafts'], mode='revise_planning_output')

"""Lossless text pages below the SDK's per-observation truncation boundary."""
from __future__ import annotations

import json
import re
from pathlib import Path

from agentflow.common import DomainError, canonical_digest

from .output_builder import MAX_CHECKPOINT_BYTES, _private_json, _write

# SDK 1.49 defaults max_message_chars to 30000; its serializer also has a
# separate 50000-character ceiling. Budget the JSON envelope, including escapes.
MAX_PAGE_JSON_CHARACTERS = 28000
SOURCE_READS = 'source_reads.json'
RESTART_REVIEW_REASON = ('The legacy output checkpoint has no trustworthy source-read history. '
    'Its original draft is preserved but is not imported as a result. Perform a new independent review '
    'of the frozen source and required scope, then write a new result; do not reuse the old conclusion.')


def source_history(identity, files=None, imports=None, restart=None, *, known=True):
    value = {'version': 1, 'identity': identity, 'files': files or {}, 'imports': imports or {},
             'restart': restart, 'known': known}
    return {**value, 'storage_digest': canonical_digest(value)}


def validate_source_history(value, identity):
    def invalid():
        raise DomainError('result_checkpoint_corrupt', 'Source-read history cannot be verified', 409)

    if not isinstance(value, dict):
        invalid()
    expected = {key: item for key, item in value.items() if key != 'storage_digest'}
    if (set(value) != {'version', 'identity', 'files', 'imports', 'restart', 'known', 'storage_digest'}
            or type(value['version']) is not int or value['version'] != 1 or value['identity'] != identity
            or value['storage_digest'] != canonical_digest(expected)
            or type(value['known']) is not bool
            or not isinstance(value['files'], dict) or not isinstance(value['imports'], dict)):
        invalid()
    digest_pattern = r'sha256:[0-9a-f]{64}'
    for path, entry in value['files'].items():
        if (not isinstance(path, str) or not path or len(path) > 4096 or '\x00' in path
                or Path(path).is_absolute() or '..' in Path(path).parts or str(Path(path)) != path
                or not isinstance(entry, dict) or set(entry) != {'digest', 'length'}
                or not isinstance(entry['digest'], str) or not re.fullmatch(digest_pattern, entry['digest'])
                or type(entry['length']) is not int or not 0 <= entry['length'] <= 256 * 1024):
            invalid()
    for key, digest in value['imports'].items():
        if (not isinstance(key, str) or not re.fullmatch(digest_pattern, key)
                or not isinstance(digest, str) or not re.fullmatch(digest_pattern, digest)):
            invalid()
    restart = value['restart']
    if restart is not None and (not isinstance(restart, str) or not re.fullmatch(digest_pattern, restart)):
        invalid()
    return value


def load_source_history(store):
    owner = _private_json(store.root / 'owner.json', anchor=store.artifact_root)
    if owner == {'version': 1, 'identity': store.identity}:
        return None  # Old namespace: missing history is unknown, never an empty read set.
    try:
        value = _private_json(store.root / SOURCE_READS, maximum=MAX_CHECKPOINT_BYTES, anchor=store.artifact_root)
    except (OSError, ValueError, UnicodeError) as error:
        raise DomainError('result_checkpoint_corrupt', 'Required source-read history is missing or unreadable', 409) from error
    return validate_source_history(value, store.identity)


def save_source_history(store, value, *, exclusive=False):
    validate_source_history(value, store.identity)
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_CHECKPOINT_BYTES:
        raise DomainError('result_requires_partition', 'Source-read history exceeds its checkpoint bound', 422)
    _write(store.root / SOURCE_READS, value, exclusive=exclusive, anchor=store.artifact_root)


def import_source_history(store, previous, checkpoint_id, *, restart=False):
    current = load_source_history(store)
    if current is None:
        raise DomainError('result_checkpoint_corrupt', 'Cannot import into a legacy output namespace', 409)
    digest = previous['storage_digest'] if previous is not None else checkpoint_id
    if checkpoint_id in current['imports']:
        if current['imports'][checkpoint_id] != digest:
            raise DomainError('result_checkpoint_conflict', 'Source-read import identity changed', 409)
        return
    files = {**(previous['files'] if previous else {}), **current['files']}
    value = source_history(store.identity, files, {**current['imports'], checkpoint_id: digest},
        checkpoint_id if restart else current['restart'],
        known=restart or (current['known'] and previous is not None and previous['known']))
    save_source_history(store, value)


def text_page(path, text, digest, offset, limit):
    start, end = min(offset, len(text)), min(offset + limit, len(text))

    def result(stop):
        return {'path': path, 'text': text[start:stop], 'offset': offset,
                'next_offset': stop, 'has_more': stop < len(text), 'digest': digest}

    page = result(end)
    if len(json.dumps(page, ensure_ascii=False)) <= MAX_PAGE_JSON_CHARACTERS:
        return page
    low, high = start, end
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(result(middle), ensure_ascii=False)) <= MAX_PAGE_JSON_CHARACTERS:
            low = middle
        else:
            high = middle - 1
    if low == start:
        raise DomainError('file_not_supported', 'Text page metadata exceeds the observation bound', 422)
    return result(low)


class SourceReads:
    """Track delivered ranges; overlapping or reordered reads never hide gaps."""

    def __init__(self, store=None):
        self.store = store
        self.files = {}
        self.history = None
        self.restart_reason = None
        if store is not None:
            store._namespace(create=True)
            self.history = load_source_history(store)
            if self.history is not None:
                self.files = {path: {**entry, 'ranges': []} for path, entry in self.history['files'].items()}
                if self.history['restart']:
                    self.restart_reason = RESTART_REVIEW_REASON

    def _check_history(self):
        if self.store is not None and load_source_history(self.store) != self.history:
            raise DomainError('result_checkpoint_corrupt', 'Source-read history changed during this worker', 409)

    def record(self, path, page, length):
        self._check_history()
        previous = self.files.get(path)
        ranges = list(previous['ranges']) if previous and previous['digest'] == page['digest'] else []
        ranges.append((min(page['offset'], length), page['next_offset']))
        merged = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        if self.store is not None and self.history is not None:
            history = source_history(self.store.identity,
                {**self.history['files'], path: {'digest': page['digest'], 'length': length}},
                self.history['imports'], self.history['restart'], known=self.history['known'])
            save_source_history(self.store, history)
            self.history = history
        self.files[path] = {'digest': page['digest'], 'length': length, 'ranges': merged}

    def require_complete(self):
        self._check_history()
        if self.store is not None and (self.history is None or not self.history['known']):
            raise DomainError('review_source_history_unavailable', RESTART_REVIEW_REASON, 422)
        missing = []
        for path, value in sorted(self.files.items()):
            cursor = 0
            for start, end in value['ranges']:
                if start > cursor:
                    break
                cursor = max(cursor, end)
            if not value['ranges'] or cursor < value['length']:
                missing.append({'path': path, 'offset': cursor, 'limit': 24000})
        if missing:
            raise DomainError('review_source_incomplete',
                'Read remaining source pages with read_code before finishing this review; '
                'missing source cannot be treated as reviewed.', 422, {'next_reads': missing[:8]})

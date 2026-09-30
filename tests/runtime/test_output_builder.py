"""Durable staged role output using temporary fixtures and no model execution."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError

from agentflow.adapters.openhands import output_builder
from agentflow.adapters.openhands.output_builder import (
    CHECKPOINT,
    MAX_RESULT_BYTES,
    NAMESPACE,
    ResultBuilderStore,
    export_partial,
    import_partial,
    result_identity,
)
from agentflow.adapters.openhands.tools import ToolBroker
from agentflow.common import DomainError, canonical_digest

CASE_SCHEMA = {'type': 'object', 'properties': {
    'case_id': {'type': 'string'}, 'requirement_id': {'type': 'string'},
    'target_config_id': {'type': 'string'}, 'phase': {'type': 'string', 'enum': ['unit', 'integration']},
    'framework_case_ids': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1}},
    'required': ['case_id', 'requirement_id', 'target_config_id', 'phase', 'framework_case_ids'],
    'additionalProperties': False}
PLAN_SCHEMA = {'type': 'object', 'properties': {'title': {'type': 'string', 'minLength': 1},
    'content': {'type': 'string', 'minLength': 1},
    'test_cases': {'type': 'array', 'items': CASE_SCHEMA, 'minItems': 1}},
    'required': ['title', 'content', 'test_cases'], 'additionalProperties': False}


def case(index):
    return {'case_id': f'case-{index}', 'requirement_id': f'requirement-{index}',
        'target_config_id': 'web', 'phase': 'unit', 'framework_case_ids': [f'tests/case-{index}']}


def make_store(task, root, **changes):
    root.mkdir(mode=0o700)
    bound = task.model_copy(update={'artifact_dir': root, 'output_schema': PLAN_SCHEMA,
        'max_output_tokens': 8192, 'max_tool_calls': 100, **changes})
    return SimpleNamespace(task=bound, root=root, manager=ResultBuilderStore(bound))


@pytest.fixture
def staged(task, tmp_path):
    return make_store(task, tmp_path / 'staged-artifacts')


def begin_content(staged):
    return staged.manager.begin({'title': 'Plan', 'test_cases': [case(0)]}, {'content': 'string'}, 'plan')


def test_large_model_can_append_a_complete_chinese_section_beyond_four_kib(task, tmp_path):
    staged = make_store(task, tmp_path / 'large-model', max_output_tokens=65536)
    initial = begin_content(staged)
    content = '完整验收。' * 400
    assert len(json.dumps(content, ensure_ascii=True).encode()) > 4096
    ref = append(staged.manager, initial['result_ref'], 'content', 'complete-section', 0, content, final=True)
    assert staged.manager.resolve(ref)['content'] == content
    assert initial['limits']['max_chunk_bytes'] <= staged.task.max_output_tokens // 2
    from agentflow.adapters.openhands.worker import _role_instructions
    assert f"within {initial['limits']['max_chunk_bytes']} bytes" in _role_instructions(staged.task)


def test_complete_inline_result_uses_model_allowance_instead_of_sixteen_kib(task, tmp_path):
    staged = make_store(task, tmp_path / 'inline-large-model', max_output_tokens=65536, role='product')
    broker = ToolBroker(staged.task)
    result = {'title': '完整测试方案', 'content': '完整' * 4000, 'test_cases': [case(0)]}
    assert len(json.dumps(result, ensure_ascii=False).encode()) > 16384
    assert broker.finish(result) == result
    assert json.loads((staged.root / 'openhands_final.json').read_text()) == result
    assert broker.calls == 1


def test_small_model_still_requires_staging_for_large_inline_results(task, tmp_path):
    staged = make_store(task, tmp_path / 'inline-small-model', max_output_tokens=1024, role='product')
    broker = ToolBroker(staged.task)
    with pytest.raises(DomainError) as error:
        broker.finish({'title': 'Plan', 'content': 'x' * 5000, 'test_cases': [case(0)]})
    assert error.value.code == 'result_requires_staging'
    assert not (staged.root / 'openhands_final.json').exists()


def test_rebinding_or_copying_drafts_does_not_manufacture_output_progress(staged, task, tmp_path):
    first = begin_content(staged)['result_ref']
    first = append(staged.manager, first, 'content', 'first', 0, 'Saved first draft')
    second = staged.manager.begin({'title': 'Another plan', 'test_cases': [case(1)]}, {'content': 'string'}, 'other')['result_ref']
    append(staged.manager, second, 'content', 'second', 0, 'Saved second draft')
    receipt = export_partial(staged.root, expected_identity=result_identity(staged.task))
    target = make_store(task, tmp_path / 'imported-progress', attempt_id='next-attempt', input_fingerprint=canonical_digest('new input'))
    imported = import_partial(target.task, artifact_root=target.root, source_directory=staged.root,
        expected_digest=receipt['digest'], expected_source_identity=result_identity(staged.task))
    exported = export_partial(target.root, expected_identity=result_identity(target.task))
    assert imported['progress_digest'] == exported['progress_digest'] == receipt['progress_digest']
    # A second identical draft has no new completed text or sealing progress.
    duplicate = staged.manager.begin({'title': 'Plan', 'test_cases': [case(0)]}, {'content': 'string'}, 'copy')['result_ref']
    append(staged.manager, duplicate, 'content', 'copy-text', 0, 'Saved first draft')
    assert export_partial(staged.root, expected_identity=result_identity(staged.task))['progress_digest'] == receipt['progress_digest']


def append(manager, ref, field, chunk, offset, value, *, final=False):
    return manager.append(ref, field, chunk, offset, value, final=final)['result_ref']


def manifest(staged, ref):
    return staged.root / NAMESPACE / ref['id'] / 'manifest.json'


def test_long_content_and_test_case_batches_reassemble_exactly_without_repeating_the_result(staged):
    manager = staged.manager
    initial = manager.begin({'title': 'Complete plan'}, {'content': 'string', 'test_cases': 'array'}, 'long-plan')
    ref = initial['result_ref']
    parts = [f'第 {index} 段：保持所有验收条件。\n["quoted", "\\path", "🙂"]\n' * 15 for index in range(20)]
    offset = 0
    for index, text in enumerate(parts):
        ref = append(manager, ref, 'content', f'content-{index}', offset, text, final=index == len(parts) - 1)
        offset += len(text)
    cases = [case(index) for index in range(60)]
    for index in range(0, len(cases), 5):
        ref = append(manager, ref, 'test_cases', f'cases-{index}', index, cases[index:index + 5],
                     final=index + 5 == len(cases))
    expected = {'title': 'Complete plan', 'content': ''.join(parts), 'test_cases': cases}
    assert len(json.dumps(expected).encode()) > initial['limits']['max_chunk_bytes'] * 10
    assert manager.resolve(ref) == expected
    summary = manager.status(initial['result_ref'])
    assert summary['result_ref'] == ref and summary['all_streams_sealed']
    assert summary['committed_chunks'] == len(parts) + len(cases) // 5
    page = manager.status(ref, 'content', offset=13, limit=97)
    assert page['value'] == expected['content'][13:110] and page['next_offset'] == 110 and page['has_more']
    array_page = manager.status(ref, 'test_cases', offset=53, limit=7)
    assert array_page['value'] == cases[53:] and not array_page['has_more']
    expected['test_cases'][0]['case_id'] = 'caller mutation'
    assert manager.resolve(ref)['test_cases'][0] == case(0)


def test_each_stream_must_be_explicitly_sealed_and_completed_fields_cannot_be_overwritten(staged):
    manager = staged.manager
    ref = manager.begin({'title': 'Kept title'}, {'content': 'string', 'test_cases': 'array'}, 'seal')['result_ref']
    ref = append(manager, ref, 'content', 'text', 0, 'Complete body')
    ref = append(manager, ref, 'test_cases', 'cases', 0, [case(1)], final=True)
    with pytest.raises(DomainError) as error:
        manager.resolve(ref)
    assert error.value.code == 'result_incomplete'
    ref = append(manager, ref, 'content', 'seal-content', len('Complete body'), '', final=True)
    assert manager.resolve(ref)['title'] == 'Kept title'
    for field, value, offset in [('content', 'lost body', len('Complete body')), ('title', 'replacement', 10)]:
        with pytest.raises(DomainError) as error:
            manager.append(ref, field, f'overwrite-{field}', offset, value)
        assert error.value.code == 'result_stream_closed'
    with pytest.raises(DomainError) as error:
        manager.begin({'title': 'Replaced title'}, {'content': 'string', 'test_cases': 'array'}, 'seal')
    assert error.value.code == 'result_begin_conflict'
    assert manager.resolve(ref)['content'] == 'Complete body'


def test_sealing_does_not_bypass_the_complete_frozen_schema(staged):
    manager = staged.manager
    ref = manager.begin({}, {'content': 'string', 'test_cases': 'array'}, 'schema')['result_ref']
    ref = append(manager, ref, 'content', 'content', 0, 'Text', final=True)
    ref = append(manager, ref, 'test_cases', 'cases', 0, [{'case_id': 'incomplete'}], final=True)
    assert manager.status(ref)['all_streams_sealed']
    with pytest.raises(ValidationError):
        manager.resolve(ref)
    assert not (staged.root / 'openhands_final.json').exists()


@pytest.mark.parametrize('changes', [
    {'attempt_id': 'another-attempt'}, {'fencing_token': 2}, {'input_fingerprint': 'sha256:' + 'b' * 64},
    {'run_id': 'another-run'}, {'work_item_id': 'another-work'}, {'iteration_id': 'another-iteration'},
    {'output_schema': {'type': 'object'}},
])
def test_result_namespace_rejects_a_different_frozen_task_identity(staged, changes):
    ref = begin_content(staged)['result_ref']
    other = ResultBuilderStore(staged.task.model_copy(update=changes))
    with pytest.raises(DomainError) as error:
        other.status(ref)
    assert error.value.code == 'result_identity_mismatch'
    assert staged.manager.status(ref)['committed_chunks'] == 0


def test_reference_revision_digest_and_cursor_conflicts_do_not_change_committed_content(staged):
    manager = staged.manager
    original = begin_content(staged)['result_ref']
    current = append(manager, original, 'content', 'first', 0, 'Saved')
    for invalid in (original, {**current, 'revision': current['revision'] + 1},
                    {**current, 'digest': 'sha256:' + '0' * 64}):
        with pytest.raises(DomainError) as error:
            manager.append(invalid, 'content', 'wrong-reference', 5, 'Unexpected')
        assert error.value.code == 'result_revision_conflict'
    with pytest.raises(DomainError):
        manager.status({**current, 'id': '0' * 64})
    with pytest.raises(DomainError) as error:
        manager.append(current, 'content', 'wrong-offset', 0, 'Unexpected')
    assert error.value.code == 'result_offset_conflict'
    assert manager.status(original)['result_ref'] == current
    assert manager.status(current, 'content')['value'] == 'Saved'


def test_repeated_exact_chunk_is_idempotent_even_after_later_chunks(staged):
    manager = staged.manager
    ref = begin_content(staged)['result_ref']
    request = {'result_ref': ref, 'field': 'content', 'chunk_id': 'stable-chunk',
               'expected_offset': 0, 'value': 'First ', 'final': False}
    receipt = manager.append(**request)
    final = append(manager, receipt['result_ref'], 'content', 'second-chunk', 6, 'second', final=True)
    reopened = ResultBuilderStore(staged.task)
    assert reopened.append(**request) == receipt
    with pytest.raises(DomainError) as error:
        reopened.append(**{**request, 'value': 'Conflicting '})
    assert error.value.code == 'result_chunk_conflict'
    assert reopened.resolve(final)['content'] == 'First second'
    assert reopened.status(ref)['committed_chunks'] == 2


def test_immutable_chunk_survives_failed_manifest_update_and_retry_commits_it_once(staged, monkeypatch):
    manager = staged.manager
    ref = begin_content(staged)['result_ref']
    path = manifest(staged, ref)
    before = path.read_bytes()
    original_write = output_builder._write
    failed = False

    def interrupted(path_to_write, value, *, exclusive=False, anchor=None):
        nonlocal failed
        if Path(path_to_write) == path and not failed:
            failed = True
            raise OSError('Fixture interruption after immutable chunk publication')
        return original_write(path_to_write, value, exclusive=exclusive, anchor=anchor)

    request = {'result_ref': ref, 'field': 'content', 'chunk_id': 'durable-chunk',
               'expected_offset': 0, 'value': 'Exactly once', 'final': True}
    with monkeypatch.context() as patch:
        patch.setattr(output_builder, '_write', interrupted)
        with pytest.raises(OSError, match='Fixture interruption'):
            manager.append(**request)
    assert path.read_bytes() == before
    chunks = list((path.parent / 'chunks').glob('*.json'))
    assert len(chunks) == 1
    saved_chunk = chunks[0].read_bytes()
    reopened = ResultBuilderStore(staged.task)
    assert reopened.status(ref)['committed_chunks'] == 0
    receipt = reopened.append(**request)
    assert reopened.append(**request) == receipt
    assert reopened.resolve(receipt['result_ref'])['content'] == 'Exactly once'
    assert reopened.status(ref)['committed_chunks'] == 1
    assert list((path.parent / 'chunks').glob('*.json')) == chunks
    assert chunks[0].read_bytes() == saved_chunk


def test_append_rejects_replaced_chunk_directory_without_any_external_write(staged, tmp_path):
    manager = staged.manager
    ref = begin_content(staged)['result_ref']
    ref = append(manager, ref, 'content', 'accepted', 0, 'Keep this prefix')
    path = manifest(staged, ref)
    manifest_before = path.read_bytes()
    chunks = path.parent / 'chunks'
    original_chunks = {file.name: file.read_bytes() for file in chunks.iterdir()}
    preserved = tmp_path / 'original-chunks'
    chunks.rename(preserved)
    outside = tmp_path / 'external-directory'
    outside.mkdir(mode=0o700)
    sentinel = outside / 'owner-file.txt'
    sentinel.write_bytes(b'Unrelated owner content')
    before_directory = outside.stat()
    before_sentinel = sentinel.stat()
    chunks.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DomainError) as error:
        manager.append(ref, 'content', 'must-not-publish', len('Keep this prefix'), 'Outside write', final=True)
    assert error.value.code == 'result_checkpoint_corrupt'
    assert set(outside.iterdir()) == {sentinel}
    assert sentinel.read_bytes() == b'Unrelated owner content'
    assert (outside.stat().st_mtime_ns, outside.stat().st_ctime_ns) == (
        before_directory.st_mtime_ns, before_directory.st_ctime_ns)
    assert (sentinel.stat().st_ino, sentinel.stat().st_mtime_ns, sentinel.stat().st_ctime_ns) == (
        before_sentinel.st_ino, before_sentinel.st_mtime_ns, before_sentinel.st_ctime_ns)
    assert path.read_bytes() == manifest_before
    assert {file.name: file.read_bytes() for file in preserved.iterdir()} == original_chunks
    assert manager.status(ref, 'content')['value'] == 'Keep this prefix'


def test_begin_recovers_from_first_manifest_failure_using_its_persisted_intent(staged, monkeypatch):
    manager = staged.manager
    request = {'fields': {'title': 'Initialization survives', 'test_cases': [case(0)]},
               'streamed_fields': {'content': 'string'}, 'request_id': 'interrupted-begin'}
    original_write = output_builder._write
    failed = False

    def interrupted(path, value, *, exclusive=False, anchor=None):
        nonlocal failed
        if Path(path).name == 'manifest.json' and not failed:
            failed = True
            assert (Path(path).parent / 'begin.json').is_file()
            raise OSError('Fixture interruption after durable begin intent')
        return original_write(path, value, exclusive=exclusive, anchor=anchor)

    with monkeypatch.context() as patch:
        patch.setattr(output_builder, '_write', interrupted)
        with pytest.raises(OSError, match='Fixture interruption'):
            manager.begin(**request)
    directories = [path for path in manager.root.iterdir() if path.is_dir()]
    assert len(directories) == 1
    directory = directories[0]
    intent = (directory / 'begin.json').read_bytes()
    assert not (directory / 'manifest.json').exists()
    assert not list((directory / 'chunks').iterdir())
    reopened = ResultBuilderStore(staged.task)
    recovered = reopened.begin(**request)
    assert recovered['result_ref']['id'] == directory.name
    assert recovered['result_ref']['revision'] == 0 and recovered['committed_chunks'] == 0
    ref = append(reopened, recovered['result_ref'], 'content', 'after-recovery', 0, 'Saved once', final=True)
    assert reopened.begin(**request)['result_ref'] == ref
    assert reopened.resolve(ref) == {**request['fields'], 'content': 'Saved once'}
    assert [path for path in manager.root.iterdir() if path.is_dir()] == directories
    assert (directory / 'begin.json').read_bytes() == intent


@pytest.mark.parametrize('persisted_chunk', [False, True], ids=['empty-initialization', 'missing-manifest-with-chunk'])
def test_pending_empty_begin_does_not_hide_committed_drafts_but_lost_chunk_manifest_is_rejected(
        staged, monkeypatch, persisted_chunk):
    manager = staged.manager
    ref = begin_content(staged)['result_ref']
    ref = append(manager, ref, 'content', 'accepted-prefix', 0, 'Previously committed content')
    original_manifest = manifest(staged, ref).read_bytes()
    prior_export = export_partial(staged.root, expected_identity=result_identity(staged.task))
    prior_checkpoint = (staged.root / CHECKPOINT).read_bytes()
    original_write = output_builder._write

    def interrupted(path, value, *, exclusive=False, anchor=None):
        if Path(path).name == 'manifest.json':
            assert (Path(path).parent / 'begin.json').is_file()
            raise OSError('Fixture interruption before the second builder manifest')
        return original_write(path, value, exclusive=exclusive, anchor=anchor)

    with monkeypatch.context() as patch:
        patch.setattr(output_builder, '_write', interrupted)
        with pytest.raises(OSError, match='Fixture interruption'):
            manager.begin({'title': 'Not acknowledged yet'}, {'content': 'string'}, 'pending-second-builder')
    pending = [path for path in manager.root.iterdir() if path.is_dir() and path.name != ref['id']]
    assert len(pending) == 1 and (pending[0] / 'begin.json').is_file()
    assert not (pending[0] / 'manifest.json').exists()
    if persisted_chunk:
        # Any persisted chunk means this is no longer an empty initialization;
        # status/export must not silently discard evidence when its manifest is lost.
        output_builder._write(pending[0] / 'chunks' / 'orphan.json', {'persisted_chunk': 'must be reconciled'})
    reopened = ResultBuilderStore(staged.task)
    if persisted_chunk:
        for action in (reopened.status,
                       lambda: export_partial(staged.root, expected_identity=result_identity(staged.task))):
            with pytest.raises(DomainError) as error:
                action()
            assert error.value.code == 'result_checkpoint_corrupt'
    else:
        drafts = reopened.status()['drafts']
        assert len(drafts) == 1 and drafts[0]['result_ref'] == ref
        assert drafts[0]['committed_chunks'] == 1
        assert export_partial(staged.root, expected_identity=result_identity(staged.task)) == prior_export
    assert reopened.status(ref, 'content')['value'] == 'Previously committed content'
    assert manifest(staged, ref).read_bytes() == original_manifest
    assert (staged.root / CHECKPOINT).read_bytes() == prior_checkpoint
    assert (pending[0] / 'begin.json').is_file() and not (pending[0] / 'manifest.json').exists()


def test_exact_one_mib_result_is_allowed_but_an_extra_byte_does_not_change_it(task, tmp_path):
    schema = {'type': 'object', 'properties': {'content': {'type': 'string'}},
              'required': ['content'], 'additionalProperties': False}
    env = make_store(task, tmp_path / 'size-boundary', output_schema=schema)
    ref = env.manager.begin({}, {'content': 'string'}, 'size')['result_ref']
    path = manifest(env, ref)
    state = json.loads(path.read_bytes())
    # Seed an already-committed near-limit draft to exercise the real 1 MiB
    # boundary without hundreds of quadratic full-manifest/fsync writes.
    prefix = 'x' * (MAX_RESULT_BYTES - len(json.dumps({'content': ''}).encode()) - 1)
    core = copy.deepcopy(state['core'])
    core['fields']['content'] = prefix
    core['streams']['/content']['offset'] = len(prefix)
    output_builder._write(path, env.manager._wrap(core, state['operations']))
    ref = env.manager.status(ref)['result_ref']
    edge = append(env.manager, ref, 'content', 'at-limit', len(prefix), 'y')
    assert env.manager.status(edge)['result_bytes'] == MAX_RESULT_BYTES
    before = path.read_bytes()
    with pytest.raises(DomainError) as error:
        env.manager.append(edge, 'content', 'over-limit', len(prefix) + 1, 'z')
    assert error.value.code == 'result_requires_partition' and path.read_bytes() == before
    sealed = append(env.manager, edge, 'content', 'seal', len(prefix) + 1, '', final=True)
    result = env.manager.resolve(sealed)
    assert result['content'] == prefix + 'y'
    assert len(json.dumps(result, ensure_ascii=False).encode()) == MAX_RESULT_BYTES


def test_chunk_budget_counts_the_complete_escaped_request_and_preserves_prior_chunks(staged):
    manager = staged.manager
    ref = begin_content(staged)['result_ref']
    request = {'result_ref': ref, 'field': 'content', 'chunk_id': 'at-budget',
               'expected_offset': 0, 'value': '', 'final': False}
    overhead = len(json.dumps(request, ensure_ascii=True, separators=(',', ':')).encode())
    request['value'] = 'x' * (manager.max_chunk_bytes - overhead)
    assert len(json.dumps(request, ensure_ascii=True, separators=(',', ':')).encode()) == manager.max_chunk_bytes
    current = manager.append(**request)['result_ref']
    before = manifest(staged, current).read_bytes()
    unicode_value = '🙂' * (manager.max_chunk_bytes // 8)
    too_large = {**request, 'result_ref': current, 'chunk_id': 'escaped-budget',
                 'expected_offset': len(request['value']), 'value': unicode_value}
    assert len(json.dumps(too_large, ensure_ascii=False, separators=(',', ':')).encode()) < manager.max_chunk_bytes
    with pytest.raises(DomainError) as error:
        manager.append(**too_large)
    assert error.value.code == 'result_chunk_too_large'
    assert manifest(staged, current).read_bytes() == before
    assert manager.status(current, 'content')['value'] == request['value']
    with pytest.raises(DomainError) as error:
        manager.begin({'title': 'x' * manager.max_chunk_bytes}, {}, 'oversized-begin')
    assert error.value.code == 'result_chunk_too_large'


def partial(staged):
    summary = staged.manager.begin({'title': 'Saved draft'}, {'content': 'string', 'test_cases': 'array'}, 'partial')
    ref = append(staged.manager, summary['result_ref'], 'content', 'saved-text', 0, 'Acknowledged')
    ref = append(staged.manager, ref, 'test_cases', 'saved-cases', 0, [case(1)], final=True)
    receipt = export_partial(staged.root, expected_identity=result_identity(staged.task))
    return ref, receipt


def test_export_has_stable_digest_and_import_rebinds_progress_to_a_new_attempt_without_resetting_it(staged, tmp_path):
    source_ref, receipt = partial(staged)
    checkpoint = staged.root / CHECKPOINT
    before = checkpoint.read_bytes()
    assert receipt['digest'] == 'sha256:' + hashlib.sha256(before).hexdigest()
    assert export_partial(staged.root, expected_identity=result_identity(staged.task)) == receipt
    assert checkpoint.read_bytes() == before and receipt['committed_chunks'] == 2
    target = make_store(staged.task, tmp_path / 'next-attempt', attempt_id='recovered-attempt',
                        operation_id='recovered-operation', fencing_token=2, input_fingerprint='sha256:' + 'b' * 64)
    kwargs = {'artifact_root': target.root, 'source_directory': staged.root,
              'expected_digest': receipt['digest'], 'expected_source_identity': receipt['source_identity']}
    imported = import_partial(target.task, **kwargs)
    assert imported['source_identity'] == receipt['source_identity']
    assert imported['target_identity'] == result_identity(target.task)
    assert imported['progress_digest'] == receipt['progress_digest']
    draft = imported['builders'][0]
    ref = draft['result_ref']
    assert ref['id'] != source_ref['id'] and ref['revision'] == 0 and draft['committed_chunks'] == 2
    with pytest.raises(DomainError):
        target.manager.status(source_ref)
    assert target.manager.status(ref, 'content')['value'] == 'Acknowledged'
    ref = append(target.manager, ref, 'content', 'continued', len('Acknowledged'), ' + continued', final=True)
    expected = {'title': 'Saved draft', 'content': 'Acknowledged + continued', 'test_cases': [case(1)]}
    assert target.manager.resolve(ref) == expected
    replay = import_partial(target.task, **kwargs)
    assert replay['builders'][0]['result_ref'] == ref
    assert replay['builders'][0]['committed_chunks'] == 3
    assert target.manager.resolve(ref) == expected
    assert staged.manager.status(source_ref, 'content')['value'] == 'Acknowledged'
    assert checkpoint.read_bytes() == before


def test_lower_output_cap_preserves_imported_large_chunks_and_limits_only_new_appends(staged, tmp_path):
    source_ref = begin_content(staged)['result_ref']
    preserved = 'Previously acknowledged text. ' * 50
    source_ref = append(staged.manager, source_ref, 'content', 'large-source-chunk', 0, preserved)
    receipt = export_partial(staged.root, expected_identity=result_identity(staged.task))
    checkpoint_bytes = (staged.root / CHECKPOINT).read_bytes()
    target = make_store(staged.task, tmp_path / 'smaller-output-attempt', attempt_id='smaller-cap-attempt',
                        operation_id='smaller-cap-operation', fencing_token=2, max_output_tokens=1024,
                        input_fingerprint='sha256:' + 'c' * 64)
    assert staged.manager.max_chunk_bytes == 4096
    assert target.manager.max_chunk_bytes == 512 < len(preserved.encode())
    imported = import_partial(target.task, artifact_root=target.root, source_directory=staged.root,
        expected_digest=receipt['digest'], expected_source_identity=receipt['source_identity'])
    draft = imported['builders'][0]
    ref = draft['result_ref']
    assert draft['committed_chunks'] == 1
    assert target.manager.status(ref, 'content')['value'] == preserved
    before = manifest(target, ref).read_bytes()
    with pytest.raises(DomainError) as error:
        target.manager.append(ref, 'content', 'exceeds-new-cap', len(preserved), 'x' * 512)
    assert error.value.code == 'result_chunk_too_large'
    assert manifest(target, ref).read_bytes() == before
    ref = append(target.manager, ref, 'content', 'small-continuation', len(preserved), 'Done.', final=True)
    assert target.manager.resolve(ref) == {'title': 'Plan', 'test_cases': [case(0)], 'content': preserved + 'Done.'}
    assert target.manager.status(ref)['committed_chunks'] == 2
    assert staged.manager.status(source_ref, 'content')['value'] == preserved
    assert (staged.root / CHECKPOINT).read_bytes() == checkpoint_bytes


@pytest.mark.parametrize('mismatch', ['digest', 'source_identity', 'schema'])
def test_import_rejects_wrong_checkpoint_hash_source_identity_or_schema_before_creating_target_state(staged, tmp_path, mismatch):
    _, receipt = partial(staged)
    changes = {'output_schema': {'type': 'object'}} if mismatch == 'schema' else {}
    target = make_store(staged.task, tmp_path / 'rejected-import', attempt_id='new-attempt', **changes)
    identity = receipt['source_identity']
    if mismatch == 'source_identity':
        identity = {**identity, 'attempt_id': 'unrelated-source'}
    digest = 'sha256:' + '0' * 64 if mismatch == 'digest' else receipt['digest']
    with pytest.raises(DomainError) as error:
        import_partial(target.task, artifact_root=target.root, source_directory=staged.root,
                       expected_digest=digest, expected_source_identity=identity)
    assert error.value.code == ('result_identity_mismatch' if mismatch == 'schema' else 'result_checkpoint_corrupt')
    assert not (target.root / NAMESPACE).exists()
    assert export_partial(staged.root, expected_identity=result_identity(staged.task)) == receipt


@pytest.mark.parametrize('reserved', [f'{NAMESPACE}/owner.json', f'{NAMESPACE.upper()}/owner.json',
    CHECKPOINT, CHECKPOINT.upper(), 'nested/' + CHECKPOINT, 'openhands_final.json'])
def test_document_writer_cannot_modify_builder_staging_or_preserved_checkpoints(staged, reserved):
    broker = ToolBroker(staged.task)
    initial = broker.execute('result_begin', fields={'title': 'Reserved', 'test_cases': [case(1)]},
                             streamed_fields={'content': 'string'}, request_id='broker')
    receipt = broker.execute('result_append', result_ref=initial['result_ref'], field='content',
                             chunk_id='chunk', expected_offset=0, value='Preserve')
    checkpoint = export_partial(staged.root, expected_identity=result_identity(staged.task))
    before = {path: path.read_bytes() for path in staged.root.rglob('*') if path.is_file()}
    with pytest.raises(DomainError) as error:
        broker.write_document(reserved, 'replacement')
    assert error.value.code == 'forbidden_path'
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert set(path for path in staged.root.rglob('*') if path.is_file()) == set(before)
    assert broker.execute('result_status', result_ref=receipt['result_ref'], field='content')['value'] == 'Preserve'
    assert canonical_digest(json.loads((staged.root / CHECKPOINT).read_bytes())) == checkpoint['digest']

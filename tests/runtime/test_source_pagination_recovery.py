"""Source obligations survive process/output recovery without trusting old read ranges."""
import json
import platform

import pytest

from agentflow.adapters.openhands.output_builder import (
    CHECKPOINT,
    NAMESPACE,
    export_partial,
    import_partial,
    result_identity,
)
from agentflow.adapters.openhands.tools import ToolBroker
from agentflow.common import DomainError, canonical_digest, canonical_json


def sealed_review(broker):
    ref = broker.result_begin({}, {'review': 'string'}, 'review')['result_ref']
    return broker.result_append(ref, 'review', 'body', 0, 'Preserved review', final=True)['result_ref']


def checkpoint(task):
    return export_partial(task.artifact_dir, expected_identity=result_identity(task))


def restore(task, target, receipt):
    return import_partial(target, artifact_root=target.artifact_dir, source_directory=task.artifact_dir,
        expected_digest=receipt['digest'], expected_source_identity=result_identity(task))


def incomplete(broker, ref=None):
    with pytest.raises(DomainError) as error:
        broker.finish_ref(ref) if ref else broker.finish({'review': 'Preserved review'})
    assert error.value.code == 'review_source_incomplete'
    assert not (broker.documents / 'openhands_final.json').exists()
    return error.value.details['next_reads']


def test_imported_review_requires_complete_reread_and_preserves_original_draft(task, tmp_path):
    (task.workspace / 'large.py').write_text('x' * 40000)
    broker = ToolBroker(task)
    broker.read_code('large.py')
    broker.read_code('original.py')
    ref = sealed_review(broker)
    incomplete(broker, ref)
    manifest = task.artifact_dir / NAMESPACE / ref['id'] / 'manifest.json'
    original = manifest.read_bytes()
    receipt = checkpoint(task)
    target = task.model_copy(update={'attempt_id': 'new-attempt', 'fencing_token': 2,
        'artifact_dir': tmp_path / 'resumed'})
    imported = restore(task, target, receipt)
    resumed = ToolBroker(target)
    new_ref = imported['builders'][0]['result_ref']
    pending = incomplete(resumed, new_ref)
    assert {item['path']: item['offset'] for item in pending} == {'large.py': 0, 'original.py': 0}
    resumed.read_code('large.py', offset=24000)
    incomplete(resumed, new_ref)
    resumed.read_code('large.py')
    resumed.read_code('original.py')
    assert resumed.finish_ref(new_ref) == {'review': 'Preserved review'}
    assert manifest.read_bytes() == original


def test_worker_restart_cannot_forget_reads_even_without_a_staged_result(task):
    (task.workspace / 'large.py').write_text('x' * 40000)
    broker = ToolBroker(task)
    broker.read_code('large.py')
    restarted = ToolBroker(task)
    assert incomplete(restarted) == [{'path': 'large.py', 'offset': 0, 'limit': 24000}]
    restarted.read_code('large.py')
    restarted.read_code('large.py', offset=24000)
    assert restarted.finish({'review': 'Preserved review'})


def test_even_empty_sources_must_be_reopened_after_restart(task):
    (task.workspace / 'empty.py').write_text('')
    ToolBroker(task).read_code('empty.py')
    restarted = ToolBroker(task)
    assert incomplete(restarted)[0]['path'] == 'empty.py'
    restarted.read_code('empty.py')
    assert restarted.finish({'review': 'Empty source verified'})


def test_repeated_import_is_idempotent_and_restart_requires_fresh_ranges(task, tmp_path):
    broker = ToolBroker(task)
    broker.read_code('original.py')
    sealed_review(broker)
    receipt = checkpoint(task)
    target = task.model_copy(update={'attempt_id': 'next', 'artifact_dir': tmp_path / 'next'})
    first = restore(task, target, receipt)
    resumed = ToolBroker(target)
    ref = first['builders'][0]['result_ref']
    resumed.read_code('original.py')
    (task.workspace / 'newly-opened.py').write_text('x' * 40000)
    resumed.read_code('newly-opened.py')
    before = checkpoint(target)
    assert restore(task, target, receipt) == first
    assert checkpoint(target) == before
    pending = incomplete(ToolBroker(target), ref)
    assert {item['path']: item['offset'] for item in pending} == {'newly-opened.py': 0, 'original.py': 0}


@pytest.mark.parametrize('damage', ['missing', 'malformed'])
def test_lost_or_corrupt_local_metadata_never_becomes_an_empty_read_set(task, damage):
    broker = ToolBroker(task)
    broker.read_code('original.py')
    ref = sealed_review(broker)
    path = task.artifact_dir / NAMESPACE / 'source_reads.json'
    if damage == 'missing':
        path.unlink(missing_ok=True)
    else:
        path.write_text('{"version":1,"files":{}}')
        path.chmod(0o600)
    with pytest.raises(DomainError):
        ToolBroker(task).finish_ref(ref)
    assert not (task.artifact_dir / 'openhands_final.json').exists()


@pytest.mark.parametrize('metadata', ['missing', None, {'version': 1, 'files': {}}])
def test_untrusted_checkpoint_cannot_resume_review_but_fresh_review_can_continue(task, tmp_path, metadata):
    broker = ToolBroker(task)
    broker.read_code('original.py')
    ref = sealed_review(broker)
    receipt = checkpoint(task)
    path = task.artifact_dir / CHECKPOINT
    value = json.loads(path.read_text())
    if metadata == 'missing':
        value.pop('source_reads', None)
    else:
        value['source_reads'] = metadata
    path.write_text(canonical_json(value))
    path.chmod(0o600)
    receipt['digest'] = canonical_digest(value)
    target = task.model_copy(update={'attempt_id': 'next', 'artifact_dir': tmp_path / 'next'})
    with pytest.raises(DomainError):
        restore(task, target, receipt)
    assert not target.artifact_dir.exists()
    assert broker.output.resolve(ref) == {'review': 'Preserved review'}
    # Explicitly starting a separate review does not discard or reuse the old draft.
    fresh = ToolBroker(target)
    fresh.read_code('original.py')
    assert fresh.finish({'review': 'New independent review'})


def test_legacy_review_checkpoint_preserves_draft_and_starts_independent_review(task, tmp_path):
    broker = ToolBroker(task)
    broker.read_code('original.py')
    ref = sealed_review(broker)
    receipt = checkpoint(task)
    path = task.artifact_dir / CHECKPOINT
    value = json.loads(path.read_text())
    value['version'] = 1
    value.pop('source_reads', None)
    path.write_text(canonical_json(value))
    path.chmod(0o600)
    receipt['digest'] = canonical_digest(value)
    original = path.read_bytes()
    target = task.model_copy(update={'attempt_id': 'next', 'artifact_dir': tmp_path / 'next'})
    imported = restore(task, target, receipt)
    assert imported['mode'] == 'restart_review' and imported['builders'] == []
    assert imported['progress_digest'] == receipt['progress_digest']
    assert restore(task, target, receipt) == imported
    resumed = ToolBroker(target)
    assert resumed.output.status()['drafts'] == []
    assert resumed.source_reads.restart_reason
    resumed.read_code('original.py')
    fresh_ref = sealed_review(resumed)
    assert restore(task, target, receipt) == imported
    assert resumed.finish_ref(fresh_ref) == {'review': 'Preserved review'}
    assert path.read_bytes() == original
    assert broker.output.resolve(ref) == {'review': 'Preserved review'}


def test_actual_legacy_namespace_is_not_silently_upgraded_to_trusted_empty_history(task, tmp_path):
    broker = ToolBroker(task)
    sealed_review(broker)
    owner = task.artifact_dir / NAMESPACE / 'owner.json'
    legacy_owner = canonical_json({'version': 1, 'identity': result_identity(task)})
    owner.write_text(legacy_owner)
    (owner.parent / 'source_reads.json').unlink()
    receipt = checkpoint(task)
    value = json.loads((task.artifact_dir / CHECKPOINT).read_text())
    assert value['version'] == 1 and 'source_reads' not in value
    assert owner.read_text() == legacy_owner
    target = task.model_copy(update={'attempt_id': 'new', 'artifact_dir': tmp_path / 'new'})
    assert restore(task, target, receipt)['mode'] == 'restart_review'


def test_legacy_non_review_import_does_not_launder_unknown_source_history(task, tmp_path):
    task = task.model_copy(update={'role': 'product'})
    broker = ToolBroker(task)
    sealed_review(broker)
    receipt = checkpoint(task)
    path = task.artifact_dir / CHECKPOINT
    value = json.loads(path.read_text())
    value['version'] = 1
    value.pop('source_reads', None)
    path.write_text(canonical_json(value))
    receipt['digest'] = canonical_digest(value)
    continued = task.model_copy(update={'attempt_id': 'continued', 'artifact_dir': tmp_path / 'continued'})
    imported = restore(task, continued, receipt)
    assert len(imported['builders']) == 1  # Existing product output recovery remains supported.
    exported = checkpoint(continued)
    review = continued.model_copy(update={'role': 'review', 'attempt_id': 'review', 'artifact_dir': tmp_path / 'review'})
    result = restore(continued, review, exported)
    assert result['mode'] == 'restart_review' and result['builders'] == []


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Real SDK sandbox requires macOS')
async def test_legacy_restart_worker_is_explicitly_told_to_review_frozen_source_anew(task, store, tmp_path, http_fixture):
    from test_role_read_efficiency import observation, response

    from agentflow.adapters.openhands import OpenHandsRoleAdapter
    from agentflow.runtime.sandbox import MacSeatbeltSandbox
    from agentflow.runtime.supervisor import Supervisor

    previous = ToolBroker(task)
    sealed_review(previous)
    receipt = checkpoint(task)
    path = task.artifact_dir / CHECKPOINT
    value = json.loads(path.read_text())
    value['version'] = 1
    value.pop('source_reads', None)
    path.write_text(canonical_json(value))
    receipt['digest'] = canonical_digest(value)

    def answer(call, number):
        if number == 1:
            messages = json.dumps(call['body']['messages'])
            assert 'new independent review' in messages
            assert 'Saved output builders' not in messages
            return response(number, [('source', 'agentflow_io',
                {'operation': 'read_code', 'arguments': {'path': 'original.py'}})])
        assert observation(call, 'source')['text'] == 'ORIGINAL = True\n'
        return response(number, [('done', 'finish', {'message': 'Fresh review',
            'result': {'review': 'Independent review complete'}})])

    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / 'sandbox'))
    with http_fixture(answer) as (url, calls):
        target = task.model_copy(update={'attempt_id': 'legacy-restarted', 'artifact_dir': tmp_path / 'new-artifacts',
            'goal': 'Continue the previous preserved output.', 'proxy_base_url': url, 'max_active_seconds': 30})
        assert restore(task, target, receipt)['mode'] == 'restart_review'
        try:
            await adapter.start(target)
            await supervisor.wait(target.attempt_id)
            result = await adapter.collect_artifacts(target.attempt_id)
            assert result['execution_status'] == 'completed', result
            assert result['result'] == {'review': 'Independent review complete'}
        finally:
            await supervisor.close()

"""Only controller recovery may import a prior role draft into a fresh attempt."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from test_recovery import env as env
from test_recovery import patch, stopped_workspace

from agentflow.adapters.openhands.output_builder import ResultBuilderStore, export_partial, result_identity
from agentflow.common import DomainError, canonical_digest
from agentflow.control.recovery import resolve_role_output_checkpoint, restore_role_output_checkpoint
from agentflow.runtime.prelaunch import prelaunch_failure_code, read_prelaunch_failure
from agentflow.runtime.service import RuntimeService

SCHEMA = {'type': 'object', 'properties': {'summary': {'type': 'string'}, 'content': {'type': 'string'}},
          'required': ['summary', 'content'], 'additionalProperties': False}


@pytest_asyncio.fixture
async def partial_role(env):
    path, task, _, _ = await stopped_workspace(env)
    (path / 'src/keep.js').unlink()
    (path / 'src').rmdir()
    await patch(env, 'work_item', 'bad', step='research', role='research', write_paths=[], status='failed')
    await patch(env, 'attempt', 'bad-attempt', status='failed', runtime_failure_code='invalid_model_output')
    root = env.settings.data_dir / 'attempt_artifacts' / canonical_digest('bad-attempt').split(':')[1]
    root.mkdir(parents=True, mode=0o700)
    task = {**task, 'step': 'research', 'role': 'research', 'allowed_write_paths': [], 'artifact_dir': str(root),
            'output_schema': SCHEMA, 'max_output_tokens': 1024}
    await patch(env, 'dispatch_context', 'bad-attempt', task=task)
    builder = ResultBuilderStore(task, root)
    created = builder.begin({'summary': 'partial fixture'}, {'content': 'string'}, 'draft-1')
    added = builder.append(created['result_ref'], 'content', 'chunk-1', 0, 'Preserved paragraph.', False)
    env.role_task, env.role_root, env.role_builder, env.role_ref = task, root, builder, added['result_ref']
    return env


async def recover_and_claim(env, mode='retry'):
    run = await env.store.read('run', 'run')
    if mode == 'continue':
        run = await env.workflow.control_run('run', {'expected_revision': run['revision'], 'action': 'cancel',
            'reason': 'Owner cancels partially written document'}, 'cancel-document')
    receipt = await env.service.recover('run', {'expected_revision': run['revision'], 'mode': mode,
        **({'work_item_id': 'bad'} if mode == 'retry' else {})}, str(uuid4()))
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    work, attempt = claim['work_item'], claim['attempt']
    root = env.settings.data_dir / 'attempt_artifacts' / canonical_digest(attempt['id']).split(':')[1]
    root.mkdir(parents=True, mode=0o700)
    task = {**env.role_task, 'attempt_id': attempt['id'], 'artifact_dir': str(root),
            'input_fingerprint': attempt['input_fingerprint'], 'fencing_token': attempt['fencing_token']}
    return receipt, work, task


@pytest.mark.parametrize('mode', ['retry', 'continue'])
async def test_retry_imports_verified_partial_output_without_sealing_or_weakening_approval(partial_role, mode):
    env = partial_role
    first = export_partial(env.role_root, expected_identity=result_identity(env.role_task))
    assert export_partial(env.role_root, expected_identity=result_identity(env.role_task)) == first
    budget = await env.store.list('budget_account')
    receipt, work, task = await recover_and_claim(env, mode)
    assert receipt['role_output_checkpoints'][0]['digest'] == first['digest']
    point = await resolve_role_output_checkpoint(env.store, env.settings, await env.store.read('run', 'run'), work)
    assert point['authorized_generation'] == work['generation'] == 2
    imported = await restore_role_output_checkpoint(env.store, env.settings, work, task)
    assert imported['target_identity']['attempt_id'] == task['attempt_id']
    builder = ResultBuilderStore(task, Path(task['artifact_dir']))
    reference = imported['builders'][0]['result_ref']
    assert builder.status(reference, 'content')['value'] == 'Preserved paragraph.'
    with pytest.raises(DomainError) as error:
        builder.resolve(reference)
    assert error.value.code == 'result_incomplete'
    assert work['approval_required'] and await env.store.list('budget_account') == budget
    assert len(await env.store.list('role_output_import')) == 1
    await restore_role_output_checkpoint(env.store, env.settings, work, task)
    assert len(await env.store.list('role_output_import')) == 1


@pytest.mark.parametrize('damage', ['schema', 'source_bytes', 'generation', 'destination'])
async def test_partial_import_rejects_changed_schema_bytes_generation_or_destination(partial_role, damage):
    env = partial_role
    _, work, task = await recover_and_claim(env)
    if damage == 'schema':
        task = {**task, 'output_schema': {'type': 'object'}}
    elif damage == 'source_bytes':
        (env.role_root / 'role_output_checkpoint.json').write_text('{}')
    elif damage == 'generation':
        point = await env.store.read('role_output_checkpoint', work['payload']['role_output_checkpoint_id'])
        await patch(env, 'role_output_checkpoint', point['id'], authorized_generation=3)
    else:
        task = {**task, 'artifact_dir': str(env.tmp_path)}
    before = await env.store.list('work_item')
    with pytest.raises(DomainError):
        await restore_role_output_checkpoint(env.store, env.settings, work, task)
    assert await env.store.list('work_item') == before and not await env.store.list('role_output_import')


async def test_cancelled_before_worker_start_preserves_prior_partial_checkpoint(partial_role):
    env = partial_role
    run = await env.store.read('run', 'run')
    first = await env.service.recover('run', {'expected_revision': run['revision'], 'mode': 'retry', 'work_item_id': 'bad'}, 'first')
    work = await patch(env, 'work_item', 'bad', status='cancelled')
    old = await env.store.read('role_output_checkpoint', work['payload']['role_output_checkpoint_id'])
    run = await env.store.read('run', 'run')
    second = await env.service.recover('run', {'expected_revision': run['revision'], 'mode': 'retry', 'work_item_id': 'bad'}, 'second')
    current = await env.store.read('work_item', 'bad')
    point = await resolve_role_output_checkpoint(env.store, env.settings, await env.store.read('run', 'run'), current)
    assert point['authorized_generation'] == 3 and point['source_generation'] == 1
    assert point['digest'] == old['digest'] and point['id'] != old['id']
    assert await env.store.read('role_output_checkpoint', old['id']) == old
    assert first['id'] != second['id']


async def test_corrupt_partial_draft_never_silently_becomes_empty_recovery(partial_role):
    env = partial_role
    (env.role_root / '.role_output/owner.json').write_text('{}')
    before = await env.store.list('work_item')
    run = await env.store.read('run', 'run')
    with pytest.raises(DomainError):
        await env.service.recover('run', {'expected_revision': run['revision'], 'mode': 'retry', 'work_item_id': 'bad'}, str(uuid4()))
    assert await env.store.list('work_item') == before and not await env.store.list('run_recovery')


def runtime_fixture(env, task, adapter):
    runtime = RuntimeService.__new__(RuntimeService)
    runtime.store, runtime.data_dir = env.store, env.settings.data_dir
    envelope = SimpleNamespace(**task, allow_code_write=False)
    runtime._envelope = AsyncMock(return_value=(envelope, adapter))
    runtime.supervisor = SimpleNamespace(wait=AsyncMock(return_value=SimpleNamespace(
        state='completed', reason=None, input_fingerprint=task['input_fingerprint'], fencing_token=task['fencing_token'])))
    return runtime


async def test_runtime_imports_into_new_namespace_before_starting_role_worker(partial_role):
    env = partial_role
    _, work, task = await recover_and_claim(env)
    async def start(envelope):
        assert await env.store.read('role_output_import', task['attempt_id'])
        drafts = ResultBuilderStore(envelope).status()['drafts']
        assert len(drafts) == 1
        assert ResultBuilderStore(envelope).status(drafts[0]['result_ref'], 'content')['value'] == 'Preserved paragraph.'
    adapter = SimpleNamespace(start=AsyncMock(side_effect=start), collect_artifacts=AsyncMock(return_value={
        'execution_status': 'completed', 'artifacts': []}))
    runtime = runtime_fixture(env, task, adapter)
    result = await runtime.execute_task(task)
    assert result['execution_status'] == 'completed'
    adapter.start.assert_awaited_once()
    assert not await env.store.list('prelaunch_failure')


async def test_runtime_import_failure_is_proven_not_started_with_zero_calls_and_no_supervisor(partial_role):
    env = partial_role
    _, work, task = await recover_and_claim(env)
    await patch(env, 'dispatch_context', task['attempt_id'], task=task)
    (env.role_root / 'role_output_checkpoint.json').write_text('{}')
    adapter = SimpleNamespace(start=AsyncMock(), collect_artifacts=AsyncMock())
    runtime = runtime_fixture(env, task, adapter)
    with pytest.raises(DomainError) as error:
        await runtime.execute_task({**task, 'task_token': 'fixture-private-token'})
    assert error.value.code == 'invalid_role_output_checkpoint'
    adapter.start.assert_not_awaited()
    assert await env.store.read('supervised_attempt', task['attempt_id']) is None
    assert not [row for row in await env.store.list('model_invocation') if row.get('attempt_id') == task['attempt_id']]
    receipt = await env.store.read('prelaunch_failure', task['attempt_id'])
    assert receipt['outcome'] == 'not_started' and receipt['failure_code'] == 'invalid_role_output_checkpoint'
    assert 'fixture-private-token' not in str(receipt)
    await env.workflow.block_attempt(task['attempt_id'], 'Role draft checkpoint needs verification', str(uuid4()),
                                    failure_code='invalid_role_output_checkpoint')
    proof = await read_prelaunch_failure(env.store, env.settings.data_dir, task['attempt_id'])
    assert proof['outcome'] == 'not_started'
    assert await prelaunch_failure_code(env.store, env.settings.data_dir, task['attempt_id']) == 'invalid_role_output_checkpoint'


async def test_runtime_resume_observes_old_worker_without_importing_drafts_again(partial_role, monkeypatch):
    env = partial_role
    _, _, task = await recover_and_claim(env)
    await patch(env, 'supervised_attempt', task['attempt_id'], backend='openhands', state='completed')
    handle = SimpleNamespace(input_fingerprint=task['input_fingerprint'], fencing_token=task['fencing_token'], state='completed')
    adapter = SimpleNamespace(recover=AsyncMock(return_value=handle), collect_artifacts=AsyncMock(return_value={
        'execution_status': 'completed', 'artifacts': []}))
    runtime = runtime_fixture(env, task, adapter)
    importer = AsyncMock(side_effect=AssertionError('Resume must not import drafts'))
    monkeypatch.setattr('agentflow.control.recovery.restore_role_output_checkpoint', importer)
    assert (await runtime.resume_task(task))['execution_status'] == 'completed'
    importer.assert_not_awaited()

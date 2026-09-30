from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agentflow.common import DomainError
from agentflow.runtime.service import RuntimeService


async def seeded_task(store):
    task = {'attempt_id': 'attempt', 'run_id': 'run', 'iteration_id': 'iteration', 'work_item_id': 'work',
        'fencing_token': 1, 'input_fingerprint': 'sha256:' + 'a' * 64, 'step': 'code_review',
        'role': 'review', 'output_schema': {'type': 'object'}, 'allowed_write_paths': []}
    def seed(tx):
        tx.put('run', 'run', {'iteration_id': 'iteration', 'execution_state': 'running'})
        tx.put('attempt', 'attempt', {**{k: task[k] for k in ('run_id', 'iteration_id', 'work_item_id',
            'fencing_token', 'input_fingerprint')}, 'status': 'running', 'generation': 1})
        tx.put('work_item', 'work', {'run_id': 'run', 'attempt_id': 'attempt', 'status': 'running', 'generation': 1,
            'fencing_token': 1, 'input_fingerprint': task['input_fingerprint']})
        return tx.put('dispatch_context', 'attempt', {'task': task})
    await store.command('fixture', 'prelaunch-task', {}, seed)
    return task


@pytest.mark.parametrize('code', ['isolation_probe_timeout', 'isolation_unverified', 'sdk_version_unverified',
    'coding_workspace_unwritable'])
async def test_runtime_seals_known_prelaunch_failure_without_exposing_task_token(store, code):
    task = await seeded_task(store)
    runtime = RuntimeService.__new__(RuntimeService)
    runtime.store, runtime.data_dir = store, store.data_dir
    adapter = SimpleNamespace(start=AsyncMock(side_effect=DomainError(code, 'fixture failure')))
    runtime._envelope = AsyncMock(return_value=(SimpleNamespace(), adapter))
    with pytest.raises(DomainError) as failure:
        await runtime.execute_task({**task, 'task_token': 'fixture-private-token'})
    assert failure.value.code == code
    receipt = await store.read('prelaunch_failure', 'attempt')
    assert receipt['outcome'] == 'not_started' and receipt['failure_code'] == code
    assert 'fixture-private-token' not in str(receipt)
    assert not await store.list('supervised_attempt') and not await store.list('model_invocation')


async def test_late_error_after_launch_record_never_becomes_a_not_started_receipt(store):
    task = await seeded_task(store)
    runtime = RuntimeService.__new__(RuntimeService)
    runtime.store, runtime.data_dir = store, store.data_dir
    async def started(_):
        await store.command('fixture', 'already-starting', {}, lambda tx: tx.put('supervised_attempt', 'attempt',
            {'state': 'launch_intent', 'attempt_id': 'attempt'}))
        raise DomainError('isolation_unverified', 'too late to attest non-execution')
    runtime._envelope = AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace(start=started)))
    with pytest.raises(DomainError):
        await runtime.execute_task(task)
    assert not await store.list('prelaunch_failure')


async def test_unknown_adapter_error_does_not_attest_non_execution(store):
    task = await seeded_task(store)
    runtime = RuntimeService.__new__(RuntimeService)
    runtime.store, runtime.data_dir = store, store.data_dir
    runtime._envelope = AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace(
        start=AsyncMock(side_effect=RuntimeError('unknown start boundary')))))
    with pytest.raises(RuntimeError):
        await runtime.execute_task(task)
    assert not await store.list('prelaunch_failure')

"""A late failure receipt resolves uncertainty without inventing a success."""
import json
import sys
from pathlib import Path

import pytest
from test_recovery import env as env
from test_recovery import patch

from agentflow.common import canonical_digest
from agentflow.runtime.contracts import LaunchSpec
from agentflow.runtime.supervisor import Supervisor


async def late_failure(env):
    await patch(env, 'run', 'run', execution_state='running')
    work = await patch(env, 'work_item', 'bad', step='code_review', role='review', status='running',
                       input_fingerprint=canonical_digest('late-receipt-fixture'))
    await patch(env, 'attempt', 'bad-attempt', status='running', input_fingerprint=work['input_fingerprint'])
    await patch(env, 'dispatch_context', 'bad-attempt', task={
        'attempt_id': 'bad-attempt', 'work_item_id': 'bad', 'run_id': 'run', 'iteration_id': 'iteration',
        'step': 'code_review', 'role': 'review', 'fencing_token': work['fencing_token'],
        'input_fingerprint': work['input_fingerprint']})
    supervisor = Supervisor(env.store, env.settings.data_dir)
    spec = LaunchSpec(attempt_id='bad-attempt', operation_id='bad-operation', run_id='run',
        input_fingerprint=work['input_fingerprint'], fencing_token=work['fencing_token'],
        backend='openhands_role', argv=[sys.executable, '-c', 'raise SystemExit(1)'],
        cwd=env.tmp_path, environment={}, timeout_seconds=10)
    try:
        await supervisor.start(spec)
        stopped = await supervisor.wait('bad-attempt')
        assert stopped.state == 'failed' and stopped.exit_code == 1
    finally:
        await supervisor.close()
    await env.workflow.finish_attempt('bad-attempt', {'execution_status': 'execution_unknown',
        'quality_result': 'unknown', 'fencing_token': work['fencing_token'],
        'input_fingerprint': work['input_fingerprint'], 'runtime_failure_code': 'execution_unconfirmed'},
        'original-unknown', verified_artifacts=[])
    return await env.store.read('supervised_attempt', 'bad-attempt')


async def test_late_failure_restores_known_state_and_keeps_original_unknown_history(env):
    await late_failure(env)
    from agentflow.control.execution_reconciliation import ExecutionReconciliation
    service = ExecutionReconciliation(env.store, env.workflow)
    old = await env.store.read('attempt', 'bad-attempt')
    before = {kind: await env.store.list(kind) for kind in ['model_invocation', 'budget_account', 'supervised_attempt', 'code_snapshot']}
    await service.reconcile()
    current = await env.store.read('work_item', 'bad')
    assert current['status'] == 'failed' and current['runtime_failure_code'] == 'worker_exited'
    assert current['attempt_id'] == 'bad-attempt' and current['generation'] == 1
    records = await env.store.list('execution_reconciliation')
    assert len(records) == 1 and records[0]['original_attempt'] == old
    assert (await env.store.read('attempt', 'bad-attempt'))['status'] == 'failed'
    for kind, rows in before.items():
        assert await env.store.list(kind) == rows
    await service.reconcile()
    assert await env.store.list('execution_reconciliation') == records
    assert (await env.store.read('work_item', 'review')) is None
    assert not await env.store.list('run_recovery')


@pytest.mark.parametrize('damage', ['receipt_missing', 'receipt_changed', 'work_generation', 'context_fence',
                                  'still_running', 'success_without_collection', 'cancelled_run'])
async def test_no_late_failure_is_inferred_from_missing_or_unrelated_evidence(env, damage):
    process = await late_failure(env)
    from agentflow.control.execution_reconciliation import ExecutionReconciliation
    path = Path(process['directory']) / 'result.json'
    if damage == 'receipt_missing':
        path.unlink()
    elif damage in {'receipt_changed', 'success_without_collection'}:
        receipt = json.loads(path.read_text())
        if damage == 'receipt_changed':
            receipt['nonce'] = 'wrong'
        else:
            receipt.update(execution_status='completed', exit_code=0)
            await patch(env, 'supervised_attempt', 'bad-attempt', state='completed', exit_code=0)
        path.write_text(json.dumps(receipt))
    elif damage == 'work_generation':
        await patch(env, 'work_item', 'bad', generation=2)
    elif damage == 'context_fence':
        context = await env.store.read('dispatch_context', 'bad-attempt')
        await patch(env, 'dispatch_context', 'bad-attempt', task={**context['task'], 'fencing_token': 99})
    elif damage == 'still_running':
        await patch(env, 'supervised_attempt', 'bad-attempt', state='running')
    else:
        await patch(env, 'run', 'run', execution_state='cancelled')
    before = await env.store.read('work_item', 'bad')
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert await env.store.read('work_item', 'bad') == before
    assert not await env.store.list('execution_reconciliation')


async def test_verified_late_failure_can_enter_existing_automatic_retry_without_touching_upstream(env):
    await late_failure(env)
    from agentflow.control.execution_reconciliation import ExecutionReconciliation
    from agentflow.control.failure_remediation import FailureRemediation
    env.workflow.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0})
    upstream = await env.store.read('work_item', 'upstream')
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    result = await FailureRemediation(env.store, env.workflow).repair('bad')
    assert result['status'] == 'repair_scheduled', result
    work = await env.store.read('work_item', 'bad')
    assert work['generation'] == 2 and work['status'] == 'pending'
    assert await env.store.read('work_item', 'upstream') == upstream
    assert not await env.store.list('model_invocation')


async def test_late_receipt_can_resolve_a_supervisor_still_marked_unknown(env):
    await late_failure(env)
    from agentflow.control.execution_reconciliation import ExecutionReconciliation
    previous = await patch(env, 'supervised_attempt', 'bad-attempt', state='execution_unknown',
                           reason='process_disappeared_without_receipt', exit_code=None)
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    process = await env.store.read('supervised_attempt', 'bad-attempt')
    assert process['state'] == 'failed' and process['exit_code'] == 1
    assert (await env.store.list('execution_reconciliation'))[0]['original_process'] == previous
    assert (await env.store.read('work_item', 'bad'))['status'] == 'failed'


async def test_changed_receipt_during_reconciliation_cannot_publish_a_known_state(env, monkeypatch):
    process = await late_failure(env)
    from agentflow.control.execution_reconciliation import ExecutionReconciliation
    service = ExecutionReconciliation(env.store, env.workflow)
    original = service._receipt
    calls = 0
    def read_then_change(*args):
        nonlocal calls
        result = original(*args)
        calls += 1
        if calls == 1:
            path = Path(process['directory']) / 'result.json'
            receipt = json.loads(path.read_text())
            path.write_text(json.dumps({**receipt, 'nonce': 'changed-after-preflight'}))
        return result
    monkeypatch.setattr(service, '_receipt', read_then_change)
    await service.reconcile()
    assert (await env.store.read('work_item', 'bad'))['status'] == 'execution_unknown'
    assert not await env.store.list('execution_reconciliation')


async def test_transient_revision_race_does_not_cache_a_permanent_noop(env, monkeypatch):
    await late_failure(env)
    from agentflow.control.execution_reconciliation import ExecutionReconciliation
    command = env.store.command
    changed = False
    async def race(scope, key, payload, handler):
        nonlocal changed
        if scope == 'execution.reconcile' and not changed:
            changed = True
            await patch(env, 'supervised_attempt', 'bad-attempt', updated_at='2026-09-25T12:00:00Z')
        return await command(scope, key, payload, handler)
    monkeypatch.setattr(env.store, 'command', race)
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert (await env.store.read('work_item', 'bad'))['status'] == 'execution_unknown'
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert (await env.store.read('work_item', 'bad'))['status'] == 'failed'
    assert len(await env.store.list('execution_reconciliation')) == 1

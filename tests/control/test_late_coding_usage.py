"""Late failed Codex receipts may repair metering, never work success or model settlement."""
import asyncio
import json
from uuid import uuid4

import pytest
from test_coding_steps import coding_env as coding_env
from test_coding_steps import execute, task_for

from agentflow.common import canonical_digest
from agentflow.control.execution_reconciliation import ExecutionReconciliation
from agentflow.runtime.launcher import atomic_json


async def patch(env, kind, identity, **changes):
    def apply(tx):
        old = tx.get(kind, identity)
        return tx.put(kind, identity, {**(old or {}), **changes}, old['revision'] if old else None)
    return await env.store.command('fixture.late-usage', str(uuid4()), {}, apply)


async def late_coding_failure(env, *, prior_step=False):
    if prior_step:
        prior = await task_for(env)
        await execute(env, prior, {'summary': 'preserved progress', 'status': 'continue', 'next_action': 'finish'}, 'VALUE = 1\n')
    task = await task_for(env)
    task.update(iteration_id='iteration', role='development')
    attempt_id = task['attempt_id']
    await patch(env, 'dispatch_context', attempt_id, task=task)
    await env.scheduler.coding_steps.account(task, {'active_seconds': None, 'observed_tool_calls': None,
                                                  'tool_observation_complete': False})
    folder = env.settings.data_dir / 'supervisor' / canonical_digest({'attempt_id': attempt_id})[7:]
    folder.mkdir(parents=True, mode=0o700)
    process = {'attempt_id': attempt_id, 'operation_id': attempt_id, 'run_id': 'run', 'backend': 'codex_exec',
        'state': 'failed', 'reason': None, 'exit_code': 1, 'directory': str(folder), 'nonce': 'fixture-nonce',
        'pid': 2147483647, 'process_started_at': 1.0, 'boot_fingerprint': canonical_digest({'boot_time': 0}),
        'fencing_token': task['fencing_token'], 'input_fingerprint': task['input_fingerprint']}
    await patch(env, 'supervised_attempt', attempt_id, **process)
    atomic_json(folder / 'result.json', {**process, 'execution_status': 'failed', 'active_seconds': 8.5,
                                       'logs_truncated': False, 'finished_at': '2026-09-25T00:00:00Z'})
    atomic_json(folder / 'child.json', {**process, 'child': {'pid': 2147483646, 'process_started_at': 1.0}})
    events = [{'type': 'turn.started'},
        {'type': 'item.started', 'item': {'type': 'command_execution', 'id': 'tool-1'}},
        {'type': 'item.updated', 'item': {'type': 'command_execution', 'id': 'tool-1'}},
        {'type': 'item.completed', 'item': {'type': 'command_execution', 'id': 'tool-1'}},
        {'type': 'item.completed', 'item': {'type': 'file_change', 'id': 'tool-2'}},
        {'type': 'turn.failed', 'error': {'message': 'worker stopped'}}]
    (folder / 'stdout.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in events))
    await env.workflow.finish_attempt(attempt_id, {'execution_status': 'execution_unknown',
        'quality_result': 'unknown', 'fencing_token': task['fencing_token'], 'input_fingerprint': task['input_fingerprint'],
        'runtime_failure_code': 'execution_unconfirmed'}, 'unknown-' + attempt_id, verified_artifacts=[])
    return task, folder


@pytest.mark.parametrize('prior_step', [False, True])
async def test_late_failed_usage_is_added_once_without_resetting_history_or_quality(coding_env, prior_step):
    env = coding_env
    task, _ = await late_coding_failure(env, prior_step=prior_step)
    usage = await env.store.read('coding_step_usage', task['attempt_id'])
    budget = await env.store.read('coding_work_budget', task['coding_step']['budget_id'])
    untouched = {kind: await env.store.list(kind) for kind in ('model_invocation', 'model_attempt_budget', 'budget_account',
        'coding_step_control', 'dispatch_context', 'code_snapshot')}
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    known = await env.store.read('coding_step_usage', task['attempt_id'])
    assert known['known'] is True and known['active_seconds'] == 8.5 and known['observed_tool_calls'] == 2
    current = await env.store.read('coding_work_budget', budget['id'])
    assert current['active_seconds'] == budget['active_seconds'] + 8.5
    assert current['observed_tool_calls'] == budget['observed_tool_calls'] + 2
    assert current['step_count'] == budget['step_count'] and current['uncertain'] is False
    for field in ('max_steps', 'max_active_seconds', 'max_tool_calls'):
        assert current[field] == budget[field]
    audits = await env.store.list('coding_usage_reconciliation')
    assert len(audits) == 1 and audits[0]['original_usage'] == usage and audits[0]['original_budget'] == budget
    attempt = await env.store.read('attempt', task['attempt_id'])
    work = await env.store.read('work_item', task['work_item_id'])
    assert attempt['status'] == work['status'] == 'failed'
    assert attempt['quality_result'] == work['quality_result'] == 'unknown'
    assert not await env.store.list('approval')
    for kind, rows in untouched.items():
        assert await env.store.list(kind) == rows
    await asyncio.gather(*(ExecutionReconciliation(env.store, env.workflow).reconcile() for _ in range(3)))
    assert await env.store.read('coding_work_budget', budget['id']) == current
    assert await env.store.list('coding_usage_reconciliation') == audits


@pytest.mark.parametrize('damage', ['truncated', 'unknown_event', 'unknown_tool', 'missing_tool_id', 'partial_line',
    'missing_log', 'bad_duration', 'bad_receipt', 'changed_schema', 'changed_scope', 'changed_fence',
    'active_process', 'pending_model', 'wrong_model_identity', 'ledger_drift', 'missing_child', 'empty_child',
    'null_child', 'empty_id', 'blank_id'])
async def test_unverified_observations_never_become_known_usage(coding_env, damage):
    env = coding_env
    task, folder = await late_coding_failure(env)
    receipt_path = folder / 'result.json'
    if damage in {'truncated', 'bad_duration', 'bad_receipt'}:
        receipt = json.loads(receipt_path.read_bytes())
        receipt.update({'logs_truncated': True} if damage == 'truncated' else
                       {'active_seconds': -1} if damage == 'bad_duration' else {'nonce': 'wrong'})
        atomic_json(receipt_path, receipt)
    elif damage in {'unknown_event', 'unknown_tool', 'missing_tool_id', 'partial_line', 'empty_id', 'blank_id'}:
        extra = {'unknown_event': {'type': 'future.event'},
            'unknown_tool': {'type': 'item.completed', 'item': {'type': 'future_tool', 'id': 'tool-x'}},
            'missing_tool_id': {'type': 'item.completed', 'item': {'type': 'mcp_tool_call'}},
            'empty_id': {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'id': ''}},
            'blank_id': {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'id': ' \t '}}}.get(damage)
        with (folder / 'stdout.jsonl').open('a') as output:
            output.write(json.dumps(extra) + '\n' if extra else '{"type":')
    elif damage == 'missing_log':
        (folder / 'stdout.jsonl').unlink()
    elif damage == 'missing_child':
        (folder / 'child.json').unlink()
    elif damage in {'empty_child', 'null_child'}:
        (folder / 'child.json').write_text('{}' if damage == 'empty_child' else 'null')
    elif damage in {'changed_schema', 'changed_scope'}:
        task = {**task, **({'output_schema': {'type': 'object'}} if damage == 'changed_schema' else {'allowed_write_paths': ['.']})}
        await patch(env, 'dispatch_context', task['attempt_id'], task=task)
    elif damage == 'changed_fence':
        await patch(env, 'work_item', task['work_item_id'], fencing_token=99)
    elif damage == 'active_process':
        await patch(env, 'supervised_attempt', task['attempt_id'], state='running')
    elif damage in {'pending_model', 'wrong_model_identity'}:
        await patch(env, 'model_invocation', 'call', run_id='run', iteration_id='iteration', attempt_id=task['attempt_id'],
            state='reserved' if damage == 'pending_model' else 'completed_unpriced', fencing_token=99,
            input_fingerprint=task['input_fingerprint'])
    else:
        budget = await env.store.read('coding_work_budget', task['coding_step']['budget_id'])
        await patch(env, 'coding_work_budget', budget['id'], active_seconds=1)
    old_usage = await env.store.read('coding_step_usage', task['attempt_id'])
    old_budget = await env.store.read('coding_work_budget', task['coding_step']['budget_id'])
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert await env.store.read('coding_step_usage', task['attempt_id']) == old_usage
    assert await env.store.read('coding_work_budget', old_budget['id']) == old_budget
    assert not await env.store.list('coding_usage_reconciliation')


async def test_another_unknown_step_is_not_cleared_by_one_late_receipt(coding_env):
    env = coding_env
    task, _ = await late_coding_failure(env, prior_step=True)
    prior = next(row for row in await env.store.list('coding_step_usage') if row['id'] != task['attempt_id'])
    await patch(env, 'coding_step_usage', prior['id'], known=False, active_seconds=None, observed_tool_calls=None)
    await patch(env, 'coding_work_budget', task['coding_step']['budget_id'], active_seconds=0, observed_tool_calls=0)
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert (await env.store.read('coding_step_usage', task['attempt_id']))['known'] is True
    assert (await env.store.read('coding_step_usage', prior['id']))['known'] is False
    budget = await env.store.read('coding_work_budget', task['coding_step']['budget_id'])
    assert budget['active_seconds'] == 8.5 and budget['observed_tool_calls'] == 2 and budget['uncertain'] is True


async def test_usage_receipt_arriving_after_execution_reconciliation_is_retried(coding_env):
    env = coding_env
    task, folder = await late_coding_failure(env)
    log = (folder / 'stdout.jsonl').read_bytes()
    (folder / 'stdout.jsonl').unlink()
    service = ExecutionReconciliation(env.store, env.workflow)
    await service.reconcile()
    assert (await env.store.read('attempt', task['attempt_id']))['status'] == 'failed'
    assert (await env.store.read('coding_step_usage', task['attempt_id']))['known'] is False
    (folder / 'stdout.jsonl').write_bytes(log)
    await service.reconcile()
    assert (await env.store.read('coding_step_usage', task['attempt_id']))['known'] is True


@pytest.mark.parametrize('damage', ['log', 'receipt', 'child'])
async def test_file_changes_during_usage_verification_keep_original_unknown_meter(coding_env, monkeypatch, damage):
    env = coding_env
    task, folder = await late_coding_failure(env)
    service = ExecutionReconciliation(env.store, env.workflow)
    evidence = service.coding_usage._evidence
    changed = False
    def race(state):
        nonlocal changed
        proof = evidence(state)
        if not changed:
            changed = True
            if damage == 'log':
                with (folder / 'stdout.jsonl').open('a') as stream:
                    stream.write('{"type":"item.completed","item":{"type":"file_change","id":"new-tool"}}\n')
            elif damage == 'receipt':
                receipt = json.loads((folder / 'result.json').read_bytes())
                atomic_json(folder / 'result.json', {**receipt, 'active_seconds': 9.5})
            else:
                atomic_json(folder / 'child.json', {'unexpected': True})
        return proof
    monkeypatch.setattr(service.coding_usage, '_evidence', race)
    before = await env.store.read('coding_work_budget', task['coding_step']['budget_id'])
    await service.reconcile()
    assert (await env.store.read('coding_step_usage', task['attempt_id']))['known'] is False
    assert await env.store.read('coding_work_budget', before['id']) == before
    assert not await env.store.list('coding_usage_reconciliation')


@pytest.mark.parametrize('damage', ['usage_revision', 'model_started'])
async def test_state_changes_before_usage_transaction_are_rechecked(coding_env, monkeypatch, damage):
    env = coding_env
    task, _ = await late_coding_failure(env)
    command = env.store.command
    changed = False
    async def race(scope, key, payload, handler):
        nonlocal changed
        if scope == 'coding.usage_reconcile' and not changed:
            changed = True
            if damage == 'usage_revision':
                await patch(env, 'coding_step_usage', task['attempt_id'], checked_again=True)
            else:
                await patch(env, 'model_invocation', 'pending', attempt_id=task['attempt_id'], run_id='run', state='reserved')
        return await command(scope, key, payload, handler)
    monkeypatch.setattr(env.store, 'command', race)
    service = ExecutionReconciliation(env.store, env.workflow)
    await service.reconcile()
    assert (await env.store.read('coding_step_usage', task['attempt_id']))['known'] is False
    assert not await env.store.list('coding_usage_reconciliation')
    if damage == 'usage_revision':
        await service.reconcile()
        assert (await env.store.read('coding_step_usage', task['attempt_id']))['known'] is True
        assert len(await env.store.list('coding_usage_reconciliation')) == 1


@pytest.mark.parametrize('seconds,blocked', [(8.5, False), (125.0, True)])
async def test_real_usage_unblocks_existing_recovery_only_within_original_limits(coding_env, seconds, blocked):
    from agentflow.control.recovery import RunRecoveryService
    env = coding_env
    task, folder = await late_coding_failure(env)
    receipt = json.loads((folder / 'result.json').read_bytes())
    atomic_json(folder / 'result.json', {**receipt, 'active_seconds': seconds})
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    usage = await env.store.read('coding_step_usage', task['attempt_id'])
    assert usage['known'] and usage['active_seconds'] == seconds
    recovery = RunRecoveryService(env.store, env.workflow)
    blockers = await recovery._target_blockers(await recovery._read('run'), {'root_work_item_ids': [task['work_item_id']]})
    assert bool(blockers) is blocked
    if blocked:
        assert blockers[0]['code'] == 'coding_budget_exhausted'
    assert (await env.store.read('work_item', task['work_item_id']))['status'] == 'failed'

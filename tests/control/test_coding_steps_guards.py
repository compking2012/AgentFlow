"""Independent regression checks for coding-step completion and collection races."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_coding_steps import coding_env as coding_env
from test_coding_steps import execute, task_for
from test_recovery import env as env
from test_recovery import patch, request, stopped_workspace

from agentflow.adapters.codex import CodexExecAdapter
from agentflow.common import DomainError
from agentflow.control.coding_steps import CodingSteps
from agentflow.runtime.contracts import TaskEnvelope
from agentflow.runtime.process_identity import current_boot_identity
from agentflow.runtime.supervisor import Supervisor


@pytest.mark.parametrize('usage', ['unknown', 'over_time', 'over_tools'])
async def test_complete_cannot_accept_uncertain_or_exceeded_shared_usage(coding_env, usage):
    env = coding_env
    task = await task_for(env)
    result = await execute(env, task,
        {'summary': 'entire task implemented', 'status': 'complete', 'next_action': ''},
        'VALUE = 1\n', known=usage != 'unknown',
        seconds=101 if usage == 'over_time' else 8,
        tools=13 if usage == 'over_tools' else 2)
    budget = await env.store.read('coding_work_budget', task['coding_step']['budget_id'])
    assert result['status'] == 'blocked', (result['status'], budget)
    assert not await env.store.list('approval')
    assert (await env.store.read('work_item', 'review'))['status'] == 'pending'


async def test_cancel_during_usage_collection_stays_cancelled(coding_env, monkeypatch):
    env = coding_env
    task = await task_for(env)
    account = env.scheduler.coding_steps.account

    async def account_then_cancel(task, result):
        await account(task, result)
        run = await env.store.read('run', 'run')
        await env.workflow.control_run('run', {'expected_revision': run['revision'],
            'action': 'cancel', 'reason': 'owner cancelled during collection'}, 'cancel-during-collection')

    monkeypatch.setattr(env.scheduler.coding_steps, 'account', account_then_cancel)
    result = await execute(env, task,
        {'summary': 'first patch saved', 'status': 'continue', 'next_action': 'second patch'}, 'VALUE = 1\n')
    assert result['status'] == 'cancelled', result
    assert not await env.store.list('coding_step_checkpoint')
    assert not await env.store.list('approval')


async def test_repeated_identical_partial_receipt_cannot_advance_twice(coding_env):
    env = coding_env
    task = await task_for(env)
    content = {'summary': 'first patch saved', 'status': 'continue', 'next_action': 'second patch'}
    first = await execute(env, task, content, 'VALUE = 1\n')
    budget = await env.store.read('coding_work_budget', task['coding_step']['budget_id'])
    second = await execute(env, task, content)
    assert second == first
    assert await env.store.read('coding_work_budget', budget['id']) == budget
    assert len(await env.store.list('coding_step_checkpoint')) == 1
    assert (Path(task['workspace']) / 'feature.py').read_text() == 'VALUE = 1\n'


@pytest.mark.parametrize('duration', ['legacy', -1, True, float('inf'), float('nan')],
                         ids=['legacy-receipt', 'negative', 'boolean', 'infinite', 'nan'])
async def test_delayed_collection_does_not_charge_controller_downtime_as_active_execution(coding_env, duration):
    env = coding_env
    task = await task_for(env)
    directory = env.root / 'supervisor-receipt'
    directory.mkdir()
    started = datetime.now(UTC) - timedelta(hours=1)
    finished = started + timedelta(seconds=8)
    nonce = 'verified-fixture-nonce'
    boot_source, boot_fingerprint = current_boot_identity()
    body = {'attempt_id': task['attempt_id'], 'operation_id': task['attempt_id'], 'run_id': 'run',
        'input_fingerprint': task['input_fingerprint'], 'fencing_token': task['fencing_token'],
        'backend': 'codex_exec', 'backend_version': 'fixture', 'nonce': nonce, 'state': 'running',
        'pid': 1073741824, 'process_started_at': started.timestamp(), 'boot_fingerprint': boot_fingerprint,
        'boot_identity_source': boot_source,
        'directory': str(directory), 'created_at': started.isoformat(), 'exit_code': None, 'reason': None}
    await env.store.command('fixture', 'meter-supervisor', {},
        lambda tx: tx.put('supervised_attempt', task['attempt_id'], body))
    receipt = {**body, 'execution_status': 'completed', 'exit_code': 0, 'finished_at': finished.isoformat()}
    if duration != 'legacy':
        receipt['active_seconds'] = duration
    (directory / 'result.json').write_text(json.dumps(receipt))
    (directory / 'stdout.jsonl').write_text(json.dumps({'type': 'turn.completed',
        'usage': {'input_tokens': 1, 'output_tokens': 1}}) + '\n')
    (directory / 'stderr.log').write_text('')
    artifact_dir = env.root / 'adapter-artifacts'
    artifact_dir.mkdir()
    (artifact_dir / 'codex_final.json').write_text(json.dumps(
        {'summary': 'partial', 'status': 'continue', 'next_action': 'next patch'}))
    envelope = TaskEnvelope(attempt_id=task['attempt_id'], operation_id=task['attempt_id'],
        work_item_id='code', run_id='run', iteration_id='iteration', role='development', goal='meter fixture',
        input_fingerprint=task['input_fingerprint'], fencing_token=task['fencing_token'],
        workspace=Path(task['workspace']), artifact_dir=artifact_dir, allow_code_write=True,
        model_profile_id='fixture', model='fixture', proxy_base_url='http://127.0.0.1:1/v1',
        proxy_token='fixture', max_active_seconds=100, output_schema=task['output_schema'])
    supervisor = Supervisor(env.store, env.settings.data_dir)
    adapter = CodexExecAdapter(supervisor, None)
    result = await adapter.collect_artifacts(task['attempt_id'], envelope)
    assert result['execution_status'] == 'completed'
    if duration == 'legacy':
        assert result['active_seconds'] == pytest.approx(8, abs=0.01), result
    else:
        assert result['active_seconds'] is None, result


async def test_recovery_cannot_ignore_a_started_step_without_usage_receipt(env):
    _, task, _, _ = await stopped_workspace(env)
    work = await patch(env, 'work_item', 'bad', status='running')
    attempt = await patch(env, 'attempt', 'bad-attempt', status='running')
    run = await env.store.read('run', 'run')
    control = await CodingSteps(env.store, env.settings, env.service.repository).prepare(
        run, work, attempt, task['source_commit'], 512)
    await patch(env, 'dispatch_context', attempt['id'], task={**task, 'coding_step': control})
    # The process has stopped and left real Git progress, but collection failed
    # before coding_step_usage was durably written.
    await env.workflow.block_attempt(attempt['id'], 'fixture collection failed after process stop',
        'fixture-collection-failure', failure_code='worker_internal_error')
    assert not await env.store.list('coding_step_usage')
    run = await env.store.read('run', 'run')
    with pytest.raises(DomainError) as raised:
        await env.service.recover('run', request(revision=run['revision']), 'retry-unaccounted-step')
    assert raised.value.code.startswith('coding_budget_')
    assert not await env.store.list('run_recovery')


async def test_upstream_revision_rebinds_diff_baseline_without_resetting_usage(coding_env):
    env = coding_env
    prototype = await env.store.read('work_item', 'code')
    await patch(env, 'work_item', 'upstream', **{key: value for key, value in prototype.items()
        if key not in {'id', 'revision'}},)
    await patch(env, 'work_item', 'upstream', key='upstream', status='completed',
        approval_required=False, write_paths=['main.py'], attempt_id='upstream-1', output_fingerprint='upstream-1')
    await patch(env, 'work_item', 'code', dependencies=['upstream'], step='unit_test_implementation', role='unit_test')

    async def upstream_version(number, source, base):
        workspace = env.root / f'upstream-{number}'
        await env.repository.clone_snapshot(source, workspace, base)
        (workspace / 'main.py').write_text(f'VERSION = {number}\n')
        snapshot = await env.repository.freeze_workspace(workspace, base, f'upstream {number}')
        await patch(env, 'code_snapshot', f'upstream-{number}', run_id='run', work_item_id='upstream',
            generation=number, repository_path=str(workspace), commit_oid=snapshot['commit_oid'],
            tree_oid=snapshot['tree_oid'], base_oid=base, parent_commit_oids=[], stale=False)
        return workspace, snapshot['commit_oid']

    source, initial = await upstream_version(1, Path(env.project['local_path']), env.project['base_commit'])
    first = await task_for(env)
    first['step'] = 'unit_test_implementation'
    await execute(env, first, {'summary': 'tests partially written', 'status': 'continue', 'next_action': 'add case'},
        'def test_version():\n    assert True\n')
    budget_before = await env.store.read('coding_work_budget', first['coding_step']['budget_id'])
    run = await env.store.read('run', 'run')
    await env.workflow.revise('run', {'expected_revision': run['revision'], 'work_item_ids': ['upstream'],
        'reason': 'owner requested an upstream implementation correction'}, 'revise-upstream')
    _, updated = await upstream_version(2, source, initial)
    await patch(env, 'work_item', 'upstream', status='completed', attempt_id='upstream-2', output_fingerprint='upstream-2')
    second = await task_for(env)
    second['step'] = 'unit_test_implementation'
    assert second['source_commit'] == updated
    assert second['coding_step']['max_active_seconds'] == 100 - budget_before['active_seconds']
    result = await execute(env, second, {'summary': 'tests complete', 'status': 'complete', 'next_action': ''},
        'def test_version():\n    assert True\n')
    assert result['status'] == 'waiting_approval', (result['status'], result.get('blocking_reason'),
        first['coding_step']['base_commit'], second['coding_step']['base_commit'], updated)


async def test_diagnostic_error_item_does_not_make_completed_tool_metering_unknown(coding_env):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    env = coding_env
    task = await task_for(env)
    directory = env.root / 'diagnostic-events'
    directory.mkdir()
    events = [
        {'type': 'item.completed', 'item': {'id': 'notice', 'type': 'error', 'message': 'A recoverable client diagnostic'}},
        {'type': 'item.started', 'item': {'id': 'tool', 'type': 'command_execution'}},
        {'type': 'item.completed', 'item': {'id': 'tool', 'type': 'command_execution'}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 20, 'output_tokens': 30}},
    ]
    (directory / 'stdout.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in events))
    (directory / 'codex_final.json').write_text(json.dumps({'summary': 'small step', 'status': 'continue', 'next_action': 'next'}))
    handle = SimpleNamespace(input_fingerprint=task['input_fingerprint'], fencing_token=task['fencing_token'],
        stdout_path=str(directory / 'stdout.jsonl'), stderr_path=str(directory / 'stderr.log'),
        state='completed', reason=None, exit_code=0, active_seconds=2.0)
    supervisor = SimpleNamespace(inspect=AsyncMock(return_value=handle))
    adapter = CodexExecAdapter(supervisor, None)
    envelope = SimpleNamespace(attempt_id=task['attempt_id'], input_fingerprint=task['input_fingerprint'],
        fencing_token=task['fencing_token'], artifact_dir=directory, output_schema=task['output_schema'])
    result = await adapter.collect_artifacts(task['attempt_id'], envelope)
    assert result['execution_status'] == 'completed'
    assert result['tool_observation_complete'] and result['observed_tool_calls'] == 1
    assert result['active_seconds'] == 2.0


async def test_recollection_does_not_recharge_a_verified_reconciled_usage_receipt(coding_env):
    env = coding_env
    task = await task_for(env)
    steps = env.scheduler.coding_steps
    await steps.account(task, {'active_seconds': None, 'observed_tool_calls': 0, 'tool_observation_complete': False})
    def verified_reconciliation(tx):
        old = tx.get('coding_step_usage', task['attempt_id'])
        budget = tx.get('coding_work_budget', task['coding_step']['budget_id'])
        tx.put('coding_usage_reconciliation', 'fixture-audit', {'old_usage': old, 'old_budget': budget})
        tx.put('coding_step_usage', old['id'], {**old, 'known': True, 'active_seconds': 8.0,
            'observed_tool_calls': 2, 'reconciliation_id': 'fixture-audit'}, old['revision'])
        return tx.put('coding_work_budget', budget['id'], {**budget, 'active_seconds': 8.0,
            'observed_tool_calls': 2, 'uncertain': False}, budget['revision'])
    before = await env.store.command('fixture', 'verified-reconciliation', {}, verified_reconciliation)
    await steps.account(task, {'active_seconds': 8.0, 'observed_tool_calls': 2, 'tool_observation_complete': True})
    assert await env.store.read('coding_work_budget', before['id']) == before
    assert (await env.store.read('coding_step_usage', task['attempt_id']))['reconciliation_id'] == 'fixture-audit'

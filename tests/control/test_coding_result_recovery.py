"""Result-only recovery uses temporary Store/Git evidence; never a model or worker."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_coding_steps import coding_env as coding_env
from test_coding_steps import task_for

from agentflow.adapters.codex import CodexExecAdapter
from agentflow.common import canonical_digest
from agentflow.runtime.launcher import atomic_json
from agentflow.runtime.workspace import WorkspaceManager


async def patch(env, kind, identity, **fields):
    def apply(tx):
        prior = tx.get(kind, identity)
        return tx.put(kind, identity, {**(prior or {}), **fields}, prior['revision'] if prior else None)
    return await env.store.command('fixture.recollection', str(uuid4()), {}, apply)


async def stopped_failure(env, *, status='failed', content=None, snapshot=False):
    task = await task_for(env)
    workspace = await WorkspaceManager(env.settings.data_dir).create_clone(
        Path(env.project['local_path']), task['source_commit'], task['attempt_id'])
    task.update(workspace=str(workspace), iteration_id='iteration', role='development')
    (workspace / 'feature.py').write_text('VALUE = 42\n')
    artifacts = env.settings.data_dir / 'attempt_artifacts' / canonical_digest(task['attempt_id']).split(':')[1]
    artifacts.mkdir(parents=True, mode=0o700)
    raw = json.dumps(content or {'summary': '实现已经完成', 'status': 'complete', 'next_action': '交给审查和测试。'},
                     ensure_ascii=False).encode()
    (artifacts / 'codex_final.json').write_bytes(raw)
    process_dir = env.settings.data_dir / 'supervisor' / canonical_digest({'attempt_id': task['attempt_id']}).split(':')[1]
    process_dir.mkdir(parents=True, mode=0o700)
    process = {'attempt_id': task['attempt_id'], 'operation_id': task['attempt_id'], 'run_id': 'run',
        'backend': 'codex_exec', 'state': 'completed', 'fencing_token': task['fencing_token'],
        'input_fingerprint': task['input_fingerprint'], 'directory': str(process_dir), 'nonce': 'fixture-nonce',
        'pid': 2147483647, 'process_started_at': 1.0, 'boot_fingerprint': canonical_digest({'boot_time': 0}),
        'exit_code': 0, 'reason': None}
    atomic_json(process_dir / 'result.json', {**process, 'execution_status': 'completed', 'active_seconds': 8.0})
    events = [{'type': 'item.completed', 'item': {'type': 'command_execution', 'id': f'tool-{number}'}}
              for number in range(2)] + [{'type': 'turn.completed'}]
    (process_dir / 'stdout.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in events))
    (process_dir / 'stderr.log').write_text('')
    await patch(env, 'dispatch_context', task['attempt_id'], task=task)
    await patch(env, 'supervised_attempt', task['attempt_id'], **process)
    await patch(env, 'model_invocation', 'original-call', run_id='run', iteration_id='iteration',
                attempt_id=task['attempt_id'], state='settled', fencing_token=task['fencing_token'],
                input_fingerprint=task['input_fingerprint'])
    await env.scheduler.coding_steps.account(task, {'active_seconds': 8.0, 'observed_tool_calls': 2,
                                                 'tool_observation_complete': True})
    if snapshot:
        frozen = await env.repository.freeze_workspace(workspace, task['source_commit'], 'AgentFlow implementation')
        await env.store.command('code.snapshot', task['attempt_id'], {'commit_oid': frozen['commit_oid']},
            lambda tx: tx.put('code_snapshot', task['attempt_id'], {'run_id': 'run', 'work_item_id': 'code',
                'generation': 1, 'repository_path': str(workspace), 'commit_oid': frozen['commit_oid'],
                'tree_oid': frozen['tree_oid'], 'base_oid': task['coding_step']['base_commit'],
                'parent_commit_oids': [task['source_commit']], 'stale': False}))
    if status == 'failed':
        await env.workflow.finish_attempt(task['attempt_id'], {'execution_status': 'failed',
            'quality_result': 'unknown', 'fencing_token': task['fencing_token'],
            'input_fingerprint': task['input_fingerprint'], 'summary': '旧回执解析失败',
            'runtime_failure_code': 'final_schema_invalid'}, f"finish-{task['attempt_id']}", verified_artifacts=[])
    else:
        await env.workflow.block_attempt(task['attempt_id'], '旧回执解析失败', str(uuid4()),
                                         failure_code='final_schema_invalid')
    handle = SimpleNamespace(attempt_id=task['attempt_id'], input_fingerprint=task['input_fingerprint'],
        fencing_token=task['fencing_token'], state='completed', reason=None, exit_code=0, active_seconds=8.0,
        stdout_path=str(process_dir / 'stdout.jsonl'), stderr_path=str(process_dir / 'stderr.log'))
    class CompletedSupervisor:
        async def inspect(self, identity):
            assert identity == task['attempt_id']
            return handle
    adapter = CodexExecAdapter(CompletedSupervisor(), None)
    class ExistingResultRuntime:
        def __init__(self):
            self.collections = 0
        async def collect_completed_task(self, frozen):
            assert frozen == task
            self.collections += 1
            envelope = SimpleNamespace(**frozen, artifact_dir=artifacts)
            return await adapter.collect_artifacts(task['attempt_id'], envelope)
        async def execute_task(self, _):
            raise AssertionError('Result recollection cannot execute a model')
        async def resume_task(self, _):
            raise AssertionError('Result recollection cannot wait or resume a worker')
    env.scheduler.runtime = ExistingResultRuntime()
    return task, raw, artifacts, process_dir


async def recollect(env):
    from agentflow.control.coding_result_recovery import CodingResultRecovery
    await CodingResultRecovery(env.scheduler).reconcile()
    rows = await env.store.list('coding_result_recollection')
    assert len(rows) == 1
    return rows[0]


async def test_failed_finish_is_recollected_without_new_execution_or_duplicate_usage(coding_env):
    env = coding_env
    task, raw, artifacts, _ = await stopped_failure(env)
    old_attempt = await env.store.read('attempt', task['attempt_id'])
    before = {kind: await env.store.list(kind) for kind in ('coding_work_budget', 'coding_step_usage',
              'budget_account', 'model_invocation', 'supervised_attempt', 'dispatch_context')}
    record = await recollect(env)
    assert record['status'] == 'completed', record.get('failure_code')
    assert record['original_attempt'] == old_attempt
    work = await env.store.read('work_item', 'code')
    assert work['status'] == 'waiting_approval'
    assert work['attempt_id'] == task['attempt_id'] and work['generation'] == 1
    assert (await env.store.read('attempt', task['attempt_id']))['finished_at'] == old_attempt['finished_at']
    assert not work.get('blocking_reason') and not work.get('runtime_failure_code')
    assert (await env.store.read('work_item', 'review'))['status'] == 'pending'
    assert len(await env.store.list('attempt')) == 1 and len(await env.store.list('approval')) == 1
    assert not await env.store.list('coding_step_checkpoint')
    assert (artifacts / 'codex_final.json').read_bytes() == raw
    for kind, rows in before.items():
        assert await env.store.list(kind) == rows
    await recollect(env)
    assert len(await env.store.list('approval')) == 1 and env.scheduler.runtime.collections == 1
    replay = await env.workflow.finish_attempt(task['attempt_id'], {'execution_status': 'failed',
        'quality_result': 'unknown', 'fencing_token': task['fencing_token'],
        'input_fingerprint': task['input_fingerprint'], 'summary': '旧回执解析失败',
        'runtime_failure_code': 'final_schema_invalid'}, f"finish-{task['attempt_id']}", verified_artifacts=[])
    assert replay['status'] == 'failed'
    assert (await env.store.read('work_item', 'code'))['status'] == 'waiting_approval'


@pytest.mark.parametrize('legacy_version,prose', [(1, ''), (2, 'Implementation saved.\n\n')])
async def test_new_formatter_rule_rechecks_old_blocked_result_without_new_execution(coding_env, legacy_version, prose):
    from agentflow.control.coding_result_recovery import CodingResultRecovery
    env = coding_env
    task, _, artifacts, _ = await stopped_failure(env)
    raw = (prose + '{"type":"object","summary":"完成六应用实现。","status":"complete","next_action":""}'
           '</｜｜DSML｜｜parameter>\n</｜｜DSML｜｜invoke>\n</｜｜DSML｜｜tool_calls>').encode()
    (artifacts / 'codex_final.json').write_bytes(raw)
    old = await patch(env, 'coding_result_recollection', f'coding-result-v{legacy_version}:' + task['attempt_id'],
        rule_version=legacy_version, actor='controller', execution='existing_result_only', status='blocked',
        failure_code='coding_result_not_complete', run_id='run', work_item_id='code', attempt_id=task['attempt_id'],
        original_work=await env.store.read('work_item', 'code'),
        original_attempt=await env.store.read('attempt', task['attempt_id']), task=task)
    before = {kind: await env.store.list(kind) for kind in ('coding_work_budget', 'coding_step_usage',
              'budget_account', 'model_invocation', 'supervised_attempt', 'dispatch_context')}
    recovery = CodingResultRecovery(env.scheduler)
    await recovery.reconcile()
    work = await env.store.read('work_item', 'code')
    assert work['status'] == 'waiting_approval'
    assert work['attempt_id'] == task['attempt_id'] and work['generation'] == 1
    assert await env.store.read('coding_result_recollection', old['id']) == old
    records = await env.store.list('coding_result_recollection')
    assert len(records) == 2
    current = next(record for record in records if record['id'] != old['id'])
    assert current['rule_version'] > old['rule_version'] and current['status'] == 'completed'
    assert (artifacts / 'codex_final.json').read_bytes() == raw
    assert json.loads((artifacts / 'codex_final.normalized.json').read_bytes()) == {
        'summary': '完成六应用实现。', 'status': 'complete', 'next_action': ''}
    for kind, rows in before.items():
        assert await env.store.list(kind) == rows
    assert len(await env.store.list('attempt')) == 1 and len(await env.store.list('approval')) == 1
    await recovery.reconcile()
    assert env.scheduler.runtime.collections == 1 and len(await env.store.list('coding_result_recollection')) == 2


async def test_saved_snapshot_is_reused_while_independent_sibling_keeps_running(coding_env):
    env = coding_env
    task, _, _, _ = await stopped_failure(env, status='blocked', snapshot=True)
    original = await env.store.read('code_snapshot', task['attempt_id'])
    sibling = await patch(env, 'work_item', 'sibling', run_id='run', status='running', required=True,
        quality_result='unknown', step='implementation', attempt_id='sibling-attempt', generation=1)
    await patch(env, 'supervised_attempt', 'sibling-attempt', run_id='run', state='running')
    await patch(env, 'model_invocation', 'sibling-call', run_id='run', attempt_id='sibling-attempt', state='reserved')
    record = await recollect(env)
    assert record['status'] == 'completed'
    assert await env.store.read('code_snapshot', task['attempt_id']) == original
    assert await env.store.read('work_item', 'sibling') == sibling
    assert (await env.store.read('supervised_attempt', 'sibling-attempt'))['state'] == 'running'


@pytest.mark.parametrize('problem', ['usage_unknown', 'call_uncertain', 'live_process', 'receipt_tampered', 'continue'])
async def test_uncertain_or_incomplete_result_is_retained_without_hot_retry(coding_env, problem):
    env = coding_env
    content = {'summary': 'partial', 'status': 'continue', 'next_action': 'more coding'} if problem == 'continue' else None
    task, _, _, process_dir = await stopped_failure(env, content=content)
    if problem == 'usage_unknown':
        await patch(env, 'coding_step_usage', task['attempt_id'], known=False)
    elif problem == 'call_uncertain':
        await patch(env, 'model_invocation', 'original-call', state='uncertain')
    elif problem == 'live_process':
        await patch(env, 'supervised_attempt', task['attempt_id'], state='running')
    elif problem == 'receipt_tampered':
        receipt = json.loads((process_dir / 'result.json').read_bytes())
        atomic_json(process_dir / 'result.json', {**receipt, 'nonce': 'different'})
    before = await env.store.read('work_item', 'code')
    record = await recollect(env)
    assert record['status'] == 'blocked'
    calls = env.scheduler.runtime.collections
    await recollect(env)
    assert env.scheduler.runtime.collections == calls
    assert await env.store.read('work_item', 'code') == before
    assert not await env.store.list('approval')


async def test_workspace_change_after_authorization_cannot_replace_original_snapshot(coding_env):
    env = coding_env
    task, _, _, _ = await stopped_failure(env, status='blocked', snapshot=True)
    original = await env.store.read('code_snapshot', task['attempt_id'])
    execute = env.scheduler._execute_existing
    async def changed(frozen, **kwargs):
        (Path(frozen['workspace']) / 'feature.py').write_text('VALUE = 999\n')
        await execute(frozen, **kwargs)
    env.scheduler._execute_existing = changed
    record = await recollect(env)
    assert record['status'] == 'blocked'
    assert await env.store.read('code_snapshot', task['attempt_id']) == original
    assert not await env.store.list('approval')


@pytest.mark.parametrize('boundary', ['authorized', 'validated', 'finished'])
async def test_restart_replays_collection_checkpoints_without_reexecution(coding_env, boundary):
    from agentflow.control.coding_result_recovery import CodingResultRecovery
    env = coding_env
    task, _, _, _ = await stopped_failure(env)
    recovery = CodingResultRecovery(env.scheduler)
    execute, finish_attempt, finish_record = env.scheduler._execute_existing, env.workflow.finish_attempt, recovery._finish
    async def interrupted_execute(*args, **kwargs):
        raise asyncio.CancelledError()
    async def interrupted_finish_attempt(*args, **kwargs):
        raise asyncio.CancelledError()
    async def interrupted_record(*args, **kwargs):
        attempt = await env.store.read('attempt', task['attempt_id'])
        if attempt.get('result_recollection_id'):
            raise asyncio.CancelledError()
        return await finish_record(*args, **kwargs)
    if boundary == 'authorized':
        env.scheduler._execute_existing = interrupted_execute
    elif boundary == 'validated':
        env.workflow.finish_attempt = interrupted_finish_attempt
    else:
        recovery._finish = interrupted_record
    with pytest.raises(asyncio.CancelledError):
        await recovery.reconcile()
    assert (await env.store.list('coding_result_recollection'))[0]['status'] == 'collecting'
    original_snapshot = await env.store.read('code_snapshot', task['attempt_id'])
    env.scheduler._execute_existing, env.workflow.finish_attempt = execute, finish_attempt
    record = await recollect(env)
    assert record['status'] == 'completed'
    assert env.scheduler.runtime.collections == 1 and len(await env.store.list('approval')) == 1
    if original_snapshot:
        assert await env.store.read('code_snapshot', task['attempt_id']) == original_snapshot


async def test_concurrent_recollection_does_not_reblock_a_successful_result(coding_env):
    from agentflow.control.coding_result_recovery import CodingResultRecovery
    env = coding_env
    await stopped_failure(env)
    await asyncio.gather(CodingResultRecovery(env.scheduler).reconcile(), CodingResultRecovery(env.scheduler).reconcile())
    assert (await env.store.read('work_item', 'code'))['status'] == 'waiting_approval'
    assert (await env.store.list('coding_result_recollection'))[0]['status'] == 'completed'
    assert len(await env.store.list('approval')) == 1
    assert env.scheduler.runtime.collections == 1


async def test_original_snapshot_metadata_change_is_rejected_before_finishing(coding_env):
    env = coding_env
    task, _, _, _ = await stopped_failure(env, status='blocked', snapshot=True)
    execute = env.scheduler._execute_existing
    async def changed(frozen, **kwargs):
        await patch(env, 'code_snapshot', task['attempt_id'], base_oid='f' * 40)
        await execute(frozen, **kwargs)
    env.scheduler._execute_existing = changed
    assert (await recollect(env))['status'] == 'blocked'
    assert not await env.store.list('approval')


async def test_final_receipt_change_after_authorization_is_rejected_on_restart(coding_env):
    from agentflow.control.coding_result_recovery import CodingResultRecovery
    env = coding_env
    _, _, artifacts, _ = await stopped_failure(env)
    execute = env.scheduler._execute_existing
    async def interrupted(*args, **kwargs):
        raise asyncio.CancelledError()
    env.scheduler._execute_existing = interrupted
    with pytest.raises(asyncio.CancelledError):
        await CodingResultRecovery(env.scheduler).reconcile()
    (artifacts / 'codex_final.json').write_text('{"summary":"changed","status":"complete","next_action":""}')
    env.scheduler._execute_existing = execute
    assert (await recollect(env))['status'] == 'blocked'
    assert not await env.store.list('approval')


async def test_final_receipt_change_during_collection_cannot_publish_new_bytes(coding_env):
    env = coding_env
    _, _, artifacts, _ = await stopped_failure(env)
    execute = env.scheduler._execute_existing
    async def changed(task, **kwargs):
        (artifacts / 'codex_final.json').write_text('{"summary":"changed","status":"complete","next_action":""}')
        await execute(task, **kwargs)
    env.scheduler._execute_existing = changed
    assert (await recollect(env))['status'] == 'blocked'
    assert not await env.store.list('approval')


async def test_recollection_can_finish_at_the_exact_shared_budget_limit(coding_env):
    env = coding_env
    task, _, _, _ = await stopped_failure(env)
    before = await patch(env, 'coding_work_budget', task['coding_step']['budget_id'],
                         max_active_seconds=8.0, max_tool_calls=2, max_steps=1)
    assert (await recollect(env))['status'] == 'completed'
    assert await env.store.read('coding_work_budget', before['id']) == before


@pytest.mark.parametrize('problem', ['outside_scope', 'no_changes', 'snapshot_wrong_base'])
async def test_recollection_still_requires_real_authorized_code(coding_env, problem):
    env = coding_env
    task, _, _, _ = await stopped_failure(env, snapshot=problem == 'snapshot_wrong_base')
    if problem == 'outside_scope':
        (Path(task['workspace']) / 'outside.py').write_text('UNAUTHORIZED = True\n')
    elif problem == 'no_changes':
        (Path(task['workspace']) / 'feature.py').unlink()
    else:
        await patch(env, 'code_snapshot', task['attempt_id'], base_oid='f' * 40)
    before = await env.store.read('work_item', 'code')
    assert (await recollect(env))['status'] == 'blocked'
    assert await env.store.read('work_item', 'code') == before
    assert not await env.store.list('approval')


@pytest.mark.parametrize('problem', ['fence_changed', 'context_changed', 'usage_changed'])
async def test_authorization_cannot_follow_a_replaced_attempt_or_changed_usage(coding_env, problem):
    env = coding_env
    task, _, _, _ = await stopped_failure(env)
    execute = env.scheduler._execute_existing
    async def changed(frozen, **kwargs):
        if problem == 'fence_changed':
            await patch(env, 'work_item', 'code', fencing_token=task['fencing_token'] + 1)
        elif problem == 'context_changed':
            await patch(env, 'dispatch_context', task['attempt_id'], task={**task, 'allowed_write_paths': ['.']})
        else:
            await patch(env, 'coding_step_usage', task['attempt_id'], active_seconds=9.0)
        await execute(frozen, **kwargs)
    env.scheduler._execute_existing = changed
    assert (await recollect(env))['status'] == 'blocked'
    assert not await env.store.list('approval')


@pytest.mark.parametrize('evidence', ['raw', 'normalized', 'receipt', 'stdout', 'child_added', 'child_removed', 'child_changed'])
async def test_final_writer_rechecks_all_original_evidence_even_when_publishing_normalized_json(coding_env, evidence):
    env = coding_env
    task, _, artifacts, process_dir = await stopped_failure(env, content={
        'type': 'object', 'summary': 'complete', 'status': 'complete', 'next_action': 'handoff'})
    if evidence in {'child_removed', 'child_changed'}:
        process = await env.store.read('supervised_attempt', task['attempt_id'])
        atomic_json(process_dir / 'child.json', {**process, 'child': {'pid': 2147483646, 'process_started_at': 1.0}})
    finish = env.workflow.finish_attempt
    async def changed(*args, **kwargs):
        if evidence == 'raw':
            (artifacts / 'codex_final.json').write_text('{"summary":"partial","status":"continue","next_action":"more code"}')
        elif evidence == 'normalized':
            (artifacts / 'codex_final.normalized.json').write_text('{"summary":"replacement","status":"complete","next_action":""}')
        elif evidence == 'receipt':
            receipt = json.loads((process_dir / 'result.json').read_bytes())
            atomic_json(process_dir / 'result.json', {**receipt, 'active_seconds': 7.0})
        elif evidence == 'stdout':
            (process_dir / 'stdout.jsonl').write_text('{"type":"turn.completed"}\n')
        elif evidence == 'child_removed':
            (process_dir / 'child.json').unlink()
        else:
            atomic_json(process_dir / 'child.json', {'child': 'replacement'})
        return await finish(*args, **kwargs)
    env.workflow.finish_attempt = changed
    before = await env.store.read('work_item', 'code')
    assert (await recollect(env))['status'] == 'blocked'
    assert await env.store.read('work_item', 'code') == before
    assert not await env.store.list('approval')


@pytest.mark.parametrize('problem', ['scope', 'source_commit', 'model_overrun'])
async def test_preexisting_task_scope_source_and_confirmed_model_overrun_are_not_new_authority(coding_env, problem):
    env = coding_env
    task, _, _, _ = await stopped_failure(env)
    if problem == 'scope':
        task['allowed_write_paths'] = ['.']
        (Path(task['workspace']) / 'outside.py').write_text('UNAUTHORIZED = True\n')
        await patch(env, 'dispatch_context', task['attempt_id'], task=task)
    elif problem == 'source_commit':
        different = await env.repository.freeze_workspace(Path(task['workspace']), task['source_commit'], 'unrelated source')
        task['source_commit'] = different['commit_oid']
        await patch(env, 'dispatch_context', task['attempt_id'], task=task)
    else:
        await patch(env, 'model_invocation', 'original-call', overrun_micros=100, amount_micros=20, actual_micros=120)
    before = await env.store.read('work_item', 'code')
    assert (await recollect(env))['status'] == 'blocked'
    assert await env.store.read('work_item', 'code') == before
    assert env.scheduler.runtime.collections == 0
    assert not await env.store.list('approval')

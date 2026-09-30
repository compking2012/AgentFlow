"""Missing coding usage is never silently treated as zero after a possible launch."""
from uuid import uuid4

import pytest
from test_recovery import env as env
from test_recovery import patch, request

from agentflow.common import DomainError, canonical_digest
from agentflow.control.coding_steps import CodingSteps
from agentflow.control.recovery import coding_usage_blockers, coding_usage_snapshot
from agentflow.runtime.prelaunch import record_prelaunch_failure
from agentflow.runtime.workspace import WorkspaceManager


async def prepared_but_not_dispatched(env):
    run = await patch(env, 'run', 'run', execution_state='running')
    work = await patch(env, 'work_item', 'bad', step='implementation', role='development',
                       write_paths=['src'], status='running', quality_result='unknown')
    attempt = await patch(env, 'attempt', 'bad-attempt', status='running')
    control = await CodingSteps(env.store, env.settings, env.service.repository).prepare(
        run, work, attempt, env.project['base_commit'], 512)
    await env.workflow.block_attempt(attempt['id'], 'Original dispatch preparation failed before context creation',
                                    str(uuid4()), failure_code='worker_environment_unavailable')
    return control


async def blockers(env, *, exclude=None):
    state = await coding_usage_snapshot(env.store, 'run', 'bad')
    return await coding_usage_blockers(state, env.settings.data_dir, 'bad', exclude_attempt_id=exclude)


async def test_original_preparation_failure_can_retry_without_inventing_zero_usage(env):
    await prepared_but_not_dispatched(env)
    budget = await env.store.list('coding_work_budget')
    assert await blockers(env) == []
    run = await env.store.read('run', 'run')
    receipt = await env.service.recover('run', request(revision=run['revision'], target='bad'), 'prepared-only')
    assert receipt['execution'] == 'fresh_attempt'
    assert not await env.store.list('coding_step_usage')
    assert await env.store.list('coding_work_budget') == budget


async def test_bound_prelaunch_receipt_proves_no_coding_use_without_fabricating_accounting(env):
    control = await prepared_but_not_dispatched(env)
    work = await env.store.read('work_item', 'bad')
    workspace = await WorkspaceManager(env.settings.data_dir).create_clone(
        env.tmp_path / 'project', env.project['base_commit'], 'bad-attempt')
    task = {'attempt_id': 'bad-attempt', 'run_id': 'run', 'iteration_id': 'iteration', 'work_item_id': 'bad',
        'step': 'implementation', 'role': 'development', 'fencing_token': work['fencing_token'],
        'input_fingerprint': work['input_fingerprint'], 'source_commit': env.project['base_commit'],
        'workspace': str(workspace), 'allowed_write_paths': ['src'], 'output_schema': {'type': 'object'},
        'coding_step': control}
    await patch(env, 'dispatch_context', 'bad-attempt', task=task)
    assert await blockers(env), 'A context by itself cannot prove zero execution'
    assert await record_prelaunch_failure(env.store, env.settings.data_dir, task,
        phase='sandbox_validation', failure_code='isolation_probe_timeout')
    assert await blockers(env) == []
    before = await env.store.list('coding_work_budget')
    run = await env.store.read('run', 'run')
    await env.service.recover('run', request(revision=run['revision'], target='bad'), 'prelaunch-only')
    assert await env.store.list('coding_work_budget') == before and not await env.store.list('coding_step_usage')


@pytest.mark.parametrize('evidence', ['call', 'authorization', 'orphan_counter', 'launch_directory', 'finished_marker', 'collected_code'])
async def test_preparation_absence_is_not_a_proof_when_any_execution_evidence_remains(env, evidence):
    await prepared_but_not_dispatched(env)
    if evidence == 'call':
        await patch(env, 'model_invocation', 'old-call', run_id='run', iteration_id='iteration',
                    attempt_id='bad-attempt', state='completed_unpriced')
    elif evidence == 'authorization':
        await patch(env, 'task_authorization', 'old-token-hash', run_id='run', attempt_id='bad-attempt')
    elif evidence == 'orphan_counter':
        await patch(env, 'model_attempt_budget', 'bad-attempt', request_count=1, uncertain_invocations=0)
    elif evidence == 'launch_directory':
        (env.settings.data_dir / 'supervisor' / canonical_digest({'attempt_id': 'bad-attempt'}).split(':')[1]).mkdir(parents=True)
    elif evidence == 'collected_code':
        await patch(env, 'code_snapshot', 'bad-attempt', run_id='run', work_item_id='bad', generation=1, commit_oid='f' * 40)
    else:
        await patch(env, 'attempt', 'bad-attempt', finished_at='2026-09-23T00:00:00Z')
    assert (await blockers(env))[0]['code'] == 'coding_budget_unaccounted'
    before = await env.store.list('work_item')
    run = await env.store.read('run', 'run')
    with pytest.raises(DomainError) as error:
        await env.service.recover('run', request(revision=run['revision'], target='bad'), str(uuid4()))
    assert error.value.code == 'coding_budget_unaccounted'
    assert await env.store.list('work_item') == before and not await env.store.list('coding_step_usage')


async def test_unknown_usage_and_underreported_totals_remain_blocked(env):
    control = await prepared_but_not_dispatched(env)
    await patch(env, 'coding_step_usage', 'bad-attempt', run_id='run', work_item_id='bad',
                budget_id=control['budget_id'], known=False, active_seconds=None, observed_tool_calls=None)
    assert (await blockers(env))[0]['code'] == 'coding_budget_uncertain'
    await patch(env, 'coding_step_usage', 'bad-attempt', known=True, active_seconds=8.0, observed_tool_calls=2)
    assert (await blockers(env))[0]['code'] == 'coding_budget_uncertain'
    await patch(env, 'coding_work_budget', control['budget_id'], step_count=1, active_seconds=8.0, observed_tool_calls=2)
    assert await blockers(env) == []


async def test_current_prepare_exclusion_does_not_hide_an_older_started_step(env):
    control = await prepared_but_not_dispatched(env)
    await patch(env, 'supervised_attempt', 'bad-attempt', attempt_id='bad-attempt', state='completed', run_id='run')
    current = await patch(env, 'work_item', 'bad', attempt_id='next-attempt', generation=2, fencing_token=2,
                          input_fingerprint='next-input', status='running')
    await patch(env, 'attempt', 'next-attempt', run_id='run', iteration_id='iteration', work_item_id='bad',
                generation=2, fencing_token=2, input_fingerprint=current['input_fingerprint'], status='running')
    assert (await blockers(env, exclude='next-attempt'))[0]['code'] == 'coding_budget_unaccounted'
    assert (await env.store.read('coding_work_budget', control['budget_id']))['step_count'] == 0


async def test_late_launch_evidence_after_capture_prevents_recovery_commit(env, monkeypatch):
    await prepared_but_not_dispatched(env)
    capture = env.service._checkpoints
    async def capture_then_change(*args, **kwargs):
        points = await capture(*args, **kwargs)
        (env.settings.data_dir / 'supervisor' / canonical_digest({'attempt_id': 'bad-attempt'}).split(':')[1]).mkdir(parents=True)
        return points
    monkeypatch.setattr(env.service, '_checkpoints', capture_then_change)
    run = await env.store.read('run', 'run')
    with pytest.raises(DomainError) as error:
        await env.service.recover('run', request(revision=run['revision'], target='bad'), 'late-launch-evidence')
    assert error.value.code == 'coding_budget_unaccounted'
    assert (await env.store.read('work_item', 'bad'))['generation'] == 1


async def test_missing_usage_on_a_different_successful_sibling_does_not_block_target(env):
    await prepared_but_not_dispatched(env)
    await patch(env, 'coding_step_control', 'sibling-attempt', run_id='run', work_item_id='sibling',
                attempt_id='sibling-attempt', budget_id='sibling-budget')
    await patch(env, 'attempt', 'sibling-attempt', run_id='run', work_item_id='sibling', status='completed')
    assert await blockers(env) == []
    run = await env.store.read('run', 'run')
    assert await env.service.recover('run', request(revision=run['revision'], target='bad'), 'target-only')

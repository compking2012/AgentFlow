"""Controller no-progress recovery uses real Git and isolated stores, without models."""
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from test_coding_steps import _stopped_coding_process_proof, execute, task_for
from test_coding_steps import coding_env as coding_env
from test_failure_remediation import automatic as automatic
from test_recovery import env as env
from test_recovery import patch, stopped_workspace

from agentflow.control.coding_steps import CodingSteps
from agentflow.control.failure_remediation import FailureRemediation, resolve_bounded_coding_recovery
from agentflow.control.recovery import validate_recovery_checkpoint
from agentflow.runtime.workspace import WorkspaceManager


@pytest_asyncio.fixture
async def no_progress(automatic):
    value = automatic
    path, task, _, _ = await stopped_workspace(value)
    (path / 'src/keep.js').unlink()
    (path / 'src').rmdir()
    await patch(value, 'dispatch_context', 'bad-attempt', task={**task, 'max_output_tokens': 512})
    await patch(value, 'work_item', 'bad', runtime_failure_code='coding_no_progress')
    await patch(value, 'attempt', 'bad-attempt', runtime_failure_code='coding_no_progress')
    value.workspace = path
    return value


async def fail_recovered_step(value, *, changed=False):
    claim = await value.workflow.claim_next('run', 'fixture', str(uuid4()))
    work = claim['work_item']
    assert work['id'] == 'bad'
    checkpoint = await value.store.read('code_snapshot', work['payload']['recovery_checkpoint_id'])
    workspace = await WorkspaceManager(value.settings.data_dir).create_clone(
        Path(checkpoint['repository_path']), checkpoint['commit_oid'], work['attempt_id'])
    if changed:
        (workspace / 'src').mkdir(exist_ok=True)
        (workspace / 'src/step.js').write_text(f'export const step = {work["generation"]};\n')
    task = {'attempt_id': work['attempt_id'], 'work_item_id': work['id'], 'run_id': 'run',
        'fencing_token': work['fencing_token'], 'input_fingerprint': work['input_fingerprint'],
        'workspace': str(workspace), 'allowed_write_paths': work['write_paths'], 'step': work['step'],
        'source_commit': checkpoint['commit_oid'], 'max_output_tokens': 512}
    await _stopped_coding_process_proof(value, task)
    await value.workflow.block_attempt(work['attempt_id'], 'No code changes in this step', str(uuid4()),
                                       failure_code='coding_no_progress')
    return workspace


async def bounded_task_for(value):
    claim = await value.workflow.claim_next('run', 'fixture', str(uuid4()))
    work, attempt, run = claim['work_item'], claim['attempt'], claim['run']
    source, commit = await value.scheduler._source(run, work)
    recovery = await resolve_bounded_coding_recovery(value.store, run, work)
    control = await value.scheduler.coding_steps.prepare(run, work, attempt, commit, 512, recovery)
    workspace = await WorkspaceManager(value.settings.data_dir).create_clone(source, commit, attempt['id'])
    return {'attempt_id': attempt['id'], 'work_item_id': work['id'], 'run_id': run['id'], 'step': work['step'],
        'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint'],
        'workspace': str(workspace), 'source_commit': commit, 'allowed_write_paths': work['write_paths'],
        'coding_step': control, 'max_output_tokens': 512}


async def test_no_progress_schedules_fresh_attempt_from_verified_unchanged_tree(no_progress):
    value = no_progress
    budgets = await value.store.list('budget_account')
    profiles = await value.store.list('model_profile')
    upstream = await value.store.read('work_item', 'upstream')
    result = await value.automatic.repair('bad')
    assert result['status'] == 'repair_scheduled', result
    assert result['failure_code'] == 'coding_no_progress' and result['action'] == 'retry_current'
    work = await value.store.read('work_item', 'bad')
    assert work['status'] == 'pending' and work['generation'] == 2 and work['attempt_id'] is None
    assert work['write_paths'] == ['src'] and work['approval_required']
    assert result['repair_instruction'] in work['payload']['recovery_instruction']
    run = await value.store.read('run', 'run')
    plan = await resolve_bounded_coding_recovery(value.store, run, work)
    assert plan['round'] == 1 and plan['no_progress_streak'] == 1
    assert plan['max_files_per_step'] == 1 and plan['max_changed_lines_per_step'] == 80
    assert plan['step_reduction_factor'] == 0.5 and plan['observed_output_cap'] == 512
    assert plan['progress']['tree_oid'] == plan['progress']['source_tree_oid']
    assert not plan['progress']['has_code_changes']
    checkpoint = await value.store.read('code_snapshot', plan['checkpoint_id'])
    await validate_recovery_checkpoint(value.store, run, work, checkpoint, value.service.repository)
    assert checkpoint['tree_oid'] == plan['progress']['tree_oid']
    assert await value.store.list('budget_account') == budgets
    assert await value.store.list('model_profile') == profiles
    assert await value.store.read('work_item', 'upstream') == upstream
    assert not await value.store.list('model_invocation')


async def test_empty_completion_also_receives_a_smaller_evidence_bound_retry(no_progress):
    value = no_progress
    await patch(value, 'work_item', 'bad', runtime_failure_code='no_code_changes')
    await patch(value, 'attempt', 'bad-attempt', runtime_failure_code='no_code_changes')
    result = await value.automatic.repair('bad')
    assert result['status'] == 'repair_scheduled'
    work = await value.store.read('work_item', 'bad')
    plan = await resolve_bounded_coding_recovery(value.store, await value.store.read('run', 'run'), work)
    assert plan and plan['round'] == 1 and plan['max_files_per_step'] == 1
    assert '空注释' in plan['next_action']


@pytest.mark.parametrize('git_state', ['unstaged', 'staged', 'committed'])
async def test_no_progress_preserves_saved_code_and_does_not_mutate_source_index(no_progress, git_state):
    value = no_progress
    path = value.workspace
    (path / 'src').mkdir()
    (path / 'src/keep.js').write_text('export const valuable = 42;\n')
    repository = value.service.repository
    if git_state != 'unstaged':
        await asyncio.to_thread(repository._run, path, ['add', '--', 'src/keep.js'])
    if git_state == 'committed':
        await asyncio.to_thread(repository._run, path,
            ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'Saved code'])
    commands = [['rev-parse', 'HEAD'], ['ls-files', '--stage'], ['status', '--porcelain']]
    before = [await asyncio.to_thread(repository._run, path, args) for args in commands]
    result = await value.automatic.repair('bad')
    assert result['status'] == 'repair_scheduled', result
    assert [await asyncio.to_thread(repository._run, path, args) for args in commands] == before
    work = await value.store.read('work_item', 'bad')
    checkpoint = await value.store.read('code_snapshot', work['payload']['recovery_checkpoint_id'])
    fresh = value.tmp_path / 'verified-no-progress-code'
    await repository.clone_snapshot(path, fresh, checkpoint['commit_oid'])
    assert (fresh / 'src/keep.js').read_text() == 'export const valuable = 42;\n'


async def test_repeated_no_progress_uses_configured_retries_before_stopping(no_progress):
    value = no_progress
    value.workflow.settings = value.workflow.settings.model_copy(update={'auto_failure_retry_limit': 2})
    assert (await value.automatic.repair('bad'))['status'] == 'repair_scheduled'
    await fail_recovered_step(value)
    result = await value.automatic.repair('bad')
    assert result['status'] == 'repair_scheduled'
    work = await value.store.read('work_item', 'bad')
    plan = await resolve_bounded_coding_recovery(value.store, await value.store.read('run', 'run'), work)
    assert plan['round'] == 2 and plan['no_progress_streak'] == 2
    assert '不同' in plan['next_action']
    await fail_recovered_step(value)
    before = await value.store.list('work_item')
    result = await value.automatic.repair('bad')
    assert result['status'] == 'blocked'
    assert 'automatic_repair_limit' in {row['code'] for row in result['blockers']}
    assert any('2/2' in row['message'] for row in result['blockers'])
    assert await value.store.list('work_item') == before
    assert len(await value.store.list('run_recovery')) == 2


async def test_real_progress_allows_smaller_retry_until_existing_work_limit(no_progress):
    value = no_progress
    value.workflow.settings = value.workflow.settings.model_copy(update={'auto_failure_retry_limit': 2})
    assert (await value.automatic.repair('bad'))['status'] == 'repair_scheduled'
    await fail_recovered_step(value, changed=True)
    result = await value.automatic.repair('bad')
    assert result['status'] == 'repair_scheduled', result
    work = await value.store.read('work_item', 'bad')
    plan = await resolve_bounded_coding_recovery(value.store, await value.store.read('run', 'run'), work)
    assert plan['round'] == 2 and plan['no_progress_streak'] == 0
    assert plan['max_changed_lines_per_step'] == 40 and plan['step_reduction_factor'] == 0.25
    await fail_recovered_step(value, changed=True)
    result = await value.automatic.repair('bad')
    assert result['status'] == 'blocked'
    assert 'automatic_repair_limit' in {row['code'] for row in result['blockers']}
    assert len(await value.store.list('run_recovery')) == 2


@pytest.mark.parametrize('setting', ['auto_failure_retry_limit', 'auto_failure_run_limit'])
async def test_disabled_retry_policy_keeps_no_progress_blocked(no_progress, setting):
    value = no_progress
    value.workflow.settings = value.settings.model_copy(update={setting: 0})
    before = await value.store.list('work_item')
    result = await value.automatic.repair('bad')
    assert result['status'] == 'blocked'
    assert 'automatic_repair_limit' in {row['code'] for row in result['blockers']}
    assert await value.store.list('work_item') == before and not await value.store.list('run_recovery')


@pytest.mark.parametrize('damage', ['scope', 'identity', 'missing_cap', 'non_coding'])
async def test_no_progress_requires_verified_scope_identity_and_coding_context(no_progress, damage):
    value = no_progress
    context = await value.store.read('dispatch_context', 'bad-attempt')
    task = context['task']
    if damage == 'scope':
        (value.workspace / 'unauthorized.js').write_text('export const outside = true;\n')
    elif damage == 'identity':
        await patch(value, 'dispatch_context', 'bad-attempt', task={**task, 'input_fingerprint': 'wrong'})
    elif damage == 'missing_cap':
        await patch(value, 'dispatch_context', 'bad-attempt', task={**task, 'max_output_tokens': None})
    else:
        await patch(value, 'work_item', 'bad', step='research')
    before = await value.store.list('work_item')
    result = await value.automatic.repair('bad')
    assert result['status'] == 'blocked'
    assert await value.store.list('work_item') == before and not await value.store.list('run_recovery')


@pytest.mark.parametrize('changes', [
    {'uncertain': True}, {'active_seconds': 120}, {'observed_tool_calls': 30}, {'step_count': 32},
])
async def test_no_progress_recovery_does_not_reset_shared_work_budget(no_progress, changes):
    value = no_progress
    budget = await patch(value, 'coding_work_budget', CodingSteps.budget_id('run', 'bad'), **{
        'run_id': 'run', 'work_item_id': 'bad', 'max_steps': 32, 'max_active_seconds': 120,
        'max_tool_calls': 30, 'active_seconds': 0.0, 'observed_tool_calls': 0, 'step_count': 0,
        'uncertain': False, **changes})
    before = await value.store.list('work_item')
    result = await value.automatic.repair('bad')
    assert result['status'] == 'blocked'
    assert any(row['code'].startswith('coding_budget_') for row in result['blockers'])
    assert await value.store.list('work_item') == before
    assert await value.store.read('coding_work_budget', budget['id']) == budget
    assert not await value.store.list('run_recovery')


async def test_auto_recovery_retains_successful_small_step_and_continues_real_remaining_code(coding_env):
    value = coding_env
    value.workflow.settings = value.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0})
    first = await task_for(value)
    first['max_output_tokens'] = 512
    await _stopped_coding_process_proof(value, first)
    await execute(value, first, {'summary': 'first function saved', 'status': 'continue',
        'next_action': 'add the second planned function'}, 'FIRST = True\n')
    second = await task_for(value)
    path = await WorkspaceManager(value.settings.data_dir).create_clone(
        Path(first['workspace']), second['source_commit'], second['attempt_id'])
    second.update(workspace=str(path), max_output_tokens=512)
    await _stopped_coding_process_proof(value, second)
    stopped = await execute(value, second, {'summary': 'no second function yet', 'status': 'continue',
        'next_action': 'add the second planned function'}, tools=1)
    assert stopped['status'] == 'blocked' and stopped['runtime_failure_code'] == 'coding_no_progress'
    original = await value.store.read('attempt', first['attempt_id'])
    budget = await value.store.list('coding_work_budget')
    result = await FailureRemediation(value.store, value.workflow).repair('code')
    assert result['status'] == 'repair_scheduled', result
    work = await value.store.read('work_item', 'code')
    plan = await resolve_bounded_coding_recovery(value.store, await value.store.read('run', 'run'), work)
    assert plan['no_progress_streak'] == 1 and not plan['progress']['has_code_changes']
    assert await value.store.list('coding_work_budget') == budget
    assert await value.store.read('attempt', first['attempt_id']) == original
    restored = await bounded_task_for(value)
    assert (Path(restored['workspace']) / 'feature.py').read_text() == 'FIRST = True\n'
    assert restored['coding_step']['base_commit'] == first['source_commit']
    assert restored['coding_step']['step_reduction_factor'] == 0.5
    assert restored['coding_step']['next_action'] == plan['next_action']
    assert restored['coding_step']['max_active_seconds'] == 84
    assert restored['coding_step']['max_tool_calls'] == 9
    completed = await execute(value, restored, {'summary': 'both planned functions saved',
        'status': 'complete', 'next_action': ''}, 'FIRST = True\nSECOND = True\n', tools=1)
    assert completed['status'] == 'waiting_approval'
    assert (await value.store.read('work_item', 'review'))['status'] == 'pending'


@pytest.mark.parametrize('write_code', [False, True])
async def test_recovering_empty_code_cannot_complete_without_real_implementation(coding_env, write_code):
    value = coding_env
    value.workflow.settings = value.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0})
    first = await task_for(value)
    path = await WorkspaceManager(value.settings.data_dir).create_clone(
        Path(value.project['local_path']), first['source_commit'], first['attempt_id'])
    first.update(workspace=str(path), max_output_tokens=512)
    await _stopped_coding_process_proof(value, first)
    stopped = await execute(value, first, {'summary': 'no code yet', 'status': 'continue',
        'next_action': 'implement the planned feature'})
    assert stopped['status'] == 'blocked' and stopped['runtime_failure_code'] == 'coding_no_progress'
    assert (await FailureRemediation(value.store, value.workflow).repair('code'))['status'] == 'repair_scheduled'
    restored = await bounded_task_for(value)
    result = await execute(value, restored, {'summary': 'feature complete', 'status': 'complete', 'next_action': ''},
                           'READY = True\n' if write_code else None)
    if write_code:
        assert result['status'] == 'waiting_approval'
        assert (Path(restored['workspace']) / 'feature.py').read_text() == 'READY = True\n'
    else:
        assert result['status'] == 'blocked' and result['runtime_failure_code'] == 'no_code_changes'
        assert not await value.store.list('approval')
    assert (await value.store.read('work_item', 'review'))['status'] == 'pending'

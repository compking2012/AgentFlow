"""No new edits may retain only a proven nonempty child contribution for re-review."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_coding_steps import _stopped_coding_process_proof, execute, task_for
from test_coding_steps import coding_env as coding_env
from test_parallel_remediation import CollectedFixtureRuntime, finding, update
from test_parallel_remediation import parallel_env as parallel_env
from test_recovery_correction_context import prepare_review_repair, timeout_without_edits
from test_remediation import env as env
from test_single_source_review_batch import single_source as single_source

from agentflow.control.scheduler import Scheduler
from agentflow.runtime.workspace import WorkspaceManager


async def prepare(env, tmp_path, siblings=False):
    await update(env.store, 'review', 'review-attempt-1', blocking_findings=[finding('a'), *([finding('b')] if siblings else [])])
    await update(env.store, 'attempt', 'review-attempt-1', quality_result='failed')
    await prepare_review_repair(env, tmp_path, 'module-a')
    original = await env.store.read('attempt', 'module-a-attempt-1')
    snapshot = env.contributions['module-a']
    await _stopped_coding_process_proof(env, {'attempt_id': original['id'], 'work_item_id': 'module-a', 'run_id': 'run',
        'step': 'implementation', 'fencing_token': original['fencing_token'], 'input_fingerprint': original['input_fingerprint'],
        'workspace': snapshot['repository_path'], 'source_commit': snapshot['base_oid'], 'allowed_write_paths': ['src/a.mjs']})


async def next_task(env, identity='module-a'):
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    work, attempt, run = claim['work_item'], claim['attempt'], claim['run']
    assert work['id'] == identity
    source, commit = await env.scheduler._source(run, work)
    control = await env.scheduler.coding_steps.prepare(run, work, attempt, commit, 512)
    workspace = await WorkspaceManager(env.settings.data_dir).create_clone(source, commit, attempt['id'])
    task = {'attempt_id': attempt['id'], 'work_item_id': identity, 'run_id': 'run', 'iteration_id': 'iteration',
        'step': 'implementation', 'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint'],
        'workspace': str(workspace), 'source_commit': commit, 'allowed_write_paths': work['write_paths'], 'coding_step': control}
    await _stopped_coding_process_proof(env, task)
    return task


async def finish(env, task, *, status='complete', known=True):
    result = {'summary': 'Existing assigned module remains complete; submit it for independent review.',
              'status': status, 'next_action': 'Continue inspecting' if status == 'continue' else ''}
    folder = env.settings.data_dir / 'attempt_artifacts' / task['attempt_id']
    folder.mkdir(parents=True, exist_ok=True)
    file = folder / 'codex_final.json'
    file.write_text(json.dumps(result))
    async def run(_task):
        return {'execution_status': 'completed', 'result': result, 'artifacts': [{'path': str(file)}],
            'active_seconds': 2, 'observed_tool_calls': 1, 'tool_observation_complete': known}
    env.scheduler.runtime = SimpleNamespace(execute_task=run)
    await env.scheduler._execute_existing(task)
    return await env.store.read('work_item', task['work_item_id'])


async def test_unchanged_completed_child_preserves_full_source_and_merges_repaired_sibling_before_review(parallel_env, tmp_path):
    env = parallel_env
    await prepare(env, tmp_path, siblings=True)
    first = await next_task(env)
    before = await env.store.list('budget_account')
    work = await finish(env, first)
    assert work['status'] == 'completed', work
    assert work['quality_result'] == 'unknown'
    retained = await env.store.read('retained_review_contribution', first['attempt_id'])
    assert retained['source_child_snapshot_id'] == 'module-a-attempt-1'
    assert retained['original_changed_paths'] == ['src/a.mjs']
    assert retained['current_diff_has_changes'] is False and retained['requires_independent_review'] is True
    snapshot = await env.store.read('code_snapshot', first['attempt_id'])
    assert snapshot['base_oid'] == env.aggregate['commit_oid']
    assert all((Path(snapshot['repository_path']) / path).is_file() for path in env.paths.values())
    second = await next_task(env, 'module-b')
    (Path(second['workspace']) / 'src/b.mjs').write_text('export const b = 1;\n')
    assert (await finish(env, second))['status'] == 'completed'
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert claim['work_item']['id'] == 'implementation'
    await env.scheduler._dispatch(claim)
    assembly = await env.store.read('work_item', 'implementation')
    assert assembly['status'] == 'completed', assembly
    review = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    source, commit = await env.scheduler._source(review['run'], review['work_item'])
    assert (source / 'src/a.mjs').read_text() == env.original_text['module-a']
    assert (source / 'src/b.mjs').read_text() == 'export const b = 1;\n'
    assert all((source / path).is_file() for path in env.paths.values())
    workspace = await WorkspaceManager(env.settings.data_dir).create_clone(source, commit, review['attempt']['id'])
    env.scheduler.runtime = CollectedFixtureRuntime(env)
    await env.scheduler._execute_existing({'attempt_id': review['attempt']['id'], 'work_item_id': 'review-work', 'run_id': 'run',
        'step': 'code_review', 'fencing_token': review['attempt']['fencing_token'], 'input_fingerprint': review['attempt']['input_fingerprint'],
        'workspace': str(workspace), 'source_commit': commit, 'allowed_write_paths': []})
    assert (await env.store.read('work_item', 'review-work'))['quality_result'] == 'passed'
    assert (await env.store.read('review', 'review-attempt-1'))['quality_result'] == 'failed'
    assert await env.store.list('budget_account') == before


async def test_recovery_alias_chain_retains_original_contribution_without_resetting_coding_budget(parallel_env, tmp_path):
    env = parallel_env
    await prepare(env, tmp_path)
    await timeout_without_edits(env, 'module-a')
    await timeout_without_edits(env, 'module-a')
    previous = (await env.store.list('coding_work_budget'))[0]
    task = await next_task(env)
    assert task['coding_step']['base_commit'] == env.aggregate['commit_oid']
    assert (await finish(env, task))['status'] == 'completed'
    budget = (await env.store.list('coding_work_budget'))[0]
    assert budget['step_count'] == previous['step_count'] + 1
    assert budget['active_seconds'] == previous['active_seconds'] + 2
    assert budget['max_steps'] == previous['max_steps']
    proof = await env.store.read('retained_review_contribution', task['attempt_id'])
    assert len(proof['checkpoint_chain_ids']) == 3


async def test_first_empty_implementation_still_fails(coding_env):
    env = coding_env
    task = await task_for(env)
    work = await execute(env, task, {'summary': 'complete', 'status': 'complete', 'next_action': ''})
    assert work['status'] == 'blocked' and work['runtime_failure_code'] == 'no_code_changes'
    assert not await env.store.list('retained_review_contribution')


@pytest.mark.parametrize('damage', ['scope', 'source_alias', 'original_dispatch', 'empty_original', 'current_process',
                                     'unknown_usage', 'exhausted_budget', 'unknown_model', 'new_requirement'])
async def test_retention_rejects_changed_authority_or_unproven_completion(parallel_env, tmp_path, damage):
    env = parallel_env
    await prepare(env, tmp_path)
    task = await next_task(env)
    work = await env.store.read('work_item', 'module-a')
    alias_id = work['payload']['repair_base_snapshot_id']
    if damage == 'scope':
        await update(env.store, 'work_item', 'module-a', write_paths=['src'])
        task['allowed_write_paths'] = ['src']
        await update(env.store, 'dispatch_context', task['attempt_id'], task=task)
    elif damage == 'source_alias':
        await update(env.store, 'code_snapshot', alias_id, source_child_snapshot_id='module-b-attempt-1')
    elif damage in {'original_dispatch', 'empty_original'}:
        old = await env.store.read('dispatch_context', 'module-a-attempt-1')
        changed = {'allowed_write_paths': ['src']} if damage == 'original_dispatch' else {'source_commit': env.contributions['module-a']['commit_oid']}
        await update(env.store, 'dispatch_context', old['id'], task={**old['task'], **changed})
        if damage == 'empty_original':
            await update(env.store, 'code_snapshot', 'module-a-attempt-1', base_oid=env.contributions['module-a']['commit_oid'])
    elif damage == 'current_process':
        await update(env.store, 'supervised_attempt', task['attempt_id'], state='running')
    elif damage == 'exhausted_budget':
        await update(env.store, 'coding_work_budget', task['coding_step']['budget_id'], max_active_seconds=1)
    elif damage == 'unknown_model':
        await update(env.store, 'model_invocation', 'unknown', run_id='run', attempt_id=task['attempt_id'], state='uncertain')
    elif damage == 'new_requirement':
        await update(env.store, 'work_item', 'module-a', payload={'change_expectation': 'A new requirement with no review return'})
    stopped = await finish(env, task, known=damage != 'unknown_usage')
    assert stopped['status'] == 'blocked'
    assert not await env.store.list('retained_review_contribution')


async def test_retention_uses_current_git_objects_after_original_workspace_retirement(parallel_env, tmp_path):
    import shutil
    env = parallel_env
    await prepare(env, tmp_path)
    task = await next_task(env)
    shutil.rmtree(env.contributions['module-a']['repository_path'])
    assert (await finish(env, task))['status'] == 'completed'


async def test_continuing_without_new_progress_is_still_rejected(parallel_env, tmp_path):
    env = parallel_env
    await prepare(env, tmp_path)
    task = await next_task(env)
    work = await finish(env, task, status='continue')
    assert work['status'] == 'blocked' and work['runtime_failure_code'] == 'coding_no_progress'
    assert not await env.store.list('retained_review_contribution')


async def test_late_source_change_is_rejected_before_retention_is_persisted(parallel_env, tmp_path, monkeypatch):
    from agentflow.control.retained_review_contribution import RetainedReviewContribution
    env = parallel_env
    await prepare(env, tmp_path)
    task = await next_task(env)
    original = RetainedReviewContribution._filesystem
    calls = 0
    def changed(self, *args):
        nonlocal calls
        calls += 1
        if calls == 2:
            (Path(task['workspace']) / 'src/b.mjs').write_text('export const b = 99;\n')
        return original(self, *args)
    monkeypatch.setattr(RetainedReviewContribution, '_filesystem', changed)
    assert (await finish(env, task))['status'] == 'blocked'
    assert not await env.store.list('retained_review_contribution')


async def test_single_source_review_alias_can_reconfirm_accepted_nonempty_work(single_source):
    from test_single_source_review_batch import failed_facets
    env = single_source
    env.scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    for kind, identity in [('run', 'run'), ('iteration', 'iteration')]:
        row = await env.store.read(kind, identity)
        await update(env.store, kind, identity, budget_limit={**row['budget_limit'], 'max_active_seconds': 100,
            'max_tool_calls': 20, 'cost_mode': 'request_limited'})
    facets, _ = await failed_facets(env, 1)
    assert await env.remediation.repair(facets[0]['id'])
    original = await env.store.read('attempt', 'code-attempt')
    snapshot = await env.store.read('code_snapshot', 'code-attempt')
    await _stopped_coding_process_proof(env, {'attempt_id': original['id'], 'work_item_id': 'code', 'run_id': 'run',
        'step': 'implementation', 'fencing_token': original['fencing_token'], 'input_fingerprint': original['input_fingerprint'],
        'workspace': snapshot['repository_path'], 'source_commit': snapshot['base_oid'], 'allowed_write_paths': ['.']})
    task = await next_task(env, 'code')
    result = await finish(env, task)
    assert result['status'] == 'completed', result.get('blocking_reason')
    retained = await env.store.read('retained_review_contribution', task['attempt_id'])
    assert retained['source_child_snapshot_id'] == 'code-attempt'
    assert set(retained['original_changed_paths']) == {'product.py', 'keep.txt'}


async def test_dispatch_task_token_is_ephemeral_and_not_part_of_retained_evidence(parallel_env, tmp_path):
    env = parallel_env
    await prepare(env, tmp_path)
    task = await next_task(env)
    execution_task = {**task, 'task_token': 'private-fixture-ephemeral-token'}
    assert (await finish(env, execution_task))['status'] == 'completed'
    proof = await env.store.read('retained_review_contribution', task['attempt_id'])
    assert execution_task['task_token'] not in json.dumps(proof)
    assert (await env.store.read('dispatch_context', task['attempt_id']))['task'] == task


async def test_unrecognized_transient_task_fields_do_not_relax_dispatch_binding(parallel_env, tmp_path):
    env = parallel_env
    await prepare(env, tmp_path)
    task = await next_task(env)
    assert (await finish(env, {**task, 'unrecognized_transient_override': True}))['status'] == 'blocked'
    assert not await env.store.list('retained_review_contribution')


@pytest.mark.asyncio
async def test_sibling_heartbeat_does_not_reject_a_verified_retained_contribution(parallel_env,tmp_path,monkeypatch):
    from agentflow.control.retained_review_contribution import RetainedReviewContribution
    env=parallel_env
    await prepare(env,tmp_path,siblings=True)
    target=await next_task(env)
    sibling=await next_task(env,'module-b')
    inspected,advanced=asyncio.Event(),asyncio.Event()
    original=RetainedReviewContribution.inspect
    async def inspect_then_yield(self,task,snapshot,content):
        proof=await original(self,task,snapshot,content)
        assert proof is not None
        inspected.set()
        await advanced.wait()
        return proof
    monkeypatch.setattr(RetainedReviewContribution,'inspect',inspect_then_yield)
    async def heartbeat():
        await inspected.wait()
        await update(env.store,'attempt',sibling['attempt_id'],last_heartbeat=1234567890)
        advanced.set()
    result,_=await asyncio.wait_for(asyncio.gather(finish(env,target),heartbeat()),timeout=30)
    assert (await env.store.read('attempt',sibling['attempt_id']))['last_heartbeat']==1234567890
    assert result['status']=='completed',result.get('blocking_reason')
    retained=await env.store.read('retained_review_contribution',target['attempt_id'])
    assert retained['source_child_snapshot_id']=='module-a-attempt-1'
    assert retained['requires_independent_review'] is True


@pytest.mark.parametrize('changed', ['shared_budget', 'own_usage'])
async def test_collection_rechecks_real_budget_changes_after_inspection(parallel_env, tmp_path, monkeypatch, changed):
    from agentflow.control.retained_review_contribution import RetainedReviewContribution
    from agentflow.models.budget import account_id
    env = parallel_env
    await prepare(env, tmp_path)
    task = await next_task(env)
    original = RetainedReviewContribution.inspect
    async def change_after_inspection(self, *args):
        proof = await original(self, *args)
        assert proof is not None
        if changed == 'shared_budget':
            account = await env.store.read('budget_account', account_id('run', 'run'))
            await update(env.store, 'budget_account', account['id'], request_count=account['max_requests'] + 1)
        else:
            usage = await env.store.read('coding_step_usage', task['attempt_id'])
            await update(env.store, 'coding_step_usage', usage['id'], active_seconds=usage['active_seconds'] + 1)
        return proof
    monkeypatch.setattr(RetainedReviewContribution, 'inspect', change_after_inspection)
    assert (await finish(env, task))['status'] == 'blocked'
    assert not await env.store.list('retained_review_contribution')


async def test_damaged_declared_review_alias_is_not_reclassified_as_ordinary_recovery(parallel_env, tmp_path):
    env = parallel_env
    await prepare(env, tmp_path)
    task = await next_task(env)
    work = await env.store.read('work_item', 'module-a')
    await update(env.store, 'code_snapshot', work['payload']['repair_base_snapshot_id'], checkpoint_kind=None)
    result = await finish(env, task)
    assert result['status'] == 'blocked'
    assert '审查返工无法核验' in result['blocking_reason']
    assert not await env.store.list('retained_review_contribution')

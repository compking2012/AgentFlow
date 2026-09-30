"""A failed model review aggregation returns findings to its frozen coding producer."""
from uuid import uuid4

import pytest
from test_parallel_remediation import finding, update
from test_parallel_remediation import parallel_env as parallel_env
from test_parallel_review_child_remediation import collect_review

from agentflow.control.failure_remediation import FailureRemediation
from agentflow.control.review_checkpoint import validate_review_repair_source
from agentflow.control.scheduler import Scheduler
from agentflow.domain.expansion import StageExpander


async def failed_aggregation(env):
    env.workflow.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0})
    await update(env.store, 'plan', 'plan', actual_steps=['implementation', 'code_review', 'unit_test_plan'],
        work_specs=[{'key': 'code_review', 'step': 'code_review', 'role': 'review'}])
    parent = await update(env.store, 'work_item', 'review-work', status='pending', quality_result='unknown',
                          attempt_id=None, output_fingerprint=None)
    await update(env.store, 'review', 'review-attempt-1', stale=True)
    expanded = await StageExpander(env.store).expand('run', parent['id'], [
        {'key': name, 'goal': 'Review current code', 'write_paths': [], 'review_focus': 'current_code'}
        for name in ('facet-a', 'facet-b')], str(uuid4()), parent['revision'])
    for _ in range(2):
        claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
        assert (await collect_review(env, claim, []))['quality_result'] == 'passed'
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert claim['work_item']['id'] == parent['id'] and claim['work_item']['kind'] == 'aggregation'
    result = await collect_review(env, claim, [finding('b')])
    assert result['status'] == 'completed' and result['quality_result'] == 'failed'
    return result, expanded['work_item_ids']


@pytest.mark.parametrize('automatic', [False, True])
async def test_failed_review_aggregation_repairs_only_owner_and_can_dispatch_preserved_source(parallel_env, automatic):
    env = parallel_env
    parent, child_ids = await failed_aggregation(env)
    preserved = {identity: await env.store.read('work_item', identity) for identity in ('module-a', 'module-c', 'module-d')}
    budgets = await env.store.list('budget_account')
    if automatic:
        result = await FailureRemediation(env.store, env.workflow, review=env.remediation).repair(parent['id'])
        assert result['status'] == 'repair_scheduled', result
        receipt = (await env.store.list('review_repair'))[0]
    else:
        receipt = await env.remediation.repair(parent['id'])
        assert receipt is not None, 'The LLM aggregation can fail after all child reviews passed'
    assert receipt['repair_work_item_ids'] == ['module-b']
    assert set(receipt['affected_work_item_ids']) == {'module-b', 'implementation', parent['id'], 'unit', *child_ids}
    assert {identity: await env.store.read('work_item', identity) for identity in preserved} == preserved
    assert await env.store.list('budget_account') == budgets
    for identity in [parent['id'], *child_ids]:
        revised = await env.store.read('work_item', identity)
        assert revised['status'] == 'pending'
        assert revised['generation'] == parent['generation'] + 1
    assert (await env.store.read('work_item', parent['id']))['kind'] == 'aggregation'
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    work, run = claim['work_item'], claim['run']
    assert work['id'] == 'module-b' and work['write_paths'] == ['src/b.mjs']
    scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    source, commit = await scheduler._source(run, work)
    assert commit == env.aggregate['commit_oid']
    assert all((source / path).read_text() == env.original_text[identity] for identity, path in env.paths.items())
    alias = await env.store.read('code_snapshot', work['payload']['repair_base_snapshot_id'])
    authorized = await validate_review_repair_source(env.store, run, work, alias)
    assert authorized['review']['id'] == parent['attempt_id']


@pytest.mark.parametrize('damage', ['membership', 'original_dependencies', 'expansion_record', 'parent_write',
    'source_commit', 'source_generation', 'source_author', 'parent_attempt', 'child_not_passed',
    'child_dependencies', 'child_write', 'child_review_owner', 'child_review_source', 'child_attempt'])
async def test_review_aggregation_rejects_incoherent_graph_source_or_accepted_child_identity(parallel_env, damage):
    env = parallel_env
    parent, child_ids = await failed_aggregation(env)
    child = await env.store.read('work_item', child_ids[0])
    if damage == 'membership':
        await update(env.store, 'work_item', parent['id'], expanded_child_ids=child_ids[:1])
    elif damage == 'original_dependencies':
        await update(env.store, 'work_item', parent['id'], original_dependencies=['module-b'])
    elif damage == 'expansion_record':
        expansion = (await env.store.list('stage_expansion'))[0]
        await update(env.store, 'stage_expansion', expansion['id'], child_ids=[])
    elif damage == 'parent_write':
        await update(env.store, 'work_item', parent['id'], write_paths=['src'])
    elif damage == 'source_commit':
        await update(env.store, 'review', parent['attempt_id'], reviewed_commit=env.project['base_commit'])
    elif damage == 'source_generation':
        await update(env.store, 'code_snapshot', env.aggregate['id'], generation=99)
    elif damage == 'source_author':
        await update(env.store, 'attempt', env.aggregate['id'], input_fingerprint='wrong-input')
    elif damage == 'parent_attempt':
        await update(env.store, 'attempt', parent['attempt_id'], fencing_token=99)
    elif damage == 'child_not_passed':
        await update(env.store, 'work_item', child['id'], quality_result='failed')
    elif damage == 'child_dependencies':
        await update(env.store, 'work_item', child['id'], dependencies=['module-b'])
    elif damage == 'child_write':
        await update(env.store, 'work_item', child['id'], write_paths=['src'])
    elif damage == 'child_review_owner':
        await update(env.store, 'review', child['attempt_id'], work_item_id='another-child')
    elif damage == 'child_review_source':
        await update(env.store, 'review', child['attempt_id'], reviewed_commit=env.project['base_commit'])
    else:
        await update(env.store, 'attempt', child['attempt_id'], input_fingerprint='wrong-input')
    before = {kind: await env.store.list(kind) for kind in ('work_item', 'review', 'code_snapshot', 'budget_account')}
    assert await env.remediation.repair(parent['id']) is None
    assert not await env.store.list('review_repair')
    assert {kind: await env.store.list(kind) for kind in before} == before

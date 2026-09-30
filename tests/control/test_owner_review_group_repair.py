"""Owner fixes parent-only findings without mutating sealed review expansion history."""
from uuid import uuid4

import pytest
import pytest_asyncio
from test_owner_review_source_repair import PATHS, invoke, payload
from test_owner_review_source_repair import env as env
from test_parallel_remediation import CollectedFixtureRuntime, update
from test_review_baseline_recovery import stopped_task

from agentflow.common import DomainError
from agentflow.control.recovery import RunRecoveryService, _ReadState
from agentflow.control.review_producer import sealed_review_group
from agentflow.control.scheduler import Scheduler
from agentflow.domain.expansion import StageExpander


async def review_result(env, claim, findings):
    scheduler = Scheduler(env.workflow, env.store, CollectedFixtureRuntime(env, findings), None, env.settings)
    source, commit = await scheduler._source(claim['run'], claim['work_item'])
    _, task = await stopped_task(env, claim, source, commit, failed=False)
    await scheduler._execute_existing(task)
    return await env.store.read('work_item', claim['work_item']['id'])


@pytest_asyncio.fixture
async def group(env):
    for identity in ('implementation-attempt', 'producer-attempt'):
        await update(env.store, 'code_snapshot', identity, base_oid=env.project['base_commit'])
        context = await env.store.read('dispatch_context', identity)
        await update(env.store, 'dispatch_context', identity, task={**context['task'], 'source_commit': env.project['base_commit']})
    await update(env.store, 'plan', 'plan', actual_steps=['implementation', 'integration_test_implementation', 'code_review', 'unit_test_execution'])
    parent = await update(env.store, 'work_item', 'review', status='pending', quality_result='unknown', attempt_id=None)
    await update(env.store, 'review', 'review-attempt', stale=True)
    expanded = await StageExpander(env.store).expand('run', 'review', [
        {'key': name, 'goal': 'Review current source', 'write_paths': [], 'review_focus': 'current_code'}
        for name in ('source', 'tests')], str(uuid4()), parent['revision'])
    for _ in range(2):
        claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
        assert (await review_result(env, claim, []))['quality_result'] == 'passed'
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert claim['work_item']['id'] == 'review'
    failed = await review_result(env, claim, [{'severity': 'blocking', 'path': PATHS[0], 'description': 'Fix the source boundary.'}])
    assert failed['quality_result'] == 'failed'
    env.children = expanded['work_item_ids']
    env.failed_parent = failed
    return env


async def fix_owner_work(env, result, text='export const fixed = true;\n'):
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert claim['work_item']['id'] == result['repair_work_item_id']
    scheduler = Scheduler(env.workflow, env.store, CollectedFixtureRuntime(env), None, env.settings)
    source, commit = await scheduler._source(claim['run'], claim['work_item'])
    workspace, task = await stopped_task(env, claim, source, commit, failed=False)
    (workspace / PATHS[0]).write_text(text)
    await scheduler._execute_existing(task)
    assert (await env.store.read('work_item', result['repair_work_item_id']))['status'] == 'completed'
    return await env.store.read('code_snapshot', claim['attempt']['id'])


async def test_parent_only_finding_gets_owner_fix_then_fresh_children_parent_and_downstream(group):
    env = group
    expansion = await env.store.list('stage_expansion')
    history = {kind: await env.store.list(kind) for kind in ('attempt', 'budget_account', 'plan')}
    old_reviews = [(await env.store.read('work_item', identity))['attempt_id'] for identity in env.children]
    result = await invoke(env, await payload(env, write_paths=[PATHS[0]]), key='owner-group-fix')
    assert await env.store.list('stage_expansion') == expansion
    assert {kind: await env.store.list(kind) for kind in history} == history
    parent = await env.store.read('work_item', 'review')
    assert parent['dependencies'] == env.children
    assert parent['original_dependencies'] == [result['repair_work_item_id']]
    for identity in ['review', *env.children]:
        work = await env.store.read('work_item', identity)
        assert work['status'] == 'pending' and work['quality_result'] == 'unknown'
        assert work['generation'] == 2
        if identity != 'review':
            assert work['dependencies'] == [result['repair_work_item_id']]
    assert all(row['stale'] for row in [await env.store.read('review', identity) for identity in old_reviews])
    fixed = await fix_owner_work(env, result)
    state = await RunRecoveryService(env.store, env.workflow)._read('run')
    assert sealed_review_group(_ReadState(state), state['run'][0], await env.store.read('work_item', 'review')) is not None
    for _ in range(2):
        claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
        assert claim['work_item']['id'] in env.children
        assert (await review_result(env, claim, []))['quality_result'] == 'passed'
        assert (await env.store.read('review', claim['attempt']['id']))['reviewed_commit'] == fixed['commit_oid']
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert claim['work_item']['id'] == 'review'
    assert (await review_result(env, claim, []))['quality_result'] == 'passed'
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['work_item']['id'] == 'execution'
    assert await env.store.list('stage_expansion') == expansion


@pytest.mark.parametrize('damage', ['child_failed', 'child_source', 'extra_member', 'parent_source'])
async def test_owner_group_fix_rejects_unpassed_or_mismatched_frozen_group(group, damage):
    env = group
    child = await env.store.read('work_item', env.children[0])
    if damage == 'child_failed':
        await update(env.store, 'work_item', child['id'], quality_result='failed')
    elif damage == 'child_source':
        await update(env.store, 'review', child['attempt_id'], reviewed_commit=env.project['base_commit'])
    elif damage == 'extra_member':
        await update(env.store, 'work_item', 'foreign-child', **{key: value for key, value in child.items() if key not in {'id', 'revision'}})
    else:
        await update(env.store, 'review', env.failed_parent['attempt_id'], reviewed_commit=env.project['base_commit'])
    before = await env.store.list('work_item')
    with pytest.raises(DomainError):
        await invoke(env, await payload(env, write_paths=[PATHS[0]]))
    assert await env.store.list('work_item') == before


@pytest.mark.parametrize('damage', ['missing_receipt', 'actor', 'member_binding', 'child_generation', 'expansion'])
async def test_rebound_review_group_requires_exact_owner_receipt(group, damage):
    env = group
    result = await invoke(env, await payload(env, write_paths=[PATHS[0]]))
    await fix_owner_work(env, result)
    parent = await env.store.read('work_item', 'review')
    if damage == 'missing_receipt':
        await update(env.store, 'work_item', 'review', payload={**parent['payload'], 'owner_review_producer_binding': 'missing'})
    elif damage == 'actor':
        await update(env.store, 'review_source_repair', result['id'], actor='model')
    elif damage == 'member_binding':
        binding = dict(result['review_group_binding'])
        binding['child_ids'] = env.children[:1]
        await update(env.store, 'review_source_repair', result['id'], review_group_binding=binding)
    elif damage == 'child_generation':
        await update(env.store, 'work_item', env.children[0], generation=1)
    else:
        row = (await env.store.list('stage_expansion'))[0]
        await update(env.store, 'stage_expansion', row['id'], input_fingerprint='tampered')
    state = await RunRecoveryService(env.store, env.workflow)._read('run')
    assert sealed_review_group(_ReadState(state), state['run'][0], await env.store.read('work_item', 'review')) is None


async def test_synchronized_group_rewire_without_owner_receipt_is_rejected(group):
    env = group
    await update(env.store, 'work_item', 'review', original_dependencies=['implementation'])
    for identity in env.children:
        await update(env.store, 'work_item', identity, dependencies=['implementation'])
    with pytest.raises(DomainError):
        await invoke(env, await payload(env, write_paths=[PATHS[0]]))


async def test_second_parent_owner_repair_chains_authority_without_changing_expansion(group):
    env = group
    expansion = await env.store.list('stage_expansion')
    first = await invoke(env, await payload(env, write_paths=[PATHS[0]]), key='group-first')
    first_source = await fix_owner_work(env, first)
    for _ in range(2):
        claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
        assert (await review_result(env, claim, []))['quality_result'] == 'passed'
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert (await review_result(env, claim, [{'severity': 'blocking', 'path': PATHS[0], 'description': 'A remaining boundary defect.'}]))['quality_result'] == 'failed'
    second = await invoke(env, await payload(env, write_paths=[PATHS[0]]), key='group-second')
    assert second['review_group_binding']['previous_binding_receipt_id'] == first['id']
    assert second['source_commit'] == first_source['commit_oid']
    await fix_owner_work(env, second, 'export const fixed = 2;\n')
    state = await RunRecoveryService(env.store, env.workflow)._read('run')
    rebound = sealed_review_group(_ReadState(state), state['run'][0], await env.store.read('work_item', 'review'))
    assert rebound is not None and rebound['producer']['id'] == second['repair_work_item_id']
    assert await env.store.list('stage_expansion') == expansion

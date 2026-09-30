"""Parallel review facets can repair their shared code without bypassing review gates."""
from pathlib import Path
from uuid import uuid4

import pytest
from test_parallel_remediation import CollectedFixtureRuntime, complete_repair, finding, update
from test_parallel_remediation import parallel_env as parallel_env
from test_remediation import env as env

from agentflow.control.scheduler import Scheduler
from agentflow.domain.expansion import StageExpander


async def collect_review(env, claim, findings):
    scheduler = Scheduler(env.workflow, env.store, CollectedFixtureRuntime(env, findings), None, env.settings)
    work, attempt = claim['work_item'], claim['attempt']
    source, commit = await scheduler._source(claim['run'], work)
    workspace = env.temporary / ('review-facet-' + attempt['id'])
    await env.repository.clone_snapshot(source, workspace, commit)
    await scheduler._execute_existing({'attempt_id': attempt['id'], 'work_item_id': work['id'], 'run_id': 'run',
        'step': 'code_review', 'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint'],
        'workspace': str(workspace), 'source_commit': commit, 'allowed_write_paths': []})
    return await env.store.read('work_item', work['id'])


async def expanded_reviews(env, *, producer_id='implementation', findings=None):
    await update(env.store, 'plan', 'plan', actual_steps=['implementation', 'code_review', 'unit_test_plan'],
        work_specs=[{'key': 'code_review', 'step': 'code_review', 'role': 'review'}])
    parent = await update(env.store, 'work_item', 'review-work', status='pending', quality_result='unknown',
                          attempt_id=None, output_fingerprint=None)
    await update(env.store, 'review', 'review-attempt-1', stale=True)
    result = await StageExpander(env.store).expand('run', 'review-work', [
        {'key': 'logic', 'goal': 'Review behavior and contracts', 'write_paths': [], 'review_focus': 'current_code'},
        {'key': 'security', 'goal': 'Review security boundaries', 'write_paths': [], 'review_focus': 'current_code'}],
        str(uuid4()), parent['revision'])
    children = {row['expansion_key']: row for row in result['items']}
    assert all(row['dependencies'] == [producer_id] for row in children.values())
    claims = [await env.workflow.claim_next('run', 'fixture', str(uuid4())) for _ in range(2)]
    claims = {claim['work_item']['id']: claim for claim in claims}
    failed = await collect_review(env, claims[children['logic']['id']], findings or [finding('a')])
    # Even a valid finding must wait for the other review process to finish.
    assert await env.remediation.repair(failed['id']) is None
    good = await collect_review(env, claims[children['security']['id']], [])
    assert failed['quality_result'] == 'failed' and good['quality_result'] == 'passed'
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['attempt'] is None
    return failed, good


async def test_review_child_repairs_only_code_owner_then_rechecks_all_review_facets(parallel_env):
    env = parallel_env
    failed, good = await expanded_reviews(env)
    before_code = {identity: await env.store.read('work_item', identity) for identity in env.paths}
    budgets = await env.store.list('budget_account')
    result = await env.remediation.repair(failed['id'])
    assert result is not None, 'A failed review child otherwise prevents its parent from ever becoming runnable'
    assert result['repair_work_item_ids'] == ['module-a']
    assert set(result['affected_work_item_ids']) == {'module-a', 'implementation', failed['id'], good['id'], 'review-work', 'unit'}
    assert result['base_commit'] == env.aggregate['commit_oid']
    assert await env.store.list('budget_account') == budgets
    for identity in ('module-b', 'module-c', 'module-d'):
        assert await env.store.read('work_item', identity) == before_code[identity]
    for original in (failed, good):
        current = await env.store.read('work_item', original['id'])
        assert current['status'] == 'pending' and current['generation'] == original['generation'] + 1
        assert (await env.store.read('review', original['attempt_id']))['stale']
    assert (await env.store.read('work_item', 'review-work'))['kind'] == 'aggregation'
    aggregate = await complete_repair(env, ['a'], 1)
    for identity, path in env.paths.items():
        value = 1 if identity == 'module-a' else 0
        assert (Path(aggregate['repository_path']) / path).read_text() == f'export const {identity[-1]} = {value};\n'
    claims = [await env.workflow.claim_next('run', 'fixture', str(uuid4())) for _ in range(2)]
    assert {claim['work_item']['id'] for claim in claims} == {failed['id'], good['id']}
    for claim in claims:
        assert (await collect_review(env, claim, []))['quality_result'] == 'passed'
    parent = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert parent['work_item']['id'] == 'review-work'
    assert (await collect_review(env, parent, []))['quality_result'] == 'passed'
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['work_item']['id'] == 'unit'


@pytest.mark.parametrize('status', ['running', 'execution_unknown', 'waiting_approval'])
async def test_review_child_cannot_invalidate_active_unknown_or_human_gated_review_sibling(parallel_env, status):
    env = parallel_env
    failed, good = await expanded_reviews(env)
    await update(env.store, 'work_item', good['id'], status=status)
    before = {kind: await env.store.list(kind) for kind in ('work_item', 'review', 'code_snapshot', 'budget_account')}
    assert await env.remediation.repair(failed['id']) is None
    assert {kind: await env.store.list(kind) for kind in before} == before


@pytest.mark.parametrize('target,fields', [
    ('parent', {'project_id': 'another-project'}),
    ('parent', {'original_dependencies': ['module-b']}),
    ('parent', {'expanded_child_ids': []}),
    ('failed', {'write_paths': ['src']}),
    ('failed', {'parent_stage_id': 'missing-parent'}),
    ('good', {'run_id': 'another-run'}),
])
async def test_review_child_requires_current_parent_membership_dependencies_and_read_only_scope(parallel_env, target, fields):
    env = parallel_env
    failed, good = await expanded_reviews(env)
    identity = {'parent': 'review-work', 'failed': failed['id'], 'good': good['id']}[target]
    await update(env.store, 'work_item', identity, **fields)
    before = await env.store.list('work_item')
    assert await env.remediation.repair(failed['id']) is None
    assert await env.store.list('work_item') == before


@pytest.mark.parametrize('path,allowed', [('product.py', True), ('../outside.py', False)])
async def test_review_child_can_repair_a_single_coding_producer_only_with_valid_finding_scope(env, tmp_path, path, allowed):
    env.temporary = tmp_path
    await update(env.store, 'work_item', 'code', approval_required=False)
    await update(env.store, 'work_item', 'review-work', approval_required=False)
    prior = await env.store.read('code_snapshot', 'failed-snapshot')
    await update(env.store, 'code_snapshot', 'code-attempt',
                 **{key: value for key, value in prior.items() if key not in {'id', 'revision'}})
    await update(env.store, 'code_snapshot', 'failed-snapshot', stale=True)
    failed, good = await expanded_reviews(env, producer_id='code',
        findings=[{'path': path, 'severity': 'blocking', 'description': 'Correct the implementation'}])
    result = await env.remediation.repair(failed['id'])
    assert (result is not None) is allowed
    if allowed:
        assert set(result['affected_work_item_ids']) == {'code', failed['id'], good['id'], 'review-work', 'unit'}
        code = await env.store.read('work_item', 'code')
        checkpoint = await env.store.read('code_snapshot', code['payload']['repair_base_snapshot_id'])
        assert checkpoint['commit_oid'] == prior['commit_oid']
        assert code['approval_required'] is False
    else:
        assert (await env.store.read('work_item', 'code'))['generation'] == 1

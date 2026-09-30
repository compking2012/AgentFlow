"""Fail-closed parallel repair boundaries, using only isolated Git fixtures."""
from uuid import uuid4

import pytest
from test_parallel_remediation import finding, update
from test_parallel_remediation import parallel_env as parallel_env

from agentflow.models.budget import account_id


async def state(store):
    kinds = ('run', 'work_item', 'work_revision', 'attempt', 'artifact', 'approval',
             'code_snapshot', 'review', 'check', 'budget_account', 'model_invocation',
             'review_repair', 'candidate', 'delivery_intent')
    return {kind: await store.list(kind) for kind in kinds}


async def refuses_without_mutation(env):
    before = await state(env.store)
    assert await env.remediation.repair('review-work') is None
    assert await state(env.store) == before


async def remove_field(store, kind, identity, field):
    def remove(tx):
        row = tx.get(kind, identity)
        body = {key: value for key, value in row.items() if key != field}
        return tx.put(kind, identity, body, row['revision'])
    await store.command('fixture.remove-field', str(uuid4()), {}, remove)


@pytest.mark.parametrize('path', [
    '/src/a.mjs', '../src/a.mjs', 'src/../a.mjs', r'src\a.mjs',
    'src/*.mjs', 'src/a.mjs:12', 'src/.git/config', 'src/.GIT/config',
    'src/a.mjs ', 'src/a.mjs\n', '.', '', None, 'src/unknown.mjs',
])
async def test_unsafe_or_unowned_finding_rejects_the_entire_repair(parallel_env, path):
    env = parallel_env
    # A valid earlier finding must not allow a later invalid one to be dropped.
    await update(env.store, 'review', 'review-attempt-1',
                 blocking_findings=[finding('b'), {**finding('a'), 'path': path}])
    await refuses_without_mutation(env)


@pytest.mark.parametrize('bad_finding', [None, 'src/a.mjs', {'path': 'src/a.mjs'},
    {'path': 'src/a.mjs', 'severity': 'warning'}])
async def test_malformed_finding_rejects_the_entire_repair(parallel_env, bad_finding):
    env = parallel_env
    await update(env.store, 'review', 'review-attempt-1', blocking_findings=[finding('b'), bad_finding])
    await refuses_without_mutation(env)


@pytest.mark.parametrize('identity,fields', [
    ('implementation', {'expanded_child_ids': ['module-a', 'module-b', 'module-c']}),
    ('implementation', {'expanded_child_ids': ['module-a', 'module-a', 'module-c', 'module-d']}),
    ('implementation', {'original_dependencies': None}),
    ('implementation', {'original_write_paths': []}),
    ('implementation', {'attempt_id': 'unrelated-aggregate-attempt'}),
    ('module-a', {'dependencies': ['module-b']}),
    ('module-a', {'write_paths': ['docs/a.mjs']}),
    ('module-a', {'kind': 'stage'}),
    ('module-a', {'role': 'unit_test'}),
    ('module-a', {'archived': True}),
    ('module-a', {'attempt_id': 'missing-child-snapshot'}),
    ('module-b', {'write_paths': ['src']}),
])
async def test_changed_graph_or_ambiguous_ownership_is_refused(parallel_env, identity, fields):
    env = parallel_env
    await update(env.store, 'work_item', identity, **fields)
    await refuses_without_mutation(env)


@pytest.mark.parametrize('identity', ['aggregate-attempt-1', 'module-a-attempt-1'])
@pytest.mark.parametrize('field', ['repository_path', 'commit_oid', 'tree_oid', 'base_oid'])
async def test_missing_snapshot_metadata_is_refused_without_exception(parallel_env, identity, field):
    env = parallel_env
    await remove_field(env.store, 'code_snapshot', identity, field)
    await refuses_without_mutation(env)


@pytest.mark.parametrize('identity,fields', [
    ('aggregate-attempt-1', {'stale': True}),
    ('aggregate-attempt-1', {'parent_commit_oids': [None]}),
    ('module-a-attempt-1', {'generation': 0}),
    ('module-a-attempt-1', {'stale': True}),
    ('module-a-attempt-1', {'run_id': 'another-run'}),
    ('module-a-attempt-1', {'work_item_id': 'module-b'}),
    ('module-a-attempt-1', {'parent_commit_oids': 'not-a-list'}),
])
async def test_stale_or_misbound_snapshot_is_refused(parallel_env, identity, fields):
    env = parallel_env
    await update(env.store, 'code_snapshot', identity, **fields)
    await refuses_without_mutation(env)


@pytest.mark.parametrize('kind,identity,field,value', [
    ('review', 'review-attempt-1', 'stale', True),
    ('review', 'review-attempt-1', 'run_id', 'another-run'),
    ('review', 'review-attempt-1', 'work_item_id', 'another-reviewer'),
    ('review', 'review-attempt-1', 'work_item_id', None),
    ('attempt', 'review-attempt-1', 'run_id', 'another-run'),
    ('attempt', 'review-attempt-1', 'run_id', None),
    ('attempt', 'review-attempt-1', 'work_item_id', 'another-reviewer'),
    ('attempt', 'review-attempt-1', 'work_item_id', None),
    ('work_item', 'implementation', 'run_id', 'another-run'),
    ('work_item', 'implementation', 'project_id', 'another-project'),
    ('work_item', 'review-work', 'project_id', 'another-project'),
    ('work_item', 'review-work', 'step', 'unit_test_plan'),
])
async def test_review_and_attempt_must_belong_to_the_current_run_and_work(parallel_env, kind, identity, field, value):
    env = parallel_env
    await update(env.store, kind, identity, **{field: value})
    await refuses_without_mutation(env)


@pytest.mark.parametrize('owner_kind,owner', [('run', 'run'), ('iteration', 'iteration')])
@pytest.mark.parametrize('fields', [
    {'max_requests': 10, 'request_count': 10},
    {'max_requests': False},
    {'request_count': -1},
    {'restore_uncertain': True},
    {'uncertain_micros': 1},
])
async def test_both_budget_layers_must_be_available_and_certain(parallel_env, owner_kind, owner, fields):
    env = parallel_env
    await update(env.store, 'budget_account', account_id(owner_kind, owner), **fields)
    await refuses_without_mutation(env)


@pytest.mark.parametrize('owner_kind,owner', [('run', 'run'), ('iteration', 'iteration')])
async def test_exhausted_money_prevents_repair_even_with_unlimited_requests(parallel_env, owner_kind, owner):
    env = parallel_env
    account = await env.store.read('budget_account', account_id(owner_kind, owner))
    await update(env.store, 'budget_account', account['id'], max_requests=0,
                 settled_micros=account['limit_micros'] - 1, reserved_micros=1)
    await refuses_without_mutation(env)


async def test_zero_request_limits_remain_unlimited_without_resetting_spend(parallel_env):
    env = parallel_env
    for owner_kind, owner in [('run', 'run'), ('iteration', 'iteration')]:
        await update(env.store, 'budget_account', account_id(owner_kind, owner),
                     max_requests=0, request_count=5000, settled_micros=20)
        owner = await env.store.read(owner_kind, owner)
        await update(env.store, owner_kind, owner['id'], budget_limit={**owner['budget_limit'], 'max_model_requests': 0})
    before = await env.store.list('budget_account')
    assert await env.remediation.repair('review-work')
    assert await env.store.list('budget_account') == before


@pytest.mark.parametrize('status', ['running', 'waiting_execution', 'execution_unknown',
                                  'cancel_requested', 'waiting_approval'])
async def test_active_uncertain_or_human_gated_descendants_prevent_repair(parallel_env, status):
    env = parallel_env
    await update(env.store, 'work_item', 'unit', status=status)
    await refuses_without_mutation(env)


@pytest.mark.parametrize('invocation_state', ['reserved', 'dispatching', 'uncertain'])
async def test_outstanding_model_calls_prevent_repair(parallel_env, invocation_state):
    env = parallel_env
    await update(env.store, 'model_invocation', 'ongoing-call', run_id='run', state=invocation_state)
    await refuses_without_mutation(env)


@pytest.mark.parametrize('kind,identity,fields', [
    ('plan', 'plan', {'authorized_rework_steps': []}),
    ('run', 'run', {'execution_state': 'paused'}),
    ('run', 'run', {'delivery_ids': ['already-delivered']}),
    ('candidate', 'already-frozen', {'run_id': 'run'}),
    ('delivery_intent', 'already-publishing', {'run_id': 'run'}),
    ('work_item', 'review-work', {'status': 'waiting_approval'}),
    ('attempt', 'review-attempt-1', {'fencing_token': 0}),
])
async def test_existing_run_authorization_and_delivery_guards_still_apply(parallel_env, kind, identity, fields):
    env = parallel_env
    await update(env.store, kind, identity, **fields)
    await refuses_without_mutation(env)


async def test_archived_descendants_and_their_evidence_are_untouched(parallel_env):
    env = parallel_env
    identity = 'archived-review'
    await update(env.store, 'work_item', identity, **{**env.common, 'key': identity, 'step': 'code_review',
        'role': 'review', 'dependencies': ['implementation'], 'archived': True, 'status': 'superseded',
        'attempt_id': None, 'write_paths': []})
    kinds = ('artifact', 'approval', 'check', 'review', 'code_snapshot')
    for kind in kinds:
        await update(env.store, kind, identity, run_id='run', work_item_id=identity,
                     generation=1, stale=False)
    before = {kind: await env.store.read(kind, identity) for kind in ('work_item', *kinds)}
    result = await env.remediation.repair('review-work')
    assert result and identity not in result['affected_work_item_ids']
    assert {kind: await env.store.read(kind, identity) for kind in before} == before


async def test_repaired_child_requires_fresh_human_approval_and_preserves_sibling_approval(parallel_env):
    env = parallel_env
    await update(env.store, 'work_item', 'module-a', approval_required=True, approved_fingerprint='old-a')
    for identity in ('module-a', 'module-b'):
        await update(env.store, 'approval', identity, work_item_id=identity, run_id='run',
                     stale=False, decision='approve', fingerprint='old')
        await update(env.store, 'artifact', identity, work_item_id=identity, run_id='run', stale=False)
    sibling = {kind: await env.store.read(kind, 'module-b') for kind in ('work_item', 'approval', 'artifact')}
    assert await env.remediation.repair('review-work')
    repaired = await env.store.read('work_item', 'module-a')
    assert repaired['approval_required'] and repaired['approved_fingerprint'] is None
    assert (await env.store.read('approval', 'module-a'))['stale']
    assert (await env.store.read('artifact', 'module-a'))['stale']
    assert {kind: await env.store.read(kind, 'module-b') for kind in sibling} == sibling
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert claim['work_item']['id'] == 'module-a'
    blob = await env.artifacts.put_bytes(b'{"summary":"isolated approval regression"}')
    attempt = claim['attempt']
    await env.workflow.finish_attempt(attempt['id'], {'fencing_token': attempt['fencing_token'],
        'input_fingerprint': attempt['input_fingerprint'], 'execution_status': 'completed', 'quality_result': 'unknown'},
        str(uuid4()), verified_artifacts=[{'digest': blob['id'], 'name': 'result.json'}])
    assert (await env.store.read('work_item', 'module-a'))['status'] == 'waiting_approval'
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['attempt'] is None
    fresh = [row for row in await env.store.list('approval')
             if row['work_item_id'] == 'module-a' and not row['stale']]
    assert len(fresh) == 1 and fresh[0]['decision'] is None


async def test_missing_review_findings_fail_closed_before_budget_preflight(parallel_env):
    await remove_field(parallel_env.store, 'review', 'review-attempt-1', 'blocking_findings')
    await refuses_without_mutation(parallel_env)

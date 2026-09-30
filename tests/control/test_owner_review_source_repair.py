"""Owner scoped source repair against isolated Store/Git, without model calls."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from review_fixture_utils import complete_review_guard_context
from test_coding_steps import coding_env as coding_env
from test_coding_steps import execute, task_for
from test_parallel_remediation import CollectedFixtureRuntime, update
from test_parallel_remediation import parallel_env as parallel_env
from test_review_baseline_recovery import stopped_task

from agentflow.common import DomainError, canonical_digest
from agentflow.control.api import create_app
from agentflow.control.scheduler import Scheduler
from agentflow.control.service import WorkflowService
from agentflow.models.budget import BudgetLedger, account_id
from agentflow.repository import RepositoryAdapter
from agentflow.settings import Settings
from agentflow.storage import LocalArtifactStore, Store

PATHS = ['public/shell/layers.mjs', 'public/shell/dock.mjs', 'tests/web.spec.mjs', 'tests/unit.test.mjs']


@pytest_asyncio.fixture
async def env(tmp_path):
    settings = Settings(data_dir=tmp_path / 'controller')
    store = Store(settings.data_dir)
    await store.start()
    try:
        artifacts = LocalArtifactStore(settings.data_dir / 'artifacts')
        workflow = WorkflowService(store, artifacts, settings)
        repository = RepositoryAdapter()
        project = await workflow.create_project({'name': 'Owner source repair fixture', 'local_path': str(tmp_path / 'project'),
            'import_mode': 'initialize_managed', 'dirty_worktree_policy': 'require_clean'}, 'project')
        env = SimpleNamespace(store=store, settings=settings, artifacts=artifacts, workflow=workflow,
                              repository=repository, project=project, temporary=tmp_path)
        fingerprint = canonical_digest({'fixture': 'scoped repair'})
        source = tmp_path / 'source'
        await repository.clone_snapshot(Path(project['local_path']), source, project['base_commit'])
        for path in [*PATHS, 'public/shell/boot.mjs', 'tooling/build.mjs', 'tests/playwright.config.mjs', 'config/runtime.mjs']:
            target = source / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('export const value = 0;\n')
        frozen = await repository.freeze_workspace(source, project['base_commit'], 'complete source and tests')
        common = {'run_id': 'run', 'project_id': project['id'], 'generation': 1, 'fencing_token': 1,
            'input_fingerprint': fingerprint, 'policy_fingerprint': fingerprint, 'required': True,
            'status': 'completed', 'approval_required': False, 'artifact_ids': [], 'payload': {}, 'quality_result': 'unknown'}
        specs = [dict(key='implementation', step='implementation', role='development', dependencies=[]),
                 dict(key='producer', step='integration_test_implementation', role='integration_test', dependencies=['implementation']),
                 dict(key='review', step='code_review', role='review', dependencies=['producer']),
                 dict(key='execution', step='unit_test_execution', role='unit_test', dependencies=['review'])]
        def seed(tx):
            tx.put('plan', 'plan', {'project_id': project['id'], 'iteration_id': 'iteration', 'state': 'started',
                'started_run_id': 'run', 'work_specs': specs, 'authorized_rework_steps': ['implementation', 'integration_test_implementation'],
                'approval_steps': [], 'reused_inputs': []})
            tx.put('run', 'run', {'project_id': project['id'], 'plan_id': 'plan', 'iteration_id': 'iteration',
                'execution_state': 'running', 'quality_result': 'failed', 'input_fingerprint': fingerprint,
                'base_commit': project['base_commit'], 'base_ref': project['base_ref'], 'delivery_ids': [],
                'runtime_bindings': {'coding_model_profile_id': 'frozen-model'},
                'blocking_reasons': ['review failed'], 'budget_limit': {'currency': 'USD', 'limit_micros': 1000, 'max_model_requests': 0}})
            tx.put('iteration', 'iteration', {'project_id': project['id'],
                'budget_limit': {'currency': 'USD', 'limit_micros': 2000, 'max_model_requests': 0}})
            for spec in specs:
                identity = spec['key']
                pending = identity == 'execution'
                quality = 'failed' if identity == 'review' else 'unknown'
                tx.put('work_item', identity, {**common, **spec, 'quality_result': quality,
                    'status': 'pending' if pending else 'completed', 'attempt_id': None if pending else identity + '-attempt',
                    'write_paths': ['public'] if identity == 'implementation' else ['tests'] if identity == 'producer' else []})
                if pending:
                    continue
                tx.put('attempt', identity + '-attempt', {**common, 'work_item_id': identity, 'iteration_id': 'iteration',
                    'quality_result': quality})
                if identity != 'review':
                    tx.put('code_snapshot', identity + '-attempt', {**frozen, 'run_id': 'run', 'work_item_id': identity,
                        'generation': 1, 'repository_path': str(source), 'stale': False})
            tx.put('review', 'review-attempt', {'run_id': 'run', 'work_item_id': 'review', 'generation': 1,
                'quality_result': 'failed', 'reviewed_commit': frozen['commit_oid'],
                'blocking_findings': [{'path': path, 'severity': 'blocking', 'description': 'Correct behavior'} for path in PATHS[:3]]})
            return {}
        await store.command('fixture', 'seed', {}, seed)
        await BudgetLedger(store).setup_accounts('run', 'iteration', 1000, 2000, run_max_requests=0, iteration_max_requests=0)
        for identity in ('implementation', 'producer', 'review'):
            work = await store.read('work_item', identity)
            claim = {'work_item': work, 'attempt': await store.read('attempt', work['attempt_id'])}
            await stopped_task(env, claim, source, frozen['commit_oid'], failed=False)
            if identity != 'review':
                context = await store.read('dispatch_context', work['attempt_id'])
                await update(store, 'code_snapshot', work['attempt_id'], repository_path=context['task']['workspace'], base_oid=frozen['commit_oid'])
        env.source = Path((await store.read('code_snapshot', 'producer-attempt'))['repository_path'])
        env.commit = frozen['commit_oid']
        yield env
    finally:
        await store.close()


async def payload(env, **changes):
    return {'expected_revision': (await env.store.read('run', 'run'))['revision'], 'review_work_item_id': 'review',
            'write_paths': PATHS, 'reason': 'Fix blocking source issues and correct unit z-index expectation without weakening assertions.', **changes}


async def invoke(env, request=None, key='owner-command'):
    from agentflow.control.review_source_repair import OwnerReviewSourceRepair
    return await OwnerReviewSourceRepair(env.store, env.workflow).schedule('run', request or await payload(env), key)


async def test_owner_command_is_registered_and_preserves_graph_history_ledgers(env):
    app = create_app(env.settings, store=env.store, artifacts=env.artifacts)
    route = '/api/v1/runs/run/review_repairs'
    request = await payload(env)
    protected = {kind: await env.store.list(kind) for kind in ('attempt', 'budget_account', 'coding_work_budget', 'model_invocation', 'plan')}
    old_producer = await env.store.read('work_item', 'producer')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=env.settings.origin) as client:
        headers = {'Origin': env.settings.origin, 'Idempotency-Key': 'api-repair'}
        assert (await client.post(route, json=request, headers=headers)).status_code == 401
        token = app.state.tokens.issue('agentflow_owner', {'owner:*'}, 'owner', 60)
        headers['Authorization'] = 'Bearer ' + token
        response = await client.post(route, json=request, headers=headers)
        assert response.status_code == 200, response.text
        result = response.json()
        replay = await client.post(route, json=request, headers=headers)
        assert replay.json() == result
    repair = await env.store.read('work_item', result['repair_work_item_id'])
    assert repair['dependencies'] == ['producer'] and repair['write_paths'] == sorted(PATHS)
    assert repair['step'] == 'implementation' and repair['role'] == 'development'
    assert repair['status'] == 'pending' and repair['generation'] == 1
    assert not repair['payload'].get('product_frozen_repair')
    assert (await env.store.read('work_item', 'review'))['dependencies'] == [repair['id']]
    assert (await env.store.read('work_item', 'review'))['quality_result'] == 'unknown'
    assert (await env.store.read('work_item', 'execution'))['generation'] == 2
    assert await env.store.read('work_item', 'producer') == old_producer
    assert (await env.store.read('review', 'review-attempt'))['stale'] is True
    assert {kind: await env.store.list(kind) for kind in protected} == protected
    assert result['actor'] == 'owner' and result['source_commit'] == env.commit


@pytest.mark.parametrize('paths', [PATHS[:2], ['.'], ['public'], ['public/../tests/web.spec.mjs'],
    ['public/shell/no.mjs'], ['tooling/build.mjs'], ['tests/playwright.config.mjs'], ['config/runtime.mjs'],
    [*PATHS, 'package.json'], [*PATHS, '/tmp/escape.mjs'], [*PATHS, 'tests/web.spec.mjs']])
async def test_repair_rejects_missing_findings_and_unsafe_frozen_or_nonexistent_paths(env, paths):
    before = await env.store.list('work_item')
    with pytest.raises(DomainError):
        await invoke(env, await payload(env, write_paths=paths))
    assert await env.store.list('work_item') == before
    assert not await env.store.list('review_source_repair')


@pytest.mark.parametrize('kind,identity,fields', [
    ('attempt', 'review-attempt', {'fencing_token': 99}),
    ('review', 'review-attempt', {'reviewed_commit': '0' * 40}),
    ('review', 'review-attempt', {'stale': True}),
    ('code_snapshot', 'producer-attempt', {'generation': 99}),
    ('work_item', 'producer', {'status': 'running'}),
    ('work_item', 'execution', {'status': 'waiting_approval'}),
    ('attempt', 'orphan', {'run_id': 'run', 'status': 'execution_unknown'}),
    ('model_invocation', 'unknown', {'run_id': 'run', 'state': 'uncertain'}),
    ('model_invocation', 'active', {'run_id': 'run', 'state': 'sending'}),
    ('model_attempt_budget', 'producer-attempt', {'run_id': 'run', 'uncertain_invocations': 1}),
    ('budget_account', account_id('run', 'run'), {'uncertain_micros': 1}),
    ('budget_account', account_id('run', 'run'), {'settled_micros': 1000}),
    ('run', 'run', {'execution_state': 'cancelled'}),
    ('run', 'run', {'execution_state': 'publishing'}),
    ('run', 'run', {'delivery_ids': ['delivered']}),
    ('run', 'run', {'restore_uncertain': True}),
    ('candidate', 'candidate', {'run_id': 'run'}),
    ('approval', 'approval', {'run_id': 'run', 'work_item_id': 'review', 'decision': None, 'stale': False}),
    ('work_item', 'implementation', {'quality_result': 'failed'}),
    ('coding_work_budget', 'uncertain-old-budget', {'run_id': 'run', 'work_item_id': 'producer', 'uncertain': True}),
])
async def test_repair_fails_closed_for_identity_activity_budget_and_approval(env, kind, identity, fields):
    await update(env.store, kind, identity, **fields)
    before = {k: await env.store.list(k) for k in ('work_item', 'review', 'budget_account', 'coding_work_budget', 'approval')}
    with pytest.raises(DomainError):
        await invoke(env)
    assert {k: await env.store.list(k) for k in before} == before


async def test_revision_and_original_plan_authorization_are_required(env):
    with pytest.raises(DomainError, match='changed'):
        await invoke(env, await payload(env, expected_revision=100))
    plan = await env.store.read('plan', 'plan')
    await update(env.store, 'plan', 'plan', work_specs=[spec for spec in plan['work_specs'] if spec['key'] != 'implementation'])
    with pytest.raises(DomainError):
        await invoke(env)


async def test_paths_cannot_be_granted_by_finding_prose_or_unrelated_coding_work(env):
    await update(env.store, 'work_item', 'implementation', write_paths=['public/shell/boot.mjs'])
    await update(env.store, 'dispatch_context', 'implementation-attempt', task={
        **(await env.store.read('dispatch_context', 'implementation-attempt'))['task'],
        'allowed_write_paths': ['public/shell/boot.mjs']})
    with pytest.raises(DomainError) as caught:
        await invoke(env)
    assert caught.value.code == 'review_repair_not_authorized'


async def test_symlink_replacement_and_git_dirty_source_are_rejected(env):
    path = env.source / PATHS[0]
    path.unlink()
    path.symlink_to(env.source / PATHS[1])
    with pytest.raises(DomainError):
        await invoke(env)


async def test_same_key_concurrent_replay_and_paused_never_starts(env):
    await update(env.store, 'run', 'run', execution_state='paused')
    request = await payload(env)
    results = await asyncio.gather(*(invoke(env, request) for _ in range(3)))
    assert results[0] == results[1] == results[2]
    assert results[0]['run']['execution_state'] == 'paused'
    assert len(await env.store.list('review_source_repair')) == 1
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['attempt'] is None
    with pytest.raises(DomainError) as caught:
        await invoke(env, {**request, 'reason': 'different content'})
    assert caught.value.code == 'idempotency_conflict'


async def test_real_claim_gets_complete_source_and_scoped_repair_then_rereview(env):
    result = await invoke(env)
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert claim['work_item']['id'] == result['repair_work_item_id']
    scheduler = Scheduler(env.workflow, env.store, CollectedFixtureRuntime(env), None, env.settings)
    source, commit = await scheduler._source(claim['run'], claim['work_item'])
    assert commit == env.commit
    workspace, task = await stopped_task(env, claim, source, commit, failed=False)
    assert task['allowed_write_paths'] == sorted(PATHS)
    assert all((workspace / path).is_file() for path in [*PATHS, 'public/shell/boot.mjs'])
    for path in PATHS:
        (workspace / path).write_text('export const value = 1;\n')
    await scheduler._execute_existing(task)
    work = await env.store.read('work_item', claim['work_item']['id'])
    assert work['status'] == 'completed', work
    review_claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert review_claim['work_item']['id'] == 'review'
    source, commit = await scheduler._source(review_claim['run'], review_claim['work_item'])
    assert commit != env.commit
    _, task = await stopped_task(env, review_claim, source, commit, failed=False)
    await scheduler._execute_existing(task)
    assert (await env.store.read('review', review_claim['attempt']['id']))['quality_result'] == 'passed'
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['work_item']['id'] == 'execution'


async def test_real_collection_rejects_out_of_scope_edit(env):
    result = await invoke(env)
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    scheduler = Scheduler(env.workflow, env.store, CollectedFixtureRuntime(env), None, env.settings)
    source, commit = await scheduler._source(claim['run'], claim['work_item'])
    workspace, task = await stopped_task(env, claim, source, commit, failed=False)
    (workspace / 'public/shell/boot.mjs').write_text('export const unauthorized = true;\n')
    await scheduler._execute_existing(task)
    assert (await env.store.read('work_item', result['repair_work_item_id']))['status'] != 'completed'
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['attempt'] is None


async def test_request_cap_exhaustion_and_unsettled_coding_usage_do_not_get_new_work(env):
    run = await env.store.read('run', 'run')
    await update(env.store, 'run', 'run', budget_limit={**run['budget_limit'], 'max_model_requests': 1})
    await update(env.store, 'budget_account', account_id('run', 'run'), max_requests=1, request_count=1)
    with pytest.raises(DomainError) as caught:
        await invoke(env)
    assert caught.value.code == 'recovery_budget_exhausted'


async def test_exhausted_old_work_budget_is_preserved_for_explicit_new_task(env):
    from agentflow.control.coding_steps import CodingSteps
    identity = CodingSteps.budget_id('run', 'producer')
    await update(env.store, 'coding_work_budget', identity, run_id='run', work_item_id='producer', uncertain=False,
        max_steps=1, step_count=1, max_active_seconds=10, active_seconds=10, max_tool_calls=3, observed_tool_calls=3)
    before = await env.store.list('coding_work_budget')
    result = await invoke(env)
    assert result['repair_work_item_id'] != 'producer'
    assert await env.store.list('coding_work_budget') == before


async def test_new_repair_still_requires_original_implementation_approval(env):
    await update(env.store, 'plan', 'plan', approval_steps=['implementation'])
    result = await invoke(env)
    assert (await env.store.read('work_item', result['repair_work_item_id']))['approval_required'] is True


async def test_non_owner_agent_token_cannot_call_repair_api(env):
    app = create_app(env.settings, store=env.store, artifacts=env.artifacts)
    token = app.state.tokens.issue('agentflow_runtime', {'owner:*'}, 'model', 60)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=env.settings.origin) as client:
        response = await client.post('/api/v1/runs/run/review_repairs', json=await payload(env), headers={
            'Origin': env.settings.origin, 'Idempotency-Key': 'agent-forbidden', 'Authorization': 'Bearer ' + token})
    assert response.status_code in {401, 403}
    assert not await env.store.list('review_source_repair')


async def test_git_and_state_are_rechecked_before_invalidation(env, monkeypatch):
    from agentflow.control.review_source_repair import OwnerReviewSourceRepair
    service = OwnerReviewSourceRepair(env.store, env.workflow)
    original = service._filesystem_proof
    calls = 0
    def race(state, proof):
        nonlocal calls
        calls += 1
        if calls == 2:
            (env.source / PATHS[0]).write_text('export const changed = 1;\n')
        return original(state, proof)
    monkeypatch.setattr(service, '_filesystem_proof', race)
    before = await env.store.list('work_item')
    with pytest.raises(DomainError):
        await service.schedule('run', await payload(env), 'raced')
    assert await env.store.list('work_item') == before


async def make_aggregate(env):
    producer = await env.store.read('work_item', 'producer')
    for label in ('web', 'api'):
        identity = 'producer-' + label
        await update(env.store, 'work_item', identity, **{key: value for key, value in producer.items() if key not in {'id', 'revision'}},
                     **{})
        child = await env.store.read('work_item', identity)
        await update(env.store, 'work_item', identity, key='producer:' + label, kind='stage_child', parent_stage_id='producer',
                     attempt_id=identity + '-attempt', write_paths=['tests/' + ('web.spec.mjs' if label == 'web' else 'unit.test.mjs')])
        child = await env.store.read('work_item', identity)
        await update(env.store, 'attempt', identity + '-attempt', run_id='run', iteration_id='iteration', work_item_id=identity,
            generation=1, fencing_token=1, input_fingerprint=child['input_fingerprint'], status='completed')
        snapshot = await env.store.read('code_snapshot', 'producer-attempt')
        await update(env.store, 'code_snapshot', identity + '-attempt', **{k: v for k, v in snapshot.items() if k not in {'id', 'revision', 'work_item_id'}}, work_item_id=identity)
        path, _ = await stopped_task(env, {'work_item': child, 'attempt': await env.store.read('attempt', child['attempt_id'])},
                                     env.source, env.commit, failed=False)
        await update(env.store, 'code_snapshot', identity + '-attempt', repository_path=str(path))
    fingerprint = canonical_digest({'expansion': 'fixture'})
    await update(env.store, 'stage_expansion', 'expansion', run_id='run', stage_work_item_id='producer',
        original_stage=producer, input_fingerprint=fingerprint, child_ids=['producer-web', 'producer-api'])
    await update(env.store, 'work_item', 'producer', kind='aggregation', expanded_child_ids=['producer-web', 'producer-api'],
        expansion_fingerprint=fingerprint, original_dependencies=['implementation'], original_write_paths=['tests'],
        write_paths=[], dependencies=['producer-web', 'producer-api'])


async def test_aggregate_producer_uses_sealed_children_without_fake_dispatch(env):
    await make_aggregate(env)
    result = await invoke(env)
    assert result['producer_work_item_id'] == 'producer'
    assert (await env.store.read('work_item', 'producer'))['status'] == 'completed'


async def test_business_support_helpers_are_source_but_frozen_fixtures_are_not(env):
    # A new fixture commit supplies real helper blobs; existing dispatch/snapshots
    # are rebound only by this test's seeding code.
    helper = env.source / 'tests/support/web-helpers.mjs'
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text('export const helper = true;\n')
    frozen = await env.repository.freeze_workspace(env.source, env.commit, 'helper source')
    await update(env.store, 'code_snapshot', 'producer-attempt', commit_oid=frozen['commit_oid'], tree_oid=frozen['tree_oid'])
    await update(env.store, 'review', 'review-attempt', reviewed_commit=frozen['commit_oid'])
    context = await env.store.read('dispatch_context', 'review-attempt')
    await update(env.store, 'dispatch_context', 'review-attempt', task={**context['task'], 'source_commit': frozen['commit_oid']})
    result = await invoke(env, await payload(env, write_paths=[*PATHS, 'tests/support/web-helpers.mjs']))
    assert 'tests/support/web-helpers.mjs' in result['write_paths']


async def test_retired_ancestor_workspace_does_not_erase_original_authorization(env):
    import shutil
    snapshot = await env.store.read('code_snapshot', 'implementation-attempt')
    shutil.rmtree(snapshot['repository_path'])
    assert (await invoke(env))['source_commit'] == env.commit


@pytest.mark.parametrize('path', ['tests/conftest.py', 'settings.py', 'setup.py', 'tests/setup.mjs', 'jest.setup.js',
                                  'tests/support/fixtures.mjs', 'tests/support/global-setup.mjs'])
async def test_frozen_support_and_configuration_files_are_not_source_grants(env, path):
    with pytest.raises(DomainError) as caught:
        await invoke(env, await payload(env, write_paths=[*PATHS, path]))
    assert caught.value.code == 'invalid_review_repair_scope'


async def test_second_owner_repair_preserves_the_first_repair_and_requires_new_review(env):
    first = await invoke(env)
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    scheduler = Scheduler(env.workflow, env.store, CollectedFixtureRuntime(env), None, env.settings)
    source, commit = await scheduler._source(claim['run'], claim['work_item'])
    workspace, task = await stopped_task(env, claim, source, commit, failed=False)
    (workspace / PATHS[0]).write_text('export const repaired = 1;\n')
    await scheduler._execute_existing(task)
    original = await env.store.read('work_item', first['repair_work_item_id'])
    assert original['status'] == 'completed'
    review_claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    source, commit = await scheduler._source(review_claim['run'], review_claim['work_item'])
    _, task = await stopped_task(env, review_claim, source, commit, failed=False)
    scheduler.runtime = CollectedFixtureRuntime(env, [{'path': PATHS[0], 'severity': 'blocking', 'description': 'Defect remains'}])
    await scheduler._execute_existing(task)
    assert (await env.store.read('work_item', 'review'))['quality_result'] == 'failed'
    second = await invoke(env, await payload(env), key='second-owner-command')
    assert second['producer_work_item_id'] == first['repair_work_item_id']
    assert await env.store.read('work_item', first['repair_work_item_id']) == original
    assert (await env.store.read('work_item', 'review'))['quality_result'] == 'unknown'


@pytest.mark.parametrize('tamper', ['extra_member', 'foreign_frozen_stage', 'foreign_receipt'])
async def test_aggregate_membership_and_frozen_identity_must_match(env, tamper):
    await make_aggregate(env)
    if tamper == 'extra_member':
        child = await env.store.read('work_item', 'producer-web')
        await update(env.store, 'work_item', 'hidden-child', **{k: v for k, v in child.items() if k not in {'id', 'revision'}})
        await update(env.store, 'work_item', 'hidden-child', key='producer:hidden', status='pending', attempt_id=None)
    else:
        expansion = await env.store.read('stage_expansion', 'expansion')
        if tamper == 'foreign_receipt':
            await update(env.store, 'stage_expansion', 'expansion', run_id='foreign-run')
        else:
            await update(env.store, 'stage_expansion', 'expansion', original_stage={**expansion['original_stage'],
                'project_id': 'foreign-project', 'key': 'foreign-stage', 'role': 'foreign-role'})
    with pytest.raises(DomainError):
        await invoke(env)


async def test_owner_repair_real_assembly_has_no_dispatch_and_runs(parallel_env):
    from agentflow.control.review_source_repair import OwnerReviewSourceRepair
    env = parallel_env
    await update(env.store, 'plan', 'plan', project_id=env.project['id'], iteration_id='iteration',
                 state='started', started_run_id='run', approval_steps=[],
                 work_specs=[{'key':'implementation','step':'implementation','role':'development','dependencies':[]}])
    await update(env.store, 'run', 'run', runtime_bindings={'coding_model_profile_id':'frozen-model'})
    producer = await env.store.read('work_item', 'implementation')
    frozen = {**producer, 'kind':'stage', 'dependencies':[], 'write_paths':['src']}
    fingerprint = canonical_digest({'expansion':'real-assembly-fixture'})
    await update(env.store, 'stage_expansion', 'expansion', run_id='run', stage_work_item_id='implementation',
                 original_stage=frozen, input_fingerprint=fingerprint, child_ids=list(env.contributions))
    await update(env.store, 'work_item', 'implementation', expansion_fingerprint=fingerprint)
    for work_id, snapshot in env.contributions.items():
        work = await env.store.read('work_item', work_id)
        claim = {'work_item':work, 'attempt':await env.store.read('attempt',work['attempt_id'])}
        _, task = await stopped_task(env, claim, snapshot['repository_path'], snapshot['base_oid'], failed=False)
        await update(env.store, 'dispatch_context', work['attempt_id'], task={**task, 'workspace':snapshot['repository_path']})
    work = await env.store.read('work_item', 'review-work')
    await update(env.store, 'attempt', work['attempt_id'], quality_result='failed')
    claim = {'work_item':work, 'attempt':await env.store.read('attempt',work['attempt_id'])}
    await stopped_task(env, claim, env.aggregate['repository_path'], env.aggregate['commit_oid'], failed=False)
    assert await env.store.read('dispatch_context', producer['attempt_id']) is None
    assert await env.store.read('supervised_attempt', producer['attempt_id']) is None
    run = await env.store.read('run','run')
    receipt = await OwnerReviewSourceRepair(env.store,env.workflow).schedule('run',
        {'expected_revision':run['revision'], 'review_work_item_id':'review-work', 'write_paths':['src/a.mjs'],
         'reason':'Fix the sole blocking module defect'}, 'real-assembly-owner-repair')
    claim = await env.workflow.claim_next('run','fixture',str(uuid4()))
    assert claim['work_item']['id'] == receipt['repair_work_item_id']
    scheduler = Scheduler(env.workflow,env.store,CollectedFixtureRuntime(env),None,env.settings)
    source, commit = await scheduler._source(claim['run'],claim['work_item'])
    assert commit == env.aggregate['commit_oid']
    workspace, task = await stopped_task(env,claim,source,commit,failed=False)
    assert all((workspace / path).is_file() for path in env.paths.values())
    (workspace / 'src/a.mjs').write_text('export const a = 5;\n')
    await scheduler._execute_existing(task)
    assert (await env.store.read('work_item',receipt['repair_work_item_id']))['status'] == 'completed'
    assert (await env.workflow.claim_next('run','fixture',str(uuid4())))['work_item']['id'] == 'review-work'


async def test_real_two_step_producer_can_be_owner_repaired(coding_env):
    from agentflow.control.review_source_repair import OwnerReviewSourceRepair
    env = coding_env
    await complete_review_guard_context(env.store)
    await update(env.store,'plan','plan',project_id=env.project['id'],iteration_id='iteration',state='started',
                 started_run_id='run',approval_steps=[],
                 work_specs=[{'key':'implementation','step':'implementation','role':'development','dependencies':[]}])
    await update(env.store,'run','run',runtime_bindings={'coding_model_profile_id':'frozen-model'})
    for work_id in ['code','review']:
        await update(env.store,'work_item',work_id,approval_required=False)
    review = await env.store.read('work_item','review')
    downstream = {k:v for k,v in review.items() if k not in {'id','revision'}}
    downstream.update(key='unit_execution',step='unit_test_execution',role='unit_test',dependencies=['review'])
    await update(env.store,'work_item','downstream',**downstream)
    for index in range(2):
        task = await task_for(env)
        work = await env.store.read('work_item','code')
        claim = {'work_item':work,'attempt':await env.store.read('attempt',work['attempt_id'])}
        await stopped_task(env,claim,Path(task['workspace']),task['source_commit'],failed=False)
        await update(env.store,'dispatch_context',task['attempt_id'],task=task)
        result = {'summary':'first progress' if index == 0 else 'complete work',
                  'status':'continue' if index == 0 else 'complete', 'next_action':'finish implementation' if index == 0 else ''}
        work = await execute(env,task,result,f'VALUE = {index + 1}\n')
    assert work['status'] == 'completed' and work['generation'] == 2
    snapshot = await env.store.read('code_snapshot',work['attempt_id'])
    assert snapshot['base_oid'] != task['source_commit']
    review_claim = await env.workflow.claim_next('run','fixture',str(uuid4()))
    source,commit = await env.scheduler._source(review_claim['run'],review_claim['work_item'])
    _,review_task = await stopped_task(env,review_claim,source,commit,failed=False)
    env.scheduler.runtime = CollectedFixtureRuntime(env,[{'path':'feature.py','severity':'blocking','description':'Fix remaining defect'}])
    await env.scheduler._execute_existing(review_task)
    assert (await env.store.read('work_item','review'))['quality_result'] == 'failed'
    run = await env.store.read('run','run')
    receipt = await OwnerReviewSourceRepair(env.store,env.workflow).schedule('run',
        {'expected_revision':run['revision'],'review_work_item_id':'review','write_paths':['feature.py'],
         'reason':'Repair current failed review after two coding steps'},'multistep-repair')
    assert receipt['source_commit'] == snapshot['commit_oid']


async def test_owner_api_wakes_scheduler_only_after_success_and_keeps_paused_gate(env):
    await update(env.store, 'run', 'run', execution_state='paused')
    scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    app = create_app(env.settings, store=env.store, artifacts=env.artifacts, scheduler=scheduler)
    token = app.state.tokens.issue('agentflow_owner', {'owner:*'}, 'owner', 60)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=env.settings.origin,
            headers={'Authorization': 'Bearer ' + token, 'Origin': env.settings.origin}) as client:
        invalid = await client.post('/api/v1/runs/run/review_repairs', json=await payload(env, write_paths=['.']),
                                    headers={'Idempotency-Key': 'reject-no-wake'})
        assert invalid.status_code == 409
        assert not scheduler._wake.is_set()
        response = await client.post('/api/v1/runs/run/review_repairs', json=await payload(env),
                                     headers={'Idempotency-Key': 'schedule-wake'})
        assert response.status_code == 200, response.text
        assert scheduler._wake.is_set()
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['attempt'] is None

"""Review source identity using temporary Git/Store data, without model execution."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr

from agentflow.adapters.openhands.tools import ToolBroker
from agentflow.common import DomainError, canonical_digest
from agentflow.control.project_code import ProjectCodeService
from agentflow.control.scheduler import Scheduler
from agentflow.control.service import WorkflowService
from agentflow.repository import RepositoryAdapter
from agentflow.runtime.contracts import TaskEnvelope
from agentflow.runtime.workspace import WorkspaceManager
from agentflow.settings import Settings
from agentflow.storage import LocalArtifactStore, Store


async def update(store, record_kind, identity, **fields):
    def write(tx):
        current = tx.get(record_kind, identity)
        return tx.put(record_kind, identity, {**(current or {}), **fields}, current['revision'] if current else None)
    return await store.command('fixture.update', str(uuid4()), {}, write)


@pytest_asyncio.fixture
async def source_env(tmp_path, request):
    mode = getattr(request, 'param', 'valid')
    settings = Settings(data_dir=tmp_path / 'controller')
    store = Store(settings.data_dir)
    await store.start()
    try:
        project = tmp_path / 'owner-project'
        project.mkdir()
        repository = RepositoryAdapter()
        repository._run(project, ['init', '-b', 'main'])
        (project / 'feature.py').write_text('VALUE = 0\n')
        repository._run(project, ['add', 'feature.py'])
        repository._run(project, ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '-m', 'baseline'])
        base = repository._run(project, ['rev-parse', 'HEAD']).decode().strip()
        manager = WorkspaceManager(settings.data_dir, repository)
        source = await manager.create_clone(project, base, 'implementation-attempt',
            project_root=project, project_id='project')
        (source / 'feature.py').write_text('VALUE = 1\n')
        frozen = await repository.freeze_workspace(source, base, 'completed implementation')
        fingerprint = canonical_digest({'fixture': 'review source identity'})
        common = {'run_id': 'run', 'project_id': 'project', 'generation': 1, 'required': True,
            'quality_result': 'unknown', 'input_fingerprint': fingerprint, 'policy_fingerprint': fingerprint,
            'fencing_token': 0, 'approval_required': False, 'artifact_ids': []}
        producer = {**common, 'key': 'implementation', 'step': 'implementation', 'kind': 'stage',
            'role': 'development', 'status': 'completed', 'write_paths': ['.'], 'dependencies': [],
            'attempt_id': None if mode == 'missing_attempt' else 'implementation-attempt'}
        snapshot = {'run_id': 'other-run' if mode == 'wrong_run' else 'run',
            'work_item_id': 'other-producer' if mode == 'wrong_producer' else 'implementation',
            'generation': 0 if mode == 'old_generation' else 2 if mode == 'future_generation' else 1,
            'repository_path': None if mode == 'incomplete' else str(source),
            'commit_oid': frozen['commit_oid'], 'tree_oid': frozen['tree_oid'], 'base_oid': base,
            'stale': mode == 'stale'}
        def seed(tx):
            tx.put('project', 'project', {'local_path': str(project), 'base_commit': base, 'base_ref': 'refs/heads/main'})
            tx.put('plan', 'plan', {'project_id': 'project', 'reused_inputs': [], 'actual_steps': ['implementation', 'code_review']})
            tx.put('run', 'run', {'project_id': 'project', 'plan_id': 'plan', 'iteration_id': 'iteration',
                'base_commit': base, 'base_ref': 'refs/heads/main', 'goal': 'Review the completed source',
                'purpose': 'code_delivery', 'execution_state': 'running', 'quality_result': 'unknown',
                'input_fingerprint': fingerprint, 'delivery_ids': [], 'blocking_reasons': []})
            tx.put('work_item', 'implementation', producer)
            tx.put('work_item', 'review', {**common, 'key': 'code_review', 'step': 'code_review', 'kind': 'stage',
                'role': 'review', 'status': 'pending', 'write_paths': [], 'dependencies': ['implementation'], 'attempt_id': None})
            if mode != 'missing':
                tx.put('code_snapshot', 'older-attempt' if mode == 'wrong_attempt' else 'implementation-attempt', snapshot)
            return {}
        await store.command('fixture', 'seed', {}, seed)
        artifacts = LocalArtifactStore(settings.data_dir / 'artifacts')
        workflow = WorkflowService(store, artifacts, settings)
        scheduler = Scheduler(workflow, store, None, None, settings)
        yield SimpleNamespace(store=store, settings=settings, manager=manager, repository=repository,
            workflow=workflow, scheduler=scheduler, project=project, source=source, base=base,
            frozen=frozen, common=common, fingerprint=fingerprint, mode=mode)
    finally:
        await store.close()


@pytest.mark.parametrize('source_env', ['missing', 'stale', 'old_generation', 'future_generation',
    'wrong_attempt', 'wrong_run', 'wrong_producer', 'missing_attempt', 'incomplete'], indirect=True)
async def test_completed_producer_requires_its_exact_current_snapshot(source_env):
    env = source_env
    run, review = await env.store.read('run', 'run'), await env.store.read('work_item', 'review')
    with pytest.raises(DomainError) as error:
        await env.scheduler._source(run, review)
    assert error.value.code == 'source_snapshot_missing'
    assert error.value.details['work_item_id'] == 'implementation'
    assert error.value.details['generation'] == 1 and error.value.details['run_id'] == 'run'
    assert env.repository._run(env.project, ['rev-parse', 'HEAD']).decode().strip() == env.base


@pytest.mark.parametrize('source_env', ['missing'], indirect=True)
async def test_missing_snapshot_blocks_review_before_cloning_or_contacting_a_model(source_env, monkeypatch):
    env = source_env
    clone = AsyncMock(side_effect=AssertionError('Missing source must not create a review clone'))
    monkeypatch.setattr(env.manager, 'create_clone', clone)
    runtime = SimpleNamespace(workspaces=env.manager, execute_task=AsyncMock())
    models = SimpleNamespace(registry=SimpleNamespace(get=AsyncMock()), ledger=SimpleNamespace(setup_accounts=AsyncMock()))
    scheduler = Scheduler(env.workflow, env.store, runtime, models, env.settings)
    claim = await env.workflow.claim_next('run', 'fixture', 'claim-review')
    assert claim['work_item']['id'] == 'review'
    await scheduler._dispatch(claim)
    work = await env.store.read('work_item', 'review')
    assert work['status'] == 'blocked'
    clone.assert_not_awaited()
    models.registry.get.assert_not_awaited()
    models.ledger.setup_accounts.assert_not_awaited()
    runtime.execute_task.assert_not_awaited()
    assert not await env.store.list('model_invocation')
    assert not await env.store.list('dispatch_context')


async def test_old_attempt_snapshot_cannot_compete_with_the_current_completed_producer(source_env):
    env = source_env
    current = await env.store.read('code_snapshot', 'implementation-attempt')
    await update(env.store, 'code_snapshot', 'older-attempt',
        **{key: value for key, value in current.items() if key not in {'id', 'revision'}},)
    result = await env.scheduler._source(await env.store.read('run', 'run'), await env.store.read('work_item', 'review'))
    assert result == (env.source, env.frozen['commit_oid'])


async def test_review_without_coding_ancestors_keeps_its_original_project_baseline(source_env):
    env = source_env
    review = await update(env.store, 'work_item', 'review', dependencies=[])
    result = await env.scheduler._source(await env.store.read('run', 'run'), review)
    assert result == (env.project, env.base)


async def test_project_head_advancing_does_not_change_the_commit_or_files_reviewed(source_env):
    env = source_env
    visible = await ProjectCodeService(env.store).sync('run')
    assert visible['state'] == 'ready'
    assert env.repository._run(env.project, ['symbolic-ref', 'HEAD']).decode().strip() == visible['development_ref']
    (env.project / 'feature.py').write_text('VALUE = 2\n')
    env.repository._run(env.project, ['add', 'feature.py'])
    env.repository._run(env.project, ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '-m', 'later owner work'])
    head = env.repository._run(env.project, ['rev-parse', 'HEAD']).decode().strip()
    assert head != env.frozen['commit_oid']
    source, commit = await env.scheduler._source(await env.store.read('run', 'run'), await env.store.read('work_item', 'review'))
    assert (source, commit) == (env.source, env.frozen['commit_oid'])
    clone = await env.manager.create_clone(source, commit, 'review-attempt', project_root=env.project, project_id='project')
    assert env.repository._run(clone, ['rev-parse', 'HEAD']).decode().strip() == commit
    task = TaskEnvelope(attempt_id='review-attempt', operation_id='review-attempt', work_item_id='review',
        run_id='run', iteration_id='iteration', role='review', goal='Read the exact frozen source',
        input_fingerprint=env.fingerprint, fencing_token=1, workspace=clone,
        artifact_dir=env.settings.data_dir / 'fixture-review-artifacts', model_profile_id='fixture', model='fixture',
        proxy_base_url='http://127.0.0.1:1/v1', proxy_token=SecretStr('fixture-token'), output_schema={'type': 'object'})
    task.assert_paths()
    assert ToolBroker(task).read_code('feature.py')['text'] == 'VALUE = 1\n'
    assert (env.project / 'feature.py').read_text() == 'VALUE = 2\n'
    assert (await env.store.read('code_snapshot', 'implementation-attempt'))['repository_path'] == str(env.source)


@pytest.mark.parametrize('save_aggregate', [False, True])
async def test_review_requires_the_completed_aggregate_snapshot_not_only_child_contributions(source_env, save_aggregate):
    env = source_env
    await update(env.store, 'work_item', 'left', **env.common, key='left', step='implementation', kind='stage_child',
        role='development', status='completed', dependencies=[], write_paths=['feature.py'], attempt_id='implementation-attempt')
    await update(env.store, 'code_snapshot', 'implementation-attempt', work_item_id='left')
    right = await env.manager.create_clone(env.project, env.base, 'right-attempt', project_root=env.project, project_id='project')
    (right / 'another.py').write_text('ANOTHER = True\n')
    right_snapshot = await env.repository.freeze_workspace(right, env.base, 'right module')
    await update(env.store, 'work_item', 'right', **env.common, key='right', step='implementation', kind='stage_child',
        role='development', status='completed', dependencies=[], write_paths=['another.py'], attempt_id='right-attempt')
    await update(env.store, 'code_snapshot', 'right-attempt', run_id='run', work_item_id='right', generation=1,
        repository_path=str(right), commit_oid=right_snapshot['commit_oid'], tree_oid=right_snapshot['tree_oid'],
        base_oid=env.base, stale=False)
    aggregation = await update(env.store, 'work_item', 'implementation', kind='aggregation', status='running',
        dependencies=['left', 'right'], attempt_id='assembly-attempt')
    run = await env.store.read('run', 'run')
    source, commit = await env.scheduler._source(run, aggregation)
    assert commit not in {env.frozen['commit_oid'], right_snapshot['commit_oid']}
    assert (source / 'feature.py').read_text() == 'VALUE = 1\n' and (source / 'another.py').is_file()
    if save_aggregate:
        await update(env.store, 'code_snapshot', 'assembly-attempt',
            **env.scheduler._assemblies[('implementation', 1)], run_id='run', work_item_id='implementation', generation=1, stale=False)
    await update(env.store, 'work_item', 'implementation', status='completed')
    review = await env.store.read('work_item', 'review')
    if save_aggregate:
        assert await env.scheduler._source(run, review) == (source, commit)
    else:
        with pytest.raises(DomainError) as error:
            await env.scheduler._source(run, review)
        assert error.value.code == 'source_snapshot_missing'
        assert error.value.details['work_item_id'] == 'implementation'


@pytest.mark.parametrize('source_env', ['valid', 'missing'], indirect=True)
async def test_reused_code_from_a_prior_run_cannot_hide_a_missing_current_producer(source_env):
    env = source_env
    await update(env.store, 'run', 'prior-run', project_id='project')
    await update(env.store, 'artifact', 'reused-code', run_id='prior-run', work_item_id='prior-producer',
        generation=1, step='implementation', stale=False)
    await update(env.store, 'code_snapshot', 'prior-attempt', run_id='prior-run', work_item_id='prior-producer',
        generation=1, repository_path=str(env.source), commit_oid=env.frozen['commit_oid'],
        tree_oid=env.frozen['tree_oid'], base_oid=env.base, stale=False)
    await update(env.store, 'plan', 'plan', reused_inputs=['reused-code'])
    if env.mode == 'valid':
        await update(env.store, 'work_item', 'review', dependencies=[])
    run, review = await env.store.read('run', 'run'), await env.store.read('work_item', 'review')
    if env.mode == 'valid':
        assert await env.scheduler._source(run, review) == (env.source, env.frozen['commit_oid'])
    else:
        with pytest.raises(DomainError) as error:
            await env.scheduler._source(run, review)
        assert error.value.code == 'source_snapshot_missing'


@pytest.mark.parametrize('related', [True, False], ids=['recovery-chain', 'unmerged-branch'])
async def test_git_ancestry_resolves_incomplete_checkpoint_metadata_without_losing_branches(source_env, tmp_path, related):
    env = source_env
    run = await env.store.read('run', 'run')
    source = tmp_path / 'unit-code'
    base = env.frozen['commit_oid'] if related else env.base
    await env.repository.clone_snapshot(env.source if related else env.project, source, base)
    # An intermediate recovery checkpoint need not be a current work snapshot.
    (source / 'tests').mkdir()
    (source / 'tests/unit.py').write_text('def test_placeholder():\n    assert True\n')
    first = await env.repository.freeze_workspace(source, base, 'intermediate checkpoint')
    (source / 'tests/unit.py').write_text('def test_feature():\n    assert True\n')
    second = await env.repository.freeze_workspace(source, first['commit_oid'], 'unit implementation')
    await update(env.store, 'work_item', 'unit-code', **env.common, key='unit_test_implementation',
        step='unit_test_implementation', role='unit_test', dependencies=['implementation'],
        status='completed', write_paths=['tests'], attempt_id='unit-attempt')
    await update(env.store, 'code_snapshot', 'unit-attempt', run_id='run', work_item_id='unit-code',
        generation=1, repository_path=str(source), commit_oid=second['commit_oid'],
        base_oid=first['commit_oid'], tree_oid=second['tree_oid'], parent_commit_oids=[first['commit_oid']], stale=False)
    await update(env.store, 'work_item', 'review', dependencies=['unit-code'])
    review = await env.store.read('work_item', 'review')
    if related:
        assert await env.scheduler._source(run, review) == (source, second['commit_oid'])
        assert (source / 'feature.py').read_text() == 'VALUE = 1\n'
    else:
        with pytest.raises(DomainError, match='Parallel code branches') as error:
            await env.scheduler._source(run, review)
        assert error.value.code == 'assembly_required'
    assert not await env.store.list('model_invocation')


async def test_review_dispatch_freezes_producer_contract_separately_from_old_child_goal(source_env, monkeypatch):
    from agentflow.models.profiles import ModelProfile

    env = source_env
    budget = {'max_active_seconds': 30, 'max_tool_calls': 10, 'max_model_requests': 10,
              'limit_micros': 1000000, 'currency': 'USD', 'cost_mode': 'request_limited'}
    await update(env.store, 'run', 'run', budget_limit=budget,
                 runtime_bindings={'role_model_profile_id': 'fixture'})
    await update(env.store, 'iteration', 'iteration', budget_limit=budget)
    await update(env.store, 'work_item', 'review',
        payload={'goal': 'Block until all future unit and integration tests have been written'})
    await update(env.store, 'work_item', 'unit-code', **env.common, key='unit_test_implementation',
        step='unit_test_implementation', role='unit_test', status='pending', dependencies=['review'],
        write_paths=['tests'], attempt_id=None)
    profile = ModelProfile(model_profile_id='fixture', provider='openai_compatible', requested_model='fixture-model',
        accepted_api_model='fixture-model', acceptance_status='accepted', protocols=['chat_completions'],
        base_url='https://fixture.example.invalid/v1', credential_reference='fixture')
    runtime = SimpleNamespace(workspaces=env.manager)
    models = SimpleNamespace(registry=SimpleNamespace(get=AsyncMock(return_value=profile)),
        ledger=SimpleNamespace(setup_accounts=AsyncMock()))
    scheduler = Scheduler(env.workflow, env.store, runtime, models, env.settings)
    execute = AsyncMock()
    monkeypatch.setattr(scheduler, '_execute_existing', execute)
    monkeypatch.setattr(scheduler, '_maintain', AsyncMock())
    claim = await env.workflow.claim_next('run', 'fixture', 'claim-review-phase')
    await scheduler._dispatch(claim)
    assert execute.await_count == 1
    task = execute.await_args.args[0]
    contract = task.get('review_phase_contract', {})
    assert contract.get('source_commit') == env.frozen['commit_oid']
    assert contract.get('producer_stages') == [{'work_item_id': 'implementation', 'step': 'implementation',
                                              'generation': 1, 'write_paths': ['.']}]
    assert contract.get('required_test_phases') == []
    assert contract.get('deferred_test_phases') == ['unit']
    saved = await env.store.read('dispatch_context', claim['attempt']['id'])
    assert saved['task']['review_phase_contract'] == contract

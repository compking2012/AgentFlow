"""Parallel review repair with real temporary Git graphs and no model calls."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from review_fixture_utils import complete_review_guard_context

from agentflow.common import canonical_digest
from agentflow.control.remediation import ReviewRemediation
from agentflow.control.scheduler import Scheduler
from agentflow.control.service import WorkflowService
from agentflow.models.budget import BudgetLedger
from agentflow.repository import RepositoryAdapter
from agentflow.repository.assembly import AssemblyManager
from agentflow.settings import Settings
from agentflow.storage import LocalArtifactStore, Store


async def update(store, record_kind, identity, **fields):
    def write(tx):
        prior = tx.get(record_kind, identity)
        return tx.put(record_kind, identity, {**(prior or {}), **fields}, prior['revision'] if prior else None)
    return await store.command('fixture.update', str(uuid4()), {}, write)


def finding(letter):
    return {'path': f'src/{letter}.mjs', 'severity': 'blocking', 'description': f'Correct module {letter} behavior'}


@pytest_asyncio.fixture
async def parallel_env(tmp_path):
    settings = Settings(data_dir=tmp_path / 'controller', agent_concurrency=4)
    store = Store(settings.data_dir)
    await store.start()
    try:
        artifacts = LocalArtifactStore(settings.data_dir / 'artifacts')
        workflow = WorkflowService(store, artifacts, settings)
        project = await workflow.create_project({'name': 'Parallel repair fixture', 'local_path': str(tmp_path / 'project'),
            'import_mode': 'initialize_managed', 'dirty_worktree_policy': 'require_clean'}, 'project')
        repository = RepositoryAdapter()
        contributions, paths, original_text = {}, {}, {}
        for letter in 'abcd':
            identity = 'module-' + letter
            source = tmp_path / ('source-' + letter)
            await repository.clone_snapshot(Path(project['local_path']), source, project['base_commit'])
            paths[identity] = f'src/{letter}.mjs'
            original_text[identity] = f'export const {letter} = 0;\n'
            (source / 'src').mkdir()
            (source / paths[identity]).write_text(original_text[identity])
            frozen = await repository.freeze_workspace(source, project['base_commit'], 'module ' + letter)
            contributions[identity] = {'id': f'{identity}-attempt-1', 'run_id': 'run', 'work_item_id': identity,
                'generation': 1, 'repository_path': str(source), 'commit_oid': frozen['commit_oid'],
                'tree_oid': frozen['tree_oid'], 'base_oid': frozen['base_oid'], 'parent_commit_oids': [], 'stale': False}
        assembly = await AssemblyManager(settings.data_dir, repository).assemble(list(contributions.values()),
            Path(contributions['module-a']['repository_path']), project['base_commit'], 'initial-aggregate')
        aggregate = {**assembly, 'id': 'aggregate-attempt-1', 'run_id': 'run', 'work_item_id': 'implementation',
                     'generation': 1, 'stale': False}
        fingerprint = canonical_digest({'fixture': 'parallel review'})
        common = {'run_id': 'run', 'project_id': project['id'], 'generation': 1, 'fencing_token': 1,
            'input_fingerprint': fingerprint, 'policy_fingerprint': fingerprint, 'required': True,
            'status': 'completed', 'approval_required': False, 'artifact_ids': [], 'payload': {},
            'quality_result': 'unknown'}
        def seed(tx):
            tx.put('plan', 'plan', {'authorized_rework_steps': ['implementation'], 'reused_inputs': []})
            tx.put('run', 'run', {'project_id': project['id'], 'plan_id': 'plan', 'iteration_id': 'iteration',
                'execution_state': 'running', 'quality_result': 'unknown', 'input_fingerprint': fingerprint,
                'base_commit': project['base_commit'], 'base_ref': project['base_ref'],
                'delivery_ids': [], 'blocking_reasons': ['review failed'], 'budget_limit': {'limit_micros': 1000}})
            for identity, contribution in contributions.items():
                tx.put('work_item', identity, {**common, 'key': identity, 'step': 'implementation', 'role': 'development',
                    'kind': 'stage_child', 'parent_stage_id': 'implementation', 'dependencies': [],
                    'attempt_id': contribution['id'], 'write_paths': [paths[identity]]})
                tx.put('code_snapshot', contribution['id'], {key: value for key, value in contribution.items() if key != 'id'})
            tx.put('work_item', 'implementation', {**common, 'key': 'implementation', 'step': 'implementation',
                'role': 'development', 'kind': 'aggregation', 'dependencies': list(contributions),
                'expanded_child_ids': list(contributions), 'original_dependencies': [], 'original_write_paths': ['src'],
                'write_paths': [], 'attempt_id': aggregate['id']})
            tx.put('code_snapshot', aggregate['id'], {key: value for key, value in aggregate.items() if key != 'id'})
            tx.put('work_item', 'review-work', {**common, 'key': 'code_review', 'step': 'code_review', 'role': 'review',
                'dependencies': ['implementation'], 'write_paths': [], 'attempt_id': 'review-attempt-1', 'quality_result': 'failed'})
            tx.put('attempt', 'review-attempt-1', {'run_id': 'run', 'work_item_id': 'review-work', 'iteration_id': 'iteration',
                'status': 'completed', 'generation': 1, 'fencing_token': 1, 'input_fingerprint': fingerprint})
            tx.put('review', 'review-attempt-1', {'run_id': 'run', 'work_item_id': 'review-work', 'generation': 1,
                'reviewed_commit': aggregate['commit_oid'], 'quality_result': 'failed', 'blocking_findings': [finding('a')]})
            tx.put('work_item', 'unit', {**common, 'key': 'unit_test_plan', 'step': 'unit_test_plan', 'role': 'unit_test',
                'status': 'pending', 'dependencies': ['review-work'], 'write_paths': [], 'attempt_id': None})
            return {}
        await store.command('fixture', 'seed', {}, seed)
        ledger = BudgetLedger(store)
        await ledger.setup_accounts('run', 'iteration', 1000, 2000)
        await complete_review_guard_context(store)
        yield SimpleNamespace(store=store, workflow=workflow, settings=settings, repository=repository, artifacts=artifacts,
            project=project, aggregate=aggregate, contributions=contributions, paths=paths, original_text=original_text,
            common=common, ledger=ledger, remediation=ReviewRemediation(store, workflow), temporary=tmp_path)
    finally:
        await store.close()


class CollectedFixtureRuntime:
    def __init__(self, env, review_findings=None):
        self.env, self.review_findings = env, review_findings

    async def execute_task(self, task):
        result = {'summary': 'Deterministic local fixture result'}
        if task['step'] == 'code_review':
            result.update(reviewed_commit=task['source_commit'], findings=self.review_findings or [])
        folder = self.env.settings.data_dir / 'attempt_artifacts' / task['attempt_id']
        folder.mkdir(parents=True)
        path = folder / ('openhands_final.json' if task['step'] == 'code_review' else 'codex_final.json')
        path.write_text(json.dumps(result))
        return {'execution_status': 'completed', 'result': result, 'artifacts': [{'path': str(path)}]}


async def complete_repair(env, repaired_letters, round_number):
    scheduler = Scheduler(env.workflow, env.store, CollectedFixtureRuntime(env), None, env.settings)
    run = await env.store.read('run', 'run')
    latest_repair = max(await env.store.list('review_repair'), key=lambda record: record['ordinal'])
    old_aggregate = await env.store.read('code_snapshot', latest_repair['base_snapshot_id'])
    claims = [await env.workflow.claim_next('run', 'fixture', str(uuid4())) for _ in repaired_letters]
    assert {claim['work_item']['id'] for claim in claims} == {'module-' + letter for letter in repaired_letters}
    # All affected children can be claimed before any finishes; the source write
    # scopes stay disjoint and each receives the same complete reviewed base.
    tasks = []
    for claim in claims:
        work, attempt = claim['work_item'], claim['attempt']
        source, commit = await scheduler._source(run, work)
        assert commit == old_aggregate['commit_oid']
        workspace = env.temporary / ('repair-' + attempt['id'])
        await env.repository.clone_snapshot(source, workspace, commit)
        assert all((workspace / path).is_file() for path in env.paths.values())
        letter = work['id'][-1]
        (workspace / env.paths[work['id']]).write_text(f'export const {letter} = {round_number};\n')
        tasks.append({'attempt_id': attempt['id'], 'work_item_id': work['id'], 'run_id': 'run', 'step': 'implementation',
            'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint'],
            'workspace': str(workspace), 'source_commit': commit, 'allowed_write_paths': work['write_paths']})
    await asyncio.gather(*(scheduler._execute_existing(task) for task in tasks))
    for task in tasks:
        work = await env.store.read('work_item', task['work_item_id'])
        assert work['status'] == 'completed', work
        snapshot = await env.store.read('code_snapshot', task['attempt_id'])
        assert snapshot['base_oid'] == old_aggregate['commit_oid']
        assert {s['commit_oid'] for s in env.contributions.values()} <= set(snapshot['parent_commit_oids'])
    aggregate_claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert aggregate_claim['work_item']['id'] == 'implementation'
    assert not aggregate_claim['work_item']['payload'].get('repair_base_snapshot_id')
    await scheduler._dispatch(aggregate_claim)
    aggregate_work = await env.store.read('work_item', 'implementation')
    assert aggregate_work['status'] == 'completed', aggregate_work
    aggregate = await env.store.read('code_snapshot', aggregate_work['attempt_id'])
    review = await env.store.read('work_item', 'review-work')
    assert await scheduler._source(await env.store.read('run', 'run'), review) == (
        Path(aggregate['repository_path']), aggregate['commit_oid'])
    return aggregate


async def complete_failed_review(env, letter):
    scheduler = Scheduler(env.workflow, env.store, CollectedFixtureRuntime(env, [finding(letter)]), None, env.settings)
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    work, attempt = claim['work_item'], claim['attempt']
    assert work['id'] == 'review-work'
    source, commit = await scheduler._source(claim['run'], work)
    workspace = env.temporary / ('review-' + attempt['id'])
    await env.repository.clone_snapshot(source, workspace, commit)
    await scheduler._execute_existing({'attempt_id': attempt['id'], 'work_item_id': work['id'], 'run_id': 'run',
        'step': 'code_review', 'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint'],
        'workspace': str(workspace), 'source_commit': commit, 'allowed_write_paths': []})
    reviewed = await env.store.read('work_item', 'review-work')
    assert reviewed['status'] == 'completed' and reviewed['quality_result'] == 'failed'
    assert (await env.store.read('review', attempt['id']))['reviewed_commit'] == commit


@pytest.mark.parametrize('letters', [('a',), ('a', 'b')], ids=['one-child', 'two-parallel-children'])
async def test_parallel_review_repair_reopens_only_owners_and_preserves_all_other_modules(parallel_env, letters):
    env = parallel_env
    await update(env.store, 'review', 'review-attempt-1', blocking_findings=[finding(letter) for letter in letters])
    before_items = {row['id']: row for row in await env.store.list('work_item')}
    before_snapshots = {row['id']: row for row in await env.store.list('code_snapshot')}
    before_review = await env.store.read('review', 'review-attempt-1')
    before_budget = await env.store.list('budget_account')
    results = await asyncio.gather(*(env.remediation.repair('review-work') for _ in range(3)))
    assert sum(result is not None for result in results) == 1
    result = next(result for result in results if result)
    owners = {'module-' + letter for letter in letters}
    assert set(result['affected_work_item_ids']) == owners | {'implementation', 'review-work', 'unit'}
    assert set(result['preserved_sibling_ids']) == set(env.paths) - owners
    for identity in env.paths:
        current = await env.store.read('work_item', identity)
        if identity not in owners:
            assert current == before_items[identity]
            prior_snapshot = env.contributions[identity]['id']
            assert await env.store.read('code_snapshot', prior_snapshot) == before_snapshots[prior_snapshot]
            continue
        assert current['generation'] == 2 and current['status'] == 'pending'
        assert current['write_paths'] == before_items[identity]['write_paths']
        alias = await env.store.read('code_snapshot', result['checkpoint_alias_ids'][identity])
        assert alias['work_item_id'] == identity and alias['generation'] == 1 and not alias['stale']
        assert alias['commit_oid'] == env.aggregate['commit_oid'] and alias['repository_path'] == env.aggregate['repository_path']
        assert alias['source_snapshot_id'] == env.aggregate['id'] and alias['source_review_id'] == 'review-attempt-1'
        assert alias['child_scope'] == current['write_paths']
        assert set(env.aggregate['parent_commit_oids']) <= set(alias['parent_commit_oids'])
    assert (await env.store.read('work_item', 'implementation'))['kind'] == 'aggregation'
    assert await env.store.list('budget_account') == before_budget
    for identity, original in {**before_snapshots, 'review-attempt-1': before_review}.items():
        kind = 'review' if identity == 'review-attempt-1' else 'code_snapshot'
        current = await env.store.read(kind, identity)
        assert {key: value for key, value in current.items() if key not in {'stale', 'revision'}} == {
            key: value for key, value in original.items() if key not in {'stale', 'revision'}}
    aggregate = await complete_repair(env, letters, 1)
    for identity, path in env.paths.items():
        expected = f'export const {identity[-1]} = 1;\n' if identity in owners else env.original_text[identity]
        assert (Path(aggregate['repository_path']) / path).read_text() == expected
    assert len(await env.store.list('review_repair')) == 1


@pytest.mark.parametrize('second_letter', ['a', 'b'], ids=['same-child-again', 'different-child-next'])
async def test_two_repair_rounds_keep_siblings_and_stop_at_the_existing_limit(parallel_env, second_letter):
    env = parallel_env
    env.workflow.settings = env.settings.model_copy(update={'auto_review_repair_limit': 2})
    assert await env.remediation.repair('review-work')
    first = await complete_repair(env, ['a'], 1)
    await complete_failed_review(env, second_letter)
    before_second = {identity: await env.store.read('work_item', identity) for identity in env.paths}
    result = await env.remediation.repair('review-work')
    assert result['ordinal'] == 2 and result['base_commit'] == first['commit_oid']
    owner = 'module-' + second_letter
    alias = await env.store.read('code_snapshot', result['checkpoint_alias_ids'][owner])
    assert alias['generation'] == before_second[owner]['generation']
    assert {s['commit_oid'] for s in env.contributions.values()} <= set(alias['parent_commit_oids'])
    second = await complete_repair(env, [second_letter], 2)
    for identity, path in env.paths.items():
        expected_value = 2 if identity == owner else 1 if identity == 'module-a' else 0
        assert (Path(second['repository_path']) / path).read_text() == f'export const {identity[-1]} = {expected_value};\n'
        if identity != owner:
            assert await env.store.read('work_item', identity) == before_second[identity]
    await complete_failed_review(env, second_letter)
    before = await env.store.list('work_item')
    assert await env.remediation.repair('review-work') is None
    assert await env.store.list('work_item') == before
    assert len(await env.store.list('review_repair')) == 2

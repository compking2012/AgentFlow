"""Known-review repair with real durable graphs and preserved Git snapshots."""
import asyncio
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from review_fixture_utils import complete_review_guard_context

from agentflow.common import canonical_digest
from agentflow.control.remediation import ReviewRemediation
from agentflow.control.scheduler import Scheduler
from agentflow.control.service import WorkflowService
from agentflow.models.budget import BudgetLedger, account_id
from agentflow.repository import RepositoryAdapter
from agentflow.settings import Settings
from agentflow.storage import LocalArtifactStore, Store


@pytest_asyncio.fixture
async def env(tmp_path):
    settings = Settings(data_dir=tmp_path / 'data')
    store = Store(settings.data_dir)
    await store.start()
    artifacts = LocalArtifactStore(settings.data_dir / 'artifacts')
    workflow = WorkflowService(store, artifacts, settings)
    project = await workflow.create_project({'name': 'Repair fixture', 'local_path': str(tmp_path / 'repo'),
        'import_mode': 'initialize_managed', 'dirty_worktree_policy': 'require_clean'}, 'project')
    repository = RepositoryAdapter()
    source = tmp_path / 'failed-code'
    await repository.clone_snapshot(tmp_path / 'repo', source, project['base_commit'])
    (source / 'product.py').write_text('def total(a, b):\n    return a - b\n')
    (source / 'keep.txt').write_text('valuable prior implementation\n')
    snapshot = await repository.freeze_workspace(source, project['base_commit'], 'known defective implementation')
    def seed(tx):
        tx.put('plan', 'plan', {'authorized_rework_steps': ['implementation']})
        tx.put('run', 'run', {'project_id': project['id'], 'plan_id': 'plan', 'iteration_id': 'iteration',
            'execution_state': 'running', 'quality_result': 'unknown', 'input_fingerprint': 'original',
            'delivery_ids': [], 'blocking_reasons': ['review failed'], 'budget_limit': {'limit_micros': 1000}})
        common = {'run_id': 'run', 'project_id': project['id'], 'generation': 1, 'fencing_token': 1,
            'input_fingerprint': 'current-input', 'policy_fingerprint': 'owner-policy', 'required': True,
            'status': 'completed', 'approval_required': True, 'artifact_ids': [], 'payload': {}}
        tx.put('work_item', 'code', {**common, 'step': 'implementation', 'key': 'implementation',
            'role': 'development', 'dependencies': [], 'write_paths': ['.'], 'attempt_id': 'code-attempt',
            'quality_result': 'unknown'})
        tx.put('work_item', 'review-work', {**common, 'step': 'code_review', 'key': 'code_review',
            'role': 'review', 'dependencies': ['code'], 'write_paths': [], 'attempt_id': 'review-attempt',
            'quality_result': 'failed'})
        tx.put('work_item', 'unit', {**common, 'step': 'unit_test_plan', 'key': 'unit_test_plan',
            'role': 'unit_test', 'status': 'pending', 'dependencies': ['review-work'], 'write_paths': [],
            'attempt_id': None, 'quality_result': 'unknown'})
        tx.put('attempt', 'review-attempt', {'work_item_id': 'review-work', 'status': 'completed',
            'generation': 1, 'fencing_token': 1, 'input_fingerprint': 'current-input'})
        tx.put('code_snapshot', 'failed-snapshot', {'run_id': 'run', 'work_item_id': 'code', 'generation': 1,
            'commit_oid': snapshot['commit_oid'], 'base_oid': snapshot['base_oid'],
            'tree_oid': snapshot['tree_oid'], 'repository_path': str(source), 'stale': False})
        tx.put('review', 'review-attempt', {'run_id': 'run', 'work_item_id': 'review-work', 'generation': 1,
            'reviewed_commit': snapshot['commit_oid'], 'quality_result': 'failed',
            'blocking_findings': [{'path': 'product.py', 'severity': 'blocking', 'description': 'Addition subtracts'}]})
        tx.put('approval', 'old-approval', {'work_item_id': 'code', 'run_id': 'run', 'stale': False,
            'decision': 'approve', 'fingerprint': 'old'})
        tx.put('artifact', 'old-diff', {'work_item_id': 'code', 'run_id': 'run', 'stale': False})
        return {}
    await store.command('fixture', 'seed', {}, seed)
    ledger = BudgetLedger(store)
    await ledger.setup_accounts('run', 'iteration', 1000, 2000)
    await complete_review_guard_context(store)
    value = SimpleNamespace(store=store, workflow=workflow, settings=settings, repository=repository,
        artifacts=artifacts, project=project, snapshot=snapshot, source=source, ledger=ledger,
        remediation=ReviewRemediation(store, workflow))
    yield value
    await store.close()


async def test_atomic_repair_keeps_failed_code_and_reopens_review_human_gate(env, tmp_path):
    before_budget = await env.ledger.snapshot('run', 'run')
    results = await asyncio.gather(*(env.remediation.repair('review-work') for _ in range(3)))
    assert sum(r is not None for r in results) == 1
    assert len(await env.store.list('review_repair')) == 1
    assert len(await env.store.list('work_revision')) == 3
    assert (await env.store.read('approval', 'old-approval'))['stale']
    assert (await env.store.read('artifact', 'old-diff'))['stale']
    assert (await env.store.read('review', 'review-attempt'))['quality_result'] == 'failed'
    assert await env.ledger.snapshot('run', 'run') == before_budget
    run = await env.store.read('run', 'run')
    code = await env.store.read('work_item', 'code')
    assert run['input_fingerprint'] != 'original'
    assert code['generation'] == 2 and code['approval_required'] and code['status'] == 'pending'
    assert code['write_paths'] == ['.']
    scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    path, commit = await scheduler._source(run, code)
    assert path == env.source and commit == env.snapshot['commit_oid']
    workspace = tmp_path / 'repair'
    await env.repository.clone_snapshot(path, workspace, commit)
    assert (workspace / 'keep.txt').read_text() == 'valuable prior implementation\n'
    assert (workspace / 'product.py').read_text().endswith('return a - b\n')
    (workspace / 'product.py').write_text('def total(a, b):\n    return a + b\n')
    fixed = await env.repository.freeze_workspace(workspace, commit, 'fix review finding')
    assert {row['path'] for row in fixed['diff']['changes']} == {'product.py'}
    claim = await env.workflow.claim_next('run', 'controller', 'repair-claim')
    assert claim['work_item']['id'] == 'code'
    assert (await env.workflow.claim_next('run', 'controller', 'cannot-bypass'))['attempt'] is None
    blob = await env.artifacts.put_bytes(b'{"summary":"fixed"}')
    attempt = claim['attempt']
    await env.workflow.finish_attempt(attempt['id'], {'fencing_token': attempt['fencing_token'],
        'input_fingerprint': attempt['input_fingerprint'], 'execution_status': 'completed', 'quality_result': 'unknown'},
        'finish-fixed', verified_artifacts=[{'digest': blob['id'], 'name': 'fixed.json'}])
    assert (await env.store.read('work_item', 'code'))['status'] == 'waiting_approval'
    assert (await env.workflow.claim_next('run', 'controller', 'approval-still-required'))['attempt'] is None
    approval = [a for a in await env.store.list('approval') if not a['stale']][0]
    await env.workflow.decide(approval['id'], {'decision': 'approve', 'expected_revision': approval['revision'],
        'expected_fingerprint': approval['fingerprint']}, 'approve-fixed')
    assert (await env.workflow.claim_next('run', 'controller', 'fresh-review'))['work_item']['id'] == 'review-work'


@pytest.mark.parametrize('case', ['unauthorized', 'paused', 'candidate', 'delivery_intent', 'unknown_call',
    'human_review', 'active_descendant', 'exhausted_budget', 'restored_budget', 'stale_review', 'wrong_commit',
    'parallel_stage', 'limit_reached', 'stale_fence'])
async def test_unsafe_or_unapproved_repair_never_starts(env, case):
    if case == 'limit_reached':
        env.workflow.settings = env.settings.model_copy(update={'auto_review_repair_limit': 2})
    def modify(tx):
        def change(record_kind, identity, **values):
            row = tx.get(record_kind, identity)
            return tx.put(record_kind, identity, {**row, **values}, row['revision'])
        if case == 'unauthorized':
            change('plan', 'plan', authorized_rework_steps=[])
        elif case == 'paused':
            change('run', 'run', execution_state='paused')
        elif case in {'candidate', 'delivery_intent'}:
            tx.put(case, 'already-frozen', {'run_id': 'run'})
        elif case == 'unknown_call':
            tx.put('model_invocation', 'unknown', {'run_id': 'run', 'state': 'uncertain'})
        elif case == 'human_review':
            change('work_item', 'review-work', status='waiting_approval')
        elif case == 'active_descendant':
            change('work_item', 'unit', status='waiting_execution')
        elif case == 'exhausted_budget':
            change('budget_account', account_id('run', 'run'), settled_micros=1000)
        elif case == 'restored_budget':
            change('budget_account', account_id('iteration', 'iteration'), restore_uncertain=True)
        elif case == 'stale_review':
            change('review', 'review-attempt', generation=0)
        elif case == 'stale_fence':
            change('attempt', 'review-attempt', fencing_token=0)
        elif case == 'wrong_commit':
            change('review', 'review-attempt', reviewed_commit='f' * 40)
        elif case == 'parallel_stage':
            change('work_item', 'code', kind='aggregation')
        elif case == 'limit_reached':
            for index in range(2):
                tx.put('review_repair', f'previous-{index}', {'run_id': 'run', 'review_attempt_id': str(index)})
        return {}
    await env.store.command('fixture', case, {}, modify)
    assert await env.remediation.repair('review-work') is None
    assert (await env.store.read('work_item', 'code'))['generation'] == 1


async def test_explicit_later_revision_discards_automatic_repair_checkpoint(env):
    await env.remediation.repair('review-work')
    run = await env.store.read('run', 'run')
    await env.workflow.revise('run', {'expected_revision': run['revision'], 'work_item_ids': ['code'],
                                     'reason': 'Change the original product design'}, 'owner-revision')
    assert 'repair_base_snapshot_id' not in (await env.store.read('work_item', 'code'))['payload']


async def test_scheduler_collects_repair_with_preserved_parentage(env, tmp_path):
    await env.remediation.repair('review-work')
    claim = await env.workflow.claim_next('run', 'controller', 'claim')
    attempt = claim['attempt']
    workspace = tmp_path / 'repair-collection'
    await env.repository.clone_snapshot(env.source, workspace, env.snapshot['commit_oid'])
    (workspace / 'product.py').write_text('def total(a, b):\n    return a + b\n')
    staged = env.settings.data_dir / 'attempt_artifacts' / 'result.json'
    staged.parent.mkdir()
    staged.write_text('{"summary":"Fixed subtraction; retained original scope"}')
    class CollectedRuntime:
        async def execute_task(self, task):
            return {'execution_status': 'completed', 'result': {'summary': 'Fixed subtraction'},
                    'artifacts': [{'path': str(staged)}]}
    scheduler = Scheduler(env.workflow, env.store, CollectedRuntime(), None, env.settings)
    task = {'attempt_id': attempt['id'], 'work_item_id': 'code', 'run_id': 'run', 'step': 'implementation',
            'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint'],
            'workspace': str(workspace), 'source_commit': env.snapshot['commit_oid'], 'allowed_write_paths': ['.']}
    await scheduler._execute_existing(task)
    saved = await env.store.read('code_snapshot', attempt['id'])
    assert saved['base_oid'] == env.snapshot['commit_oid']
    assert {env.snapshot['base_oid'], env.snapshot['commit_oid']} <= set(saved['parent_commit_oids'])
    assert (workspace / 'keep.txt').read_text() == 'valuable prior implementation\n'
    assert (await env.store.read('work_item', 'code'))['status'] == 'waiting_approval'
    review = await env.store.read('work_item', 'review-work')
    path, commit = await scheduler._source(await env.store.read('run', 'run'), review)
    assert path == workspace and commit == saved['commit_oid']


async def test_reconciliation_waits_for_new_committed_run_events(env):
    def pause(tx):
        run = tx.get('run', 'run')
        return tx.put('run', 'run', {**run, 'execution_state': 'paused'}, run['revision'])
    await env.store.command('fixture', 'pause', {}, pause)
    await env.remediation.reconcile()
    assert not await env.store.list('review_repair')
    run = await env.store.read('run', 'run')
    await env.workflow.control_run('run', {'action': 'resume', 'expected_revision': run['revision'],
                                         'reason': 'Resume authorized correction'}, 'resume')
    await env.remediation.reconcile()
    assert len(await env.store.list('review_repair')) == 1
    await env.remediation.reconcile()
    assert len(await env.store.list('review_repair')) == 1


@pytest.mark.parametrize('maximum,count,allowed', [(0, 5000, True), (20, 20, False), (None, 0, False),
    (False, 0, False), (-1, 0, False), (0, None, False)])
async def test_review_repair_honors_unlimited_requests_without_treating_invalid_accounts_as_unlimited(env, maximum, count, allowed):
    def limits(tx):
        for kind, owner in [('run', 'run'), ('iteration', 'iteration')]:
            account = tx.get('budget_account', account_id(kind, owner))
            tx.put('budget_account', account['id'], {**account, 'max_requests': maximum, 'request_count': count},
                   account['revision'])
            row = tx.get(kind, owner)
            tx.put(kind, owner, {**row, 'budget_limit': {**row['budget_limit'], 'max_model_requests': maximum}}, row['revision'])
        return {}
    await env.store.command('fixture', 'limits', {}, limits)
    before = await env.store.list('budget_account')
    result = await env.remediation.repair('review-work')
    assert (result is not None) is allowed
    assert await env.store.list('budget_account') == before


async def test_prior_stage_review_repairs_do_not_exhaust_later_stage_review_limit(env):
    def seed(tx):
        for index in range(2):
            identity = f'earlier-review-{index}'
            tx.put('attempt', identity, {'work_item_id': 'earlier-stage-review'})
            tx.put('review_repair', identity, {'run_id': 'run', 'review_attempt_id': identity})
        return {}
    await env.store.command('fixture', 'prior-stage-reviews', {}, seed)
    result = await env.remediation.repair('review-work')
    assert result and result['review_work_item_id'] == 'review-work'
    assert result['ordinal'] == 3


@pytest.mark.parametrize('candidate_state,allowed', [
    ('historical', True), ('current', False), ('missing', False), ('empty', False), ('malformed', False),
    ('corrupt_digest', False), ('null', False), ('unknown_run', False), ('historical_with_delivery', False)])
async def test_only_a_known_historical_candidate_permits_current_review_repair(env, candidate_state, allowed):
    def seed(tx):
        current_fingerprint = canonical_digest('current authorized run')
        run = tx.get('run', 'run')
        tx.put('run', 'run', {**run, 'input_fingerprint':
            'unknown' if candidate_state == 'unknown_run' else current_fingerprint}, run['revision'])
        candidate = {'run_id': 'run', 'source_repository': str(env.source),
                     'source_commit': env.snapshot['commit_oid'], 'tree_oid': env.snapshot['tree_oid']}
        if candidate_state != 'missing':
            candidate['run_input_fingerprint'] = {'current': current_fingerprint, 'empty': '', 'malformed': 42,
                'corrupt_digest': 'sha256:' + 'g' * 64, 'null': None}.get(
                    candidate_state, canonical_digest('previous authorized input'))
        tx.put('candidate', 'frozen-history', candidate)
        if candidate_state == 'historical_with_delivery':
            tx.put('delivery_intent', 'pending-delivery', {'run_id': 'run', 'state': 'failed'})
        return {}
    await env.store.command('fixture.review-freeze', candidate_state, {}, seed)
    original_candidate = await env.store.read('candidate', 'frozen-history')
    before = {kind: await env.store.list(kind) for kind in ('run', 'work_item', 'review', 'code_snapshot', 'budget_account')}
    repaired = await env.remediation.repair('review-work')
    assert bool(repaired) is allowed
    assert await env.store.read('candidate', 'frozen-history') == original_candidate
    if not allowed:
        assert {kind: await env.store.list(kind) for kind in before} == before
    else:
        work = await env.store.read('work_item', 'code')
        assert work['generation'] == 2 and work['write_paths'] == ['.'] and work['approval_required']
        assert await env.store.list('budget_account') == before['budget_account']


async def test_review_of_owner_runtime_repair_keeps_its_full_candidate_snapshot(tmp_path):
    from test_execution_pipeline import fixture
    from test_product_test_runtime_repair import failed_candidate, patch, request_repair

    async with fixture(tmp_path, app_targets=('api',)) as env:
        request = await failed_candidate(env)
        scheduled = await request_repair(env, request)
        await complete_review_guard_context(env.store)
        run = await env.store.read('run', 'run')
        await patch(env, 'run', 'run', budget_limit={**run['budget_limit'], 'max_active_seconds': 300, 'max_tool_calls': 30})
        run = await env.store.read('run', 'run')
        await env.workflow.control_run('run', {'action': 'resume', 'expected_revision': run['revision'],
            'reason': 'Execute the owner-authorized test fixture repair'}, 'resume-test-runtime')
        candidate = await env.store.read('candidate', request['candidate_id'])
        prior_code = await env.store.read('work_item', 'code-work')
        plans = [row for row in await env.store.list('artifact') if row['step'] in {'unit_test_plan', 'integration_test_strategy'}]
        scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
        claim = await env.claim()
        assert claim['work_item']['id'] == scheduled['repair_work_item_id']
        work, attempt = claim['work_item'], claim['attempt']
        source, commit = await scheduler._source(claim['run'], work)
        workspace = tmp_path / 'reviewed-fixture-repair'
        await env.repository.clone_snapshot(source, workspace, commit)
        original_test = (workspace / 'tests/unit.test.mjs').read_text()
        (workspace / 'tests/unit.test.mjs').write_text(original_test + 'const port = Number(process.env.AGENTFLOW_TEST_PORT ?? 0);\n')
        control = await scheduler.coding_steps.prepare(claim['run'], work, attempt, commit, 512)
        staged = env.settings.data_dir / 'attempt_artifacts' / 'fixture-repair.json'
        staged.parent.mkdir(parents=True)
        receipt = {'summary': 'Preserved assertions and used the supplied port', 'status': 'complete', 'next_action': ''}
        staged.write_text(json.dumps(receipt))
        class CollectedFixture:
            async def execute_task(self, task):
                return {'execution_status': 'completed', 'result': receipt, 'artifacts': [str(staged)],
                        'active_seconds': 2.0, 'observed_tool_calls': 1, 'tool_observation_complete': True}
        scheduler.runtime = CollectedFixture()
        await scheduler._execute_existing({'run_id': 'run', 'work_item_id': work['id'], 'attempt_id': attempt['id'],
            'step': work['step'], 'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint'],
            'source_commit': commit, 'workspace': str(workspace), 'allowed_write_paths': work['write_paths'],
            'coding_step': control})
        saved = await env.store.read('code_snapshot', attempt['id'])
        assert (await env.store.read('work_item', work['id']))['status'] == 'completed'
        review_claim = await env.claim()
        reviewer, review_attempt = review_claim['work_item'], review_claim['attempt']
        assert reviewer['id'] == scheduled['review_work_item_id']
        review_workspace = tmp_path / 'fixture-review'
        await env.repository.clone_snapshot(workspace, review_workspace, saved['commit_oid'])
        receipt = {'summary': 'Listening failure must also release resources', 'reviewed_commit': saved['commit_oid'],
            'findings': [{'severity': 'blocking', 'path': 'tests/unit.test.mjs',
                          'description': 'Close the database and remove the temporary directory when listening fails.'}]}
        staged.write_text(json.dumps(receipt))
        await scheduler._execute_existing({'run_id': 'run', 'work_item_id': reviewer['id'], 'attempt_id': review_attempt['id'],
            'step': 'code_review', 'fencing_token': review_attempt['fencing_token'],
            'input_fingerprint': review_attempt['input_fingerprint'], 'source_commit': saved['commit_oid'],
            'workspace': str(review_workspace), 'allowed_write_paths': []})
        meters = {kind: await env.store.list(kind) for kind in ('budget_account', 'coding_work_budget', 'coding_step_usage')}
        repair = await ReviewRemediation(env.store, env.workflow).repair(reviewer['id'])
        assert repair and repair['base_commit'] == saved['commit_oid']
        revised = await env.store.read('work_item', work['id'])
        assert revised['generation'] == 2 and revised['write_paths'] == ['tests/unit.test.mjs']
        path, frozen = await scheduler._source(await env.store.read('run', 'run'), revised)
        assert frozen == saved['commit_oid']
        next_workspace = tmp_path / 'next-fixture-step'
        await env.repository.clone_snapshot(path, next_workspace, frozen)
        assert (next_workspace / 'feature.txt').read_text() == 'frozen implementation\n'
        assert (next_workspace / 'tests/unit.test.mjs').read_text().startswith(original_test)
        assert 'AGENTFLOW_TEST_PORT' in (next_workspace / 'tests/unit.test.mjs').read_text()
        assert await env.store.read('candidate', candidate['id']) == candidate
        assert await env.store.read('work_item', 'code-work') == prior_code
        for row in plans:
            assert await env.store.read('artifact', row['id']) == row
        for kind, rows in meters.items():
            assert await env.store.list(kind) == rows

"""Real controller transitions; isolated Git and receipts at NodeService's verified boundary."""
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from test_review_contract_repair import setup_batch
from test_workflow import flow as flow

from agentflow.common import DomainError
from agentflow.control.review_contract_repair import ReviewContractRepair
from agentflow.control.service import WorkflowService


async def test_dispatch_and_reconciliation_do_not_prepare_diagnostic_concurrently(flow, monkeypatch):
    env = await lifecycle_source(flow)
    claim, merged = await finish_repairs_and_assembly(env)
    core = ReviewContractRepair(env.store, env.workflow, env.nodes)
    prepared = await core.diagnostics.prepare_builtin_suite(claim['run'], merged)
    async def prepare(*args, **kwargs):
        return prepared
    active = maximum = 0
    overlap = asyncio.Event()
    async def poll(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active > 1:
            overlap.set()
        try:
            await asyncio.wait_for(overlap.wait(), timeout=1)
        except TimeoutError:
            pass
        finally:
            active -= 1
        return {'state': 'waiting'}
    monkeypatch.setattr(core.diagnostics, 'prepare_builtin_suite', prepare)
    monkeypatch.setattr(core.diagnostics, 'start_or_poll', poll)
    await asyncio.gather(core.validate_or_poll(claim), core.validate_or_poll(claim))
    assert maximum == 1


async def seed_triage_attempt(flow):
    _, store, _, _, _ = flow
    def seed(tx):
        work = tx.get('work_item', 'triage')
        return tx.put('attempt', work['attempt_id'], {'run_id': 'run', 'iteration_id': 'iteration',
            'work_item_id': work['id'], 'status': 'running', 'generation': work['generation'],
            'fencing_token': work['fencing_token'], 'input_fingerprint': work['input_fingerprint']})
    return await store.command('fixture.lifecycle-attempt', 'triage', {}, seed)


async def triage_finish_args(flow, result):
    attempt = await seed_triage_attempt(flow)
    blob = await flow[2].put_bytes(json.dumps(result).encode())
    return (attempt['id'], {'execution_status': 'completed', 'quality_result': 'passed',
        'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint']},
        'finish-triage'), {'verified_artifacts': [{'digest': blob['id'], 'name': 'openhands_final.json',
            'media_type': 'application/json'}], 'review_disposition_result': result}


async def test_triage_finish_and_restarted_duplicate_preserve_one_batch(flow):
    _, result = await setup_batch(flow)
    args, kwargs = await triage_finish_args(flow, result)
    first = await flow[0].finish_attempt(*args, **kwargs)
    assert first['status'] == 'completed'
    before = {kind: await flow[1].list(kind) for kind in (
        'work_item', 'attempt', 'review_contract_repair', 'review_repair', 'coding_work_budget', 'artifact')}
    restarted = WorkflowService(flow[1], flow[2], flow[0].settings)
    assert await restarted.finish_attempt(*args, **kwargs) == first
    assert {kind: await flow[1].list(kind) for kind in before} == before


async def test_triage_finish_failure_rolls_back_graph_budget_and_completion_together(flow, monkeypatch):
    _, result = await setup_batch(flow)
    args, kwargs = await triage_finish_args(flow, result)
    kinds = ('work_item', 'attempt', 'review_contract_repair', 'review_repair', 'coding_work_budget', 'code_snapshot', 'artifact', 'run')
    before = {kind: await flow[1].list(kind) for kind in kinds}
    original = flow[0]._recompute_run
    def fail_after_writes(tx, run_id):
        original(tx, run_id)
        raise DomainError('fixture_finish_failure', 'Injected after graph, budget and completion writes')
    with monkeypatch.context() as patch:
        patch.setattr(flow[0], '_recompute_run', fail_after_writes)
        with pytest.raises(DomainError, match='Injected after'):
            await flow[0].finish_attempt(*args, **kwargs)
    assert {kind: await flow[1].list(kind) for kind in kinds} == before
    assert (await flow[0].finish_attempt(*args, **kwargs))['status'] == 'completed'
    batch = await flow[1].read('review_contract_repair', 'batch')
    assert batch['state'] == 'repairing' and len(batch['repair_work_item_ids']) == 3


async def lifecycle_source(flow, *, duplicate_finding=False, coverage=False):
    """Add exact Git and sealed stage evidence to the compact contract fixture."""
    import shutil
    from copy import deepcopy
    from pathlib import Path
    from types import SimpleNamespace

    from test_execution_pipeline import PipelineFixture
    from test_starter_execution_recipes import STARTER

    from agentflow.common import canonical_digest
    from agentflow.control.review_contract_repair import new_work
    from agentflow.control.test_migration_guard import TestMigrationGuard
    from agentflow.execution.models import TargetConfig
    from agentflow.execution.service import NodeService
    from agentflow.repository import RepositoryAdapter

    service, store, _, project, _ = flow
    _, result = await setup_batch(flow)
    repository = RepositoryAdapter()
    source = Path(project['local_path'])
    shutil.copytree(STARTER, source, dirs_exist_ok=True)
    old_literals = {'api': "{name: '母亲节'}", 'web': "'母亲节'"}
    for target, old in old_literals.items():
        (source / f'tests/{target}.spec.mjs').write_text(
            "import {test,expect} from './support/fixtures.mjs';\n"
            f"test('contract', async()=>{{expect(actual()).toEqual({old});}});\n")
    snapshot = await repository.freeze_workspace(source, project['base_commit'], 'Existing contract fixture')
    configs = [TargetConfig(target_config_id=f'target-{i}', app_target=target, os_name='Linux',
        os_version_constraint='24.04', cpu_architecture='x86_64', required_display_protocol='not_required',
        required_device_mode='not_required') for i, target in enumerate(('api', 'web'))]
    guard = TestMigrationGuard()
    assertions = []
    for target in old_literals:
        path = f'tests/{target}.spec.mjs'
        manifest = guard.inspect(path, (source / path).read_text())
        for case in manifest['cases']:
            assertions.extend({**assertion, 'path': path, 'case_id': case['case_id']} for assertion in case['assertions'])
    result['source_commit'] = snapshot['commit_oid']
    for action in result['actions']:
        for migration in action['migrations']:
            actual = next(row for row in assertions if row['path'] == migration['path'])
            migration.update(case_id=actual['case_id'], assertion_id=actual['assertion_id'])
    def seed(tx):
        run = tx.get('run', 'run')
        plan = tx.get('plan', 'plan')
        tx.put('plan', 'plan', {**plan, 'target_configs': [c.model_dump(mode='json') for c in configs],
            'product_contract': {'stack': 'node_web_api'}}, plan['revision'])
        old = tx.get('code_snapshot', 'snapshot')
        frozen = tx.put('code_snapshot', 'snapshot', {**old, **snapshot, 'repository_path': str(source)}, old['revision'])
        review = tx.get('review', 'failed-review')
        review = tx.put('review', review['id'], {**review, 'reviewed_commit': snapshot['commit_oid']}, review['revision'])
        children = []
        for identity in ('bad', 'good'):
            work = tx.get('work_item', identity)
            children.append(tx.put('work_item', identity, {**work, 'kind': 'stage_child'}, work['revision']))
        stage = tx.get('work_item', 'review')
        expansion = tx.get('stage_expansion', 'expanded')
        tx.put('stage_expansion', 'expanded', {**expansion, 'run_id': 'run', 'child_ids': ['bad', 'good'],
            'original_stage': {**stage, 'kind': 'stage', 'dependencies': ['source']}}, expansion['revision'])
        batch = tx.get('review_contract_repair', 'batch')
        context = deepcopy(batch['context'])
        context.update(snapshot=frozen, source_commit=snapshot['commit_oid'], reviews=[review], assertions=assertions)
        if duplicate_finding:
            context['findings'].append({**context['findings'][0], 'finding_id': 'same-assertion-second-review'})
            result['actions'].append({**deepcopy(result['actions'][0]), 'finding_id': 'same-assertion-second-review'})
        tx.put('review_contract_repair', 'batch', {**batch, 'source_commit': snapshot['commit_oid'],
            'context': context, 'context_digest': canonical_digest(context), 'cohort': children}, batch['revision'])
        tx.put('work_item', 'formal-unit', new_work(run, 'formal-unit', 'unit_test_execution', ['api-tests', 'web-tests']))
        return {}
    await store.command('fixture.lifecycle-source', 'source', {}, seed)
    if coverage:
        from test_review_contract_coverage import add_coverage_authority
        await add_coverage_authority(store, result)
    nodes = NodeService(store, service.settings.data_dir, 'https://127.0.0.1:9443')
    receipts = PipelineFixture(None, service, store, nodes, service.settings, repository, source, snapshot, configs)
    args, kwargs = await triage_finish_args(flow, result)
    await service.finish_attempt(*args, **kwargs)
    return SimpleNamespace(flow=flow, store=store, workflow=service, source=source, snapshot=snapshot,
        repository=repository, receipts=receipts, nodes=nodes,
        core=ReviewContractRepair(store, service, nodes))


async def finish_claim(env, claim, *, quality='passed', source=None):
    attempt, work = claim['attempt'], claim['work_item']
    if source is not None:
        def collect(tx):
            return tx.put('code_snapshot', attempt['id'], {**source, 'run_id': 'run', 'work_item_id': work['id'],
                'generation': work['generation'], 'stale': False})
        await env.store.command('fixture.lifecycle-snapshot', attempt['id'], {}, collect)
    if work['step'] == 'code_review':
        def collect_review(tx):
            batch = tx.get('review_contract_repair', 'batch')
            assembly = tx.get('work_item', batch['assembly_work_item_id'])
            snapshot = tx.get('code_snapshot', assembly['attempt_id'])
            return tx.put('review', attempt['id'], {'run_id': 'run', 'work_item_id': work['id'],
                'generation': work['generation'], 'reviewer_id': attempt['id'], 'stale': False,
                'reviewed_commit': snapshot['commit_oid'], 'quality_result': quality, 'blocking_findings': []})
        await env.store.command('fixture.lifecycle-review', attempt['id'], {}, collect_review)
    blob = await env.workflow.artifacts.put_bytes(json.dumps({'fixture_scope': 'controller_lifecycle', 'work': work['id']}).encode())
    return await env.workflow.finish_attempt(attempt['id'], {'execution_status': 'completed', 'quality_result': quality,
        'input_fingerprint': attempt['input_fingerprint'], 'fencing_token': attempt['fencing_token']},
        'fixture-finish:' + attempt['id'], verified_artifacts=[{'digest': blob['id'], 'name': 'output.json'}])


async def finish_repairs_and_assembly(env, *, account_usage=False):
    from agentflow.repository.assembly import AssemblyManager

    batch = await env.store.read('review_contract_repair', 'batch')
    sources = []
    for _ in batch['repair_work_item_ids']:
        claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
        assert claim['work_item']['id'] in batch['repair_work_item_ids']
        work = claim['work_item']
        if account_usage:
            from agentflow.control.coding_steps import CodingSteps
            steps = CodingSteps(env.store, env.workflow.settings, env.repository)
            control = await steps.prepare(claim['run'], work, claim['attempt'], env.snapshot['commit_oid'], 2048)
            await steps.account({'attempt_id': claim['attempt']['id'], 'work_item_id': work['id'], 'run_id': work['run_id'],
                'coding_step': control}, {'active_seconds': 2.0, 'observed_tool_calls': 1, 'tool_observation_complete': True})
        folder = env.source.parent / ('action-' + work['id'])
        await env.repository.clone_snapshot(env.source, folder, env.snapshot['commit_oid'])
        actions = work['payload']['review_contract_actions']
        if work['payload']['review_contract_kind'] == 'test_contract_migration':
            for action in actions:
                for migration in action['migrations']:
                    path = folder / migration['path']
                    path.write_text(path.read_text().replace(migration['old_expected'], migration['new_expected']))
        elif work['payload']['review_contract_kind'] == 'test_coverage_extension':
            for name in work['write_paths']:
                path = folder / name
                path.write_text(path.read_text().replace('});\n', 'expect(1).toBe(1);});\n'))
        else:
            path = folder / 'public/index.html'
            path.write_text(path.read_text() + '\n<!-- corrected legend fixture -->\n')
        snapshot = await env.repository.freeze_workspace(folder, env.snapshot['commit_oid'], 'Controlled action result')
        source = {**snapshot, 'repository_path': str(folder), 'parent_commit_oids': [env.snapshot['commit_oid']]}
        await env.core.validate_action({**claim['attempt'], 'attempt_id': claim['attempt']['id']}, source)
        await finish_claim(env, claim, source=source)
        sources.append(source)
    assembly_claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert assembly_claim['work_item']['id'] == batch['assembly_work_item_id']
    merged = await AssemblyManager(env.workflow.settings.data_dir).assemble(
        sources, env.source, env.snapshot['commit_oid'], 'lifecycle-assembly')
    from agentflow.control.scheduler import Scheduler
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.workflow, scheduler.store = env.workflow, env.store
    scheduler._assemblies = {(assembly_claim['work_item']['id'], assembly_claim['work_item']['generation']): merged}
    await scheduler._finish_assembly(assembly_claim)
    assert (await env.store.read('work_item', assembly_claim['work_item']['id']))['quality_result'] == 'unknown'
    validation = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert validation['work_item']['id'] == batch['validation_work_item_id']
    return validation, merged


async def finish_diagnostic(env, validation, *, restart=False, complete=True):
    await env.core.validate_or_poll(validation)
    waiting = await env.store.read('work_item', validation['work_item']['id'])
    assert waiting['status'] == 'waiting_execution'
    diagnostics = await env.store.list('review_diagnostic')
    assert len(diagnostics) == 1
    before = await env.store.list('node_job')
    if restart:
        env.core = ReviewContractRepair(env.store, WorkflowService(env.store, env.workflow.artifacts, env.workflow.settings), env.nodes)
    await env.core.reconcile()
    assert await env.store.list('node_job') == before
    for job_id in diagnostics[0]['build_job_ids']:
        await env.receipts.build_receipt(job_id)
    await env.core.reconcile()
    diagnostic = (await env.store.list('review_diagnostic'))[0]
    assert diagnostic['test_job_ids']
    for job_id in diagnostic['test_job_ids']:
        await env.receipts.test_receipt(job_id)
    if not complete:
        return diagnostic
    await env.core.reconcile()
    assert (await env.store.read('work_item', validation['work_item']['id']))['status'] == 'completed'
    return (await env.store.list('review_diagnostic'))[0]


async def test_repaired_snapshot_reopens_original_reviews_before_formal_tests(flow):
    from agentflow.control.review_contract_binding import bound_producer
    from agentflow.control.review_producer import sealed_review_group

    env = await lifecycle_source(flow)
    validation, merged = await finish_repairs_and_assembly(env)
    diagnostic = await finish_diagnostic(env, validation, restart=True)
    assert diagnostic['state'] == 'passed' and diagnostic['source_manifest']['source_commit'] == merged['commit_oid']
    assert not await env.store.list('check') and not await env.store.list('candidate')
    batch = await env.store.read('review_contract_repair', 'batch')
    assert batch['state'] == 'reviewing'
    def binding(tx):
        run = tx.get('run', 'run')
        return {'producers': {identity: bound_producer(tx, run, tx.get('work_item', identity))
                             for identity in ('bad', 'good', 'review')},
                'group': sealed_review_group(tx, run, tx.get('work_item', 'review'))}
    bound = await env.store.command('fixture.lifecycle-binding', 'after-diagnostics', {}, binding)
    assert all(producer and producer['id'] == batch['assembly_work_item_id'] for producer in bound['producers'].values())
    assert bound['group'] and bound['group']['snapshot']['commit_oid'] == merged['commit_oid']
    for work_id in ('api-tests', 'web-tests', 'formal-unit'):
        assert (await env.store.read('work_item', work_id))['status'] == 'pending'
    reviewers = [await env.workflow.claim_next('run', 'fixture', str(uuid4())) for _ in range(2)]
    assert {claim['work_item']['id'] for claim in reviewers} == {'bad', 'good'}
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['attempt'] is None
    await finish_claim(env, reviewers[0])
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['attempt'] is None
    await finish_claim(env, reviewers[1])
    stage = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert stage['work_item']['id'] == 'review'
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['attempt'] is None
    await finish_claim(env, stage)
    for claim in [*reviewers, stage]:
        review = await env.store.read('review', claim['attempt']['id'])
        assert review['reviewed_commit'] == merged['commit_oid'] and review['quality_result'] == 'passed'
    await env.core.reconcile()
    assert (await env.store.read('review_contract_repair', 'batch'))['state'] == 'completed'
    tests = [await env.workflow.claim_next('run', 'fixture', str(uuid4())) for _ in range(2)]
    assert {claim['work_item']['id'] for claim in tests} == {'api-tests', 'web-tests'}
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['attempt'] is None
    for claim in tests:
        await finish_claim(env, claim)
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['work_item']['id'] == 'formal-unit'


async def test_validation_finish_rollback_and_duplicate_cannot_publish_partial_or_extra_reviews(flow, monkeypatch):
    from agentflow.control.review_contract_binding import bound_producer

    env = await lifecycle_source(flow)
    validation, _ = await finish_repairs_and_assembly(env)
    await finish_diagnostic(env, validation, complete=False)
    before = {kind: await env.store.list(kind) for kind in ('work_item', 'attempt', 'artifact', 'review_contract_repair')}
    original = env.workflow._recompute_run
    def fail_after_finish_writes(tx, run_id):
        original(tx, run_id)
        raise DomainError('fixture_validation_failure', 'Injected validation completion rollback')
    with monkeypatch.context() as patch:
        patch.setattr(env.workflow, '_recompute_run', fail_after_finish_writes)
        with pytest.raises(DomainError, match='Injected validation'):
            await env.core.validate_or_poll(validation)
    assert {kind: await env.store.list(kind) for kind in before} == before
    def producer(tx):
        return {'producer': bound_producer(tx, tx.get('run', 'run'), tx.get('work_item', 'bad'))}
    assert (await env.store.command('fixture.lifecycle-binding', 'rollback', {}, producer))['producer'] is None
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['attempt'] is None
    await env.core.validate_or_poll(validation)
    done = {kind: await env.store.list(kind) for kind in ('work_item', 'attempt', 'artifact', 'review_contract_repair', 'node_job')}
    restarted = ReviewContractRepair(env.store, WorkflowService(env.store, env.workflow.artifacts, env.workflow.settings), env.nodes)
    await restarted.validate_or_poll(validation)
    await restarted.reconcile()
    assert {kind: await env.store.list(kind) for kind in done} == done
    assert (await env.store.read('review_contract_repair', 'batch'))['state'] == 'reviewing'
    assert not await env.store.list('check') and not await env.store.list('candidate')


@pytest.mark.parametrize('tampering', ['replace_actual', 'delete_assertion'])
async def test_migration_checks_frozen_commit_even_when_dirty_worktree_matches_approved_change(flow, tampering):
    env = await lifecycle_source(flow)
    batch = await env.store.read('review_contract_repair', 'batch')
    claims = [await env.workflow.claim_next('run', 'fixture', str(uuid4())) for _ in batch['repair_work_item_ids']]
    claim = next(row for row in claims
                 if row['work_item']['payload']['review_contract_kind'] == 'test_contract_migration')
    work, attempt = claim['work_item'], claim['attempt']
    migrations = [migration for action in work['payload']['review_contract_actions'] for migration in action['migrations']]
    assert len(migrations) == 1
    migration = migrations[0]
    folder = env.source.parent / ('tampered-action-' + work['id'])
    await env.repository.clone_snapshot(env.source, folder, env.snapshot['commit_oid'])
    path = folder / migration['path']
    original = path.read_text()
    approved = original.replace(migration['old_expected'], migration['new_expected'])
    if tampering == 'replace_actual':
        committed = approved.replace('actual()', migration['new_expected'])
    else:
        committed = approved.replace(f"expect(actual()).toEqual({migration['new_expected']});", '')
    assert committed != approved
    path.write_text(committed)
    frozen = await env.repository.freeze_workspace(folder, env.snapshot['commit_oid'], 'Rejected assertion weakening')
    source = {**frozen, 'repository_path': str(folder), 'parent_commit_oids': [env.snapshot['commit_oid']]}
    # A validator reading the current files would approve this exact migration.
    path.write_text(approved)
    assert env.core.guard.verify(env.source, folder, migrations)['ok'] is True
    assert env.repository._run(folder, ['show', frozen['commit_oid'] + ':' + migration['path']]) == committed.encode()
    assert env.repository._run(folder, ['status', '--porcelain', '--', migration['path']]).strip()
    with pytest.raises(DomainError) as caught:
        await env.core.validate_action({**attempt, 'attempt_id': attempt['id']}, source)
    assert caught.value.code == 'test_migration_invalid'
    assert not await env.store.list('test_migration_check')
    assert (await env.store.read('work_item', work['id']))['status'] == 'running'


async def test_failed_rereview_builds_triage_from_verified_diagnostic_node_jobs(flow):
    from agentflow.control.review_disposition import ReviewDisposition

    env = await lifecycle_source(flow)
    validation, merged = await finish_repairs_and_assembly(env)
    diagnostic = await finish_diagnostic(env, validation)
    claims = [await env.workflow.claim_next('run', 'fixture', str(uuid4())) for _ in range(2)]
    failed, passed = claims
    await finish_claim(env, failed, quality='failed')
    await finish_claim(env, passed)
    def finding(tx):
        review = tx.get('review', failed['attempt']['id'])
        return tx.put('review', review['id'], {**review, 'blocking_findings': [
            {'path': 'public/index.html', 'message': 'Legend still misses the accepted explanation.'}]}, review['revision'])
    await env.store.command('fixture.lifecycle-rereview', 'finding', {}, finding)
    run = await env.store.read('run', 'run')
    reviewer = await env.store.read('work_item', failed['work_item']['id'])
    disposition = ReviewDisposition(env.store, env.workflow.artifacts, env.workflow.settings)
    rebuilt = await disposition.build(run, reviewer)
    assert rebuilt['ok'], rebuilt
    assert rebuilt['context']['source_commit'] == merged['commit_oid']
    assert rebuilt['context']['reviews'][0]['id'] == failed['attempt']['id']
    # Loading all node facts must preserve fail-closed source/parent validation.
    def wrong_generation(tx):
        job = tx.get('node_job', diagnostic['test_job_ids'][0])
        return tx.put('node_job', job['id'], {**job, 'parent_generation': job['parent_generation'] + 1}, job['revision'])
    await env.store.command('fixture.lifecycle-rereview', 'wrong-parent', {}, wrong_generation)
    rejected = await disposition.build(run, reviewer)
    assert not rejected['ok']
    assert rejected['issues'][0]['code'] == 'producer_unresolved'


async def test_duplicate_findings_preserve_associations_and_edit_the_frozen_assertion_once(flow):
    env = await lifecycle_source(flow, duplicate_finding=True)
    batch = await env.store.read('review_contract_repair', 'batch')
    assert len(batch['actions']) == 4 and len(batch['repair_work_item_ids']) == 3
    await finish_repairs_and_assembly(env)
    checks = await env.store.list('test_migration_check')
    assert len(checks) == 2 and all(c['report']['ok'] for c in checks)
    assert all(len(c['report']['evidence']) == 1 for c in checks)


async def test_normal_test_writers_inherit_current_code_after_migration_without_resetting_usage(flow):
    from agentflow.control.coding_steps import CodingSteps
    from agentflow.control.scheduler import Scheduler
    from agentflow.repository.assembly import AssemblyManager

    env = await lifecycle_source(flow)
    validation, merged = await finish_repairs_and_assembly(env, account_usage=True)
    await finish_diagnostic(env, validation)
    for claim in [await env.workflow.claim_next('run', 'fixture', str(uuid4())) for _ in range(2)]:
        await finish_claim(env, claim)
    await finish_claim(env, await env.workflow.claim_next('run', 'fixture', str(uuid4())))
    scheduler = Scheduler(env.workflow, env.store, None, None, env.workflow.settings)
    scheduler.project_code = None
    snapshots = []
    for claim in [await env.workflow.claim_next('run', 'fixture', str(uuid4())) for _ in range(2)]:
        work, attempt, run = claim['work_item'], claim['attempt'], claim['run']
        assert work['id'] in {'api-tests', 'web-tests'}
        path, commit = await scheduler._source(run, work)
        assert commit == merged['commit_oid']
        before = await env.store.read('coding_work_budget', CodingSteps.budget_id('run', work['id']))
        assert before['step_count'] == 1 and before['active_seconds'] == 2
        control = await scheduler.coding_steps.prepare(run, work, attempt, commit, 2048)
        assert control['base_commit'] == commit and control['step_number'] == 2
        assert control['max_active_seconds'] == before['max_active_seconds'] - 2
        folder = env.source.parent / ('formal-' + work['id'])
        await env.repository.clone_snapshot(path, folder, commit)
        test_file = folder / work['write_paths'][0]
        test_file.write_text(test_file.read_text() + "\ntest('additional formal coverage',()=>expect(actual()).toBe(1));\n")
        content = {'status': 'complete', 'summary': 'New accepted case implemented', 'next_action': ''}
        sink = env.workflow.settings.data_dir / 'attempt_artifacts' / attempt['id']
        sink.mkdir(parents=True)
        result_file = sink / 'codex_final.json'
        result_file.write_text(json.dumps(content))
        class Runtime:
            async def execute_task(self, task):
                return {'execution_status': 'completed', 'result': content, 'summary': content['summary'],
                    'artifacts': [{'path': str(result_file)}], 'active_seconds': 3.0,
                    'observed_tool_calls': 2, 'tool_observation_complete': True}
        scheduler.runtime = Runtime()
        task = {'run_id': 'run', 'work_item_id': work['id'], 'attempt_id': attempt['id'], 'step': work['step'],
            'workspace': str(folder), 'source_commit': commit, 'allowed_write_paths': work['write_paths'],
            'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint'],
            'coding_step': control}
        await scheduler._execute_existing(task)
        completed = await env.store.read('work_item', work['id'])
        assert completed['status'] == 'completed', (completed.get('blocking_reason'), completed.get('failure_diagnostic'))
        after = await env.store.read('coding_work_budget', before['id'])
        assert after['step_count'] == 2 and after['active_seconds'] == 5 and after['observed_tool_calls'] == 3
        snapshot = await env.store.read('code_snapshot', attempt['id'])
        assert snapshot['base_oid'] == commit
        snapshots.append(snapshot)
    combined = await AssemblyManager(env.workflow.settings.data_dir).assemble(
        snapshots, Path(snapshots[0]['repository_path']), commit, 'formal-tests-after-migration')
    for path in ['public/index.html', 'tests/api.spec.mjs', 'tests/web.spec.mjs']:
        actual = env.repository._run(Path(combined['repository_path']), ['show', combined['commit_oid'] + ':' + path])
        if path.startswith('tests/'):
            assert b'additional formal coverage' in actual
        else:
            assert actual == env.repository._run(Path(merged['repository_path']), ['show', merged['commit_oid'] + ':' + path])

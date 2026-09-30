"""Late test defects return to original writers against the complete source tip."""
import json
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from test_owner_review_dynamic_repairs import append_dynamic
from test_owner_review_source_repair import env as env
from test_owner_review_source_repair import make_aggregate
from test_parallel_remediation import CollectedFixtureRuntime, update
from test_review_baseline_recovery import stopped_task

from agentflow.control.recovery import RunRecoveryService, _ReadState
from agentflow.control.remediation import ReviewRemediation
from agentflow.control.review_producer import review_producer
from agentflow.control.scheduler import Scheduler
from agentflow.domain.planning import CODING_STEPS


@pytest_asyncio.fixture
async def late(env):
    config = {'target_config_id': 'target-web', 'app_target': 'web', 'os_name': 'Linux',
        'os_version_constraint': '24.04', 'cpu_architecture': 'x86_64',
        'required_display_protocol': 'not_required', 'required_device_mode': 'not_required'}
    spec = {'schema_version': 1, 'targets': [{'target_config_id': 'target-web',
        'build': {'adapter': 'web', 'output_paths': {'product': 'build/app', 'test': 'build/tests'}},
        'unit': {'adapter': 'web', 'test_kind': 'unit', 'unit_project': 'tests/unit.test.mjs', 'expected_case_ids': ['range::0-99']},
        'integration': {'adapter': 'web', 'test_kind': 'integration', 'expected_case_ids': ['web::persists']}}]}
    (env.source / 'tests/api.spec.mjs').write_text('export const apiSummaryAssertion = false;\n')
    (env.source / 'tests/unit-spare.mjs').write_text('export const independentUnitCase = true;\n')
    (env.source / 'agentflow.project.json').write_text(json.dumps(spec))
    frozen = await env.repository.freeze_workspace(env.source, env.commit, 'original fixture execution contract')
    env.commit = frozen['commit_oid']
    for identity in ['implementation', 'producer', 'review']:
        context = await env.store.read('dispatch_context', identity + '-attempt')
        await update(env.store, 'dispatch_context', context['id'], task={**context['task'],
            'source_commit': env.commit, 'workspace': str(env.source)})
        if identity != 'review':
            await update(env.store, 'code_snapshot', identity + '-attempt', commit_oid=env.commit,
                tree_oid=frozen['tree_oid'], base_oid=env.commit, repository_path=str(env.source))
    await update(env.store, 'review', 'review-attempt', reviewed_commit=env.commit)
    plan = await env.store.read('plan', 'plan')
    await update(env.store, 'plan', 'plan', product_contract={'stack': 'node_web_api', 'product_id': 'product'},
        target_configs=[config], app_targets=['web'], actual_steps=['unit_test_execution', 'integration_test_execution'])
    await update(env.store, 'run', 'run', purpose='code_delivery')
    original = await env.store.read('work_item', 'producer')
    for key, step in [('unit-plan', 'unit_test_plan'), ('integration-plan', 'integration_test_strategy')]:
        phase = 'unit' if key == 'unit-plan' else 'integration'
        cases = [{'case_id': key, 'requirement_id': 'range', 'target_config_id': 'target-web', 'phase': phase,
            'framework_case_ids': ['range::0-99'] if phase == 'unit' else ['web::persists'],
            'assertions': ['return the entire exact requested byte interval']}]
        blob = await env.artifacts.put_bytes(json.dumps({'result': {'test_cases': cases}}).encode())
        await update(env.store, 'work_item', key, **{k: v for k, v in original.items() if k not in {'id', 'revision'}},
            **{})
        await update(env.store, 'work_item', key, key=key, step=step, write_paths=[], dependencies=['implementation'],
            attempt_id=key + '-attempt', artifact_ids=[key + '-artifact'])
        work = await env.store.read('work_item', key)
        await update(env.store, 'attempt', key + '-attempt', run_id='run', iteration_id='iteration', work_item_id=key,
            status='completed', generation=1, fencing_token=1, input_fingerprint=work['input_fingerprint'])
        await update(env.store, 'artifact', key + '-artifact', run_id='run', work_item_id=key, generation=1,
            step=step, stale=False, name=key + '.json', digest=blob['id'])
    await update(env.store, 'work_item', 'producer', dependencies=['implementation', 'unit-plan', 'integration-plan'])
    await update(env.store, 'plan', 'plan', work_specs=[
        {**s, 'dependencies': ['implementation', 'unit-plan', 'integration-plan']} if s['key'] == 'producer' else s
        for s in plan['work_specs']])
    await update(env.store, 'work_item', 'review', quality_result='passed')
    await update(env.store, 'attempt', 'review-attempt', quality_result='passed')
    await update(env.store, 'review', 'review-attempt', quality_result='passed', blocking_findings=[])
    producer = await env.store.read('work_item', 'producer')
    tip = await append_dynamic(env, producer, 'product', 1)
    reviewer = await env.store.read('work_item', 'product-1-review')
    await update(env.store, 'work_item', reviewer['id'], status='pending', quality_result='unknown', attempt_id=None)
    await update(env.store, 'work_item', 'execution', dependencies=['review', reviewer['id']], status='blocked')
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert claim['work_item']['id'] == reviewer['id']
    scheduler = Scheduler(env.workflow, env.store, CollectedFixtureRuntime(env, [
        {'path': 'tests/web.spec.mjs', 'severity': 'blocking', 'description': 'Strengthen frozen range cases; preserve IDs.'}]), None, env.settings)
    source, commit = await scheduler._source(claim['run'], claim['work_item'])
    _, task = await stopped_task(env, claim, source, commit, failed=False)
    await scheduler._execute_existing(task)
    env.tip, env.tip_commit, env.late_review = tip, commit, reviewer['id']
    env.remediation = ReviewRemediation(env.store, env.workflow)
    return env


async def test_late_review_reuses_original_writer_and_preserves_complete_source(late):
    env = late
    original = await env.store.read('work_item', 'producer')
    protected = {kind: await env.store.list(kind) for kind in ('plan', 'attempt', 'coding_work_budget', 'budget_account')}
    plans = {key: await env.store.read('work_item', key) for key in ('unit-plan', 'integration-plan')}
    result = await env.remediation.repair(env.late_review)
    assert result and result['mode'] == 'late_test_owner'
    assert result['repair_work_item_ids'] == ['producer']
    repaired = await env.store.read('work_item', 'producer')
    assert repaired['generation'] == original['generation'] + 1
    assert repaired['write_paths'] == original['write_paths']
    assert repaired['approval_required'] == original['approval_required']
    point = await env.store.read('code_snapshot', repaired['payload']['repair_base_snapshot_id'])
    assert point['commit_oid'] == env.tip_commit
    assert (Path(point['repository_path']) / 'public/shell/layers.mjs').read_text() == 'export const changed1 = 1;\n'
    assert {kind: await env.store.list(kind) for kind in protected} == protected
    assert {key: await env.store.read('work_item', key) for key in plans} == plans
    assert (await env.store.read('review', result['review_attempt_id']))['quality_result'] == 'failed'
    assert (await env.store.read('work_item', env.tip['id']))['archived'] is True
    assert await env.remediation.repair(env.late_review) is None


@pytest.mark.parametrize('path', ['tests/playwright.config.mjs', '../tests/web.spec.mjs', 'tests/missing.mjs'])
async def test_late_routing_does_not_expand_test_ownership(late, path):
    env = late
    review = await env.store.read('work_item', env.late_review)
    await update(env.store, 'review', review['attempt_id'], blocking_findings=[{'path': path, 'severity': 'blocking'}])
    before = {kind: await env.store.list(kind) for kind in ('work_item', 'review', 'code_snapshot', 'review_repair')}
    assert await env.remediation.repair(env.late_review) is None
    assert {kind: await env.store.list(kind) for kind in before} == before


async def collect_next(env, edits=None, findings=None):
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert claim['attempt'], claim
    if callable(findings):
        findings = findings(claim['work_item'])
    scheduler = Scheduler(env.workflow, env.store, CollectedFixtureRuntime(env, findings), None, env.settings)
    source, commit = await scheduler._source(claim['run'], claim['work_item'])
    if claim['work_item'].get('kind') == 'aggregation' and claim['work_item']['step'] in CODING_STEPS:
        await scheduler._finish_assembly(claim)
    else:
        workspace, task = await stopped_task(env, claim, source, commit, failed=False)
        for path, contents in (edits or {}).items():
            (workspace / path).write_text(contents)
        await scheduler._execute_existing(task)
    work = await env.store.read('work_item', claim['work_item']['id'])
    assert work['status'] == 'completed', work
    return work, commit


async def test_repair_is_collected_reviewed_and_failed_again_from_new_full_source(late):
    env = late
    first = await env.remediation.repair(env.late_review)
    repaired, old_commit = await collect_next(env, {'tests/web.spec.mjs': 'export const stronger = 1;\n'})
    assert repaired['id'] == 'producer' and old_commit == env.tip_commit
    current = await env.store.read('code_snapshot', repaired['attempt_id'])
    assert current['commit_oid'] != old_commit
    collected = []
    for _ in range(2):
        work, commit = await collect_next(env, findings=[{'path': 'tests/web.spec.mjs', 'severity': 'blocking',
            'description': 'One remaining assertion must be strengthened'}])
        assert commit == current['commit_oid']
        collected.append(work)
    late_review = next(w for w in collected if w['id'] == env.late_review)
    state = await RunRecoveryService(env.store, env.workflow)._read('run')
    assert review_producer(_ReadState(state), state['run'][0], late_review)['id'] == 'producer'
    second = await env.remediation.repair(env.late_review)
    assert second and second['mode'] == 'late_test_owner'
    assert second['previous_routing_receipt_id'] == first['id']
    assert second['base_commit'] == current['commit_oid']
    assert not second['superseded_work_item_ids']
    again, source = await collect_next(env, {'tests/web.spec.mjs': 'export const stronger = 2;\n'})
    assert source == current['commit_oid']
    for _ in range(2):
        review, _ = await collect_next(env)
        assert review['quality_result'] == 'passed'
    execution = await env.store.read('work_item', 'execution')
    scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    path, final = await scheduler._source(await env.store.read('run', 'run'), execution)
    assert (path / 'tests/web.spec.mjs').read_text() == 'export const stronger = 2;\n'
    assert (path / 'public/shell/layers.mjs').read_text() == 'export const changed1 = 1;\n'
    assert final == (await env.store.read('code_snapshot', again['attempt_id']))['commit_oid']
    from agentflow.control.execution_pipeline import ExecutionPipeline
    from agentflow.execution.service import NodeService
    nodes = NodeService(env.store, env.settings.data_dir / 'nodes')
    pipeline = ExecutionPipeline(env.store, env.workflow, nodes, scheduler._source)
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert claim['work_item']['id'] == 'execution'
    await pipeline.begin(claim)
    candidates = [c for c in await env.store.list('candidate') if c.get('source_commit') == final]
    assert len(candidates) == 1 and candidates[0]['source_manifest']['source_commit'] == final
    assert {case for mapping in candidates[0]['matrix_mappings'].values() for case in mapping['framework_case_ids']} == {'range::0-99', 'web::persists'}


async def test_multiple_original_children_repair_and_aggregate_without_replaying_source_work(late):
    env = late
    dependencies = (await env.store.read('work_item', 'producer'))['dependencies']
    await make_aggregate(env)
    await update(env.store, 'work_item', 'producer', original_dependencies=dependencies)
    reviewer = await env.store.read('work_item', env.late_review)
    await update(env.store, 'review', reviewer['attempt_id'], blocking_findings=[
        {'path': path, 'severity': 'blocking', 'description': 'Strengthen accepted cases'}
        for path in ['tests/web.spec.mjs', 'tests/unit.test.mjs']])
    result = await env.remediation.repair(env.late_review)
    assert result and result['repair_work_item_ids'] == ['producer-api', 'producer-web']
    for path in ['tests/unit.test.mjs', 'tests/web.spec.mjs']:
        work, source = await collect_next(env, {path: 'export const fixed = 1;\n'})
        assert work['write_paths'] == [path] and source == env.tip_commit
    aggregate, _ = await collect_next(env)
    assert aggregate['id'] == 'producer'
    snapshot = await env.store.read('code_snapshot', aggregate['attempt_id'])
    assert all((Path(snapshot['repository_path']) / p).read_text() == 'export const fixed = 1;\n'
        for p in ['tests/unit.test.mjs', 'tests/web.spec.mjs'])
    assert (Path(snapshot['repository_path']) / 'public/shell/layers.mjs').read_text() == 'export const changed1 = 1;\n'


async def add_unit_phase(env):
    producer = await env.store.read('work_item', 'producer')
    work = await update(env.store, 'work_item', 'unit-writer', **{k: v for k, v in producer.items() if k not in {'id', 'revision'}})
    work = await update(env.store, 'work_item', work['id'], key='unit-writer', step='unit_test_implementation', role='unit_test',
        write_paths=['tests/unit.test.mjs'], attempt_id='unit-writer-attempt')
    attempt = await update(env.store, 'attempt', work['attempt_id'], run_id='run', iteration_id='iteration', work_item_id=work['id'],
        generation=1, fencing_token=1, input_fingerprint=work['input_fingerprint'], status='completed')
    workspace, _ = await stopped_task(env, {'work_item': work, 'attempt': attempt}, env.source, env.commit, failed=False)
    old = await env.store.read('code_snapshot', producer['attempt_id'])
    await update(env.store, 'code_snapshot', attempt['id'], **{k: v for k, v in old.items() if k not in {'id', 'revision', 'work_item_id', 'repository_path'}},
        work_item_id=work['id'], repository_path=str(workspace))
    await update(env.store, 'work_item', 'review', dependencies=[work['id']])
    await update(env.store, 'work_item', 'producer', write_paths=['tests/web.spec.mjs'], dependencies=[*producer['dependencies'], 'review'])
    context = await env.store.read('dispatch_context', producer['attempt_id'])
    await update(env.store, 'dispatch_context', context['id'], task={**context['task'], 'allowed_write_paths': ['tests/web.spec.mjs']})
    plan = await env.store.read('plan', 'plan')
    await update(env.store, 'plan', 'plan', authorized_rework_steps=[*plan['authorized_rework_steps'], 'unit_test_implementation'],
        work_specs=[*plan['work_specs'], {'key': work['key'], 'step': work['step'], 'role': work['role'],
            'dependencies': work['dependencies'], 'write_paths': work['write_paths']}])
    review = await env.store.read('work_item', env.late_review)
    findings = [{'path': path, 'severity': 'blocking', 'description': 'Strengthen ' + path}
        for path in ['tests/unit.test.mjs', 'tests/web.spec.mjs']]
    await update(env.store, 'review', review['attempt_id'], blocking_findings=findings)
    return findings


async def test_mixed_phase_findings_route_sequentially_preserving_unaffected_code(late):
    env = late
    findings = await add_unit_phase(env)
    integration = await env.store.read('work_item', 'producer')
    first = await env.remediation.repair(env.late_review)
    assert first and first['repair_work_item_ids'] == ['unit-writer']
    assert first['deferred_findings'] == findings[1:]
    assert await env.store.read('work_item', 'producer') == integration
    phase_review = await env.store.read('work_item', 'review')
    assert 'Strengthen tests/web.spec.mjs' not in phase_review['payload']['change_expectation']
    await collect_next(env, {'tests/unit.test.mjs': 'export const unitFixed = 1;\n'})
    for _ in range(2):
        await collect_next(env, findings=lambda work: findings[1:] if work['id'] == env.late_review else [])
    second = await env.remediation.repair(env.late_review)
    assert second and second['repair_work_item_ids'] == ['producer'] and second['deferred_findings'] == []
    assert (await env.store.read('work_item', 'review'))['quality_result'] == 'passed'
    work, _ = await collect_next(env, {'tests/web.spec.mjs': 'export const webFixed = 1;\n'})
    source = await env.store.read('code_snapshot', work['attempt_id'])
    path = Path(source['repository_path'])
    assert (path / 'tests/unit.test.mjs').read_text() == 'export const unitFixed = 1;\n'
    assert (path / 'public/shell/layers.mjs').read_text() == 'export const changed1 = 1;\n'
    review, commit = await collect_next(env)
    assert review['id'] == env.late_review and review['quality_result'] == 'passed'
    assert commit == source['commit_oid']


async def test_intermediate_review_cannot_redirect_to_its_descendant_test_phase(late):
    env = late
    findings = await add_unit_phase(env)
    assert await env.remediation.repair(env.late_review)
    await collect_next(env, {'tests/unit.test.mjs': 'export const unitFixed = 1;\n'})
    for _ in range(2):
        await collect_next(env, findings=lambda work: findings[1:] if work['id'] == 'review' else [])
    before = {kind: await env.store.list(kind) for kind in ('work_item', 'review', 'code_snapshot', 'review_repair')}
    assert await env.remediation.repair('review') is None
    assert {kind: await env.store.list(kind) for kind in before} == before


@pytest.mark.parametrize('damage', ['active', 'approval', 'pending_decision', 'uncertain_model', 'budget',
    'manual_retry', 'review_identity', 'source_dirty', 'owner_scope', 'ambiguous', 'restore', 'process_receipt'])
async def test_late_route_fails_closed_without_mutation_for_gates_and_bad_authority(late, damage):
    env = late
    if damage == 'active':
        await update(env.store, 'work_item', 'execution', status='running')
    elif damage == 'approval':
        await update(env.store, 'work_item', 'execution', status='waiting_approval')
    elif damage == 'pending_decision':
        await update(env.store, 'approval', 'pending', run_id='run', work_item_id='execution', stale=False, decision=None)
    elif damage == 'uncertain_model':
        await update(env.store, 'model_invocation', 'unknown', run_id='run', state='uncertain')
    elif damage == 'budget':
        from agentflow.models.budget import account_id
        await update(env.store, 'budget_account', account_id('run', 'run'), settled_micros=1000)
    elif damage == 'manual_retry':
        await update(env.store, 'work_execution_budget_adjustment', 'manual', run_id='run', work_item_id='producer',
            work_generation=1, requires_explicit_retry=True)
    elif damage == 'review_identity':
        review = await env.store.read('work_item', env.late_review)
        await update(env.store, 'attempt', review['attempt_id'], fencing_token=900)
    elif damage == 'source_dirty':
        source = await env.store.read('code_snapshot', env.tip['attempt_id'])
        (Path(source['repository_path']) / 'tests/web.spec.mjs').write_text('uncommitted changes\n')
    elif damage == 'owner_scope':
        await update(env.store, 'work_item', 'producer', write_paths=['.'])
    elif damage == 'ambiguous':
        original = await env.store.read('work_item', 'producer')
        await update(env.store, 'work_item', 'ambiguous', **{k: v for k, v in original.items() if k not in {'id', 'revision'}})
    elif damage == 'restore':
        await update(env.store, 'run', 'run', restore_reconciliation_required=True)
    else:
        process = await env.store.read('supervised_attempt', env.tip['attempt_id'])
        await update(env.store, 'supervised_attempt', process['id'], nonce='forged-nonce')
    before = {kind: await env.store.list(kind) for kind in ('run', 'work_item', 'review', 'code_snapshot', 'review_repair', 'budget_account')}
    assert await env.remediation.repair(env.late_review) is None
    assert {kind: await env.store.list(kind) for kind in before} == before


async def seed_original_usage(env, work_id='producer'):
    from agentflow.control.coding_steps import CodingSteps
    run = await env.store.read('run', 'run')
    run = await update(env.store, 'run', 'run', budget_limit={**run['budget_limit'], 'max_active_seconds': 100, 'max_tool_calls': 20})
    original = await update(env.store, 'work_item', work_id, status='running')
    attempt = await env.store.read('attempt', original['attempt_id'])
    steps = CodingSteps(env.store, env.settings, env.repository)
    snapshot = await env.store.read('code_snapshot', attempt['id'])
    control = await steps.prepare(run, original, attempt, snapshot['base_oid'], 512)
    context = await env.store.read('dispatch_context', attempt['id'])
    task = {**context['task'], 'coding_step': control}
    await update(env.store, 'dispatch_context', attempt['id'], task=task)
    await steps.account(task, {'active_seconds': 31, 'observed_tool_calls': 7, 'tool_observation_complete': True})
    await update(env.store, 'work_item', work_id, status='completed')
    return steps, control, await env.store.read('coding_work_budget', control['budget_id'])


async def test_original_nonzero_coding_allowance_is_reused_with_full_source_diff_base(late):
    env = late
    steps, old, budget = await seed_original_usage(env)
    assert budget['step_count'] == 1 and budget['observed_tool_calls'] == 7 and budget['active_seconds'] == 31
    assert await env.remediation.repair(env.late_review)
    assert await env.store.read('coding_work_budget', budget['id']) == budget
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert claim['work_item']['id'] == 'producer'
    scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    _, source = await scheduler._source(claim['run'], claim['work_item'])
    fresh = await steps.prepare(claim['run'], claim['work_item'], claim['attempt'], source, 512)
    assert fresh['budget_id'] == old['budget_id']
    assert fresh['max_active_seconds'] == 69 and fresh['max_tool_calls'] == 13 and fresh['step_number'] == 2
    assert fresh['base_commit'] == env.tip_commit and fresh['source_commit'] == env.tip_commit
    assert await env.store.read('coding_work_budget', budget['id']) == budget


@pytest.mark.parametrize('exhausted', ['time', 'tools', 'steps', 'unknown'])
async def test_late_owner_repair_cannot_bypass_original_cumulative_limits(late, exhausted):
    env = late
    _, _, budget = await seed_original_usage(env)
    fields = {'max_active_seconds': 31} if exhausted == 'time' else {'max_tool_calls': 7} if exhausted == 'tools' else {
        'max_steps': 1} if exhausted == 'steps' else {'uncertain': True}
    await update(env.store, 'coding_work_budget', budget['id'], **fields)
    before = {kind: await env.store.list(kind) for kind in ('coding_work_budget', 'coding_step_usage', 'work_item', 'review_repair')}
    assert await env.remediation.repair(env.late_review) is None
    assert {kind: await env.store.list(kind) for kind in before} == before


async def test_late_repair_revalidates_git_inside_atomic_apply_and_rolls_back(late, monkeypatch):
    from agentflow.common import DomainError
    from agentflow.control.late_test_review import LateTestReviewRepair
    env = late
    original = LateTestReviewRepair._filesystem
    calls = 0
    def race(service, state, proof):
        nonlocal calls
        calls += 1
        if calls == 2:
            (Path(proof['source']['repository_path']) / 'tests/web.spec.mjs').write_text('changed during proof\n')
        return original(service, state, proof)
    monkeypatch.setattr(LateTestReviewRepair, '_filesystem', race)
    before = {kind: await env.store.list(kind) for kind in ('run', 'work_item', 'review_repair', 'work_revision', 'code_snapshot')}
    with pytest.raises(DomainError):
        await env.remediation.repair(env.late_review)
    assert {kind: await env.store.list(kind) for kind in before} == before


@pytest.mark.parametrize('field,value', [('base_snapshot_id', 'missing-source'), ('base_commit', 'older'),
    ('previous_routing_receipt_id', 'self')])
async def test_corrupt_routing_receipt_cannot_authorize_second_round(late, field, value):
    env = late
    first = await env.remediation.repair(env.late_review)
    await collect_next(env, {'tests/web.spec.mjs': 'export const stronger = 1;\n'})
    for _ in range(2):
        await collect_next(env, findings=[{'path': 'tests/web.spec.mjs', 'severity': 'blocking', 'description': 'remaining'}])
    await update(env.store, 'review_repair', first['id'], **{field: env.commit if value == 'older' else first['id'] if value == 'self' else value})
    before = {kind: await env.store.list(kind) for kind in ('run', 'work_item', 'review_repair', 'code_snapshot')}
    assert await env.remediation.repair(env.late_review) is None
    assert {kind: await env.store.list(kind) for kind in before} == before


async def test_late_checkpoint_survives_recovery_before_new_dispatch(late):
    env = late
    receipt = await env.remediation.repair(env.late_review)
    recovery = RunRecoveryService(env.store, env.workflow)
    state = await recovery._read('run')
    points = await recovery._checkpoints(state, {'root_work_item_ids': ['producer']},
        freeze=True, recovery_id='later-owner-recovery')
    assert len(points) == 1 and points[0]['commit_oid'] == env.tip_commit
    assert points[0]['record']['source_review_snapshot_id'] == receipt['checkpoint_alias_ids']['producer']
    assert points[0]['record']['source_attempt_id'] == 'producer-attempt'
    assert points[0]['record']['source_review_write_paths'] == ['tests']


async def test_failure_analysis_explains_late_frozen_file_refusal(late):
    from agentflow.control.failure_remediation import FailureRemediation
    env = late
    review = await env.store.read('work_item', env.late_review)
    await update(env.store, 'review', review['attempt_id'], blocking_findings=[{
        'severity': 'blocking', 'path': 'tests/playwright.config.mjs', 'description': 'Change frozen config'}])
    analysis = await FailureRemediation(env.store, env.workflow, review=env.remediation).analyze(env.late_review)
    assert analysis['status'] == 'blocked'
    assert any(b['code'] == 'late_test_repair_unavailable' for b in analysis['blockers'])


async def test_late_route_does_not_claim_retained_integration_reviews_before_unit_repair(late):
    env = late
    await add_unit_phase(env)
    template = await env.store.read('work_item', 'review')
    for index in range(4):
        await update(env.store, 'work_item', 'integration-review-' + str(index),
            **{k: v for k, v in template.items() if k not in {'id', 'revision', 'dependencies', 'attempt_id'}},
            dependencies=['producer'], attempt_id=None)
    assert await env.remediation.repair(env.late_review)
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert claim['work_item']['id'] == 'unit-writer'
    retained = await env.store.read('work_item', 'producer')
    assert retained['status'] == 'completed'
    for _ in range(4):
        assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['attempt'] is None
    # Collect the active original owner, then the original unit phase review.
    scheduler = Scheduler(env.workflow, env.store, CollectedFixtureRuntime(env), None, env.settings)
    source, commit = await scheduler._source(claim['run'], claim['work_item'])
    workspace, task = await stopped_task(env, claim, source, commit, failed=False)
    (workspace / 'tests/unit.test.mjs').write_text('export const unitFixed = 1;\n')
    await scheduler._execute_existing(task)
    next_claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert next_claim['work_item']['id'] in {'review', env.late_review}
    before_unit_review = await env.store.read('work_item', 'review')
    assert before_unit_review['status'] in {'pending', 'running'}
    for index in range(4):
        assert (await env.store.read('work_item', 'integration-review-' + str(index)))['status'] == 'pending'
    assert await env.store.read('work_item', 'producer') == retained

"""Original phase ownership survives a newer full source from another phase."""
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from test_late_test_review_routing import add_unit_phase, collect_next, seed_original_usage
from test_late_test_review_routing import late as late
from test_owner_review_source_repair import env as env
from test_owner_review_source_repair import make_aggregate
from test_parallel_remediation import update
from test_review_baseline_recovery import stopped_task

from agentflow.common import DomainError, canonical_digest
from agentflow.control.recovery import RunRecoveryService, _ReadState
from agentflow.control.review_producer import review_cohort, sealed_review_group
from agentflow.domain.expansion import StageExpander


async def unit_aggregate(env):
    original = await update(env.store, 'work_item', 'unit-writer',
        write_paths=['tests/unit.test.mjs', 'tests/unit-spare.mjs'])
    snapshot = await env.store.read('code_snapshot', original['attempt_id'])
    ids = ['unit-domain', 'unit-spare']
    for identity, path in zip(ids, original['write_paths'], strict=True):
        child = await update(env.store, 'work_item', identity, **{**{k: v for k, v in original.items() if k not in {'id', 'revision'}},
            'key': identity, 'kind': 'stage_child', 'parent_stage_id': original['id'], 'write_paths': [path], 'attempt_id': identity + '-attempt'})
        attempt = await update(env.store, 'attempt', child['attempt_id'], run_id='run', work_item_id=identity, status='completed',
            generation=1, fencing_token=1, input_fingerprint=child['input_fingerprint'], iteration_id='iteration')
        workspace, _ = await stopped_task(env, {'work_item': child, 'attempt': attempt}, env.source, env.commit, failed=False)
        await update(env.store, 'code_snapshot', attempt['id'], **{**{k: v for k, v in snapshot.items() if k not in {'id', 'revision'}},
            'work_item_id': identity, 'repository_path': str(workspace)})
    fingerprint = canonical_digest({'fixture': 'original unit aggregate'})
    await update(env.store, 'stage_expansion', 'unit-expansion', run_id='run', stage_work_item_id=original['id'],
        original_stage=original, input_fingerprint=fingerprint, child_ids=ids)
    await update(env.store, 'work_item', original['id'], kind='aggregation', expanded_child_ids=ids,
        original_dependencies=original['dependencies'], original_write_paths=original['write_paths'],
        write_paths=[], dependencies=ids, expansion_fingerprint=fingerprint)
    plan = await env.store.read('plan', 'plan')
    await update(env.store, 'plan', 'plan', work_specs=[{**s, 'write_paths': original['write_paths']}
        if s['key'] == original['key'] else s for s in plan['work_specs']])


@pytest_asyncio.fixture
async def own_phase(late):
    env = late
    findings = await add_unit_phase(env)
    await unit_aggregate(env)
    producer = await update(env.store, 'work_item', 'producer', write_paths=['tests/web.spec.mjs', 'tests/api.spec.mjs'])
    await make_aggregate(env)
    await update(env.store, 'work_item', 'producer', original_dependencies=producer['dependencies'],
        original_write_paths=producer['write_paths'])
    await update(env.store, 'work_item', 'producer-api', write_paths=['tests/api.spec.mjs'])
    context = await env.store.read('dispatch_context', 'producer-api-attempt')
    await update(env.store, 'dispatch_context', context['id'], task={**context['task'], 'allowed_write_paths': ['tests/api.spec.mjs']})
    for identity in ['implementation', 'producer', 'unit-writer', 'unit-domain', 'unit-spare', 'producer-api', 'producer-web']:
        work = await env.store.read('work_item', identity)
        await update(env.store, 'code_snapshot', work['attempt_id'], base_oid=env.project['base_commit'])
        context = await env.store.read('dispatch_context', work['attempt_id'])
        if context:
            await update(env.store, 'dispatch_context', context['id'], task={**context['task'], 'source_commit': env.project['base_commit']})
    plan = await env.store.read('plan', 'plan')
    await update(env.store, 'plan', 'plan', actual_steps=[*plan['actual_steps'], 'code_review'],
        work_specs=[*plan['work_specs'], {'key': 'integration-review', 'step': 'code_review', 'role': 'review', 'dependencies': ['producer']}])
    template = await env.store.read('work_item', 'review')
    parent = await update(env.store, 'work_item', 'integration-review', **{**{k: v for k, v in template.items() if k not in {'id', 'revision'}},
        'key': 'integration-review', 'dependencies': ['producer'], 'status': 'pending', 'quality_result': 'unknown', 'attempt_id': None})
    expansion = await StageExpander(env.store).expand('run', parent['id'], [
        {'key': 'facet-' + str(n), 'goal': 'Review original integration tests', 'write_paths': [], 'review_focus': 'current_code'}
        for n in range(4)], str(uuid4()), parent['revision'])
    for _ in range(5):
        await collect_next(env)
    await update(env.store, 'work_item', 'execution', dependencies=['review', env.late_review, parent['id']])
    late_review = await env.store.read('work_item', env.late_review)
    await update(env.store, 'review', late_review['attempt_id'], blocking_findings=findings[:1])
    first = await env.remediation.repair(env.late_review)
    assert first and first['repair_work_item_ids'] == ['unit-domain']
    await collect_next(env, {'tests/unit.test.mjs': 'export const unitFixed = 1;\n'})
    unit, _ = await collect_next(env)
    assert unit['id'] == 'unit-writer' and unit['kind'] == 'aggregation'
    api_finding = [{'severity': 'blocking', 'path': 'tests/api.spec.mjs',
        'description': 'IT-API-NOTE-08 must reject body and media fields in each paginated summary row'}]
    for _ in range(7):
        await collect_next(env, findings=lambda work: api_finding if work['id'] == parent['id'] else [])
    parent = await env.store.read('work_item', parent['id'])
    assert parent['status'] == 'completed' and parent['quality_result'] == 'failed'
    assert (await env.store.read('work_item', env.late_review))['quality_result'] == 'passed'
    env.phase_parent, env.phase_children, env.first_route = parent, expansion['work_item_ids'], first
    env.unit_source = await env.store.read('code_snapshot', unit['attempt_id'])
    return env


async def test_original_aggregate_review_routes_its_own_api_owner_from_unit_source(own_phase):
    env = own_phase
    state = await RunRecoveryService(env.store, env.workflow)._read('run')
    cohort = review_cohort(_ReadState(state), state['run'][0], env.phase_parent)
    assert cohort['producer']['id'] == 'unit-writer' and cohort['producer']['kind'] == 'aggregation'
    assert sealed_review_group(_ReadState(state), state['run'][0], env.phase_parent)['producer']['id'] == 'producer'
    _, _, budget = await seed_original_usage(env, 'producer-api')
    original = await env.store.read('work_item', 'producer-api')
    expansion = await env.store.list('stage_expansion')
    result = await env.remediation.repair(env.phase_parent['id'])
    assert result and result['mode'] == 'late_test_owner'
    assert result['routing_kind'] == 'original_test_phase'
    assert result['repair_work_item_ids'] == ['producer-api']
    assert result['producer_work_item_id'] == 'unit-writer'
    assert result['owner_stage_work_item_ids'] == ['producer']
    assert result['base_commit'] == env.unit_source['commit_oid']
    assert result['full_source_review_work_item_id'] == env.late_review
    assert result['previous_routing_receipt_id'] == env.first_route['id']
    assert (await env.store.read('work_item', 'producer-api'))['generation'] == original['generation'] + 1
    assert (await env.store.read('work_item', 'producer-api'))['write_paths'] == ['tests/api.spec.mjs']
    assert await env.store.read('coding_work_budget', budget['id']) == budget
    for identity in [env.phase_parent['id'], *env.phase_children]:
        work = await env.store.read('work_item', identity)
        assert work['status'] == 'pending'
        assert work['dependencies'] == (env.phase_children if identity == env.phase_parent['id'] else ['producer'])
    assert await env.store.list('stage_expansion') == expansion
    repaired, commit = await collect_next(env, {'tests/api.spec.mjs': 'export const apiSummaryAssertion = true;\n'})
    assert commit == env.unit_source['commit_oid'] and repaired['id'] == 'producer-api'
    aggregate, _ = await collect_next(env)
    assert aggregate['id'] == 'producer'
    full = await env.store.read('code_snapshot', aggregate['attempt_id'])
    assert (Path(full['repository_path']) / 'tests/unit.test.mjs').read_text() == 'export const unitFixed = 1;\n'
    assert (Path(full['repository_path']) / 'public/shell/layers.mjs').read_text() == 'export const changed1 = 1;\n'
    for _ in range(6):
        work, commit = await collect_next(env)
        assert work['quality_result'] == 'passed' and commit == full['commit_oid']
    state = await RunRecoveryService(env.store, env.workflow)._read('run')
    parent = await env.store.read('work_item', env.phase_parent['id'])
    group = sealed_review_group(_ReadState(state), state['run'][0], parent)
    assert group and group['producer']['id'] == 'producer'
    assert await env.store.list('stage_expansion') == expansion
    from agentflow.control.execution_pipeline import ExecutionPipeline
    from agentflow.control.scheduler import Scheduler
    from agentflow.execution.service import NodeService
    scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    pipeline = ExecutionPipeline(env.store, env.workflow, NodeService(env.store, env.settings.data_dir / 'nodes'), scheduler._source)
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert claim['work_item']['id'] == 'execution'
    await pipeline.begin(claim)
    frozen = [c for c in await env.store.list('candidate') if c.get('source_commit') == full['commit_oid']]
    assert len(frozen) == 1
    assert {case for entry in frozen[0]['matrix_mappings'].values() for case in entry['framework_case_ids']} == {'range::0-99', 'web::persists'}


async def test_historical_peer_receipt_is_required_before_owner_dispatch(own_phase):
    from agentflow.control.scheduler import Scheduler
    env = own_phase
    peer = await env.store.read('work_item', env.phase_children[0])
    assert await env.remediation.repair(env.phase_parent['id'])
    await update(env.store, 'review', peer['attempt_id'], reviewed_commit=env.commit)
    run, work = await env.store.read('run', 'run'), await env.store.read('work_item', 'producer-api')
    with pytest.raises(DomainError):
        await Scheduler(env.workflow, env.store, None, None, env.settings)._source(run, work)


async def test_original_phase_group_rejects_bad_peer_source_scope_and_membership(own_phase):
    env = own_phase
    peer = await env.store.read('work_item', env.phase_children[0])
    raw = await env.store.read('review', peer['attempt_id'])
    attempt = await env.store.read('attempt', peer['attempt_id'])
    parent_review = await env.store.read('review', env.phase_parent['attempt_id'])
    expansion = next(e for e in await env.store.list('stage_expansion') if e['stage_work_item_id'] == env.phase_parent['id'])
    cases = [('work_item', peer, {'quality_result': 'failed'}),
        ('work_item', peer, {'archived': True}), ('review', raw, {'reviewed_commit': env.commit}),
        ('review', raw, {'stale': True}), ('attempt', attempt, {'fencing_token': attempt['fencing_token'] + 1}),
        ('stage_expansion', expansion, {'child_ids': env.phase_children[:3]}),
        ('review', parent_review, {'blocking_findings': [{'severity': 'blocking', 'path': 'tests/unit.test.mjs', 'description': 'Not this review phase'}]})]
    for kind, original, damage in cases:
        await update(env.store, kind, original['id'], **damage)
        before = {k: await env.store.list(k) for k in ('work_item', 'review', 'code_snapshot', 'review_repair', 'budget_account')}
        assert await env.remediation.repair(env.phase_parent['id']) is None, damage
        assert {k: await env.store.list(k) for k in before} == before
        await update(env.store, kind, original['id'], **{key: original.get(key) for key in damage})


async def test_own_phase_receipt_never_promotes_aggregate_to_cross_phase_gate(own_phase):
    env = own_phase
    first = await env.remediation.repair(env.phase_parent['id'])
    assert first['routing_kind'] == 'original_test_phase'
    await collect_next(env, {'tests/api.spec.mjs': 'export const apiSummaryAssertion = true;\n'})
    await collect_next(env)
    own_finding = [{'severity': 'blocking', 'path': 'tests/api.spec.mjs', 'description': 'Another exact summary assertion'}]
    for _ in range(6):
        await collect_next(env, findings=lambda work: own_finding if work['id'] == env.phase_parent['id'] else [])
    current = await env.store.read('work_item', env.phase_parent['id'])
    raw = await env.store.read('review', current['attempt_id'])
    await update(env.store, 'review', raw['id'], blocking_findings=[{
        'severity': 'blocking', 'path': 'tests/unit.test.mjs', 'description': 'Cross-phase ownership must remain blocked'}])
    before = await env.store.list('work_item')
    assert await env.remediation.repair(current['id']) is None
    assert await env.store.list('work_item') == before
    await update(env.store, 'review', raw['id'], blocking_findings=own_finding)
    second = await env.remediation.repair(current['id'])
    assert second and second['routing_kind'] == 'original_test_phase'
    assert second['full_source_review_work_item_id'] == env.late_review
    assert second['previous_routing_receipt_id'] == first['id']
    assert second['repair_work_item_ids'] == ['producer-api']


async def test_own_phase_checkpoint_recovery_rechecks_historical_peer_chain(own_phase):
    env = own_phase
    peer = await env.store.read('work_item', env.phase_children[0])
    assert await env.remediation.repair(env.phase_parent['id'])
    recovery = RunRecoveryService(env.store, env.workflow)
    state = await recovery._read('run')
    points = await recovery._checkpoints(state, {'root_work_item_ids': ['producer-api']},
        freeze=True, recovery_id='recovery-before-phase-dispatch')
    assert points[0]['commit_oid'] == env.unit_source['commit_oid']
    await update(env.store, 'review', peer['attempt_id'], quality_result='failed', blocking_findings=[{'path': 'tests/api.spec.mjs'}])
    state = await recovery._read('run')
    with pytest.raises(DomainError):
        await recovery._checkpoints(state, {'root_work_item_ids': ['producer-api']}, freeze=True, recovery_id='invalid-recovery')

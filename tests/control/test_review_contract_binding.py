"""Controller binding and shared-budget regression tests; no node/model execution."""
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

import pytest
from test_coding_steps import coding_env as coding_env
from test_recovery import env as recovery_env

from agentflow.common import DomainError, canonical_digest
from agentflow.control.review_contract_binding import bound_producer, charge_owner_budget, repair_batch

__all__ = ['recovery_env']


class State:
    def __init__(self):
        self.rows = {}

    def get(self, kind, identity):
        return self.rows.get((kind, identity))

    def list(self, kind):
        return [v for (k, _), v in self.rows.items() if k == kind]

    def put(self, kind, identity, value, revision=None):
        old = self.get(kind, identity)
        assert revision == (old['revision'] if old else None)
        item = {**deepcopy(value), 'id': identity, 'revision': (old['revision'] + 1) if old else 1}
        self.rows[kind, identity] = item
        return item


def fixture():
    tx = State()
    run = tx.put('run', 'run', {'project_id': 'project', 'plan_id': 'plan', 'execution_state': 'running'})
    tx.put('plan', 'plan', {'state': 'started', 'started_run_id': 'run'})
    owner = tx.put('work_item', 'owner', {'run_id': 'run', 'project_id': 'project', 'step': 'integration_test_implementation',
        'role': 'integration_test', 'status': 'completed', 'generation': 1, 'write_paths': ['tests'], 'approval_required': True})
    original = tx.put('code_snapshot', 'original', {'run_id': 'run', 'work_item_id': 'original-author', 'generation': 1,
        'commit_oid': 'a' * 40, 'tree_oid': 'b' * 40, 'stale': False})
    author = tx.put('work_item', 'prd', {'status': 'completed', 'generation': 1, 'artifact_ids': ['doc'],
        'approval_required': True, 'approved_fingerprint': 'approved', 'output_fingerprint': 'approved'})
    approval = tx.put('approval', 'approval', {'work_item_id': 'prd', 'decision': 'approve', 'stale': False, 'fingerprint': 'approved'})
    tx.put('artifact', 'doc', {'digest': 'digest', 'stale': False})
    context = {'source_snapshot_id': 'original', 'snapshot': deepcopy(original), 'plan_version': {'id': 'plan', 'revision': 1},
        'accepted_documents': [{'artifact_id': 'doc', 'revision': 1, 'digest': 'digest'}],
        'acceptance_bindings': [{'author': deepcopy(author), 'approvals': [deepcopy(approval)]}],
        'owners': [{'work_item_id': 'owner', 'write_paths': ['tests'], 'planned_write_paths': ['tests'],
                    'work_version': deepcopy(owner)}]}
    specs = {}
    for name, step, dependencies in [('action', 'review_integration_migration', ['triage']),
                                      ('assembly', 'implementation', ['action']), ('validation', 'review_validation', ['assembly'])]:
        spec = {'key': name, 'run_id': 'run', 'project_id': 'project', 'kind': 'aggregation' if name == 'assembly' else 'stage',
            'step': step, 'role': 'integration_test' if name == 'action' else 'development', 'write_paths': ['tests'] if name == 'action' else [],
            'required': True, 'approval_required': name == 'action', 'dependencies': dependencies,
            'payload': {'review_contract_task': 'batch', 'review_contract_kind': name},
            'generation': 1, 'fencing_token': 1, 'input_fingerprint': name + '-input', 'quality_result': 'passed'}
        if name == 'action':
            spec['payload'].update(review_contract_owner='owner', review_contract_actions=[{'migrations': []}])
        specs[name] = deepcopy(spec)
        tx.put('work_item', name, {**spec, 'status': 'completed', 'attempt_id': name + '-attempt'})
        tx.put('attempt', name + '-attempt', {'run_id': 'run', 'work_item_id': name, 'status': 'completed',
            'generation': 1, 'fencing_token': 1, 'input_fingerprint': name + '-input', 'quality_result': 'passed'})
    reviewer = tx.put('work_item', 'reviewer', {'run_id': 'run', 'project_id': 'project', 'kind': 'stage', 'generation': 2,
        'step': 'code_review', 'role': 'review', 'dependencies': ['validation'], 'payload': {'review_contract_binding': 'batch'}})
    batch = tx.put('review_contract_repair', 'batch', {'actor': 'controller', 'run_id': 'run', 'state': 'reviewing',
        'stage_id': 'reviewer', 'work_specs': specs, 'context': context,
        'validation_work_item_id': 'validation', 'assembly_work_item_id': 'assembly',
        'review_binding': {'stage_id': 'reviewer', 'review_ids': ['reviewer'], 'minimum_generations': {'reviewer': 2}}})
    snapshot = tx.put('code_snapshot', 'assembly-attempt', {'run_id': 'run', 'work_item_id': 'assembly', 'generation': 1,
        'commit_oid': 'c' * 40, 'tree_oid': 'd' * 40, 'stale': False})
    manifest = {'source_commit': snapshot['commit_oid'], 'source_tree_oid': snapshot['tree_oid'], 'fingerprint': 'manifest'}
    diagnostic_batch = str(uuid5(NAMESPACE_URL, canonical_digest(('batch', 'validation-attempt', 'diagnostic'))))
    tx.put('review_diagnostic', 'diagnostic', {'run_id': 'run', 'batch_id': diagnostic_batch, 'state': 'passed',
        'source': deepcopy(snapshot), 'source_manifest': manifest, 'jobs': ['build', 'test'],
        'build_job_ids': ['build'], 'test_job_ids': ['test'], 'blockers': []})
    for job_id in ['build', 'test']:
        tx.put('node_job', job_id, {'run_id': 'run', 'kind': job_id, 'state': 'completed', 'quality_result': 'passed',
            'parent_work_item_id': 'validation', 'parent_generation': 1, 'parent_input_fingerprint': 'validation-input',
            'source_manifest': manifest})
    tx.put('review_contract_validation', 'validation-attempt', {'run_id': 'run', 'work_item_id': 'validation',
        'attempt_id': 'validation-attempt', 'batch_id': 'batch', 'state': 'passed', 'source_snapshot_id': snapshot['id'],
        'source_commit': snapshot['commit_oid'], 'source_tree_oid': snapshot['tree_oid'], 'validation_generation': 1,
        'diagnostic_id': 'diagnostic'})
    pool = tx.put('coding_work_budget', 'pool', {'run_id': 'run', 'work_item_id': 'owner', 'step_count': 2,
        'active_seconds': 3.0, 'observed_tool_calls': 4, 'uncertain': False})
    budget = tx.put('coding_work_budget', 'child', {'run_id': 'run', 'work_item_id': 'action',
        'review_contract_owner_budget': pool['id'], 'review_contract_batch': batch['id']})
    task = {'run_id': 'run', 'work_item_id': 'action', 'attempt_id': 'action-attempt'}
    return tx, run, batch, reviewer, budget, task


@pytest.mark.parametrize('step', ['review_disposition', 'review_validation', 'review_unit_migration', 'review_integration_migration'])
def test_internal_step_without_controller_identity_is_rejected(step):
    tx, *_ = fixture()
    with pytest.raises(DomainError):
        repair_batch(tx, {'id': 'rogue', 'step': step, 'payload': {}})
    assert repair_batch(tx, {'id': 'normal', 'step': 'implementation', 'payload': {}}) is None


@pytest.mark.parametrize('change', ['source', 'plan', 'acceptance', 'approval', 'owner_scope', 'owner_approval'])
def test_claim_binding_rechecks_frozen_facts(change):
    tx, *_ = fixture()
    if change == 'source':
        tx.get('code_snapshot', 'original')['tree_oid'] = 'wrong'
    if change == 'plan':
        tx.get('plan', 'plan')['revision'] += 1
    if change == 'acceptance':
        tx.get('work_item', 'prd')['status'] = 'cancelled'
    if change == 'approval':
        tx.get('approval', 'approval')['stale'] = True
    if change == 'owner_scope':
        tx.get('work_item', 'owner')['write_paths'] = ['elsewhere']
    if change == 'owner_approval':
        tx.get('work_item', 'owner')['approval_required'] = False
    with pytest.raises(DomainError):
        repair_batch(tx, tx.get('work_item', 'action'))


def test_valid_frozen_binding_and_diagnostic_resolve_producer():
    tx, run, batch, reviewer, *_ = fixture()
    assert repair_batch(tx, tx.get('work_item', 'action')) == batch
    assert bound_producer(tx, run, reviewer)['id'] == 'assembly'


@pytest.mark.parametrize('kind, identity, field, value', [
    ('review_contract_validation', 'validation-attempt', 'run_id', 'other'),
    ('review_contract_validation', 'validation-attempt', 'work_item_id', 'other'),
    ('review_contract_validation', 'validation-attempt', 'attempt_id', 'other'),
    ('review_contract_validation', 'validation-attempt', 'source_tree_oid', 'other'),
    ('review_diagnostic', 'diagnostic', 'run_id', 'other'),
    ('review_diagnostic', 'diagnostic', 'batch_id', 'other'),
    ('attempt', 'validation-attempt', 'generation', 2),
    ('attempt', 'assembly-attempt', 'run_id', 'other'),
    ('node_job', 'test', 'parent_generation', 2),
    ('node_job', 'build', 'run_id', 'other'),
])
def test_wrong_diagnostic_run_tree_batch_attempt_or_job_cannot_bind(kind, identity, field, value):
    tx, run, _, reviewer, *_ = fixture()
    tx.get(kind, identity)[field] = value
    assert bound_producer(tx, run, reviewer) is None


def test_unknown_then_known_charges_once_and_clears_only_its_unknown():
    tx, _, _, _, budget, task = fixture()
    charge_owner_budget(tx, task, budget, known=False, seconds=None, calls=None)
    pool = tx.get('coding_work_budget', 'pool')
    assert pool['step_count'] == 3 and pool['uncertain']
    charge_owner_budget(tx, task, budget, known=True, seconds=2.5, calls=3)
    charge_owner_budget(tx, task, budget, known=True, seconds=2.5, calls=3)
    pool = tx.get('coding_work_budget', 'pool')
    assert (pool['step_count'], pool['active_seconds'], pool['observed_tool_calls'], pool['uncertain']) == (3, 5.5, 7, False)


@pytest.mark.parametrize('other', ['charge', 'direct_usage', 'unexplained'])
def test_reconciling_one_unknown_never_clears_other_uncertainty(other):
    tx, _, _, _, budget, task = fixture()
    if other == 'charge':
        charge_owner_budget(tx, {**task, 'attempt_id': 'another'}, budget, known=False, seconds=None, calls=None)
    elif other == 'direct_usage':
        tx.put('coding_step_usage', 'direct', {'budget_id': 'pool', 'known': False})
        tx.get('coding_work_budget', 'pool')['uncertain'] = True
    else:
        tx.get('coding_work_budget', 'pool')['uncertain'] = True
    charge_owner_budget(tx, task, budget, known=False, seconds=None, calls=None)
    charge_owner_budget(tx, task, budget, known=True, seconds=2.5, calls=3)
    assert tx.get('coding_work_budget', 'pool')['uncertain']
    assert tx.get('coding_work_budget', 'pool')['active_seconds'] == 5.5


def test_late_usage_still_accounts_after_requirements_revoked_and_conflict_rejected():
    tx, _, _, _, budget, task = fixture()
    charge_owner_budget(tx, task, budget, known=False, seconds=None, calls=None)
    tx.get('work_item', 'prd')['status'] = 'cancelled'
    charge_owner_budget(tx, task, budget, known=True, seconds=2, calls=1)
    assert tx.get('coding_work_budget', 'pool')['active_seconds'] == 5
    with pytest.raises(DomainError):
        charge_owner_budget(tx, task, budget, known=True, seconds=200, calls=100)


def test_bound_ordinary_step_cannot_erase_its_auxiliary_identity():
    tx, *_ = fixture()
    assembly = tx.get('work_item', 'assembly')
    assembly['payload'] = {}
    with pytest.raises(DomainError):
        repair_batch(tx, assembly)


def test_context_diagnostic_failure_evidence_cannot_be_replaced():
    tx, _, batch, *_ = fixture()
    diagnostic = tx.put('review_diagnostic', 'prior-failure', {'run_id': 'run', 'state': 'failed', 'reports': ['old']})
    batch['context']['diagnostic'] = deepcopy(diagnostic)
    diagnostic['reports'] = ['replacement']
    with pytest.raises(DomainError):
        repair_batch(tx, tx.get('work_item', 'action'))


def test_multistep_checkpoint_uses_preserved_base_not_direct_parent():
    tx, _, batch, *_ = fixture()
    work = tx.get('work_item', 'action')
    original = batch['context']['snapshot']
    base = tx.put('code_snapshot', 'base', {**original, 'id': 'base', 'work_item_id': 'action',
        'review_contract_task': 'batch', 'source_snapshot_id': 'original'})
    batch['work_specs']['action']['payload']['repair_base_snapshot_id'] = base['id']
    point = tx.put('code_snapshot', 'checkpoint', {'run_id': 'run', 'work_item_id': 'action', 'generation': 2,
        'base_oid': original['commit_oid'], 'commit_oid': 'e' * 40, 'tree_oid': 'f' * 40,
        'parent_commit_oids': ['intermediate-commit'], 'stale': False})
    tx.put('coding_step_checkpoint', 'point', {'run_id': 'run', 'work_item_id': 'action',
        'snapshot_id': point['id'], 'commit_oid': point['commit_oid'], 'next_generation': 3})
    work['generation'] = 3
    work['payload'].update(repair_base_snapshot_id='checkpoint', coding_step_checkpoint_id='point')
    assert repair_batch(tx, work)['id'] == 'batch'
    point['base_oid'] = 'unrelated'
    with pytest.raises(DomainError):
        repair_batch(tx, work)


@pytest.mark.parametrize('field,value', [('review_contract_kind', 'production_fix'),
                                       ('review_contract_actions', []), ('review_contract_owner', 'other')])
def test_immutable_controller_actions_cannot_be_changed(field, value):
    tx, *_ = fixture()
    work = tx.get('work_item', 'action')
    work['payload'][field] = value
    with pytest.raises(DomainError):
        repair_batch(tx, work)


def test_original_review_binding_is_not_an_auxiliary_identity():
    tx, _, _, reviewer, *_ = fixture()
    assert repair_batch(tx, reviewer) is None


async def test_delegated_usage_is_part_of_owner_recovery_snapshot_and_lower_bound(tmp_path):
    from agentflow.control.recovery import coding_usage_blockers, coding_usage_state
    tx, _, _, _, budget, task = fixture()
    charge_owner_budget(tx, task, budget, known=True, seconds=2.5, calls=3)
    records = {kind: tx.list(kind) for kind, _ in tx.rows}
    selected = coding_usage_state(records, 'run', 'owner')
    assert selected.get('review_contract_budget_charge'), 'Delegated usage disappeared from owner snapshot'
    assert not selected['coding_step_control'] and not selected['coding_step_usage']
    assert await coding_usage_blockers(selected, tmp_path, 'owner') == []
    selected['coding_work_budget'][0]['active_seconds'] = 0
    assert await coding_usage_blockers(selected, tmp_path, 'owner')


async def test_owner_late_receipt_includes_delegated_steps_without_fabricated_controls(coding_env):
    from test_late_coding_usage import late_coding_failure, patch

    from agentflow.control.coding_steps import CodingSteps
    from agentflow.control.execution_reconciliation import ExecutionReconciliation
    env = coding_env
    pool_id = CodingSteps.budget_id('run', 'code')
    await patch(env, 'coding_work_budget', pool_id, run_id='run', work_item_id='code', base_commit=env.project['base_commit'],
        max_steps=3, max_active_seconds=100, max_tool_calls=12, step_count=1, active_seconds=4.0,
        observed_tool_calls=1, uncertain=False)
    await patch(env, 'review_contract_budget_charge', 'delegated', run_id='run', work_item_id='auxiliary',
        owner_work_item_id='code', owner_budget_id=pool_id, batch_id='batch', known=True, active_seconds=4.0, observed_tool_calls=1)
    task, _ = await late_coding_failure(env)
    assert task['coding_step']['step_number'] == 2
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    usage = await env.store.read('coding_step_usage', task['attempt_id'])
    assert usage['known'] is True
    pool = await env.store.read('coding_work_budget', pool_id)
    assert (pool['step_count'], pool['active_seconds'], pool['observed_tool_calls'], pool['uncertain']) == (2, 12.5, 3, False)
    assert len(await env.store.list('coding_step_control')) == 1


@pytest.mark.parametrize('quality', ['unknown', 'passed'])
def test_completed_assembly_can_await_independent_review(quality):
    tx, run, _, reviewer, *_ = fixture()
    tx.get('work_item', 'assembly')['quality_result'] = quality
    tx.get('attempt', 'assembly-attempt')['quality_result'] = quality
    assert bound_producer(tx, run, reviewer)['id'] == 'assembly'


@pytest.mark.parametrize('kind, identity, quality', [
    ('work_item', 'assembly', 'failed'), ('work_item', 'assembly', 'inconclusive'),
    ('attempt', 'assembly-attempt', 'failed'), ('attempt', 'assembly-attempt', 'inconclusive'),
    ('work_item', 'validation', 'unknown'), ('attempt', 'validation-attempt', 'unknown'),
])
def test_failed_assembly_or_unpassed_validation_cannot_bind(kind, identity, quality):
    tx, run, _, reviewer, *_ = fixture()
    tx.get(kind, identity)['quality_result'] = quality
    assert bound_producer(tx, run, reviewer) is None


async def real_binding_store(tmp_path, state):
    from agentflow.storage import Store
    store = Store(tmp_path)
    await store.start()
    def seed(tx):
        for (kind, identity), row in state.rows.items():
            tx.put(kind, identity, {k: v for k, v in row.items() if k not in {'id', 'revision'}})
        return {}
    await store.command('binding.real-fixture', 'seed', {}, seed)
    return store


async def test_real_transaction_accepts_recovered_base_without_coding_checkpoint_id(tmp_path):
    state, _, batch, *_ = fixture()
    original = batch['context']['snapshot']
    state.put('code_snapshot', 'base', {**original, 'work_item_id': 'action', 'generation': 0,
        'review_contract_task': 'batch', 'source_snapshot_id': 'original'})
    state.put('code_snapshot', 'recovered', {'run_id': 'run', 'work_item_id': 'action', 'generation': 1,
        'base_oid': original['commit_oid'], 'commit_oid': 'e' * 40, 'tree_oid': 'f' * 40, 'stale': True,
        'purpose': 'recovery_checkpoint', 'recovery_id': 'recovery', 'source_attempt_id': 'action-attempt',
        'source_generation': 1, 'source_fencing_token': 1, 'source_input_fingerprint': 'action-input',
        'source_commit': original['commit_oid'], 'source_write_paths': ['tests'], 'source_repair_snapshot_id': 'base'})
    state.put('run_recovery', 'recovery', {'run_id': 'run', 'actor': 'owner', 'mode': 'retry',
        'execution': 'fresh_attempt', 'affected_work_item_ids': ['action'],
        'checkpoint': {'snapshot_id': 'recovered', 'work_item_id': 'action', 'commit_oid': 'e' * 40}})
    state.put('dispatch_context', 'action-attempt', {'task': {'run_id': 'run', 'work_item_id': 'action',
        'attempt_id': 'action-attempt', 'source_commit': original['commit_oid'], 'allowed_write_paths': ['tests'],
        'fencing_token': 1, 'input_fingerprint': 'action-input'}})
    batch['work_specs']['action']['payload']['repair_base_snapshot_id'] = 'base'
    work = state.get('work_item', 'action')
    work['payload'].update(repair_base_snapshot_id='recovered', recovery_checkpoint_id='recovered')
    assert 'coding_step_checkpoint_id' not in work['payload']
    work['generation'] = 2
    store = await real_binding_store(tmp_path, state)
    try:
        result = await store.command('binding.real-test', 'recover', {},
            lambda tx: repair_batch(tx, tx.get('work_item', 'action')))
        assert result['id'] == 'batch'
    finally:
        await store.close()


@pytest.mark.parametrize('kind, identity, field', [
    ('review_contract_repair', 'batch', 'stage_id'),
    ('review_contract_repair', 'batch', 'validation_work_item_id'),
    ('review_contract_repair', 'batch', 'assembly_work_item_id'),
    ('work_item', 'assembly', 'attempt_id'),
    ('work_item', 'validation', 'attempt_id'),
    ('review_contract_validation', 'validation-attempt', 'diagnostic_id'),
])
async def test_real_transaction_missing_optional_producer_fact_returns_unresolved(tmp_path, kind, identity, field):
    state, *_ = fixture()
    state.get(kind, identity)[field] = None
    store = await real_binding_store(tmp_path, state)
    try:
        result = await store.command('binding.real-test', 'missing-optional', {}, lambda tx: {
            'producer': bound_producer(tx, tx.get('run', 'run'), tx.get('work_item', 'reviewer'))})
        assert result['producer'] is None
    finally:
        await store.close()


async def test_real_transaction_missing_group_binding_is_unresolved(tmp_path):
    from agentflow.control.review_contract_binding import valid_group_binding
    state, *_ = fixture()
    state.get('work_item', 'reviewer')['payload'] = {}
    store = await real_binding_store(tmp_path, state)
    try:
        result = await store.command('binding.real-test', 'missing-group', {}, lambda tx: {
            'valid': valid_group_binding(tx, tx.get('run', 'run'), tx.get('work_item', 'reviewer'),
                                         {'id': 'expansion', 'input_fingerprint': 'expansion'}, [], [])})
        assert result['valid'] is False
    finally:
        await store.close()


@pytest.mark.parametrize('damage', [None, 'marker', 'receipt', 'scope', 'source_attempt', 'generation'])
async def test_normal_owner_recovery_authorizes_only_bound_historical_checkpoint(recovery_env, damage):
    from test_recovery import patch, request, stopped_workspace

    from agentflow.control.recovery import validate_recovery_checkpoint
    env = recovery_env
    await stopped_workspace(env)
    base_commit = env.project['base_commit']
    base_tree = env.service.repository._run(env.tmp_path / 'project', ['rev-parse', base_commit + '^{tree}']).decode().strip()
    def bind(tx):
        work = tx.get('work_item', 'bad')
        original = tx.put('code_snapshot', 'contract-original', {'run_id': 'run', 'work_item_id': 'upstream',
            'generation': 1, 'repository_path': env.project['local_path'], 'commit_oid': base_commit,
            'tree_oid': base_tree, 'base_oid': base_commit, 'stale': False})
        tx.put('code_snapshot', 'contract-base', {**{k: v for k, v in original.items() if k not in {'id', 'revision'}},
            'work_item_id': 'bad', 'generation': 0, 'review_contract_task': 'contract', 'source_snapshot_id': original['id']})
        spec = {**work, 'payload': {**work['payload'], 'review_contract_task': 'contract',
            'review_contract_kind': 'production_fix', 'repair_base_snapshot_id': 'contract-base'}}
        tx.put('work_item', 'bad', spec, work['revision'])
        return tx.put('review_contract_repair', 'contract', {'actor': 'controller', 'run_id': 'run',
            'work_specs': {'bad': {k: v for k, v in spec.items() if k not in {'id', 'revision'}}},
            'context': {'source_snapshot_id': original['id'], 'snapshot': original}})
    await env.store.command('binding.recovery-real', 'bind', {}, bind)
    receipt = await env.service.recover('run', request(), 'recover-bound')
    work = await env.store.read('work_item', 'bad')
    point = await env.store.read('code_snapshot', work['payload']['recovery_checkpoint_id'])
    assert point['stale'] and point['purpose'] == 'recovery_checkpoint'
    assert 'coding_step_checkpoint_id' not in work['payload']
    await validate_recovery_checkpoint(env.store, receipt['run'], work, point, env.service.repository)
    if damage == 'marker':
        await patch(env, 'code_snapshot', point['id'], purpose='ordinary_snapshot')
    elif damage == 'receipt':
        await patch(env, 'run_recovery', receipt['id'], actor='model')
    elif damage == 'scope':
        context = await env.store.read('dispatch_context', point['source_attempt_id'])
        await patch(env, 'dispatch_context', context['id'], task={**context['task'], 'allowed_write_paths': ['.']})
    elif damage == 'source_attempt':
        await patch(env, 'attempt', point['source_attempt_id'], input_fingerprint='changed')
    elif damage == 'generation':
        await patch(env, 'code_snapshot', point['id'], generation=work['generation'])
    if damage:
        with pytest.raises(DomainError) as rejected:
            await env.store.command('binding.recovery-real', 'check', {}, lambda tx: repair_batch(tx, tx.get('work_item', 'bad')))
        assert rejected.value.code == 'review_contract_binding_invalid'
    else:
        accepted = await env.store.command('binding.recovery-real', 'check', {}, lambda tx: repair_batch(tx, tx.get('work_item', 'bad')))
        assert accepted['id'] == 'contract'

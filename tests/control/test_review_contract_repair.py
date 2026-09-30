"""Controlled review repairs must not claim downstream tests or broaden source rights."""
import importlib
from copy import deepcopy

import pytest
from test_review_disposition import fixture as disposition_fixture
from test_workflow import flow as flow

from agentflow.common import DomainError, canonical_digest


async def setup_batch(flow, *, author_approval=False, triage_approval=False):
    service, store, _, project, _ = flow
    from agentflow.control.review_contract_repair import new_work
    context, result = disposition_fixture()
    fingerprint = 'sha256:' + 'a' * 64
    def seed(tx):
        run = tx.put('run', 'run', {'project_id': project['id'], 'plan_id': 'plan', 'iteration_id': 'iteration',
            'execution_state': 'running', 'input_fingerprint': fingerprint,
            'budget_limit': {'max_active_seconds': 100, 'max_tool_calls': 100, 'max_model_requests': 0}})
        tx.put('plan', 'plan', {'state': 'started', 'started_run_id': 'run'})
        source = tx.put('work_item', 'source', {**new_work(run, 'implementation', 'implementation', []),
            'status': 'completed', 'attempt_id': 'snapshot'})
        snapshot = tx.put('code_snapshot', 'snapshot', {'run_id': 'run', 'work_item_id': source['id'],
            'generation': 1, 'repository_path': project['local_path'], 'commit_oid': 'commit', 'tree_oid': 'tree',
            'base_oid': 'base', 'stale': False})
        bad = tx.put('work_item', 'bad', {**new_work(run, 'code_review:bad', 'code_review', ['source']),
            'status': 'completed', 'quality_result': 'failed', 'parent_stage_id': 'review', 'attempt_id': 'failed-review'})
        good = tx.put('work_item', 'good', {**new_work(run, 'code_review:good', 'code_review', ['source']),
            'status': 'completed', 'quality_result': 'passed', 'parent_stage_id': 'review', 'attempt_id': 'passed-review'})
        stage = tx.put('work_item', 'review', {**new_work(run, 'code_review', 'code_review', ['bad', 'good'], kind='aggregation'),
            'expanded_child_ids': ['bad', 'good'], 'original_dependencies': ['source'], 'original_write_paths': [],
            'expansion_fingerprint': fingerprint})
        tx.put('stage_expansion', 'expanded', {'stage_work_item_id': 'review', 'input_fingerprint': fingerprint})
        review = tx.put('review', 'failed-review', {'run_id': 'run', 'work_item_id': 'bad',
            'reviewed_commit': 'commit', 'quality_result': 'failed', 'blocking_findings': context['findings']})
        for owner in context['owners']:
            tx.put('work_item', owner['work_item_id'], {**new_work(run, owner['work_item_id'], owner['step'],
                ['source'] if owner['step'] == 'implementation' else ['review'], paths=owner['write_paths'],
                approval=author_approval), 'status': 'completed' if owner['step'] == 'implementation' else 'pending'})
        triage_spec = new_work(run, 'triage', 'review_disposition', ['source'], approval=triage_approval,
                              payload={'review_contract_task': 'batch', 'review_contract_kind': 'triage'})
        tx.put('work_item', 'triage', {**triage_spec, 'status': 'running', 'attempt_id': 'triage-attempt'})
        context.update(snapshot=snapshot, reviews=[review], accepted_documents=[])
        return tx.put('review_contract_repair', 'batch', {'actor': 'controller', 'run_id': 'run', 'state': 'triaging',
            'stage_id': 'review', 'reviewer_id': 'bad', 'context': context, 'context_digest': canonical_digest(context),
            'source_commit': 'commit', 'source_snapshot_id': 'snapshot', 'triage_work_item_id': 'triage',
            'review_work_ids': ['review', 'bad', 'good'], 'cohort': [bad, good], 'original_stage': stage,
            'work_specs': {'triage': triage_spec}})
    batch = await store.command('fixture', 'batch', {}, seed)
    return batch, result


async def schedule(flow, result):
    from agentflow.control.review_contract_repair import ReviewContractRepair
    core = ReviewContractRepair(flow[1], flow[0])
    await core.prepare_disposition(await flow[1].read('work_item', 'triage'), result)
    return await flow[1].command('fixture', 'schedule', {},
        lambda tx: core.apply_disposition(tx, {'work_item_id': 'triage'}, result) or {'ok': True})


def test_contract_repair_orchestrator_exists():
    assert importlib.util.find_spec('agentflow.control.review_contract_repair') is not None


def test_internal_migration_steps_are_coding_roles_but_not_public_pipeline_stages():
    from agentflow.domain.planning import CODING_STEPS, ROLES, STEPS
    assert ROLES.get('review_unit_migration') == 'unit_test'
    assert ROLES.get('review_integration_migration') == 'integration_test'
    assert {'review_unit_migration', 'review_integration_migration'} <= CODING_STEPS
    assert not {'review_unit_migration', 'review_integration_migration', 'review_disposition', 'review_validation'} & set(STEPS)


async def test_three_repairs_bind_one_validation_gate_and_keep_future_tests_pending(flow):
    _, result = await setup_batch(flow)
    await schedule(flow, result)
    batch = await flow[1].read('review_contract_repair', 'batch')
    assert batch['state'] == 'repairing' and len(batch['repair_work_item_ids']) == 3
    assert set((await flow[1].read('work_item', 'review'))['dependencies']) == {'bad', 'good'}
    for work_id in ['bad', 'good']:
        row = await flow[1].read('work_item', work_id)
        assert row['dependencies'] == [batch['validation_work_item_id']] and row['status'] == 'pending'
    assert (await flow[1].read('work_item', 'api-tests'))['status'] == 'pending'
    assert (await flow[1].read('work_item', 'web-tests'))['status'] == 'pending'
    assert (await flow[1].read('work_item', 'source'))['status'] == 'completed'
    assert not await flow[1].list('check') and not await flow[1].list('candidate')


async def test_invalid_action_rolls_back_entire_repair_batch(flow):
    _, result = await setup_batch(flow)
    before = {k: await flow[1].list(k) for k in ['work_item', 'coding_work_budget', 'code_snapshot', 'review_contract_repair']}
    bad = deepcopy(result)
    bad['actions'][-1]['repair_paths'] = ['tests/support/fixtures.mjs']
    with pytest.raises(DomainError):
        await schedule(flow, bad)
    assert {k: await flow[1].list(k) for k in before} == before


async def test_disposition_reports_semantic_and_migration_errors_together(flow):
    from agentflow.control.review_contract_repair import ReviewContractRepair
    _, result = await setup_batch(flow)
    result['actions'][0]['migrations'] = []
    result['actions'][1]['migrations'][0]['matcher'] = 'toContainText'
    core = ReviewContractRepair(flow[1], flow[0])
    with pytest.raises(DomainError) as error:
        await core.prepare_disposition(await flow[1].read('work_item', 'triage'), result)
    assert any(row.get('code') == 'migration_evidence_missing' for row in error.value.details)
    assert any('Matcher requires production repair' in row.get('message', '') for row in error.value.details)
    assert not await flow[1].list('review_disposition_check')


async def test_repair_inherits_original_author_approval(flow):
    _, result = await setup_batch(flow, author_approval=True)
    await schedule(flow, result)
    batch = await flow[1].read('review_contract_repair', 'batch')
    for work_id in batch['repair_work_item_ids']:
        assert (await flow[1].read('work_item', work_id))['approval_required']


async def test_invalid_pending_binding_is_reported_without_stalling_other_repairs(flow):
    _, result = await setup_batch(flow)
    await schedule(flow, result)
    store = flow[1]
    batch = await store.read('review_contract_repair', 'batch')
    bad_id = sorted(batch['repair_work_item_ids'])[0]
    def damage(tx):
        triage = tx.get('work_item', 'triage')
        tx.put('work_item', 'triage', {**triage, 'status': 'completed'}, triage['revision'])
        bad = tx.get('work_item', bad_id)
        tx.put('work_item', bad_id, {**bad, 'write_paths': ['unauthorized.mjs']}, bad['revision'])
        return {}
    await store.command('fixture', 'damage-pending', {}, damage)
    claim = await flow[0].claim_next('run', 'fixture', 'claim-with-invalid-binding')
    assert claim['work_item']['id'] != bad_id
    bad = await store.read('work_item', bad_id)
    assert bad['status'] == 'blocked'
    assert bad['runtime_failure_code'] == 'review_contract_binding_invalid'
    assert bad['failure_diagnostic']['code'] == 'review_contract_binding_invalid'
    assert bad['attempt_id'] is None


async def test_triage_human_gate_does_not_schedule_repairs_before_approval(flow):
    _, result = await setup_batch(flow, triage_approval=True)
    await schedule(flow, result)
    batch = await flow[1].read('review_contract_repair', 'batch')
    assert batch['state'] == 'awaiting_approval'
    assert not batch.get('repair_work_item_ids') and not await flow[1].list('coding_work_budget')
    assert (await flow[1].read('work_item', 'bad'))['quality_result'] == 'failed'


async def test_delegated_usage_is_charged_once_to_original_author(flow):
    from agentflow.control.coding_steps import CodingSteps
    from agentflow.control.review_contract_binding import charge_owner_budget
    _, result = await setup_batch(flow)
    await schedule(flow, result)
    batch = await flow[1].read('review_contract_repair', 'batch')
    action = None
    for identity in batch['repair_work_item_ids']:
        row = await flow[1].read('work_item', identity)
        if row['payload']['review_contract_owner'] == 'api-tests':
            action = row
    budget = await flow[1].read('coding_work_budget', CodingSteps.budget_id('run', action['id']))
    task = {'attempt_id': 'action-attempt', 'work_item_id': action['id'], 'run_id': 'run'}
    def charge(tx):
        charge_owner_budget(tx, task, budget, known=True, seconds=4.5, calls=3)
        return {}
    await flow[1].command('fixture', 'charge-one', {}, charge)
    await flow[1].command('fixture', 'charge-two', {}, charge)
    original = await flow[1].read('coding_work_budget', CodingSteps.budget_id('run', 'api-tests'))
    assert original['active_seconds'] == 4.5 and original['observed_tool_calls'] == 3 and original['step_count'] == 1

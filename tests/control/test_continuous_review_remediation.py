"""Continuous review repair reuses real Store/Git state and preserves every gate."""
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from review_fixture_utils import complete_review_guard_context
from test_parallel_remediation import complete_failed_review, complete_repair, update
from test_parallel_remediation import parallel_env as parallel_env
from test_parallel_review_child_remediation import collect_review, expanded_reviews

from agentflow.control.failure_remediation import FailureRemediation
from agentflow.models.budget import account_id


async def prepare_automatic(env, **settings):
    env.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0, **settings})
    env.workflow.settings = env.settings
    await complete_review_guard_context(env.store)
    return FailureRemediation(env.store, env.workflow, review=env.remediation)


@pytest.mark.parametrize('automatic', [False, True], ids=['direct-review', 'failure-controller'])
async def test_default_review_policy_continues_six_real_repair_rounds_until_passed(parallel_env, automatic):
    env = parallel_env
    controller = await prepare_automatic(env, auto_failure_retry_limit=0, auto_failure_run_limit=0)
    original_budgets = await env.store.list('budget_account')
    original = (Path(env.aggregate['repository_path']) / 'src/a.mjs').read_text()
    for number in range(1, 7):
        review = await env.store.read('work_item', 'review-work')
        source = await env.store.read('review', review['attempt_id'])
        action = controller.repair if automatic else env.remediation.repair
        results = await asyncio.gather(*(action('review-work') for _ in range(2)))
        receipts = await env.store.list('review_repair')
        assert len(receipts) == number, results
        receipt = max(receipts, key=lambda row: row['ordinal'])
        assert receipt['base_commit'] == source['reviewed_commit']
        frozen = await complete_repair(env, ['a'], number)
        assert (Path(frozen['repository_path']) / 'src/a.mjs').read_text() == f'export const a = {number};\n'
        assert (Path(env.aggregate['repository_path']) / 'src/a.mjs').read_text() == original
        if number < 6:
            await complete_failed_review(env, 'a')
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert (await collect_review(env, claim, []))['quality_result'] == 'passed'
    await controller.reconcile()
    await env.remediation.reconcile()
    assert len(await env.store.list('review_repair')) == 6
    assert await env.store.list('budget_account') == original_budgets
    assert (await env.workflow.claim_next('run', 'fixture', str(uuid4())))['work_item']['id'] == 'unit'


@pytest.mark.parametrize('automatic', [False, True])
@pytest.mark.parametrize('limit', [0, 2])
async def test_explicit_review_limit_still_disables_or_bounds_repair(parallel_env, automatic, limit):
    env = parallel_env
    controller = await prepare_automatic(env, auto_review_repair_limit=limit, auto_failure_retry_limit=0,
                                         auto_failure_run_limit=0)
    action = controller.repair if automatic else env.remediation.repair
    for number in range(1, limit + 1):
        result = await action('review-work')
        assert result and (not automatic or result['status'] == 'repair_scheduled'), result
        await complete_repair(env, ['a'], number)
        await complete_failed_review(env, 'a')
    before = await env.store.list('work_item')
    result = await action('review-work')
    if automatic:
        assert result['status'] == 'blocked'
        assert 'review_repair_limit' in {row['code'] for row in result['blockers']}
        assert 'automatic_repair_limit' not in {row['code'] for row in result['blockers']}
    else:
        assert result is None
    assert await env.store.list('work_item') == before
    assert len(await env.store.list('review_repair')) == limit


@pytest.mark.parametrize('kind,identity,fields', [
    ('budget_account', account_id('run', 'run'), {'settled_micros': 1000}),
    ('budget_account', account_id('iteration', 'iteration'), {'settled_micros': 2000}),
    ('model_invocation', 'unknown', {'run_id': 'run', 'iteration_id': 'iteration', 'state': 'uncertain'}),
    ('work_item', 'unit', {'status': 'waiting_approval'}),
    ('candidate', 'frozen', {'run_id': 'run'}),
    ('delivery_intent', 'publishing', {'run_id': 'run', 'status': 'prepared'}),
])
@pytest.mark.parametrize('automatic', [False, True])
async def test_continuous_review_preserves_budget_unknown_human_and_release_gates(parallel_env, automatic, kind, identity, fields):
    env = parallel_env
    controller = await prepare_automatic(env, auto_review_repair_limit=-1)
    await update(env.store, kind, identity, **fields)
    before = {name: await env.store.list(name) for name in ('work_item', 'code_snapshot', 'budget_account', 'model_invocation')}
    action = controller.repair if automatic else env.remediation.repair
    result = await action('review-work')
    assert not result or result.get('status') == 'blocked'
    assert not await env.store.list('review_repair')
    assert {name: await env.store.list(name) for name in before} == before


async def test_parallel_review_waits_then_reconciles_without_a_store_event(parallel_env):
    env = parallel_env
    controller = await prepare_automatic(env, auto_review_repair_limit=-1)
    failed, good = await expanded_reviews(env)
    await update(env.store, 'work_item', good['id'], status='running')
    await update(env.store, 'attempt', good['attempt_id'], status='running')
    before = {name: await env.store.list(name) for name in ('work_item', 'code_snapshot', 'budget_account')}
    await controller.reconcile()
    analysis = next(row for row in await env.store.list('failure_analysis') if row['work_item_id'] == failed['id'])
    assert analysis['status'] == 'blocked'
    assert '等待并行审查结束后返工' in analysis['summary']
    assert {name: await env.store.list(name) for name in before} == before
    assert not await env.store.list('review_repair')
    # Fixture updates intentionally do not emit events: a stopped process can
    # change outside the Store, so temporary waiting must have a timed retry.
    await update(env.store, 'work_item', good['id'], status='completed')
    await update(env.store, 'attempt', good['attempt_id'], status='completed')
    assert failed['id'] in controller._deferred
    controller._deferred[failed['id']] = 0
    await controller.reconcile()
    assert len(await env.store.list('review_repair')) == 1
    assert (await env.store.read('work_item', 'module-a'))['status'] == 'pending'
    assert (await env.store.read('failure_analysis', analysis['id']))['status'] == 'repair_scheduled'
    await controller.reconcile()
    assert len(await env.store.list('review_repair')) == 1


@pytest.mark.parametrize('automatic', [False, True])
@pytest.mark.parametrize('field,value', [('step_count', 10), ('observed_tool_calls', 100),
                                        ('active_seconds', 1000.0), ('uncertain', True)])
async def test_review_repair_checks_actual_coding_owner_budget(parallel_env, automatic, field, value):
    from agentflow.control.coding_steps import CodingSteps
    env = parallel_env
    controller = await prepare_automatic(env, auto_review_repair_limit=-1)
    budget = {'run_id': 'run', 'work_item_id': 'module-a', 'max_steps': 10, 'step_count': 1,
              'max_tool_calls': 100, 'observed_tool_calls': 10, 'max_active_seconds': 1000.0,
              'active_seconds': 1.0, 'uncertain': False}
    await update(env.store, 'coding_work_budget', CodingSteps.budget_id('run', 'module-a'), **{**budget, field: value})
    before = {kind: await env.store.list(kind) for kind in ('work_item', 'coding_work_budget', 'budget_account')}
    result = await (controller.repair('review-work') if automatic else env.remediation.repair('review-work'))
    assert not result or result.get('status') == 'blocked', result
    if automatic:
        assert any(row['code'].startswith('coding_budget_') for row in result['blockers'])
    assert not await env.store.list('review_repair')
    assert {kind: await env.store.list(kind) for kind in before} == before


@pytest.mark.parametrize('automatic', [False, True])
async def test_unknown_invocation_waits_and_rechecks_after_settlement_without_event(parallel_env, automatic):
    env = parallel_env
    controller = await prepare_automatic(env, auto_review_repair_limit=-1)
    service = controller if automatic else env.remediation
    await update(env.store, 'model_invocation', 'pending-call', run_id='run', iteration_id='iteration', state='uncertain')
    await service.reconcile()
    assert not await env.store.list('review_repair')
    assert 'review-work' in service._deferred
    await update(env.store, 'model_invocation', 'pending-call', state='settled', actual_micros=0)
    service._deferred['review-work'] = 0
    await service.reconcile()
    assert len(await env.store.list('review_repair')) == 1
    assert (await env.store.read('model_invocation', 'pending-call'))['state'] == 'settled'
    await service.reconcile()
    assert len(await env.store.list('review_repair')) == 1


async def test_review_budget_preflight_cannot_use_stale_producer_dependencies(parallel_env, monkeypatch):
    from agentflow.control.coding_steps import CodingSteps
    from agentflow.control.recovery import RunRecoveryService
    env = parallel_env
    await prepare_automatic(env)
    await update(env.store, 'work_item', 'replacement-code', **{**env.common, 'step': 'implementation',
        'key': 'replacement-code', 'role': 'development', 'dependencies': [], 'write_paths': ['src'],
        'attempt_id': 'replacement-snapshot'})
    await update(env.store, 'code_snapshot', 'replacement-snapshot',
                 **{key: value for key, value in env.aggregate.items() if key not in {'id', 'revision', 'work_item_id'}},
                 work_item_id='replacement-code')
    await update(env.store, 'coding_work_budget', CodingSteps.budget_id('run', 'replacement-code'),
        run_id='run', work_item_id='replacement-code', max_steps=1, step_count=1,
        max_tool_calls=100, observed_tool_calls=0, max_active_seconds=1000.0, active_seconds=0.0, uncertain=False)
    original = RunRecoveryService._read
    async def changed_before_state_read(service, run_id):
        await update(env.store, 'work_item', 'review-work', dependencies=['replacement-code'])
        return await original(service, run_id)
    monkeypatch.setattr(RunRecoveryService, '_read', changed_before_state_read)
    assert await env.remediation.repair('review-work') is None
    assert not await env.store.list('review_repair')
    assert (await env.store.read('work_item', 'replacement-code'))['generation'] == 1


@pytest.mark.parametrize('automatic', [False, True])
@pytest.mark.parametrize('kind,code', [('work_execution_budget_adjustment', 'manual_retry_after_budget_change'),
                                      ('model_uncertainty_acknowledgment', 'manual_retry_after_model_ack')])
async def test_review_repair_preserves_actual_owner_explicit_retry_gate(parallel_env, automatic, kind, code):
    env = parallel_env
    controller = await prepare_automatic(env)
    await update(env.store, kind, 'manual-owner-decision', run_id='run', work_item_id='module-a',
                 work_generation=1, requires_explicit_retry=True)
    before = await env.store.list('work_item')
    result = await (controller.repair('review-work') if automatic else env.remediation.repair('review-work'))
    assert not result or result.get('status') == 'blocked', result
    if automatic:
        assert code in {row['code'] for row in result['blockers']}
    assert not await env.store.list('review_repair')
    assert await env.store.list('work_item') == before


@pytest.mark.parametrize('automatic', [False, True])
@pytest.mark.parametrize('kind', ['work_execution_budget_adjustment', 'model_uncertainty_acknowledgment'])
async def test_review_repair_preserves_sibling_explicit_retry_gate(parallel_env, automatic, kind):
    env = parallel_env
    controller = await prepare_automatic(env)
    failed, good = await expanded_reviews(env)
    await update(env.store, 'work_item', good['id'], status='blocked')
    await update(env.store, 'attempt', good['attempt_id'], status='blocked')
    await update(env.store, kind, 'manual-sibling-decision', run_id='run', work_item_id=good['id'],
                 work_generation=good['generation'], requires_explicit_retry=True)
    before = await env.store.list('work_item')
    result = await (controller.repair(failed['id']) if automatic else env.remediation.repair(failed['id']))
    assert not result or result.get('status') == 'blocked', result
    assert not await env.store.list('review_repair')
    assert await env.store.list('work_item') == before


@pytest.mark.parametrize('automatic', [False, True])
@pytest.mark.parametrize('kind,identity,fields', [
    ('run', 'run', {'restore_reconciliation_required': True}),
    ('model_attempt_budget', 'review-attempt-1', {'run_id': 'run', 'uncertain_invocations': 1}),
    ('budget_account', account_id('run', 'run'), {'reserved_micros': 1}),
    ('supervised_attempt', 'review-attempt-1', {'run_id': 'run', 'state': 'completed'}),
])
async def test_both_review_entries_require_restore_budget_and_stopped_process_proof(parallel_env, automatic, kind, identity, fields):
    env = parallel_env
    controller = await prepare_automatic(env)
    await update(env.store, kind, identity, **fields)
    before = await env.store.list('work_item')
    result = await (controller.repair('review-work') if automatic else env.remediation.repair('review-work'))
    assert not result or result.get('status') == 'blocked', result
    assert not await env.store.list('review_repair')
    assert await env.store.list('work_item') == before

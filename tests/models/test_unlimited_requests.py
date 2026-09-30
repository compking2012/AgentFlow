import asyncio

import pytest
from pydantic import ValidationError

from agentflow.common import DomainError
from agentflow.models.budget import BudgetLedger, account_id
from agentflow.models.profiles import AttemptContext


async def reserve(ledger, context, key, amount=0):
    return await ledger.reserve(context, protocol='responses', request_fingerprint='ledger-fixture',
        profile_revision=1, amount_micros=amount, currency='USD', idempotency_key=key)


def with_limit(context, maximum):
    return AttemptContext.model_validate({**context.model_dump(), 'max_model_requests': maximum})


async def test_zero_at_all_layers_preserves_cumulative_usage_beyond_200_and_replays(store, context):
    ledger = BudgetLedger(store)
    context = with_limit(context, 0)
    await ledger.setup_accounts(context.run_id, context.iteration_id, 1000, 1000,
                                run_max_requests=0, iteration_max_requests=0)
    for index in range(205):
        invocation = await reserve(ledger, context, f'call-{index}')
        await ledger.release_not_sent(invocation['id'], 'Local ledger fixture; no provider request')
    final = await reserve(ledger, context, 'call-204')
    assert final['state'] == 'released'
    for kind, owner in [('run', context.run_id), ('iteration', context.iteration_id)]:
        snapshot = await ledger.snapshot(kind, owner)
        assert snapshot['max_requests'] == 0 and snapshot['request_count'] == 205
        assert snapshot['reserved_micros'] == snapshot['settled_micros'] == snapshot['uncertain_micros'] == 0
        assert snapshot['available_micros'] == 1000
    assert (await store.read('model_attempt_budget', context.attempt_id))['request_count'] == 205
    replay = await ledger.setup_accounts(context.run_id, context.iteration_id, 1000, 1000,
                                        run_max_requests=0, iteration_max_requests=0)
    assert replay['run']['request_count'] == replay['iteration']['request_count'] == 205
    assert len(await store.list('model_invocation')) == 205


@pytest.mark.parametrize(('attempt_max', 'run_max', 'iteration_max', 'message'), [
    (0, 2, 0, 'run cumulative'), (0, 0, 2, 'iteration cumulative'), (2, 0, 0, 'Attempt'),
    (0, 2, 3, 'run cumulative'), (3, 0, 2, 'iteration cumulative'), (2, 3, 0, 'Attempt'),
])
async def test_finite_layer_still_enforces_its_cap_when_other_layers_are_unlimited(
        store, context, attempt_max, run_max, iteration_max, message):
    ledger = BudgetLedger(store)
    context = with_limit(context, attempt_max)
    await ledger.setup_accounts(context.run_id, context.iteration_id, 1000, 1000,
                                run_max_requests=run_max, iteration_max_requests=iteration_max)
    for index in range(2):
        invocation = await reserve(ledger, context, str(index))
        await ledger.release_not_sent(invocation['id'], 'Not dispatched')
    with pytest.raises(DomainError, match=message) as error:
        await reserve(ledger, context, 'over-limit')
    assert error.value.code == 'request_limit_exceeded'
    assert (await ledger.snapshot('run', context.run_id))['request_count'] == 2
    assert (await ledger.snapshot('iteration', context.iteration_id))['request_count'] == 2
    assert (await store.read('model_attempt_budget', context.attempt_id))['request_count'] == 2


async def test_new_unlimited_run_cannot_reset_a_finite_shared_iteration(store, context):
    ledger = BudgetLedger(store)
    context = with_limit(context, 0)
    await ledger.setup_accounts(context.run_id, context.iteration_id, 0, 0,
                                run_max_requests=0, iteration_max_requests=1)
    invocation = await reserve(ledger, context, 'one')
    await ledger.release_not_sent(invocation['id'], 'Not dispatched')
    second = context.model_copy(update={'attempt_id': 'second-attempt', 'run_id': 'second-run'})
    await ledger.setup_accounts(second.run_id, second.iteration_id, 0, 0,
                                run_max_requests=0, iteration_max_requests=1)
    with pytest.raises(DomainError, match='iteration cumulative'):
        await reserve(ledger, second, 'two')
    assert (await ledger.snapshot('run', second.run_id))['request_count'] == 0
    assert (await ledger.snapshot('iteration', second.iteration_id))['request_count'] == 1


async def test_unlimited_attempts_cannot_race_past_a_finite_parent_cap(store, context):
    ledger = BudgetLedger(store)
    context = with_limit(context, 0)
    await ledger.setup_accounts(context.run_id, context.iteration_id, 0, 0,
                                run_max_requests=0, iteration_max_requests=2)
    results = await asyncio.gather(*(reserve(ledger, context, str(index)) for index in range(8)),
                                   return_exceptions=True)
    assert sum(isinstance(result, DomainError) for result in results) == 6
    assert (await ledger.snapshot('run', context.run_id))['request_count'] == 2
    assert (await ledger.snapshot('iteration', context.iteration_id))['request_count'] == 2


@pytest.mark.parametrize(('run_money', 'iteration_money', 'message'), [(10, 100, 'run budget'), (100, 10, 'iteration budget')])
async def test_unlimited_requests_do_not_remove_either_monetary_budget(store, context, run_money, iteration_money, message):
    ledger = BudgetLedger(store)
    context = with_limit(context, 0)
    await ledger.setup_accounts(context.run_id, context.iteration_id, run_money, iteration_money,
                                run_max_requests=0, iteration_max_requests=0)
    await reserve(ledger, context, 'reserved', amount=6)
    with pytest.raises(DomainError, match=message) as error:
        await reserve(ledger, context, 'over-money', amount=5)
    assert error.value.code == 'budget_exceeded'
    for kind, owner in [('run', context.run_id), ('iteration', context.iteration_id)]:
        snapshot = await ledger.snapshot(kind, owner)
        assert snapshot['request_count'] == 1 and snapshot['reserved_micros'] == 6
    assert len(await store.list('model_invocation')) == 1


async def test_unlimited_requests_keep_uncertain_zero_cost_invocations_blocked_and_idempotent(store, context):
    ledger = BudgetLedger(store)
    context = with_limit(context, 0).model_copy(update={'cost_mode': 'request_limited'})
    await ledger.setup_accounts(context.run_id, context.iteration_id, 0, 0,
                                run_max_requests=0, iteration_max_requests=0)
    invocation = await reserve(ledger, context, 'ambiguous')
    await ledger.dispatch(invocation['id'])
    assert await ledger.reconcile_interrupted() == [invocation['id']]
    with pytest.raises(DomainError, match='reconciliation'):
        await reserve(ledger, context, 'new-call')
    same = await reserve(ledger, context, 'ambiguous')
    assert same['id'] == invocation['id'] and same['state'] == 'uncertain'
    with pytest.raises(DomainError, match='dispatch again'):
        await ledger.dispatch(same['id'])
    with pytest.raises(DomainError, match='automatically refunded'):
        await ledger.release_not_sent(same['id'], 'Must not erase unknown completion')
    snapshot = await ledger.snapshot('run', context.run_id)
    assert snapshot['request_count'] == 1 and snapshot['max_requests'] == 0
    assert (await store.read('model_attempt_budget', context.attempt_id))['uncertain_invocations'] == 1


async def test_unlimited_requests_still_require_restored_budget_reconciliation(store, context):
    ledger = BudgetLedger(store)
    context = with_limit(context, 0)
    await ledger.setup_accounts(context.run_id, context.iteration_id, 1000, 1000,
                                run_max_requests=0, iteration_max_requests=0)
    def mark(tx):
        account = tx.get('budget_account', account_id('run', context.run_id))
        return tx.put('budget_account', account['id'], {**account, 'restore_uncertain': True}, account['revision'])
    await store.command('test', 'restore', {}, mark)
    with pytest.raises(DomainError) as error:
        await reserve(ledger, context, 'after-restore')
    assert error.value.code == 'budget_requires_reconciliation'
    assert (await ledger.snapshot('iteration', context.iteration_id))['request_count'] == 0


@pytest.mark.parametrize('field', ['run_max_requests', 'iteration_max_requests', 'run_limit', 'iteration_limit'])
@pytest.mark.parametrize('invalid', [-1, False, True, 0.0, '0', None])
async def test_setup_requires_exact_nonnegative_integers_without_boolean_unlimited(store, context, field, invalid):
    values = {'run_limit': 1000, 'iteration_limit': 1000, 'run_max_requests': 0, 'iteration_max_requests': 0}
    values[field] = invalid
    with pytest.raises(DomainError) as error:
        await BudgetLedger(store).setup_accounts(context.run_id, context.iteration_id, **values)
    assert error.value.code == 'invalid_budget'
    assert await store.list('budget_account') == []


@pytest.mark.parametrize('invalid', [-1, False, True, 0.0, '0', None])
async def test_attempt_context_and_ledger_reject_coerced_unlimited_values(store, context, invalid):
    with pytest.raises(ValidationError):
        with_limit(context, invalid)
    # Defensive validation also catches an internal model_copy that skipped Pydantic validation.
    unchecked = context.model_copy(update={'max_model_requests': invalid})
    with pytest.raises(DomainError) as error:
        await reserve(BudgetLedger(store), unchecked, 'invalid-context')
    assert error.value.code == 'invalid_budget'


async def test_setup_cannot_switch_existing_limit_to_unlimited_or_replay_stale_settings(store, context):
    ledger = BudgetLedger(store)
    await ledger.setup_accounts(context.run_id, context.iteration_id, 1000, 1000,
                                run_max_requests=200, iteration_max_requests=200)
    with pytest.raises(DomainError) as error:
        await ledger.setup_accounts(context.run_id, context.iteration_id, 1000, 1000,
                                    run_max_requests=0, iteration_max_requests=0)
    assert error.value.code == 'budget_configuration_conflict'
    # Simulate only the persisted effects of an explicit owner grant in this isolated store.
    def explicit_grant(tx):
        for kind, owner in [('run', context.run_id), ('iteration', context.iteration_id)]:
            account = tx.get('budget_account', account_id(kind, owner))
            tx.put('budget_account', account['id'], {**account, 'max_requests': 0, 'request_count': 200}, account['revision'])
        return {}
    await store.command('test-owner-grant', 'unlimited', {}, explicit_grant)
    current = await ledger.setup_accounts(context.run_id, context.iteration_id, 1000, 1000,
                                        run_max_requests=0, iteration_max_requests=0)
    assert current['run']['max_requests'] == 0 and current['run']['request_count'] == 200
    with pytest.raises(DomainError) as error:
        await ledger.setup_accounts(context.run_id, context.iteration_id, 1000, 1000,
                                    run_max_requests=200, iteration_max_requests=200)
    assert error.value.code == 'budget_configuration_conflict'
    assert (await ledger.snapshot('run', context.run_id))['request_count'] == 200


async def test_corrupt_boolean_account_is_not_equal_to_explicit_zero_even_on_cached_setup(store, context):
    ledger = BudgetLedger(store)
    context = with_limit(context, 0)
    await ledger.setup_accounts(context.run_id, context.iteration_id, 1000, 1000,
                                run_max_requests=0, iteration_max_requests=0)
    def corrupt(tx):
        account = tx.get('budget_account', account_id('run', context.run_id))
        return tx.put('budget_account', account['id'], {**account, 'max_requests': False}, account['revision'])
    await store.command('test', 'invalid-stored-limit', {}, corrupt)
    with pytest.raises(DomainError) as error:
        await ledger.setup_accounts(context.run_id, context.iteration_id, 1000, 1000,
                                    run_max_requests=0, iteration_max_requests=0)
    assert error.value.code == 'budget_configuration_conflict'
    with pytest.raises(DomainError) as error:
        await reserve(ledger, context, 'corrupt')
    assert error.value.code == 'budget_configuration_conflict'
    assert (await ledger.snapshot('iteration', context.iteration_id))['request_count'] == 0

import asyncio

import pytest

from agentflow.common import DomainError
from agentflow.models.budget import BudgetLedger


async def test_concurrent_reservations_respect_both_accounts(store, context):
    ledger = BudgetLedger(store)
    await ledger.setup_accounts(context.run_id, context.iteration_id, 100, 100)

    async def reserve():
        return await ledger.reserve(context, protocol="responses", request_fingerprint="x",
                                    profile_revision=1, amount_micros=60, currency="USD")
    results = await asyncio.gather(reserve(), reserve(), return_exceptions=True)
    assert sum(isinstance(value, DomainError) for value in results) == 1
    assert (await ledger.snapshot("run", context.run_id))["reserved_micros"] == 60
    assert (await ledger.snapshot("iteration", context.iteration_id))["reserved_micros"] == 60


async def test_unknown_call_holds_money_blocks_retries_and_can_be_reconciled(store, context, price):
    ledger = BudgetLedger(store)
    await ledger.setup_accounts(context.run_id, context.iteration_id, 1000, 1000)
    inv = await ledger.reserve(context, protocol="responses", request_fingerprint="x", profile_revision=1,
                               amount_micros=100, currency="USD", idempotency_key="same-request-key")
    await ledger.dispatch(inv["id"])
    await ledger.uncertain(inv["id"], "response_lost")
    with pytest.raises(DomainError, match="reconciliation"):
        await ledger.reserve(context, protocol="responses", request_fingerprint="y", profile_revision=1,
                             amount_micros=100, currency="USD")
    with pytest.raises(DomainError):
        await ledger.release_not_sent(inv["id"], "lease expired is not proof")
    state = await ledger.snapshot("run", context.run_id)
    assert state["reserved_micros"] == state["uncertain_micros"] == 100
    await ledger.settle(inv["id"], price, 3, 4)
    await ledger.settle(inv["id"], price, 3, 4)
    state = await ledger.snapshot("run", context.run_id)
    assert state["reserved_micros"] == 0 and state["settled_micros"] == 7


async def test_idempotency_reuses_current_invocation_and_conflicting_payload_fails(store, context):
    ledger = BudgetLedger(store)
    await ledger.setup_accounts(context.run_id, context.iteration_id, 1000, 1000)
    kwargs = dict(protocol="responses", request_fingerprint="a", profile_revision=1,
                  amount_micros=100, currency="USD", idempotency_key="unique-key")
    first = await ledger.reserve(context, **kwargs)
    await ledger.dispatch(first["id"])
    again = await ledger.reserve(context, **kwargs)
    assert first["id"] == again["id"] and again["state"] == "dispatching"
    assert (await ledger.snapshot("run", context.run_id))["reserved_micros"] == 100
    with pytest.raises(DomainError):
        await ledger.reserve(context, **{**kwargs, "request_fingerprint": "changed"})
    with pytest.raises(DomainError):
        await ledger.dispatch(first["id"])


async def test_iteration_request_limit_cannot_be_reset_by_new_run_or_attempt(store, context):
    ledger = BudgetLedger(store)
    await ledger.setup_accounts(context.run_id, context.iteration_id, 1000, 1000, iteration_max_requests=1)
    await ledger.reserve(context, protocol="responses", request_fingerprint="a", profile_revision=1,
                         amount_micros=10, currency="USD")
    second = context.model_copy(update={"attempt_id": "attempt-two", "run_id": "run-two"})
    await ledger.setup_accounts(second.run_id, second.iteration_id, 1000, 1000, iteration_max_requests=1)
    with pytest.raises(DomainError, match="cumulative"):
        await ledger.reserve(second, protocol="responses", request_fingerprint="b", profile_revision=1,
                             amount_micros=10, currency="USD")


async def test_restart_reconciles_dispatch_without_refunding(store, context):
    ledger = BudgetLedger(store)
    await ledger.setup_accounts(context.run_id, context.iteration_id, 1000, 1000)
    inv = await ledger.reserve(context, protocol="responses", request_fingerprint="a", profile_revision=1,
                               amount_micros=100, currency="USD")
    await ledger.dispatch(inv["id"])
    restarted = BudgetLedger(store)
    assert await restarted.reconcile_interrupted() == [inv["id"]]
    assert (await ledger.snapshot("run", context.run_id))["uncertain_micros"] == 100


async def test_overrun_is_recorded_not_hidden(store, context, price):
    ledger = BudgetLedger(store)
    await ledger.setup_accounts(context.run_id, context.iteration_id, 10, 10)
    inv = await ledger.reserve(context, protocol="responses", request_fingerprint="a", profile_revision=1,
                               amount_micros=5, currency="USD")
    await ledger.dispatch(inv["id"])
    await ledger.settle(inv["id"], price, 10, 10)
    state = await ledger.snapshot("run", context.run_id)
    assert state["settled_micros"] == 20 and state["overrun_micros"] == 10


async def test_request_limited_run_keeps_unknown_cost_without_blocking_completed_response(store, context):
    context = context.model_copy(update={'cost_mode': 'request_limited', 'max_model_requests': 2})
    ledger = BudgetLedger(store)
    await ledger.setup_accounts(context.run_id, context.iteration_id, 0, 0, run_max_requests=2)
    first = await ledger.reserve(context, protocol='responses', request_fingerprint='one', profile_revision=1,
                                 amount_micros=0, currency='USD')
    await ledger.dispatch(first['id'])
    receipt = {'media_type': 'application/json', 'body': {'output': 'real completed response'}}
    await ledger.complete_unpriced(first['id'], {'input_tokens': 8, 'output_tokens': 13}, receipt)
    await ledger.complete_unpriced(first['id'], {'input_tokens': 8, 'output_tokens': 13}, receipt)
    state = await ledger.snapshot('run', context.run_id)
    assert state['total_cost_micros'] is None and state['cost_status'] == 'unknown'
    assert state['unpriced_completed_requests'] == 1 and state['request_count'] == 1
    assert (await store.read('model_invocation', first['id']))['actual_micros'] is None
    second = await ledger.reserve(context, protocol='responses', request_fingerprint='two', profile_revision=1,
                                  amount_micros=0, currency='USD')
    await ledger.dispatch(second['id'])
    await ledger.uncertain(second['id'], 'ambiguous_response')
    with pytest.raises(DomainError, match='reconciliation'):
        await ledger.reserve(context, protocol='responses', request_fingerprint='three', profile_revision=1,
                             amount_micros=0, currency='USD')


async def test_unpriced_completion_cannot_bypass_existing_strict_money_mode(store, context):
    ledger = BudgetLedger(store)
    await ledger.setup_accounts(context.run_id, context.iteration_id, 1000, 1000)
    invocation = await ledger.reserve(context, protocol='responses', request_fingerprint='strict',
                                     profile_revision=1, amount_micros=50, currency='USD')
    await ledger.dispatch(invocation['id'])
    with pytest.raises(DomainError, match='Strict monetary'):
        await ledger.complete_unpriced(invocation['id'], {}, {})
    assert (await ledger.snapshot('run', context.run_id))['reserved_micros'] == 50

"""Transactional two-level reservations. A lost response never implies a free call."""
from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.runtime.contracts import Store, require_record

from .profiles import AttemptContext, PricingPolicy


def account_id(kind: str, id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"agentflow:budget:{kind}:{id}"))


def _nonnegative_integer(value) -> bool:
    return type(value) is int and value >= 0


class BudgetLedger:
    def __init__(self, store: Store):
        self.store = store

    async def setup_accounts(
        self, run_id: str, iteration_id: str, run_limit: int, iteration_limit: int, currency: str = "USD",
        run_max_requests: int = 1000, iteration_max_requests: int = 10000,
    ) -> dict[str, Any]:
        if not all(_nonnegative_integer(value) for value in (
                run_limit, iteration_limit, run_max_requests, iteration_max_requests)):
            raise DomainError("invalid_budget", "Budget and request limits must be nonnegative integers", 422)
        payload = dict(run_id=run_id, iteration_id=iteration_id, run_limit=run_limit,
                       iteration_limit=iteration_limit, currency=currency,
                       run_max_requests=run_max_requests, iteration_max_requests=iteration_max_requests)

        def apply(tx):
            result = {}
            for kind, id, limit, count_limit in [("iteration", iteration_id, iteration_limit, iteration_max_requests), ("run", run_id, run_limit, run_max_requests)]:
                key = account_id(kind, id)
                existing = tx.get("budget_account", key)
                if existing:
                    if (existing["currency"] != currency or existing["limit_micros"] != limit
                            or not _nonnegative_integer(existing.get('max_requests'))
                            or existing["max_requests"] != count_limit):
                        raise DomainError("budget_configuration_conflict", "Use explicit budget revision to change limits")
                    result[kind] = existing
                else:
                    result[kind] = tx.put("budget_account", key, {
                        "owner_kind": kind, "owner_id": id, "currency": currency,
                        "limit_micros": limit, "reserved_micros": 0, "settled_micros": 0,
                        "uncertain_micros": 0, "request_count": 0, "max_requests": count_limit,
                    })
            return result

        setup_key = f"{run_id}:{iteration_id}:{canonical_digest(payload)}"
        await self.store.command("budget_setup", setup_key, payload, apply)
        # A previous settings fingerprint may replay after an explicit extension.
        # Re-read current accounts so cached setup cannot authorize old limits or
        # return stale counters. Only the owner's revision operation changes caps.
        current = {}
        for kind, identity, limit, count_limit in [("iteration", iteration_id, iteration_limit, iteration_max_requests),
                                                  ("run", run_id, run_limit, run_max_requests)]:
            account = await self.store.read('budget_account', account_id(kind, identity))
            if (not account or account['currency'] != currency or account['limit_micros'] != limit
                    or not _nonnegative_integer(account.get('max_requests')) or account['max_requests'] != count_limit):
                raise DomainError('budget_configuration_conflict', 'Use explicit budget revision to change limits')
            current[kind] = account
        return current

    async def reserve(
        self, context: AttemptContext, *, protocol: str, request_fingerprint: str,
        profile_revision: int, amount_micros: int, currency: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not _nonnegative_integer(amount_micros) or not _nonnegative_integer(context.max_model_requests):
            raise DomainError("invalid_budget", "Reservation and request limit must be nonnegative integers", 422)
        operation_id = str(uuid5(NAMESPACE_URL, f"model:{context.attempt_id}:{idempotency_key}")) if idempotency_key else str(uuid4())
        payload = {
            "operation_id": operation_id, "attempt_id": context.attempt_id, "run_id": context.run_id,
            "iteration_id": context.iteration_id, "profile_id": context.model_profile_id,
            "profile_revision": profile_revision, "protocol": protocol,
            "request_fingerprint": request_fingerprint, "fencing_token": context.fencing_token,
            "input_fingerprint": context.input_fingerprint, "amount_micros": amount_micros, "currency": currency,
            "cost_mode": context.cost_mode,
        }

        def apply(tx):
            attempt = tx.get("model_attempt_budget", context.attempt_id)
            if attempt and attempt["uncertain_invocations"]:
                raise DomainError("execution_uncertain", "Prior model call needs reconciliation")
            if attempt and not _nonnegative_integer(attempt.get('request_count')):
                raise DomainError('budget_configuration_conflict', 'Attempt request count is invalid')
            if attempt and context.max_model_requests > 0 and attempt["request_count"] >= context.max_model_requests:
                raise DomainError("request_limit_exceeded", "Attempt model request limit reached", 429)
            for kind, id in [("iteration", context.iteration_id), ("run", context.run_id)]:
                key = account_id(kind, id)
                account = require_record(tx, "budget_account", key)
                if account.get("restore_uncertain"):
                    raise DomainError("budget_requires_reconciliation", "Restored budget history must be reconciled before additional paid calls")
                if account["currency"] != currency:
                    raise DomainError("budget_currency_mismatch", "Budget and pricing currency differ")
                if (not _nonnegative_integer(account.get('max_requests'))
                        or not _nonnegative_integer(account.get('request_count'))):
                    raise DomainError('budget_configuration_conflict', 'Account request limit or count is invalid')
                # Zero removes only this layer's request-count ceiling. Accounting,
                # uncertainty and monetary reservations remain unchanged below.
                if account["max_requests"] > 0 and account["request_count"] >= account["max_requests"]:
                    raise DomainError("request_limit_exceeded", f"{kind} cumulative model request limit reached", 429)
                if account["settled_micros"] + account["reserved_micros"] + amount_micros > account["limit_micros"]:
                    raise DomainError("budget_exceeded", f"{kind} budget cannot cover maximum call cost", 429)
                tx.put("budget_account", key, {
                    **account, "reserved_micros": account["reserved_micros"] + amount_micros,
                    "request_count": account["request_count"] + 1,
                    **({"cost_status": "unknown"} if context.cost_mode == "request_limited" else {}),
                }, account["revision"])
            attempt_body = {
                "attempt_id": context.attempt_id,
                "request_count": (attempt["request_count"] if attempt else 0) + 1,
                "uncertain_invocations": 0,
            }
            tx.put("model_attempt_budget", context.attempt_id, attempt_body, attempt["revision"] if attempt else None)
            tx.put("model_invocation", operation_id, {
                **payload, "state": "reserved", "created_at": utc_now(), "usage": None,
                "request_ordinal": attempt_body["request_count"],
                "actual_micros": None, "response_receipt": None, "reason": None,
            })
            tx.event("model_budget_reserved", {"operation_id": operation_id, "amount_micros": amount_micros}, run_id=context.run_id)
            return {"operation_id": operation_id}

        # A duplicate command returns its original ID, but its current state must be read anew.
        await self.store.command("model_reserve", operation_id, payload, apply)
        invocation = await self.store.read("model_invocation", operation_id)
        if invocation is None:
            raise DomainError("budget_state_missing", "Persisted invocation cannot be read", 503)
        return invocation

    async def dispatch(self, operation_id: str) -> dict[str, Any]:
        def apply(tx):
            current = require_record(tx, "model_invocation", operation_id)
            if current["state"] != "reserved":
                raise DomainError("execution_uncertain", "Invocation is not safe to dispatch again")
            current = tx.put("model_invocation", operation_id, {
                **current, "state": "dispatching", "dispatch_started_at": utc_now(),
            }, current["revision"])
            tx.event("model_dispatching", {"operation_id": operation_id}, run_id=current["run_id"])
            return current

        # Random command ID is intentional: repeating dispatch must NOT replay an authorization.
        return await self.store.command("model_dispatch", str(uuid4()), {"operation_id": operation_id}, apply)

    async def release_not_sent(self, operation_id: str, reason: str, *, transport_failure=None) -> dict[str, Any]:
        return await self._finish(operation_id, state="released", actual_micros=0, reason=reason, transport_failure=transport_failure)

    async def uncertain(self, operation_id: str, reason: str, *, transport_failure=None) -> dict[str, Any]:
        return await self._finish(operation_id, state="uncertain", actual_micros=None, reason=reason, transport_failure=transport_failure)

    async def settle(
        self, operation_id: str, pricing: PricingPolicy, input_tokens: int, output_tokens: int,
        response_receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if min(input_tokens, output_tokens) < 0:
            raise DomainError("invalid_usage", "Negative token usage", 422)
        return await self._finish(
            operation_id, state="settled", actual_micros=pricing.cost(input_tokens, output_tokens),
            usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
            response_receipt=response_receipt,
        )

    async def complete_unpriced(self, operation_id: str, usage: dict, response_receipt: dict) -> dict:
        """A completed response with unknown price is not a free or ambiguous request."""
        invocation = await self.store.read("model_invocation", operation_id)
        if not invocation or invocation.get("cost_mode") != "request_limited":
            raise DomainError("strict_pricing_required", "Strict monetary budgets require priced settlement")
        return await self._finish(operation_id, state="completed_unpriced", actual_micros=None,
                                  usage=usage, response_receipt=response_receipt, reason="pricing_not_configured")

    async def _finish(
        self, operation_id: str, *, state: str, actual_micros: int | None,
        reason: str | None = None, usage: dict | None = None, response_receipt: dict | None = None,
        transport_failure: dict | None = None,
    ) -> dict[str, Any]:
        if transport_failure is not None:
            from .transport_failures import transport_failure_code
            if not transport_failure_code(transport_failure, operation_id, state=state, reason=reason):
                raise DomainError('invalid_transport_failure', 'Transport failure metadata is invalid', 422)
        payload = dict(operation_id=operation_id, state=state, actual_micros=actual_micros,
                       reason=reason, usage=usage, response_receipt=response_receipt)
        if transport_failure is not None:
            payload['transport_failure'] = transport_failure

        def apply(tx):
            inv = require_record(tx, "model_invocation", operation_id)
            if inv["state"] in {"settled", "released", "completed_unpriced"}:
                if inv["state"] != state or inv["actual_micros"] != actual_micros:
                    raise DomainError("settlement_conflict", "Invocation already has a different final settlement")
                return inv
            if state == "released" and inv["state"] == "uncertain":
                raise DomainError("settlement_uncertain", "Uncertain dispatch cannot be automatically refunded")
            if state == "uncertain" and inv["state"] == "uncertain":
                return inv
            amount = inv["amount_micros"]
            was_uncertain = inv["state"] == "uncertain"
            for kind, owner in [("iteration", inv["iteration_id"]), ("run", inv["run_id"])]:
                key = account_id(kind, owner)
                account = require_record(tx, "budget_account", key)
                patch = dict(account)
                if state == "uncertain":
                    patch["uncertain_micros"] += amount
                else:
                    patch["reserved_micros"] -= amount
                    patch["settled_micros"] += actual_micros or 0
                    if was_uncertain:
                        patch["uncertain_micros"] -= amount
                    if state == "completed_unpriced":
                        patch["unpriced_completed_requests"] = patch.get("unpriced_completed_requests", 0) + 1
                        patch["cost_status"] = "unknown"
                if min(patch["reserved_micros"], patch["uncertain_micros"]) < 0:
                    raise DomainError("budget_invariant", "Reservation accounting would become negative", 500)
                tx.put("budget_account", key, patch, account["revision"])
            attempt = require_record(tx, "model_attempt_budget", inv["attempt_id"])
            delta = 1 if state == "uncertain" else -1 if was_uncertain else 0
            tx.put("model_attempt_budget", inv["attempt_id"], {
                **attempt, "uncertain_invocations": attempt["uncertain_invocations"] + delta,
            }, attempt["revision"])
            inv = tx.put("model_invocation", operation_id, {
                **inv, "state": state, "actual_micros": actual_micros, "usage": usage,
                "response_receipt": response_receipt, "reason": reason, "updated_at": utc_now(),
                "overrun_micros": max(0, (actual_micros or 0) - amount),
                **({'transport_failure': transport_failure} if transport_failure is not None else {}),
            }, inv["revision"])
            tx.event("model_" + state, {"operation_id": operation_id, "actual_micros": actual_micros}, run_id=inv["run_id"])
            return inv

        return await self.store.command("model_settlement", str(uuid4()), payload, apply)

    async def reconcile_interrupted(self) -> list[str]:
        """Startup reconciliation: dispatch may have charged; never blindly resend or release."""
        changed = []
        for inv in await self.store.list("model_invocation"):
            if inv["state"] == "dispatching":
                await self.uncertain(inv["id"], "controller_restarted_during_dispatch")
                changed.append(inv["id"])
            elif inv["state"] == "reserved":
                await self.release_not_sent(inv["id"], "controller_restarted_before_dispatch")
                changed.append(inv["id"])
        return changed

    async def snapshot(self, kind: str, owner_id: str) -> dict[str, Any]:
        account = await self.store.read("budget_account", account_id(kind, owner_id))
        if account is None:
            raise DomainError("not_found", "Budget account is not configured", 404)
        used = account["settled_micros"] + account["reserved_micros"]
        return {
            **account, "available_micros": max(0, account["limit_micros"] - used),
            "overrun_micros": max(0, used - account["limit_micros"]),
            "total_cost_micros": None if account.get("cost_status") == "unknown" else account["settled_micros"],
        }

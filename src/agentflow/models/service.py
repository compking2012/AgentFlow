from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response, StreamingResponse

from agentflow.common import DomainError, canonical_digest
from agentflow.runtime.contracts import Store
from agentflow.runtime.trace import ExecutionTrace, ModelTrace

from .budget import BudgetLedger
from .profiles import AttemptContext, ModelProfile, ModelRegistry
from .provider import ModelProvider, ResponseTracker


async def _await(value):
    return await value if inspect.isawaitable(value) else value


async def _trace(trace, method, *args):
    if trace is not None:
        try:
            await _await(getattr(trace, method)(*args))
        except Exception:
            logging.getLogger(__name__).warning('Model trace deferred; execution evidence remains authoritative')


def _incomplete_response(code, *, stream=False):
    message = ('Model response reached the configured output limit before completion. Partial output was not accepted.'
               if code == 'model_output_limit' else 'Model response did not complete successfully. Partial output was not accepted.')
    body = {'error': {'type': 'agentflow_policy_error', 'code': code, 'message': message, 'param': None}}
    return {'failure_code': code, 'status_code': 200 if stream else 422,
            'media_type': 'text/event-stream' if stream else 'application/json',
            'body': 'event: error\ndata: ' + json.dumps(body, separators=(',', ':')) + '\n\n' if stream else body}


class ModelService:
    def __init__(
        self, store: Store, data_dir: Path,
        authorize_attempt: Callable[[str, str], AttemptContext | Awaitable[AttemptContext]],
        secret_resolver: Callable[[ModelProfile], str | Awaitable[str]],
        http_client: httpx.AsyncClient | None = None,
    ):
        self.store = store
        self.data_dir = Path(data_dir).resolve() / "model_invocations"
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.authorize_attempt = authorize_attempt
        self.secret_resolver = secret_resolver
        self.registry = ModelRegistry(store)
        self.ledger = BudgetLedger(store)
        self.provider = ModelProvider()
        self._owned_client = http_client is None
        if http_client is not None and http_client.trust_env:
            raise ValueError("Model HTTP client must disable inherited proxies/credentials (trust_env=False)")
        self.client = http_client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        self._active: dict[str, httpx.Response] = {}
        self.traces = ExecutionTrace(store)

    async def close(self) -> None:
        for operation_id in list(self._active):
            await self._cleanup(operation_id, "service_closed_during_dispatch")
        if self._owned_client:
            await self.client.aclose()

    async def list_profiles(self) -> list[dict]:
        return [await self._profile_view(await self.registry.get(profile["model_profile_id"]))
                for profile in await self.store.list("model_profile")]

    async def get_profile(self, profile_id: str) -> dict:
        return await self._profile_view(await self.registry.get(profile_id))

    async def _profile_view(self, profile: ModelProfile) -> dict:
        # Resolve only the locally configured credential. Never probe a provider,
        # serialize a secret, or expose resolver exception text in this view.
        try:
            secret = await _await(self.secret_resolver(profile))
            configured = isinstance(secret, str) and bool(secret)
        except Exception:
            configured = False
        return {**profile.public_view(), "credential_status": "configured" if configured else "missing"}

    async def probe_profile(self, profile_id: str, mode: str = "offline", **kwargs) -> dict:
        if mode != "offline":
            # Paid probes must be scheduled as an explicitly authorized diagnostic
            # attempt and use this same proxy, not a bypass here.
            raise DomainError("explicit_probe_attempt_required", "Live probes require an authorized diagnostic attempt")
        return await self.registry.probe(profile_id)

    async def _record_expiry(self, error, context, bearer_token, protocol):
        if not isinstance(error, DomainError) or error.code != 'task_authorization_expired':
            return
        from agentflow.runtime.task_authorization import record_expiry
        raw = await self.store.read('task_authorization', hashlib.sha256(bearer_token.encode()).hexdigest())
        if not raw or any(raw.get(field) != getattr(context, field) for field in (
                'attempt_id', 'run_id', 'iteration_id', 'model_profile_id', 'fencing_token', 'input_fingerprint')):
            return
        attempt = await self.store.read('attempt', context.attempt_id)
        work = await self.store.read('work_item', attempt['work_item_id']) if attempt else None
        if work:
            await record_expiry(self.store, raw, context, attempt, work, protocol)

    async def forward(
        self, protocol: str, payload: dict, bearer_token: str, idempotency_key: str | None = None
    ) -> Response:
        if protocol not in {"chat_completions", "responses"}:
            raise DomainError("unsupported_protocol", "Unknown model protocol", 422)
        context = AttemptContext.model_validate(await _await(self.authorize_attempt(bearer_token, protocol)))
        profile = await self.registry.get(context.model_profile_id)
        try:
            body, output_limit = self.provider.normalize_request(profile, context, protocol, payload)
        except DomainError as error:
            await self._record_expiry(error, context, bearer_token, protocol)
            raise
        if profile.pricing is None and context.cost_mode == "strict":
            raise DomainError("budget_unbounded", "No reviewed pricing bound is configured")
        amount = profile.pricing.reservation_cost(output_limit) if context.cost_mode == "strict" else 0
        key = await _await(self.secret_resolver(profile))
        if not isinstance(key, str) or not key:
            raise DomainError("credential_missing", "Configured provider credential is unavailable", 409)
        inv = await self.ledger.reserve(
            context, protocol=protocol, request_fingerprint=canonical_digest(body),
            profile_revision=profile.revision, amount_micros=amount,
            currency=profile.pricing.currency if profile.pricing and context.cost_mode == "strict" else "USD",
            idempotency_key=idempotency_key,
        )
        operation_id = inv["id"]
        if inv["state"] in {"settled", "completed_unpriced"} and inv.get("response_receipt"):
            return self._replay(inv["response_receipt"], operation_id)
        if inv["state"] != "reserved":
            raise DomainError("execution_uncertain", "Invocation cannot be automatically sent again", 409)
        try:
            fresh = AttemptContext.model_validate(await _await(self.authorize_attempt(bearer_token, protocol)))
            fresh.assert_current(protocol)
            if fresh.fencing_token != context.fencing_token or fresh.input_fingerprint != context.input_fingerprint:
                raise DomainError("stale_task_token", "Task changed before model dispatch", 403)
            fresh_profile = await self.registry.get(context.model_profile_id)
            fresh_profile.assert_accepted(protocol)
            if fresh_profile.revision != profile.revision:
                raise DomainError("stale_model_profile", "Model profile changed before dispatch", 409)
        except Exception as error:
            await self.ledger.release_not_sent(operation_id, "authorization_changed_before_dispatch")
            await self._record_expiry(error, context, bearer_token, protocol)
            raise
        await self.ledger.dispatch(operation_id)
        trace = ModelTrace(self.traces, context.attempt_id, operation_id, profile.accepted_api_model,
                           protocol, (key, bearer_token))
        try:
            await _trace(trace, 'request', body)
            # Logging can yield to cancellation/revision. Recheck the same
            # frozen authority at the final boundary before any HTTP send.
            latest = AttemptContext.model_validate(await _await(self.authorize_attempt(bearer_token, protocol)))
            latest_profile = await self.registry.get(context.model_profile_id)
            if (any(getattr(latest, field) != getattr(context, field) for field in
                    ('attempt_id', 'run_id', 'iteration_id', 'model_profile_id', 'fencing_token', 'input_fingerprint'))
                    or latest_profile.revision != profile.revision):
                raise DomainError('stale_task_token', 'Task authority changed before model HTTP send', 403)
            latest.assert_current(protocol)
            endpoint = "/chat/completions" if protocol == "chat_completions" else "/responses"
            request = self.client.build_request(
                "POST", profile.base_url + endpoint, json=body,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=profile.request_timeout_seconds,
            )
        except asyncio.CancelledError:
            # Dispatch authority was acquired, but HTTP has not been called.
            # Do not leave an unsent request stranded in dispatching because
            # the optional trace write happened to be the cancellation point.
            await asyncio.shield(self.ledger.release_not_sent(operation_id, 'cancelled_before_http_send'))
            raise
        except Exception as error:
            await self.ledger.release_not_sent(operation_id, 'request_not_sent')
            await self._record_expiry(error, context, bearer_token, protocol)
            raise
        try:
            upstream = await self.client.send(request, stream=True, follow_redirects=False)
        except httpx.ConnectError as exc:
            from .transport_failures import caught_transport_failure
            await self.ledger.release_not_sent(operation_id, "connection_not_established",
                transport_failure=caught_transport_failure(exc, operation_id, delivery='not_sent'))
            await _trace(trace, 'finish', 'connection_failed')
            raise DomainError("upstream_unavailable", "Could not connect to configured provider", 502) from exc
        except asyncio.CancelledError:
            await asyncio.shield(self.ledger.uncertain(operation_id, "dispatch_result_unknown"))
            await asyncio.shield(_trace(trace, 'finish', 'cancelled'))
            raise
        except Exception as exc:
            from .transport_failures import caught_transport_failure
            await self.ledger.uncertain(operation_id, "dispatch_result_unknown",
                transport_failure=caught_transport_failure(exc, operation_id))
            await _trace(trace, 'finish', 'unknown')
            raise DomainError("upstream_failure", "Configured provider request outcome is unknown", 502) from exc
        self._active[operation_id] = upstream
        headers = {"X-AgentFlow-Invocation-ID": operation_id, "Cache-Control": "no-store"}
        if 300 <= upstream.status_code < 400:
            await self._cleanup(operation_id, "upstream_redirect_rejected")
            raise DomainError("upstream_redirect_rejected", "Configured model endpoint redirected", 502)
        if body.get("stream") and upstream.status_code < 400:
            if "text/event-stream" not in upstream.headers.get("content-type", ""):
                await self._cleanup(operation_id, "upstream_not_event_stream")
                raise DomainError("invalid_response", "Provider did not return requested event stream", 502)
            return StreamingResponse(
                self._stream(upstream, profile, protocol, operation_id, trace, output_limit=output_limit),
                status_code=upstream.status_code, media_type="text/event-stream", headers=headers,
                background=BackgroundTask(self._cleanup, operation_id, "stream_not_consumed"),
            )
        return await self._nonstream(upstream, profile, protocol, operation_id, headers, trace, output_limit=output_limit)

    async def _read_bounded(self, response: httpx.Response, maximum: int) -> bytes:
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > maximum:
                raise DomainError("response_too_large", "Provider response exceeds configured bound", 502)
        return bytes(data)

    async def _nonstream(self, upstream, profile, protocol, operation_id, headers, trace=None, *, output_limit=None):
        traced_status = 'failed'
        try:
            raw = await self._read_bounded(upstream, profile.max_response_bytes)
            # Retain bounded raw evidence even when JSON/protocol validation
            # fails; only validated accounting receives a replayable receipt.
            path = self._save_complete(operation_id, raw, ".json")
            payload = json.loads(raw)
            tracker = ResponseTracker(protocol, output_limit=output_limit)
            if upstream.status_code < 400:
                tracker.observe(payload)
            elif isinstance(payload, dict):
                tracker.error_seen = True
                # Error usage, if present, is still billable evidence.
                try:
                    tracker.observe(payload)
                except DomainError:
                    pass
            receipt = {"path": str(path), "digest": canonical_digest(payload), "status_code": upstream.status_code,
                       "media_type": "application/json", "body": payload,
                       'completion': tracker.completion_metadata()}
            if upstream.status_code < 400 and tracker.failure_code:
                receipt['client_response'] = _incomplete_response(tracker.failure_code)
            await self._account(tracker, profile, operation_id, receipt)
            await _trace(trace, 'observe', payload)
            if upstream.status_code >= 400:
                await _trace(trace, 'emit', 'error', '模型接口错误', payload.get('error', {}) if isinstance(payload, dict) else {})
            traced_status = tracker.terminal_kind or 'completed' if upstream.status_code < 400 else f'http_{upstream.status_code}'
            if receipt.get('client_response'):
                response = receipt['client_response']
                await _trace(trace, 'emit', 'error', '模型响应未完整生成', response['body']['error'])
                return JSONResponse(response['body'], status_code=response['status_code'],
                    headers={**headers, 'X-AgentFlow-Failure-Code': response['failure_code']})
            return JSONResponse(payload, status_code=upstream.status_code, headers=headers)
        except asyncio.CancelledError:
            await asyncio.shield(self._mark_if_inflight(operation_id, "response_incomplete_or_invalid"))
            raise
        except Exception as exc:
            await self._mark_if_inflight(operation_id, "response_incomplete_or_invalid")
            if isinstance(exc, DomainError):
                raise
            raise DomainError("upstream_failure", "Provider response is incomplete or invalid", 502) from exc
        finally:
            await asyncio.shield(_trace(trace, 'finish', traced_status))
            await asyncio.shield(upstream.aclose())
            self._active.pop(operation_id, None)

    async def _stream(self, upstream, profile, protocol, operation_id, trace=None, *, output_limit=None):
        tracker = ResponseTracker(protocol, output_limit=output_limit)
        path = self.data_dir / f"{operation_id}.partial"
        file = path.open("xb")
        os.chmod(path, 0o600)
        total = 0
        digest = hashlib.sha256()
        finalized = False
        receipt = None

        async def finalize():
            nonlocal finalized, receipt
            tracker.finish()
            file.flush()
            os.fsync(file.fileno())
            file.close()
            final = path.with_suffix(".sse")
            path.replace(final)
            receipt = {"path": str(final), "digest": "sha256:" + digest.hexdigest(),
                       "status_code": upstream.status_code, "media_type": "text/event-stream",
                       'completion': tracker.completion_metadata()}
            if protocol == 'chat_completions' and tracker.failure_code:
                receipt['client_response'] = _incomplete_response(tracker.failure_code, stream=True)
            await asyncio.shield(self._account(tracker, profile, operation_id, receipt))
            finalized = True

        try:
            async for chunk in upstream.aiter_bytes():
                total += len(chunk)
                if total > profile.max_response_bytes:
                    raise DomainError("response_too_large", "Provider stream exceeds configured bound", 502)
                file.write(chunk)
                digest.update(chunk)
                tracker.feed(chunk)
                await _trace(trace, 'feed', chunk)
                if tracker.terminal:
                    # Persist accounting before the client consumes a terminal marker
                    # and closes its stream without waiting for a transport EOF.
                    await finalize()
                if protocol != 'chat_completions':
                    yield chunk
                if finalized:
                    break
            if not finalized:
                await finalize()
            if protocol == 'chat_completions':
                # A length-marked response can still contain syntactically valid
                # tool JSON. Withhold all Chat output from the SDK until the full
                # response is checked; owner trace remains live above.
                rejected = receipt.get('client_response')
                if rejected:
                    await _trace(trace, 'emit', 'error', '模型响应未完整生成', {'code': rejected['failure_code']})
                    yield rejected['body'].encode('utf-8')
                else:
                    with Path(receipt['path']).open('rb') as stored:
                        while chunk := stored.read(65536):
                            yield chunk
        except asyncio.CancelledError:
            await asyncio.shield(self._mark_if_inflight(operation_id, "consumer_disconnected"))
            raise
        except Exception:
            await asyncio.shield(self._mark_if_inflight(operation_id, "stream_incomplete_or_invalid"))
            yield b'event: error\ndata: {"error":{"code":"execution_uncertain","message":"Stream verification failed; invocation requires reconciliation."}}\n\n'
        finally:
            await asyncio.shield(_trace(trace, 'finish', tracker.terminal_kind or 'completed' if finalized else 'incomplete'))
            if not file.closed:
                file.flush()
                file.close()
            await asyncio.shield(upstream.aclose())
            self._active.pop(operation_id, None)

    async def _account(self, tracker, profile, operation_id, receipt):
        if tracker.response_model is not None and tracker.response_model != profile.accepted_api_model:
            await self.ledger.uncertain(operation_id, "provider_reported_different_model")
            raise DomainError("provider_model_mismatch", "Provider reported a different model than the accepted profile", 502)
        invocation = await self.store.read("model_invocation", operation_id)
        if invocation.get("cost_mode") == "request_limited":
            return await self.ledger.complete_unpriced(operation_id,
                {"input_tokens": tracker.input_tokens, "output_tokens": tracker.output_tokens}, receipt)
        if tracker.input_tokens is None or tracker.output_tokens is None:
            await self.ledger.uncertain(operation_id, "provider_usage_missing")
            return
        settled = await self.ledger.settle(
            operation_id, profile.pricing, tracker.input_tokens, tracker.output_tokens, receipt,
        )
        if settled.get("overrun_micros"):
            raise DomainError("provider_cost_overrun", "Reported provider usage exceeded reserved maximum", 502)

    async def _mark_if_inflight(self, operation_id, reason):
        inv = await self.store.read("model_invocation", operation_id)
        if inv and inv["state"] == "dispatching":
            await self.ledger.uncertain(operation_id, reason)

    async def _cleanup(self, operation_id, reason):
        response = self._active.pop(operation_id, None)
        if response:
            await response.aclose()
        await self._mark_if_inflight(operation_id, reason)

    def _save_complete(self, operation_id, data, suffix):
        path = self.data_dir / f"{operation_id}{suffix}.tmp"
        with path.open("xb") as file:
            os.chmod(path, 0o600)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        final = path.with_suffix("")
        path.replace(final)
        return final

    def _replay(self, receipt, operation_id):
        headers = {"X-AgentFlow-Invocation-ID": operation_id, "Cache-Control": "no-store"}
        if receipt["media_type"] == "application/json":
            if receipt.get('client_response'):
                response = receipt['client_response']
                return JSONResponse(response['body'], status_code=response['status_code'],
                    headers={**headers, 'X-AgentFlow-Failure-Code': response['failure_code']})
            return JSONResponse(receipt["body"], status_code=receipt["status_code"], headers=headers)
        path = Path(receipt["path"]).resolve()
        if not path.is_relative_to(self.data_dir) or not path.is_file():
            raise DomainError("evidence_missing", "Completed invocation evidence is unavailable", 409)
        if "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != receipt.get("digest"):
            raise DomainError("evidence_corrupt", "Completed invocation stream digest does not match", 409)

        async def chunks():
            if receipt.get('client_response'):
                yield receipt['client_response']['body'].encode('utf-8')
                return
            with path.open("rb") as file:
                while chunk := file.read(65536):
                    yield chunk
        return StreamingResponse(chunks(), status_code=receipt["status_code"], media_type="text/event-stream", headers=headers)


def create_model_router(service: ModelService) -> APIRouter:
    router = APIRouter(prefix="/internal/v1/llm", tags=["model_proxy"])

    async def invoke(request: Request, protocol: str):
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise DomainError("unauthenticated", "Attempt bearer token is required", 401)
        token = authorization[7:]
        # Authenticate before reading potentially large model input. Early
        # denials use the same closed policy envelope as the final send guard.
        try:
            await _await(service.authorize_attempt(token, protocol))
        except DomainError as exc:
            return JSONResponse({'error': {'type': 'agentflow_policy_error', 'code': exc.code,
                                          'message': exc.message, 'param': None}}, status_code=exc.status,
                                headers={'X-AgentFlow-Failure-Code': exc.code})
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8 * 1024 * 1024:
                raise DomainError("request_too_large", "Proxy request exceeds bound", 413)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("invalid_request", "Expected JSON model request", 422) from exc
        try:
            return await service.forward(protocol, payload, token, request.headers.get("idempotency-key"))
        except DomainError as exc:
            return JSONResponse({"error": {"type": "agentflow_policy_error", "code": exc.code,
                                             "message": exc.message, "param": None}}, status_code=exc.status,
                                headers={"X-AgentFlow-Failure-Code": exc.code})

    @router.post("/chat/completions")
    async def chat(request: Request):
        return await invoke(request, "chat_completions")

    @router.post("/responses")
    async def responses(request: Request):
        return await invoke(request, "responses")

    return router

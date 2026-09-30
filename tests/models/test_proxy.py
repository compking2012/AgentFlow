import json

import pytest

from agentflow.common import DomainError
from agentflow.models.service import ModelService


async def make_service(store, tmp_path, context, profile):
    async def authorize(token, protocol):
        if token != "task-only-token":
            raise DomainError("unauthenticated", "Invalid task token", 401)
        return context
    service = ModelService(store, tmp_path, authorize, lambda _: "upstream-only-key")
    await service.registry.register(profile, "register-profile")
    await service.ledger.setup_accounts(context.run_id, context.iteration_id, 10000, 10000)
    return service


async def test_real_http_json_proxy_auth_budget_and_replay(store, tmp_path, context, http_stub, profile_factory, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "personal-subscription-must-not-leak")
    data = {"id": "resp-fixture", "status": "completed", "output": [], "model": "fixture-model",
            "usage": {"input_tokens": 3, "output_tokens": 4}}
    with http_stub(lambda _: (200, {"Content-Type": "application/json"}, json.dumps(data).encode())) as (url, calls):
        service = await make_service(store, tmp_path, context, profile_factory(url))
        try:
            body = {"model": "fixture-model", "input": "protocol fixture, not a live model"}
            result = await service.forward("responses", body, "task-only-token", "same-request-id")
            replay = await service.forward("responses", body, "task-only-token", "same-request-id")
            assert json.loads(result.body) == json.loads(replay.body)
            assert len(calls) == 1 and calls[0]["path"] == "/responses"
            assert calls[0]["authorization"] == "Bearer upstream-only-key"
            assert (await service.ledger.snapshot("run", context.run_id))["settled_micros"] == 7
        finally:
            await service.close()


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_real_http_streaming_usage_and_replay(protocol, store, tmp_path, context, http_stub, profile_factory):
    if protocol == "chat_completions":
        chunks = [b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
                  b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3}}\n\n',
                  b'data: [DONE]\n\n']
        body = {"model": "fixture-model", "messages": [{"role": "user", "content": "fixture"}], "stream": True}
    else:
        chunks = [b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ok"}\n\n',
                  b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":2,"output_tokens":3}}}\n\n']
        body = {"model": "fixture-model", "input": "fixture", "stream": True}
    with http_stub(lambda _: (200, {"Content-Type": "text/event-stream"}, chunks)) as (url, calls):
        service = await make_service(store, tmp_path, context, profile_factory(url))
        try:
            result = await service.forward(protocol, body, "task-only-token", "stream-key")
            observed = b"".join([chunk async for chunk in result.body_iterator])
            assert observed == b"".join(chunks)
            state = await service.ledger.snapshot("run", context.run_id)
            assert state["settled_micros"] == 5 and state["reserved_micros"] == 0
            replay = await service.forward(protocol, body, "task-only-token", "stream-key")
            assert b"".join([chunk async for chunk in replay.body_iterator]) == observed
            assert len(calls) == 1
        finally:
            await service.close()


async def test_partial_stream_holds_reservation_and_stops_new_calls(store, tmp_path, context, http_stub, profile_factory):
    chunks = [b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n']
    with http_stub(lambda _: (200, {"Content-Type": "text/event-stream"}, chunks)) as (url, calls):
        service = await make_service(store, tmp_path, context, profile_factory(url))
        try:
            result = await service.forward("responses", {"model": "fixture-model", "input": "x", "stream": True}, "task-only-token")
            output = b"".join([chunk async for chunk in result.body_iterator])
            assert b"execution_uncertain" in output
            assert (await service.ledger.snapshot("run", context.run_id))["uncertain_micros"] > 0
            with pytest.raises(DomainError, match="reconciliation"):
                await service.forward("responses", {"model": "fixture-model", "input": "x"}, "task-only-token")
            assert len(calls) == 1
        finally:
            await service.close()


async def test_pending_profile_never_contacts_server(store, tmp_path, context, http_stub, profile_factory):
    with http_stub(lambda _: (500, {}, b"")) as (url, calls):
        profile = profile_factory(url).model_copy(update={"acceptance_status": "pending_user_confirmation", "accepted_api_model": None})
        service = await make_service(store, tmp_path, context, profile)
        try:
            assert (await service.probe_profile(profile.model_profile_id))["paid_requests_started"] == 0
            with pytest.raises(DomainError, match="accepted"):
                await service.forward("responses", {"model": "fixture-model", "input": "x"}, "task-only-token")
            assert calls == [] and await store.list("model_invocation") == []
        finally:
            await service.close()


async def test_bad_json_is_unknown_and_redirect_not_followed(store, tmp_path, context, http_stub, profile_factory):
    with http_stub(lambda _: (200, {"Content-Type": "application/json"}, b"bad json")) as (url, calls):
        service = await make_service(store, tmp_path, context, profile_factory(url))
        try:
            with pytest.raises(DomainError, match="invalid"):
                await service.forward("responses", {"model": "fixture-model", "input": "x"}, "task-only-token")
            assert len(calls) == 1
            assert (await service.ledger.snapshot("run", context.run_id))["uncertain_micros"] > 0
        finally:
            await service.close()


async def test_public_profile_reports_local_credential_presence_without_upstream(store, tmp_path, context, http_stub, profile_factory):
    with http_stub(lambda _: (500, {}, b"")) as (url, calls):
        service = await make_service(store, tmp_path, context, profile_factory(url))
        try:
            profile = await service.get_profile("profile-one")
            assert profile["credential_status"] == "configured"
            assert "upstream-only-key" not in json.dumps(profile)
            assert "credential_reference" not in profile and "base_url" not in profile
            assert (await service.list_profiles())[0]["credential_status"] == "configured"
            service.secret_resolver = lambda _: ""
            assert (await service.get_profile("profile-one"))["credential_status"] == "missing"
            assert calls == []
        finally:
            await service.close()


async def test_profile_revoked_before_dispatch_releases_only_unsent_reservation(store, tmp_path, context, http_stub, profile_factory):
    with http_stub(lambda _: (500, {}, b"")) as (url, calls):
        profile = profile_factory(url)
        service = await make_service(store, tmp_path, context, profile)
        authorizations = 0

        async def authorize(token, protocol):
            nonlocal authorizations
            authorizations += 1
            if authorizations == 2:
                await service.registry.register(profile.model_copy(update={"acceptance_status": "rejected"}),
                                                "revoke-profile", expected_revision=1)
            return context

        service.authorize_attempt = authorize
        try:
            with pytest.raises(DomainError, match="accepted"):
                await service.forward("responses", {"model": "fixture-model", "input": "x"}, "task-only-token")
            assert calls == []
            snapshot = await service.ledger.snapshot("run", context.run_id)
            assert snapshot["reserved_micros"] == 0 and snapshot["settled_micros"] == 0
        finally:
            await service.close()


async def test_provider_cannot_silently_report_a_different_model(store, tmp_path, context, http_stub, profile_factory):
    data = {"status": "completed", "model": "unexpected-model", "output": [],
            "usage": {"input_tokens": 2, "output_tokens": 3}}
    with http_stub(lambda _: (200, {"Content-Type": "application/json"}, json.dumps(data).encode())) as (url, calls):
        service = await make_service(store, tmp_path, context, profile_factory(url))
        try:
            with pytest.raises(DomainError, match="different model"):
                await service.forward("responses", {"model": "fixture-model", "input": "x"}, "task-only-token")
            assert len(calls) == 1
            assert (await service.ledger.snapshot("run", context.run_id))["uncertain_micros"] > 0
        finally:
            await service.close()


async def test_explicit_request_limited_proxy_accepts_unpriced_complete_response_and_replays(store, tmp_path, context, http_stub, profile_factory):
    context = context.model_copy(update={'cost_mode': 'request_limited'})
    value = {'id': 'fixture-complete', 'model': 'fixture-model', 'status': 'completed', 'output': []}
    with http_stub(lambda _: (200, {'Content-Type': 'application/json'}, json.dumps(value).encode())) as (url, calls):
        profile = profile_factory(url).model_copy(update={'pricing': None})
        service = await make_service(store, tmp_path, context, profile)
        try:
            body = {'model': 'fixture-model', 'input': 'explicit request-limited fixture'}
            response = await service.forward('responses', body, 'task-only-token', 'idempotent')
            replay = await service.forward('responses', body, 'task-only-token', 'idempotent')
            assert json.loads(response.body) == json.loads(replay.body) and len(calls) == 1
            assert (await service.ledger.snapshot('run', context.run_id))['total_cost_micros'] is None
            assert (await store.list('model_invocation'))[0]['state'] == 'completed_unpriced'
        finally:
            await service.close()

"""Real SDK + two real HTTP hops; only the upstream model response is scripted."""
from __future__ import annotations

import asyncio
import copy
import importlib.metadata
import json
import platform
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from agentflow.adapters.openhands import OpenHandsRoleAdapter
from agentflow.adapters.openhands.compat import normalize_optional_chat_usage
from agentflow.common import canonical_digest
from agentflow.models.profiles import AttemptContext, ModelProfile, PricingPolicy
from agentflow.models.service import ModelService, create_model_router
from agentflow.runtime.sandbox import MacSeatbeltSandbox
from agentflow.runtime.supervisor import Supervisor
from agentflow.server import ManagedServer

REAL_USAGE_SHAPE = {
    "prompt_tokens": 6149, "completion_tokens": 284, "total_tokens": 6433,
    "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 6149,
    "prompt_tokens_details": {"cached_tokens": 0},
    "completion_tokens_details": {"reasoning_tokens": 92},
}
USAGE_CASES = [
    pytest.param(REAL_USAGE_SHAPE, "strict", id="deepseek-optional-cache-field-absent"),
    pytest.param(REAL_USAGE_SHAPE, "request_limited", id="deepseek-unpriced-accounting"),
    pytest.param({**REAL_USAGE_SHAPE, "prompt_cache_hit_tokens": 55, "prompt_cache_miss_tokens": 6094,
                  "prompt_tokens_details": {"cached_tokens": 55}}, "strict", id="nonzero-cache-read"),
    pytest.param({**REAL_USAGE_SHAPE, "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 18}},
                 "strict", id="reported-cache-write"),
    pytest.param({"prompt_tokens": 6149, "completion_tokens": 284, "total_tokens": 6433},
                 "strict", id="no-optional-details"),
]


@pytest.mark.parametrize("details,cache_read,cache_write", [
    ({"cached_tokens": 0}, 0, 0),
    ({"cached_tokens": 55}, 55, 0),
    ({"cached_tokens": 0, "cache_creation_tokens": 15}, 0, 15),
    ({"cached_tokens": 0, "cache_write_tokens": 18}, 0, 18),
    ({"cached_tokens": 0, "cache_creation_tokens": 0}, 0, 0),
    ({"cached_tokens": 0, "cache_creation_tokens": None}, 0, 0),
    (None, 0, 0),
])
def test_usage_metadata_repair_preserves_values_original_objects_and_serialization(
        details, cache_read, cache_write, monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    types = pytest.importorskip("litellm.types.utils")
    telemetry = pytest.importorskip("openhands.sdk.llm.utils.telemetry")
    usage = {"prompt_tokens": 6149, "completion_tokens": 284, "total_tokens": 6433,
        "prompt_tokens_details": details, "completion_tokens_details": {"reasoning_tokens": 92}}
    original = types.ModelResponse(model="deepseek-flash", usage=usage)
    original._hidden_params["fixture_transport_metadata"] = "preserved"
    json_before = original.model_dump_json()
    explicit_json_before = original.model_dump_json(exclude_unset=True)
    response_fields = set(original.model_fields_set)
    usage_fields = set(original.usage.model_fields_set)
    original_details = original.usage.prompt_tokens_details
    detail_fields = set(original_details.model_fields_set) if original_details is not None else None
    normalized = normalize_optional_chat_usage(original)
    assert normalized.model_dump_json() == json_before
    assert normalized.model_dump_json(exclude_unset=True) == explicit_json_before
    assert original.model_dump_json() == json_before
    assert original.model_fields_set == response_fields and original.usage.model_fields_set == usage_fields
    if original_details is not None:
        assert original_details.model_fields_set == detail_fields
        if "cache_creation_tokens" in detail_fields and not hasattr(original_details, "cache_creation_tokens"):
            assert normalized is not original and normalized.usage is not original.usage
            assert normalized.usage.prompt_tokens_details is not original_details
            assert normalized.usage.prompt_tokens_details.model_fields_set == detail_fields - {"cache_creation_tokens"}
        else:
            assert normalized is original
    else:
        assert normalized is original
    assert normalized._hidden_params == original._hidden_params
    assert normalize_optional_chat_usage(normalized) is normalized
    assert (normalized.usage.prompt_tokens, normalized.usage.completion_tokens, normalized.usage.total_tokens) == (6149, 284, 6433)
    assert normalized.usage.completion_tokens_details.reasoning_tokens == 92
    assert telemetry.Telemetry._cache_buckets(normalized.usage) == (cache_read, cache_write)


def test_missing_usage_is_not_invented(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    types = pytest.importorskip("litellm.types.utils")
    response = types.ModelResponse(model="deepseek-flash")
    before = response.model_dump_json(exclude_unset=True)
    assert normalize_optional_chat_usage(response) is response
    assert response.model_dump_json(exclude_unset=True) == before


@pytest.mark.parametrize("usage,cost_mode", USAGE_CASES)
async def test_actual_sdk_optional_usage_keeps_original_receipts_and_budget(task, store, tmp_path, http_fixture,
                                                                          usage, cost_mode):
    if platform.system() != "Darwin":
        pytest.skip("Actual supervised SDK requires the verified macOS sandbox")
    try:
        assert importlib.metadata.version("openhands-sdk") == "1.49.2"
        assert importlib.metadata.version("litellm") == "1.101.0"
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("Install the locked optional OpenHands dependencies")
    sent = {}
    model = "deepseek-flash"

    def answer(call, number):
        assert call["path"] == "/v1/chat/completions"
        assert call["authorization"] == "Bearer upstream-usage-fixture-secret"
        assert call["body"]["model"] == model and number <= 2
        name = "agentflow_io" if number == 1 else "finish"
        arguments = ({"operation": "write_document", "arguments": {"path": "review.md", "content": "usage fixture"}}
            if number == 1 else {"message": "done", "result": {"review": "usage fixture"}})
        body = {"id": f"usage-response-{number}", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call-{number}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]},
                "finish_reason": "tool_calls"}], "usage": copy.deepcopy(usage)}
        # Non-canonical whitespace proves the proxy retains original bytes.
        raw = (json.dumps(body, indent=2) + "\n").encode()
        sent[body["id"]] = raw
        return 200, {"Content-Type": "application/json"}, raw

    context = AttemptContext(attempt_id=task.attempt_id, run_id=task.run_id, iteration_id=task.iteration_id,
        model_profile_id=task.model_profile_id, fencing_token=task.fencing_token,
        input_fingerprint=task.input_fingerprint, expires_at=(datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
        max_model_requests=2, max_output_tokens=4096, protocols=["chat_completions"], cost_mode=cost_mode)

    def authorize(token, protocol):
        assert token == task.proxy_token.get_secret_value() and protocol == "chat_completions"
        return context

    service = ModelService(store, tmp_path, authorize, lambda _: "upstream-usage-fixture-secret")
    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / "sandbox_profiles"))
    with http_fixture(answer) as (upstream, calls):
        await service.registry.register(ModelProfile(model_profile_id=task.model_profile_id, provider="local_test",
            requested_model=model, accepted_api_model=model, acceptance_status="accepted", base_url=upstream,
            protocols=["chat_completions"], credential_reference="fixture-only", allow_loopback_upstream=True,
            pricing=PricingPolicy(input_micros_per_million=1_000_000, output_micros_per_million=1_000_000,
                input_token_upper_bound=7000, input_bound_verified=True, output_control_verified=True,
                source_version="test-fixture") if cost_mode == "strict" else None), "usage-profile")
        await service.ledger.setup_accounts(task.run_id, task.iteration_id, 100_000, 100_000,
            run_max_requests=2, iteration_max_requests=2)
        app = FastAPI()
        app.include_router(create_model_router(service))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        server = ManagedServer(uvicorn.Config(app, proxy_headers=False, access_log=False, log_level="warning", lifespan="off"))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    assert not server_task.done()
                    await asyncio.sleep(.01)
            task = task.model_copy(update={"model": model, "proxy_base_url": f"http://127.0.0.1:{port}/internal/v1/llm",
                "max_active_seconds": 45})
            await adapter.start(task)
            await supervisor.wait(task.attempt_id)
            result = await adapter.collect_artifacts(task.attempt_id)
            error = task.artifact_dir / "role_error.json"
            assert result["execution_status"] == "completed", error.read_text() if error.exists() else result
            assert result["result"] == {"review": "usage fixture"}
            assert result["tool_calls"] == 2 and len(calls) == 2
            assert (task.artifact_dir / "review.md").read_text() == "usage fixture"
            assert (task.workspace / "original.py").read_text() == "ORIGINAL = True\n"
            invocations = await store.list("model_invocation")
            assert len(invocations) == 2
            for invocation in invocations:
                receipt = invocation["response_receipt"]
                raw = sent[receipt["body"]["id"]]
                assert Path(receipt["path"]).read_bytes() == raw
                assert receipt["body"] == json.loads(raw) and receipt["body"]["usage"] == usage
                assert receipt["digest"] == canonical_digest(json.loads(raw))
                assert invocation["usage"] == {"input_tokens": 6149, "output_tokens": 284}
                assert invocation["state"] == ("settled" if cost_mode == "strict" else "completed_unpriced")
                assert invocation["actual_micros"] == (6433 if cost_mode == "strict" else None)
                replay = service._replay(receipt, invocation["id"])
                assert json.loads(replay.body) == json.loads(raw)
            assert len(calls) == 2, "SDK response handling or receipt replay repeated an upstream request"
            budget = await service.ledger.snapshot("run", task.run_id)
            assert budget["request_count"] == 2 and budget["reserved_micros"] == 0 and budget["uncertain_micros"] == 0
            if cost_mode == "strict":
                assert budget["settled_micros"] == 12866
            else:
                assert budget["total_cost_micros"] is None
        finally:
            await supervisor.close()
            server.should_exit = True
            await asyncio.wait_for(server_task, 10)
            listener.close()
            await service.close()

import asyncio
import importlib.metadata
import json
import platform
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agentflow.adapters.codex import CodexExecAdapter
from agentflow.adapters.openhands import OpenHandsRoleAdapter
from agentflow.models.profiles import AttemptContext, ModelProfile
from agentflow.models.provider import ModelProvider
from agentflow.runtime.sandbox import MacSeatbeltSandbox
from agentflow.runtime.supervisor import Supervisor


def validate_native_proxy_contract(call, protocol, output_limit=4096):
    model = call["body"]["model"]
    profile = ModelProfile(model_profile_id="fixture", provider="local_test", requested_model=model,
                           accepted_api_model=model, acceptance_status="accepted", protocols=[protocol],
                           base_url="http://127.0.0.1:1/v1", allow_loopback_upstream=True,
                           credential_reference="test-only", max_output_tokens=output_limit)
    context = AttemptContext(attempt_id="fixture", run_id="fixture", iteration_id="fixture", model_profile_id="fixture",
                             fencing_token=1, input_fingerprint="sha256:" + "a" * 64,
                             expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
                             max_model_requests=5, max_output_tokens=output_limit)
    _, normalized_limit = ModelProvider().normalize_request(profile, context, protocol, call["body"])
    assert normalized_limit == output_limit


@pytest.mark.parametrize("role_model", ["fixture-model", "gpt-5.4"])
@pytest.mark.parametrize('output_limit', [4096, 65536])
async def test_actual_openhands_sdk_supervised_against_local_chat_fixture(
        task, store, tmp_path, http_fixture, role_model, output_limit):
    if platform.system() != "Darwin":
        pytest.skip("Actual controller backend requires verified macOS sandbox")
    try:
        assert importlib.metadata.version("openhands-sdk") == "1.49.2"
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("Install project optional openhands dependencies for the SDK protocol test")

    def answer(call, number):
        assert call["path"] == "/v1/chat/completions"
        assert call["authorization"] == "Bearer scoped-fixture-token"
        if role_model == "gpt-5.4":
            assert call["body"]["reasoning_effort"] == "none"
        assert call['body'].get('max_completion_tokens', call['body'].get('max_tokens')) == output_limit
        validate_native_proxy_contract(call, "chat_completions", output_limit)
        assert {tool["function"]["name"] for tool in call["body"]["tools"]} == {"agentflow_io", "finish"}
        name = "agentflow_io" if number == 1 else "finish"
        arguments = ({"operation": "write_document", "arguments": {"path": "review.md", "content": "protocol fixture"}}
                     if number == 1 else {"message": "done", "result": {"review": "protocol fixture"}})
        message = {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call_{number}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
        response = {"id": f"chatcmpl-{number}", "object": "chat.completion", "created": 1,
                    "model": role_model, "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
        return 200, {"Content-Type": "application/json"}, json.dumps(response).encode()

    supervisor = Supervisor(store, tmp_path)
    supervisor.start = AsyncMock(wraps=supervisor.start)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / "sandbox_profiles"))
    with http_fixture(answer) as (url, calls):
        task = task.model_copy(update={"proxy_base_url": url, "max_active_seconds": 40, "model": role_model,
                                       "max_output_tokens": output_limit, "max_log_bytes": 2 * 1024 * 1024,
                                       "max_iterations": 1200})
        try:
            await adapter.start(task)
            assert supervisor.start.await_args.args[0].max_log_bytes == task.max_log_bytes
            await supervisor.wait(task.attempt_id)
            result = await adapter.collect_artifacts(task.attempt_id)
            error = task.artifact_dir / "role_error.json"
            details = error.read_text() if error.exists() else ""
            details += "\n" + Path((await adapter.inspect(task.attempt_id)).stderr_path).read_text()
            assert result["execution_status"] == "completed", details
            assert result["quality_result"] == "unknown" and result["result"] == {"review": "protocol fixture"}
            assert result["tool_calls"] == 2 and len(calls) == 2
            assert (task.artifact_dir / "review.md").read_text() == "protocol fixture"
            assert (task.workspace / "original.py").read_text() == "ORIGINAL = True\n"
            assert "scoped-fixture-token" not in (task.artifact_dir / "openhands_events.jsonl").read_text()
        finally:
            await supervisor.close()


async def test_actual_openhands_transport_does_not_retry_http_failure(task, store, tmp_path, http_fixture):
    if platform.system() != "Darwin":
        pytest.skip("Verified macOS controller sandbox required")
    try:
        assert importlib.metadata.version("openhands-sdk") == "1.49.2"
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("Install project optional openhands dependencies for the SDK protocol test")
    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / "sandbox_profiles"))
    with http_fixture(lambda *_: (500, {"Content-Type": "application/json"},
                                 b'{"error":{"message":"intentional protocol fixture error"}}')) as (url, calls):
        task = task.model_copy(update={"proxy_base_url": url})
        try:
            await adapter.start(task)
            await supervisor.wait(task.attempt_id)
            result = await adapter.collect_artifacts(task.attempt_id)
            assert result["execution_status"] == "failed"
            assert len(calls) == 1, "The SDK or underlying OpenAI transport retried a dispatched request"
            assert not (task.artifact_dir / "role_result.json").exists()
        finally:
            await supervisor.close()


@pytest.mark.parametrize("patch_target,write_allowed", [("generated.py", True), ("../escape.py", False)])
async def test_actual_codex_exec_against_local_responses_fixture(task, store, tmp_path, http_fixture, patch_target, write_allowed):
    executable = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    if platform.system() != "Darwin" or not executable.is_file():
        pytest.skip("Actual local Codex executable and verified macOS sandbox required")
    output_text = json.dumps({"review": "protocol fixture"})
    # This identifier selects the installed CLI's local tool metadata. The only
    # endpoint is our loopback protocol server; no model or account is invoked.
    fixture_model = "gpt-5.4"

    def answer(call, number):
        assert call["path"] == "/v1/responses"
        assert call["authorization"] == "Bearer scoped-fixture-token"
        assert call["body"]["model"] == fixture_model
        validate_native_proxy_contract(call, "responses")
        if number == 1:
            assert any(tool.get("type") == "custom" and tool.get("name") == "apply_patch"
                       for tool in call["body"]["tools"]), [(tool.get("type"), tool.get("name")) for tool in call["body"]["tools"]]
            patch = f"*** Begin Patch\n*** Add File: {patch_target}\n+FIXTURE_VALUE = 42\n*** End Patch"
            item = {"type": "custom_tool_call", "id": "ctc_fixture", "call_id": "call_patch_fixture",
                    "name": "apply_patch", "input": patch}
            response = {"id": "resp_patch_fixture", "object": "response", "created_at": 1,
                        "status": "completed", "model": fixture_model, "output": [item],
                        "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}}
            events = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": {**item, "input": ""}},
                {"type": "response.custom_tool_call_input.delta", "output_index": 0, "item_id": item["id"], "delta": patch},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            return 200, {"Content-Type": "text/event-stream"}, [
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events]
        (task.artifact_dir / "fixture_tool_output.json").write_text(json.dumps([
            item for item in call["body"]["input"] if "output" in item]))
        output = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                  "content": [{"type": "output_text", "text": output_text, "annotations": []}]}
        response = {"id": "resp_fixture", "object": "response", "created_at": 1, "status": "completed",
                    "model": fixture_model, "output": [output],
                    "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20}}
        events = [
            {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
            {"type": "response.output_item.added", "output_index": 0, "item": {**output, "status": "in_progress", "content": []}},
            {"type": "response.content_part.added", "output_index": 0, "item_id": output["id"], "content_index": 0,
             "part": {"type": "output_text", "text": "", "annotations": []}},
            {"type": "response.output_text.delta", "output_index": 0, "item_id": output["id"], "content_index": 0, "delta": output_text},
            {"type": "response.output_text.done", "output_index": 0, "item_id": output["id"], "content_index": 0, "text": output_text},
            {"type": "response.output_item.done", "output_index": 0, "item": output},
            {"type": "response.completed", "response": response},
        ]
        chunks = [f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events]
        return 200, {"Content-Type": "text/event-stream"}, chunks

    supervisor = Supervisor(store, tmp_path)
    adapter = CodexExecAdapter(supervisor, MacSeatbeltSandbox(tmp_path / "sandbox_profiles"), executable)
    git = await asyncio.create_subprocess_exec("/opt/homebrew/bin/git", "init", "--quiet", str(task.workspace))
    assert await git.wait() == 0
    with http_fixture(answer) as (url, calls):
        task = task.model_copy(update={"proxy_base_url": url, "model": fixture_model, "role": "development", "allow_code_write": True,
                                      "allowed_write_roots": [task.workspace], "max_active_seconds": 30})
        try:
            await adapter.start(task)
            await supervisor.wait(task.attempt_id)
            result = await adapter.collect_artifacts(task.attempt_id)
            handle = await adapter.inspect(task.attempt_id)
            details = Path(handle.stderr_path).read_text() + "\n" + Path(handle.stdout_path).read_text()
            assert result["execution_status"] == "completed", details
            assert result["result"] == {"review": "protocol fixture"}
            assert len(calls) == 2
            if write_allowed:
                assert (task.workspace / "generated.py").read_text() == "FIXTURE_VALUE = 42\n"
            else:
                assert not (task.workspace.parent / "escape.py").exists()
                assert '"status":"failed"' in details
            assert any(item.get("type") == "custom_tool_call_output" and item.get("call_id") == "call_patch_fixture"
                       for item in calls[1]["body"]["input"])
            assert not any(tool.get("type") == "web_search" for tool in calls[0]["body"].get("tools", []))
            assert "scoped-fixture-token" not in details
            assert result["quality_result"] == "unknown"
        finally:
            await supervisor.close()

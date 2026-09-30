"""Real SDK/native HTTP finish validation; no model-provider calls."""
import copy
import json
import platform

import pytest

from agentflow.adapters.openhands import OpenHandsRoleAdapter
from agentflow.control.scheduler import DOCUMENT_SCHEMA
from agentflow.runtime.sandbox import MacSeatbeltSandbox
from agentflow.runtime.supervisor import Supervisor

VALID_DOCUMENT = {"title": "PRD", "summary": "Protocol fixture", "content": "VALID_DOCUMENT_DO_NOT_ECHO_" + "x" * 12000,
                  "sources": [], "unknowns": []}


@pytest.mark.parametrize("scenario,maximum_tools,expected_calls,success", [
    ("extra_then_valid", 30, 2, True),
    ("always_invalid", 30, 3, False),
    ("quota", 1, 1, False),
    ("missing_result_then_valid", 30, 2, True),
    ("invalid_result_type_then_valid", 30, 2, True),
    ("missing_result_quota", 1, 2, True),
    ("missing_document_field_then_valid", 30, 2, True),
    ("wrong_document_field_type_then_valid", 30, 2, True),
])
async def test_real_sdk_finish_schema_and_bounded_correction(task, store, tmp_path, http_fixture,
                                                            scenario, maximum_tools, expected_calls, success):
    if platform.system() != "Darwin":
        pytest.skip("Real supervised SDK requires the supported macOS sandbox")
    schemas = []

    def answer(call, number):
        assert call["path"] == "/v1/chat/completions"
        schemas.append(next(tool["function"]["parameters"]["properties"]["result"]
                            for tool in call["body"]["tools"] if tool["function"]["name"] == "finish"))
        if number > 1:
            feedback = [message for message in call["body"]["messages"] if message.get("role") == "tool"]
            assert feedback and len(json.dumps(feedback)) < 3000
            assert "VALID_DOCUMENT_DO_NOT_ECHO_" not in json.dumps(feedback)
        result = copy.deepcopy(VALID_DOCUMENT)
        if scenario in {"always_invalid", "quota"} or (scenario == "extra_then_valid" and number == 1):
            result.update(priority="P0", stage="prd")
        arguments = {"message": "Done", "result": result}
        if number == 1:
            if scenario in {"missing_result_then_valid", "missing_result_quota"}:
                arguments.pop("result")
            elif scenario == "invalid_result_type_then_valid":
                arguments["result"] = ["invalid"]
            elif scenario == "missing_document_field_then_valid":
                result.pop("sources")
            elif scenario == "wrong_document_field_type_then_valid":
                result["sources"] = "not an array"
        return 200, {"Content-Type": "application/json"}, json.dumps({
            "id": f"finish-{number}", "object": "chat.completion", "created": 1, "model": "fixture-model",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"finish-call-{number}", "type": "function", "function": {"name": "finish",
                    "arguments": json.dumps(arguments)}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }).encode()

    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / "sandbox_profiles"))
    with http_fixture(answer) as (url, calls):
        task = task.model_copy(update={"proxy_base_url": url, "max_active_seconds": 40,
            "output_schema": DOCUMENT_SCHEMA, "max_tool_calls": maximum_tools})
        try:
            await adapter.start(task)
            await supervisor.wait(task.attempt_id)
            result = await adapter.collect_artifacts(task.attempt_id)
            assert len(calls) == expected_calls, result
            assert all(schema == DOCUMENT_SCHEMA for schema in schemas), schemas
            audit = json.loads((task.artifact_dir / "tool_audit.json").read_text())
            # Native argument-parse rejection does not execute a broker tool;
            # SDK's existing iteration/request limits still govern its feedback.
            parse_scenarios = {"missing_result_then_valid", "invalid_result_type_then_valid", "missing_result_quota"}
            assert audit["calls"] == (1 if scenario in parse_scenarios else expected_calls)
            if success:
                assert result["execution_status"] == "completed", result
                assert result["result"] == VALID_DOCUMENT
                assert json.loads((task.artifact_dir / "openhands_final.json").read_text()) == VALID_DOCUMENT
            else:
                assert result["execution_status"] == "failed", result
                assert not (task.artifact_dir / "openhands_final.json").exists()
                assert not (task.artifact_dir / "role_result.json").exists()
        finally:
            await supervisor.close()

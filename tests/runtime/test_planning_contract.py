import pytest

from agentflow.common import DomainError
from agentflow.domain.planning_contract import build_planning_contract, validate_parallel_work


def contract():
    code = dict(
        id="code",
        key="development",
        step="development",
        role="development",
        status="pending",
        dependencies=[],
        write_paths=["src"],
        revision=1,
    )
    review = dict(
        id="review",
        key="code_review",
        step="code_review",
        role="review",
        status="pending",
        dependencies=["code"],
        write_paths=[],
        revision=1,
    )
    plan = dict(id="plan", revision=1, actual_steps=["development", "code_review"], work_specs=[code, review])
    return build_planning_contract(
        dict(id="run", revision=1, input_fingerprint="input", plan_id="plan"),
        {},
        plan,
        [code, review],
        max_children=16,
        max_work_items=20,
        max_edges=40,
    )


def group(paths):
    return [
        {
            "stage_key": "code_review",
            "children": [
                dict(
                    key=str(i),
                    goal="Inspect",
                    write_paths=paths,
                    review_focus="current_code",
                    inspection_paths=["src/app.py"],
                )
                for i in range(2)
            ],
        }
    ]


def test_all_bad_children_reported_and_inspection_does_not_grant_writes():
    with pytest.raises(DomainError) as error:
        validate_parallel_work(group(["src"]), contract())
    assert error.value.code == "planning_validation_failed"
    assert len([i for i in error.value.details["issues"] if i["code"] == "role_write_scope"]) == 2
    result = validate_parallel_work(group([]), contract())
    assert result[0]["children"][0]["inspection_paths"] == ["src/app.py"]


def test_duplicate_stage_paths_and_review_focus_are_aggregated():
    proposals = group(["../escape"])
    proposals[0]["children"][0]["review_focus"] = "unit_test_coverage"
    proposals *= 2
    with pytest.raises(DomainError) as error:
        validate_parallel_work(proposals, contract())
    codes = {i["code"] for i in error.value.details["issues"]}
    assert {"invalid_write_scope", "review_phase_mismatch", "duplicate_stage"} <= codes


def test_graph_limits_apply_to_complete_batch():
    value = contract()
    value["limits"]["max_work_items"] = 3
    with pytest.raises(DomainError) as error:
        validate_parallel_work(group([]), value)
    assert "graph_limit_exceeded" in {i["code"] for i in error.value.details["issues"]}


@pytest.mark.parametrize("staged", [False, True])
def test_finish_rejects_then_corrects_without_publishing(task, staged):
    from agentflow.adapters.openhands.tools import ToolBroker

    bound = task.model_copy(
        update={
            "planning_contract": contract(),
            "max_output_tokens": 8192,
            "output_schema": {"type": "object"},
            "role": "architecture_planning",
        }
    )
    broker = ToolBroker(bound)
    result = {"content": "Preserve this document", "parallel_work": group(["src"])}
    if staged:
        ref = broker.result_begin(result, {}, "bad")["result_ref"]
        def finish():
            return broker.finish_ref(ref)
    else:
        def finish():
            return broker.finish(result)
    with pytest.raises(DomainError) as error:
        finish()
    assert error.value.code == "planning_validation_failed"
    assert broker.finished_result is None
    assert not (bound.artifact_dir / "openhands_final.json").exists()
    if staged:
        fixed = broker.result_revise_parallel_work(ref, group([]), "fixed")["result_ref"]
        assert broker.output.resolve(ref) == result
        assert broker.finish_ref(fixed)["content"] == result["content"]
    else:
        result["parallel_work"] = group([])
        assert broker.finish(result)["content"] == result["content"]


def test_contract_omitted_from_legacy_envelope(task):
    assert "planning_contract" not in task.model_dump()


def test_streamed_revision_preserves_body_and_budget(task):
    from agentflow.adapters.openhands.tools import ToolBroker

    bound = task.model_copy(
        update={
            "planning_contract": contract(),
            "max_output_tokens": 8192,
            "output_schema": {"type": "object"},
            "role": "architecture_planning",
        }
    )
    broker = ToolBroker(bound)
    ref = broker.result_begin({"parallel_work": group(["src"])}, {"content": "string"}, "original")[
        "result_ref"
    ]
    ref = broker.result_append(ref, "content", "body", 0, "body stays", True)["result_ref"]
    revision = broker.result_revise_parallel_work(ref, None, "correction")
    assert revision["committed_chunks"] == 2
    with pytest.raises(DomainError, match="sealed"):
        broker.finish_ref(revision["result_ref"])
    new = broker.result_append(revision["result_ref"], "parallel_work", "fixed", 0, group([]), True)[
        "result_ref"
    ]
    assert broker.finish_ref(new)["content"] == "body stays"
    assert broker.output.resolve(ref)["parallel_work"] == group(["src"])
    assert broker.result_revise_parallel_work(ref, None, "correction")["result_ref"] == new


@pytest.mark.parametrize("bad", [None, 42, {}, ["src"] * 129, [None], ["../bad"]])
def test_malformed_coding_scope_is_correctable(bad):
    proposals = [
        {
            "stage_key": "development",
            "children": [
                {"key": "a", "goal": "g", "write_paths": bad},
                {"key": "b", "goal": "g", "write_paths": ["src"]},
            ],
        }
    ]
    with pytest.raises(DomainError) as error:
        validate_parallel_work(proposals, contract())
    assert error.value.code == "planning_validation_failed"


@pytest.mark.parametrize("staged,always_bad", [(False, False), (True, False), (False, True)])
async def test_real_sdk_planning_feedback_and_correction(
    task, store, tmp_path, http_fixture, staged, always_bad
):
    import json
    import platform

    from agentflow.adapters.openhands import OpenHandsRoleAdapter
    from agentflow.adapters.openhands.output_builder import ResultBuilderStore
    from agentflow.common import canonical_digest
    from agentflow.runtime.sandbox import MacSeatbeltSandbox
    from agentflow.runtime.supervisor import Supervisor

    if platform.system() != "Darwin":
        pytest.skip("Supported SDK sandbox requires macOS")
    bound = task.model_copy(
        update={
            "planning_contract": contract(),
            "role": "architecture_planning",
            "output_schema": {"type": "object"},
            "max_output_tokens": 8192,
            "max_active_seconds": 40,
            "artifact_dir": tmp_path / "attempt_artifacts" / canonical_digest(task.attempt_id).split(":")[1],
        }
    )
    result = {"content": "KEEP_SAVED_BODY", "parallel_work": group(["src"])}

    def latest():
        drafts = ResultBuilderStore(bound).status()["drafts"]
        return max(drafts, key=lambda d: d["committed_chunks"])["result_ref"]

    def answer(call, number):
        if number == (3 if staged else 2):
            assert "role_write_scope" in json.dumps(call["body"]["messages"])
        name = "finish"
        if staged and number == 1:
            name = "agentflow_io"
            arguments = {
                "operation": "result_begin",
                "arguments": {"fields": result, "streamed_fields": {}, "request_id": "draft"},
            }
        elif staged and number == 3:
            name = "agentflow_io"
            arguments = {
                "operation": "result_revise_parallel_work",
                "arguments": {"result_ref": latest(), "parallel_work": group([]), "request_id": "fixed"},
            }
        elif staged:
            arguments = {"message": "Done", "result_ref": latest()}
        else:
            arguments = {
                "message": "Done",
                "result": result if number == 1 or always_bad else {**result, "parallel_work": group([])},
            }
        return (
            200,
            {"Content-Type": "application/json"},
            json.dumps(
                {
                    "id": f"c{number}",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "fixture-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": f"t{number}",
                                        "type": "function",
                                        "function": {"name": name, "arguments": json.dumps(arguments)},
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                }
            ).encode(),
        )

    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / "sandbox_profiles"))
    with http_fixture(answer) as (url, calls):
        bound = bound.model_copy(update={"proxy_base_url": url})
        try:
            await adapter.start(bound)
            await supervisor.wait(bound.attempt_id)
            collected = await adapter.collect_artifacts(bound.attempt_id)
            if always_bad:
                assert collected["runtime_failure_code"] == "planning_validation_failed", collected
                assert collected["failure_details"]["issues"][0]["code"] == "role_write_scope"
                assert len(calls) == 3
                assert not (bound.artifact_dir / "openhands_final.json").exists()
                drafts = ResultBuilderStore(bound).status()["drafts"]
                assert ResultBuilderStore(bound).resolve(drafts[0]["result_ref"]) == result
                return
            assert collected["execution_status"] == "completed", collected
            assert len(calls) == (4 if staged else 2)
            final = json.loads((bound.artifact_dir / "openhands_final.json").read_text())
            assert final["parallel_work"] == group([]) and final["content"] == "KEEP_SAVED_BODY"
        finally:
            await supervisor.close()


def test_import_large_final_preserves_evidence_and_is_idempotent(task, tmp_path):
    from agentflow.adapters.openhands.output_builder import (
        ResultBuilderStore,
        import_planning_final,
        result_identity,
    )
    from agentflow.common import canonical_digest

    result = {"content": "large body " * 20000, "parallel_work": group(["src"])}
    source = task.model_copy(update={"output_schema": {"type": "object"}})
    target = source.model_copy(
        update={
            "attempt_id": "new",
            "planning_contract": contract(),
            "role": "architecture_planning",
            "artifact_dir": tmp_path / "new",
        }
    )
    receipt = import_planning_final(
        target,
        artifact_root=target.artifact_dir,
        result=result,
        source_identity=result_identity(source),
        source_digest=canonical_digest(result),
    )
    assert receipt["mode"] == "revise_planning_output"
    ref = receipt["builders"][0]["result_ref"]
    assert ResultBuilderStore(target).resolve(ref) == result
    assert (
        import_planning_final(
            target,
            artifact_root=target.artifact_dir,
            result=result,
            source_identity=result_identity(source),
            source_digest=canonical_digest(result),
        )
        == receipt
    )
    with pytest.raises(DomainError):
        import_planning_final(
            target,
            artifact_root=target.artifact_dir,
            result=result,
            source_identity={**result_identity(source), "run_id": "other"},
            source_digest=canonical_digest(result),
        )


def test_rejected_ordinary_finish_can_be_checkpointed(task):
    from agentflow.adapters.openhands.tools import ToolBroker

    bound = task.model_copy(
        update={
            "planning_contract": contract(),
            "max_output_tokens": 8192,
            "output_schema": {"type": "object"},
            "role": "architecture_planning",
        }
    )
    broker = ToolBroker(bound)
    result = {"content": "saved body", "parallel_work": group(["src"])}
    with pytest.raises(DomainError):
        broker.finish(result)
    broker.preserve_rejected_planning()
    drafts = broker.output.status()["drafts"]
    assert len(drafts) == 1 and broker.output.resolve(drafts[0]["result_ref"]) == result
    assert broker.planning_failure.code == "planning_validation_failed"

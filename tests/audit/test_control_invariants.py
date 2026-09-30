"""Independent regression checks for control-plane audit findings."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from agentflow.common import DomainError
from agentflow.control.scheduler import Scheduler
from agentflow.control.service import WorkflowService
from agentflow.runtime.service import RuntimeService
from agentflow.settings import Settings
from agentflow.storage import LocalArtifactStore, Store


@pytest_asyncio.fixture
async def system(tmp_path):
    settings = Settings(data_dir=tmp_path / "control")
    store = Store(settings.data_dir)
    await store.start()
    artifacts = LocalArtifactStore(settings.data_dir / "artifacts")
    workflow = WorkflowService(store, artifacts, settings)
    workspace = tmp_path / "project"
    workspace.mkdir()

    def seed(tx):
        tx.put("project", "project", {"local_path": str(workspace), "base_commit": "a" * 40})
        tx.put("plan", "plan", {"target_configs": [], "reused_inputs": []})
        tx.put("run", "run", {"run_id": "run", "project_id": "project", "plan_id": "plan",
            "iteration_id": "iteration", "goal": "Audit fixture", "execution_state": "running",
            "quality_result": "unknown", "input_fingerprint": "sha256:" + "a" * 64,
            "delivery_ids": [], "blocking_reasons": []})
        tx.put("work_item", "work", {"key": "prd", "run_id": "run", "project_id": "project", "step": "prd",
            "role": "product", "generation": 1, "fencing_token": 1, "status": "pending",
            "quality_result": "unknown", "required": True, "approval_required": False,
            "input_fingerprint": "sha256:" + "a" * 64, "policy_fingerprint": "sha256:" + "b" * 64,
            "attempt_id": "attempt", "artifact_ids": [], "dependencies": [], "write_paths": []})
        tx.put("attempt", "attempt", {"run_id": "run", "work_item_id": "work", "generation": 1,
            "fencing_token": 1, "status": "running", "input_fingerprint": "sha256:" + "a" * 64})
        return {}

    await store.command("audit", "seed", {}, seed)
    yield store, workflow, artifacts, settings
    await store.close()


async def set_work(store, status):
    def update(tx):
        item = tx.get("work_item", "work")
        return tx.put("work_item", "work", {**item, "status": status}, item["revision"])
    return await store.command("audit-status", status, {}, update)


async def test_reused_input_content_reaches_selected_stage_prompt(system):
    store, workflow, artifacts, settings = system
    marker = "UNIQUE_ACCEPTED_PRD_CONSTRAINT_1849"
    blob = await artifacts.put_bytes(('{"content":"' + marker + '"}').encode())

    def reused(tx):
        tx.put("artifact", "accepted-input", {"digest": blob["id"], "step": "research", "stale": False,
            "media_type": "application/json", "work_item_id": "previous-run-work", "run_id": "previous-run"})
        plan = tx.get("plan", "plan")
        return tx.put("plan", "plan", {**plan, "reused_inputs": ["accepted-input"]}, plan["revision"])

    await store.command("audit", "reused", {}, reused)
    scheduler = Scheduler(workflow, store, None, None, settings)
    prompt = await scheduler._prompt(await store.read("run", "run"), await store.read("work_item", "work"), "a" * 40)
    assert marker in prompt, "The accepted reused input disappeared between planning and Agent dispatch"


async def test_revision_cannot_reopen_a_live_native_execution(system):
    store, workflow, _artifacts, _settings = system
    await set_work(store, "waiting_execution")
    await store.command("audit", "native-job", {}, lambda tx: tx.put("node_job", "native-job", {
        "run_id": "run", "parent_work_item_id": "work", "state": "running", "parent_generation": 1}))
    with pytest.raises(DomainError) as caught:
        await workflow.revise("run", {"expected_revision": 1, "work_item_ids": ["work"], "reason": "Change requirements"}, "revise")
    assert caught.value.code == "active_work"
    assert (await store.read("work_item", "work"))["generation"] == 1


async def test_cancel_does_not_certify_unknown_execution_as_stopped(system):
    store, workflow, _artifacts, _settings = system
    await set_work(store, "execution_unknown")
    await workflow.control_run("run", {"expected_revision": 1, "action": "cancel", "reason": "Stop"}, "cancel")
    item = await store.read("work_item", "work")
    assert item["status"] in {"execution_unknown", "cancel_requested"}, "Unknown process identity was silently converted to cancelled"
    assert (await store.read("run", "run"))["execution_state"] == "cancelling"

    def reconcile_unknown(tx):
        current = tx.get("work_item", "work")
        tx.put("work_item", "work", {**current, "status": "execution_unknown"}, current["revision"])
        workflow._recompute_run(tx, "run")
        return {}

    await store.command("audit", "unknown-still-live", {}, reconcile_unknown)
    assert (await store.read("run", "run"))["execution_state"] == "cancelling"


async def test_shutdown_tolerates_dispatch_finished_without_a_runtime_process(system):
    store, workflow, _artifacts, settings = system
    runtime = RuntimeService(store, settings.data_dir, SimpleNamespace())
    scheduler = Scheduler(workflow, store, runtime, None, settings)
    finished = asyncio.create_task(asyncio.sleep(0))
    await finished
    # Native begin() and failures before runtime.start() both leave this window
    # until the next scheduler pass removes their completed dispatch task.
    scheduler._active["native-or-prelaunch-attempt"] = finished
    try:
        await scheduler.close()
    finally:
        await runtime.close()

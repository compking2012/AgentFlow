"""Managed setup uses real TLS and real tests; no manual candidate/capability setup."""
import asyncio
import platform
import shutil
import ssl

import httpx
import pytest

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.execution.models import CapabilityReport, DisplayObservation, TargetConfig
from agentflow.local_execution import LocalExecutionService
from agentflow.storage import Store
from node_agent import daemon as daemon_module


@pytest.fixture
async def pending_local_execution(tmp_path, monkeypatch):
    """Persisted queue fixture; reference verification fails before executing any host tools."""
    store = Store(tmp_path / "data")
    await store.start()
    service = LocalExecutionService(store, store.data_dir)
    target = TargetConfig(target_config_id="managed-api", app_target="api", os_name="Darwin",
        os_version_constraint="*", cpu_architecture="arm64", required_display_protocol="not_required",
        required_device_mode="not_required").model_dump(mode="json")
    await service._update(node_id="managed-node", state="ready", phase="ready", target_configs=[target])

    def seed(tx):
        tx.put("node", "managed-node", {"state": "offline", "allowed_app_targets": ["api", "web"]})
        tx.put("run", "run", {"execution_state": "running", "input_fingerprint": "run-input"})
        tx.put("work_item", "work", {"run_id": "run", "status": "waiting_execution", "generation": 4,
            "input_fingerprint": "work-input", "candidate_id": "candidate", "execution_phase": "unit"})
        tx.put("candidate", "candidate", {"run_id": "run", "run_input_fingerprint": "run-input",
            "matrix_plan": {"target_configs": [target]}, "build_job_ids": ["queued-job"], "phase_jobs": {},
            "recipes": {"targets": [{"target_config_id": "managed-api", "unit": {"adapter": "api"}}]}})
        tx.put("node_job", "queued-job", {"run_id": "run", "node_id": None, "kind": "build",
            "state": "queued", "app_target": "api", "target_config": target, "parent_work_item_id": "work",
            "parent_generation": 4, "parent_input_fingerprint": "work-input", "created_at": utc_now()})
        return {}

    await store.command("fixture", "pending-managed-work", {}, seed)

    def failed_reference():
        raise DomainError("local_reference_failed", "本机验证未通过：build。fixture reference failed")

    monkeypatch.setattr(service, "_fixture", failed_reference)
    try:
        yield service, store, target
    finally:
        await service.close()
        await store.close()


async def change_record(store, kind, identity, **changes):
    def update(tx):
        row = tx.get(kind, identity)
        return tx.put(kind, identity, {**row, **changes}, row["revision"])
    return await store.command("fixture-change", str(changes), {"kind": kind, **changes}, update)


async def test_resume_pending_prepares_the_existing_queue_and_exposes_failure_without_retry(pending_local_execution):
    service, store, _ = pending_local_execution
    before = {kind: await store.list(kind) for kind in ("node_job", "work_item", "candidate", "run")}
    state = await service.resume_pending(wait=True)
    assert state["state"] == "blocked" and state["error_code"] == "local_reference_failed"
    assert "fixture reference failed" in state["message"]
    failed = await store.read("local_execution", "managed-local")
    await service.resume_pending(wait=True)
    assert await store.read("local_execution", "managed-local") == failed
    assert {kind: await store.list(kind) for kind in before} == before
    assert not await store.list("node_result")


async def test_resume_pending_attempts_new_target_when_its_run_resumes_without_retrying_failed_target(pending_local_execution):
    service, store, api = pending_local_execution
    first = await service.resume_pending(wait=True)
    assert first["error_code"] == "local_reference_failed" and first["required_targets"] == ["api"]
    web = {**api, "app_target": "web", "target_config_id": "managed-web"}
    await service._update(target_configs=[api, web])

    def seed(tx):
        tx.put("run", "web-run", {"execution_state": "paused", "input_fingerprint": "web-input"})
        return tx.put("node_job", "web-job", {"run_id": "web-run", "node_id": None, "kind": "test",
            "state": "queued", "app_target": "web", "target_config": web,
            "parent_run_fingerprint": "web-input", "created_at": utc_now()})

    await store.command("fixture", "later-web-target", {}, seed)
    failed = await store.read("local_execution", "managed-local")
    await service.resume_pending(wait=True)
    assert await store.read("local_execution", "managed-local") == failed
    await change_record(store, "run", "web-run", execution_state="running")
    jobs = await store.list("node_job")
    second = await service.resume_pending(wait=True)
    assert second.get("required_targets") == ["web"]
    assert second["error_code"] == "local_reference_failed"
    assert (await store.read("local_execution", "managed-local"))["revision"] > failed["revision"]
    failed = await store.read("local_execution", "managed-local")
    await service.resume_pending(wait=True)
    assert await store.read("local_execution", "managed-local") == failed
    assert await store.list("node_job") == jobs


async def test_resume_pending_prepares_waiting_work_before_its_first_job_is_enqueued(pending_local_execution):
    service, store, _ = pending_local_execution
    await change_record(store, "node_job", "queued-job", state="completed")
    await change_record(store, "candidate", "candidate", build_job_ids=[])
    state = await service.resume_pending(wait=True)
    assert state["error_code"] == "local_reference_failed"
    assert len(await store.list("node_job")) == 1


async def test_resume_pending_ignores_local_target_without_a_recipe_in_the_waiting_phase(pending_local_execution):
    service, store, _ = pending_local_execution
    await change_record(store, "node_job", "queued-job", state="completed", quality_result="passed")
    await change_record(store, "candidate", "candidate", recipes={"targets": [
        {"target_config_id": "managed-api", "unit": None, "integration": {"adapter": "api"}}]})
    before = await store.read("local_execution", "managed-local")
    await service.resume_pending(wait=True)
    assert await store.read("local_execution", "managed-local") == before


@pytest.mark.parametrize("install_state", ["queued", "failed"])
async def test_resume_pending_does_not_prepare_tests_before_other_required_installations_pass(pending_local_execution, install_state):
    service, store, target = pending_local_execution
    await change_record(store, "node_job", "queued-job", state="completed", quality_result="passed")
    await store.command("fixture", "remote-install", {}, lambda tx: tx.put("node_job", "remote-install",
        {"run_id": "run", "node_id": "remote-node", "state": install_state, "kind": "install",
         "target_config": {**target, "target_config_id": "remote-target"}}))
    await change_record(store, "candidate", "candidate", phase_jobs={"install": ["remote-install"]})
    before = await store.read("local_execution", "managed-local")
    await service.resume_pending(wait=True)
    assert await store.read("local_execution", "managed-local") == before


@pytest.mark.parametrize("reason", ["paused", "cancelled", "cancelling", "completed", "stale_parent",
    "other_target", "revoked_node", "target_not_allowed", "finished_jobs", "stale_candidate"])
async def test_resume_pending_does_not_prepare_ineligible_work(pending_local_execution, reason):
    service, store, target = pending_local_execution
    if reason in {"paused", "cancelled", "cancelling", "completed"}:
        await change_record(store, "run", "run", execution_state=reason)
    elif reason == "stale_parent":
        await change_record(store, "work_item", "work", generation=5)
    elif reason == "other_target":
        await change_record(store, "node_job", "queued-job", target_config={**target, "target_config_id": "other-host"})
    elif reason == "revoked_node":
        await change_record(store, "node", "managed-node", state="revoked")
    elif reason == "target_not_allowed":
        await change_record(store, "node", "managed-node", allowed_app_targets=[])
    elif reason == "finished_jobs":
        await change_record(store, "node_job", "queued-job", state="completed", quality_result="passed")
        await change_record(store, "candidate", "candidate", phase_jobs={"unit": ["queued-job"]})
    else:
        await change_record(store, "node_job", "queued-job", state="completed")
        await change_record(store, "candidate", "candidate", build_job_ids=[], run_input_fingerprint="old-input")
    before = await store.read("local_execution", "managed-local")
    await service.resume_pending(wait=True)
    assert await store.read("local_execution", "managed-local") == before


async def test_resume_pending_blocks_unknown_local_process_before_preparing(pending_local_execution):
    service, store, _ = pending_local_execution
    await store.command("fixture", "unknown-process", {}, lambda tx: tx.put("node_job", "unknown-job",
        {"node_id": "managed-node", "state": "execution_unknown"}))
    state = await service.resume_pending(wait=True)
    assert state["state"] == "blocked" and state["error_code"] == "local_execution_unknown"
    assert (await store.read("node_job", "queued-job"))["state"] == "queued"


async def test_resume_pending_blocks_unknown_reference_even_without_an_assigned_node(pending_local_execution):
    service, store, _ = pending_local_execution
    await service._update(reference_id="reference")
    def seed(tx):
        tx.put("local_execution_reference", "reference", {"source_manifest": {"fingerprint": "reference-source"}})
        return tx.put("node_job", "unknown-reference", {"node_id": None, "state": "execution_unknown",
            "source_manifest": {"fingerprint": "reference-source"}})
    await store.command("fixture", "unknown-reference", {}, seed)
    state = await service.resume_pending(wait=True)
    assert state["state"] == "blocked" and state["error_code"] == "local_execution_unknown"


async def test_resume_pending_on_fresh_installation_does_not_pair_or_self_test(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    await store.start()
    service = LocalExecutionService(store, store.data_dir)
    try:
        state = await service.resume_pending(wait=True)
        assert state["state"] == "not_prepared" and not state["preparing"]
        assert not await store.list("node") and not await store.list("node_job")
        assert await store.read("local_execution", "managed-local") is None
    finally:
        await service.close()
        await store.close()


async def test_resume_pending_reconnects_the_real_worker_without_granting_static_proof(tmp_path, monkeypatch):
    """Use real enrollment, TLS and pull loop; replace host probing, never fabricate a functional pass."""
    boot = canonical_digest("fixture-boot")

    async def static_report(target):
        return CapabilityReport(app_target=target.app_target, target_config_fingerprint=target.fingerprint,
            os_name=target.os_name, os_version="fixture", architecture=target.cpu_architecture,
            boot_fingerprint=boot, display=DisplayObservation(session_fingerprint=boot), tools=[],
            state="static_verified", observed_at=utc_now())

    async def failed_reference(*_args):
        raise DomainError("local_reference_failed", "Fixture reference deliberately did not run")

    monkeypatch.setattr(daemon_module, "probe_target", static_report)
    store = Store(tmp_path / "data")
    await store.start()
    first, second = LocalExecutionService(store, store.data_dir), None
    try:
        await first._update()
        await first.start()
        await first._ensure_daemon()
        local = await store.read("local_execution", "managed-local")
        target = local["target_configs"][0]
        await first.close()

        def seed(tx):
            tx.put("run", "run", {"execution_state": "running", "input_fingerprint": "run-input"})
            return tx.put("node_job", "test-job", {"run_id": "run", "node_id": None, "kind": "test",
                "state": "queued", "app_target": "api", "capability_id": None, "target_config": target,
                "parent_run_fingerprint": "run-input", "created_at": utc_now()})

        original_job = await store.command("fixture", "formal-test", {}, seed)
        second = LocalExecutionService(store, store.data_dir)
        monkeypatch.setattr(second, "_reference", failed_reference)
        state = await second.resume_pending(wait=True)
        assert state["error_code"] == "local_reference_failed"
        assert second._daemon is not None and not second._worker_task.done()
        assert state["node_id"] == local["node_id"] and len(await store.list("node")) == 1
        assert await store.read("node_job", "test-job") == original_job
        assert {cap["verification_state"] for cap in await store.list("node_capability")} == {"static_verified"}
        assert not await store.list("node_result")
    finally:
        await first.close()
        if second:
            await second.close()
        await store.close()


async def test_start_only_opens_private_transport_without_pairing_or_installing(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    await store.start()
    service = LocalExecutionService(store, store.data_dir)

    async def forbidden_spawn(*_args, **_kwargs):
        pytest.fail("Starting the owner application must not install dependencies or execute project code")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    try:
        state = await service.start()
        assert state["state"] == "not_prepared" and state["ready_targets"] == []
        assert await store.list("node") == [] and await store.list("node_resource") == []
        context = ssl.create_default_context(cadata=service.nodes.pki.ca_pem)
        async with httpx.AsyncClient(base_url=service.origin, verify=context, trust_env=False) as client:
            response = await client.get("/executor/v1/jobs/absent", headers={"X-Node-Id": "forged"})
            assert response.status_code == 401
    finally:
        await service.close()
        await store.close()


async def test_modified_bundled_fixture_is_blocked_before_any_node_job(tmp_path):
    store = Store(tmp_path / "data")
    await store.start()
    service = LocalExecutionService(store, store.data_dir)
    fixture = tmp_path / "modified-fixture"
    shutil.copytree(service.reference_root, fixture)
    (fixture / "web_api/src/main.tsx").write_text("modified fixture")
    service.reference_root = fixture
    try:
        state = await service.prepare(wait=True)
        assert state["state"] == "blocked" and state["error_code"] == "local_reference_changed"
        assert not await store.list("node_job") and not await store.list("node")
    finally:
        await service.close()
        await store.close()


async def test_concurrent_prepare_requests_share_serial_batches_without_inventing_readiness(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    await store.start()
    service = LocalExecutionService(store, store.data_dir)
    release = asyncio.Event()
    active, maximum, requested = 0, 0, set()

    async def observe_only(targets):
        nonlocal active, maximum
        requested.update(targets)
        active += 1
        maximum = max(maximum, active)
        await release.wait()
        await service._update(state="blocked", error_code="test_did_not_execute_any_framework")
        active -= 1

    monkeypatch.setattr(service, "_prepare", observe_only)
    try:
        first, second, third = await asyncio.gather(service.prepare(required_targets=["api"]),
            service.prepare(required_targets=["web"]), service.prepare())
        assert all(state["state"] != "ready" for state in (first, second, third))
        release.set()
        await service._prepare_task
        assert maximum == 1 and requested == {"api", "web"}
        assert (await service.status())["ready_targets"] == [] and not await store.list("node_result")
    finally:
        await service.close()
        await store.close()


async def test_cancelled_preparation_persists_stopped_state_and_no_false_readiness(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    await store.start()
    service = LocalExecutionService(store, store.data_dir)

    async def incomplete(_targets):
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_prepare", incomplete)
    await service.prepare()
    await service.close()
    try:
        state = await store.read("local_execution", "managed-local")
        assert state["state"] == "not_prepared" and state["phase"] == "stopped"
        assert not await store.list("node_result")
    finally:
        await store.close()


@pytest.mark.skipif(platform.system() != "Darwin", reason="Managed node static probes run on macOS")
async def test_unknown_reference_checkpoint_is_not_replaced_by_new_jobs(tmp_path):
    store = Store(tmp_path / "data")
    await store.start()
    service = LocalExecutionService(store, store.data_dir)
    source = canonical_digest("uncertain-source")
    await service._update(reference_id="prior-reference")

    def seed(tx):
        tx.put("local_execution_reference", "prior-reference", {"source_manifest": {"fingerprint": source}})
        return tx.put("node_job", "unknown-job", {"state": "execution_unknown", "node_id": None,
            "source_manifest": {"fingerprint": source}, "created_at": utc_now()})

    await store.command("fixture", "uncertain-reference", {}, seed)
    try:
        state = await service.prepare(wait=True, required_targets=["api"])
        assert state["state"] == "blocked" and state["error_code"] == "local_execution_unknown"
        assert [job["id"] for job in await store.list("node_job")] == ["unknown-job"]
        assert len(await store.list("local_execution_reference")) == 1
    finally:
        await service.close()
        await store.close()


@pytest.mark.skipif(platform.system() != "Darwin", reason="The managed local executor requires measured macOS isolation")
async def test_api_only_setup_really_builds_and_tests_without_a_browser_and_reuses_proof_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_BROWSER_EXECUTABLE", "/missing-agentflow-browser/not-installed")
    store = Store(tmp_path / "data")
    await store.start()
    first = LocalExecutionService(store, store.data_dir)
    second = None
    try:
        ready = await first.prepare(wait=True, required_targets=["api"])
        await first._prepare_task
        assert ready["state"] == "ready" and ready["ready_targets"] == ["api"], ready
        overall = await first.status()
        assert overall["state"] == "partial" and overall["ready_targets"] == ["api"]
        jobs = await store.list("node_job")
        assert len(jobs) == 3 and {job["app_target"] for job in jobs} == {"api"}
        assert all(job["state"] == "completed" and job["quality_result"] == "passed" for job in jobs)
        reports = [await store.read("node_result", job["result_id"]) for job in jobs]
        assert all(report["assessment_state"] == "validated" for report in reports)
        cap = next(cap for cap in await store.list("node_capability") if cap["app_target"] == "api")
        assert cap["verification_state"] == "functional_verified"
        web_caps = [cap for cap in await store.list("node_capability") if cap["app_target"] == "web"]
        assert all(cap["verification_state"] != "functional_verified" for cap in web_caps)
        node_id, target_configs = overall["node_id"], overall["target_configs"]
        await first.close()
        second = LocalExecutionService(store, store.data_dir)
        repeated = await second.prepare(wait=True, required_targets=["api"])
        await second._prepare_task
        assert repeated["state"] == "ready" and repeated["node_id"] == node_id
        assert repeated["target_configs"] == target_configs
        assert len(await store.list("node_job")) == 3
        assert len(await store.list("node")) == 1 and len(await store.list("node_resource")) == 1
    finally:
        await first.close()
        if second:
            await second.close()
        await store.close()

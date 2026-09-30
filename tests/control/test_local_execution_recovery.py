"""Application startup restores authorized node work without starting model work."""
import asyncio

import pytest
from test_execution_pipeline import fixture as pipeline_fixture

from agentflow.application import Application
from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.products import ProductService
from agentflow.control.scheduler import Scheduler
from agentflow.execution.models import CapabilityReport, DisplayObservation, TargetConfig
from agentflow.local_execution import LocalExecutionService
from agentflow.settings import Settings
from agentflow.storage import Store
from node_agent import daemon as daemon_module


async def test_application_passes_configured_package_fetch_timeout_to_the_local_executor(tmp_path, monkeypatch):
    async def static_report(target):
        boot = canonical_digest("configuration-wiring-fixture")
        return CapabilityReport(app_target=target.app_target, target_config_fingerprint=target.fingerprint,
            os_name=target.os_name, os_version="fixture", architecture=target.cpu_architecture,
            boot_fingerprint=boot, display=DisplayObservation(session_fingerprint=boot), tools=[],
            state="static_verified", observed_at=utc_now())

    monkeypatch.setattr(daemon_module, "probe_target", static_report)
    application = await Application(Settings(data_dir=tmp_path / "data", package_fetch_timeout_seconds=7.5)).start(schedule=False)
    try:
        await application.local_execution._update()
        await application.local_execution._ensure_daemon()
        executor = application.local_execution._daemon.runner.executor
        assert executor.sandbox.package_fetch_timeout_seconds == 7.5
        assert not await application.store.list("node_job")
        assert not await application.store.list("model_invocation")
    finally:
        await application.close()


@pytest.mark.parametrize("schedule", [True, False])
async def test_application_only_recovers_pending_local_work_when_scheduling_is_enabled(tmp_path, monkeypatch, schedule):
    settings = Settings(data_dir=tmp_path / "data")
    store = Store(settings.data_dir)
    await store.start()
    target = TargetConfig(target_config_id="managed-api", app_target="api", os_name="Darwin",
        os_version_constraint="*", cpu_architecture="arm64", required_display_protocol="not_required",
        required_device_mode="not_required").model_dump(mode="json")

    def seed(tx):
        tx.put("local_execution", "managed-local", {"installation_id": "installed", "node_id": "managed-node",
            "state": "ready", "phase": "ready", "target_configs": [target]})
        tx.put("node", "managed-node", {"state": "offline", "allowed_app_targets": ["api"]})
        tx.put("run", "run", {"execution_state": "running", "input_fingerprint": "run-input"})
        tx.put("node_job", "existing-job", {"run_id": "run", "node_id": None, "kind": "build",
            "state": "queued", "app_target": "api", "target_config": target,
            "parent_run_fingerprint": "run-input", "created_at": utc_now()})
        return {}

    await store.command("fixture", "queued-managed-work", {}, seed)
    original_job = await store.read("node_job", "existing-job")
    await store.close()

    async def other_background_work(_self):
        # Keep model/product schedulers outside this startup recovery test.
        pass

    def failed_reference(_self):
        raise DomainError("local_reference_failed", "Reference fixture failed before any host tool executed")

    monkeypatch.setattr(Scheduler, "start", other_background_work)
    monkeypatch.setattr(ProductService, "start", other_background_work)
    monkeypatch.setattr(LocalExecutionService, "_fixture", failed_reference)
    application = await Application(settings).start(schedule=schedule)
    try:
        for _ in range(50):
            state = await application.local_execution.status()
            if state.get("error_code") or not schedule:
                break
            await asyncio.sleep(.01)
        assert state.get("error_code") == ("local_reference_failed" if schedule else None)
        assert await application.store.read("node_job", "existing-job") == original_job
        assert not await application.store.list("model_invocation")
    finally:
        await application.close()


async def test_application_recovers_local_work_queued_after_startup_without_repeating_failed_preparation(tmp_path, monkeypatch):
    async with pipeline_fixture(tmp_path, app_targets=("api",)) as env:
        await env.pipeline.begin(await env.claim())
        await env.finish_builds()
        candidate = await env.candidate()
        for identity in candidate["phase_jobs"]["unit"]:
            await env.test_receipt(identity)

        def seed(tx):
            run = tx.get("run", "run")
            tx.put("run", "run", {**run, "execution_state": "paused"}, run["revision"])
            tx.put("local_execution", "managed-local", {"installation_id": "installed", "node_id": "managed-node",
                "state": "ready", "phase": "ready", "target_configs": [env.configs[0].model_dump(mode="json")]})
            return tx.put("node", "managed-node", {"state": "offline", "allowed_app_targets": ["api"]})

        await env.store.command("fixture", "restart-before-next-test-stage", {}, seed)
        await env.store.close()

        def failed_reference(_self):
            raise DomainError("local_reference_failed", "Fixture reference failed; explicit retry is required")

        monkeypatch.setattr(LocalExecutionService, "_fixture", failed_reference)
        application = await Application(env.settings).start()
        try:
            assert not (await application.local_execution.status()).get("error_code")
            assert all(job["state"] == "completed" for job in await application.store.list("node_job"))

            def resume_run(tx):
                run = tx.get("run", "run")
                return tx.put("run", "run", {**run, "execution_state": "running"}, run["revision"])

            await application.store.command("fixture", "unpause-original-run", {}, resume_run)
            application.scheduler.wake()
            async with asyncio.timeout(5):
                while "integration" not in (await application.store.read("candidate", candidate["id"]))["phase_jobs"]:
                    await asyncio.sleep(.02)
            for _ in range(100):
                local = await application.store.read("local_execution", "managed-local")
                if local.get("error_code"):
                    break
                await asyncio.sleep(.02)
            assert local.get("error_code") == "local_reference_failed"
            failed_revision = local["revision"]
            for _ in range(3):
                application.scheduler.wake()
                await asyncio.sleep(.05)
            assert (await application.store.read("local_execution", "managed-local"))["revision"] == failed_revision
            assert len(await application.store.list("candidate")) == 1
            assert len(await application.store.list("run")) == 1
            assert len(await application.store.list("node_job")) == 3
            assert not await application.store.list("model_invocation")
        finally:
            await application.close()

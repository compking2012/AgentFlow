"""Real ASGI HTTP/Store/NodeService scene tests; native receipts are explicit fixtures.

No native OS/toolchain is executed. Fixture-only state updates model native UI
effects after a validated receipt, never an assignment API call by the controller.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentflow.common import canonical_digest, canonical_json
from agentflow.control.scenarios import CrossScenarioCoordinator, CrossScenarioDefinition
from agentflow.execution.manifests import (
    BuildArtifact,
    MatrixPlan,
    MatrixPlanEntry,
    SourceManifest,
    bind_matrix,
    execution_key,
    freeze_platform_manifest,
)
from agentflow.execution.models import TargetConfig
from agentflow.execution.service import NodeService
from agentflow.storage import Store
from agentflow.testing.reports import parse_instrumentation, parse_junit, parse_playwright

ORIGIN = "http://127.0.0.1:18765"
CASES = ["local.agentflow.reference.TicketCrossClientTest::assignApiTicketUsingNativeControl",
         "tests.cross_client.test_assignment::test_android_assignment_is_read_and_updated_in_gtk",
         "assignment.spec.ts::Android then Linux changes are visible in Web after reload::"]


@dataclass
class ScenarioFixture:
    coordinator: CrossScenarioCoordinator
    store: Store
    nodes: NodeService
    run: dict
    candidate: dict
    definition: CrossScenarioDefinition
    backend: dict
    requests: list

    async def start(self, key="scene"):
        return await self.coordinator.start(self.run, self.candidate, self.definition, key, parent_work_item_id="integration")

    async def patch(self, kind, identity, changes):
        def update(tx):
            current = tx.get(kind, identity)
            return tx.put(kind, identity, {**current, **changes}, current["revision"])
        return await self.store.command("fixture.change", str(uuid4()), {}, update)

    async def receipt(self, scene, *, quality="passed", assessment="validated", wrong_case=False, missing_service=False, apply_native_effect=True):
        index = scene["current_step"]
        job = await self.store.read("node_job", scene["job_ids"][index])
        step = self.definition.steps[index]
        if index == 0:
            raw = ("INSTRUMENTATION_STATUS: class=local.agentflow.reference.TicketCrossClientTest\n"
                   "INSTRUMENTATION_STATUS: test=assignApiTicketUsingNativeControl\n"
                   f"INSTRUMENTATION_STATUS_CODE: {'0' if quality == 'passed' else '-2'}\nINSTRUMENTATION_CODE: -1\n").encode()
            parser, format_name, filename = parse_instrumentation, "instrumentation", "android.txt"
        elif index == 1:
            raw = ('<testsuite><testcase classname="tests.cross_client.test_assignment" name="test_android_assignment_is_read_and_updated_in_gtk">'
                   + ("" if quality == "passed" else "<failure>fixture native assertion</failure>") + "</testcase></testsuite>").encode()
            parser, format_name, filename = parse_junit, "junit", "linux.xml"
        else:
            raw = canonical_json({"suites": [{"title": "assignment.spec.ts", "specs": [{"title": "Android then Linux changes are visible in Web after reload",
                "tests": [{"projectName": "", "results": [{"status": "passed" if quality == "passed" else "failed", "duration": 1}]}]}]}]}).encode()
            parser, format_name, filename = parse_playwright, "playwright", "web.json"
        artifact = await self.nodes.import_input(raw, filename, self.run["id"])
        normalized = parser(self.nodes.artifacts.object_path(artifact["digest"]), set(step.recipe.expected_case_ids))
        assert normalized.quality_result == quality
        mapping = self.candidate["matrix_mappings"][step.matrix_entry_id]
        key = execution_key(mapping["test_case_id"], step.target_config_id, self.candidate["fingerprint"])
        check = {"matrix_entry_id": step.matrix_entry_id, "target_config_id": step.target_config_id,
            "target_config_revision": 1, "report_format": format_name, "normalized_report": normalized.model_dump(mode="json"),
            "raw_report_artifact_version_id": artifact["id"], "platform_artifact_manifest_fingerprint": self.candidate["fingerprint"],
            "matrix_binding_fingerprint": self.candidate["matrix_binding"]["binding_fingerprint"],
            "expected_execution_keys": [key], "actual_execution_keys": [key]}
        if wrong_case:
            check["normalized_report"]["cases"][0]["case_id"] = "unplanned-case"
        frame = scene["frame_context"]
        observations = [] if missing_service else [{"service_name": "api", "component_id": frame["backend_product_artifact_id"],
            "source_manifest_fingerprint": frame["source_manifest_fingerprint"], "product_content_digest": frame["backend_product_content_digest"]}]
        result_id = str(uuid4())
        def inject(tx):
            current = tx.get("node_job", job["id"])
            cap = tx.get("node_capability", current["capability_id"])
            tx.put("node_result", result_id, {"job_id": job["id"], "node_id": cap["node_id"],
                "assessment_state": assessment, "verified_checks": [check], "request": {"service_observations": observations},
                "fixture_scope": "controller_integration_only_not_native_os_execution"})
            return tx.put("node_job", current["id"], {**current, "state": "completed", "quality_result": quality,
                "result_id": result_id, "node_id": cap["node_id"]}, current["revision"])
        await self.store.command("fixture.native-receipt", result_id, {}, inject)
        if apply_native_effect and quality == "passed" and assessment == "validated":
            # An explicit fixture effect, not a controller HTTP write or native-pass claim.
            self.backend["tickets"][int(frame["ticket_id"])]["assignee"] = "member" if index == 0 else "manager"
        return await self.store.read("node_result", result_id)


@asynccontextmanager
async def fixture(tmp_path, *, preconfigured=True, credential=True, credential_token="reference.manager"):
    store = Store(tmp_path / "data")
    await store.start()
    nodes = NodeService(store, tmp_path / "data", "https://127.0.0.1:9443")
    try:
        configs = []
        for target in ["api", "android_native", "linux_native", "web"]:
            configs.append(TargetConfig(target_config_id=target, app_target=target, os_name="fixture",
                os_version_constraint="1.0", cpu_architecture="fixture", required_display_protocol="x11" if target == "linux_native" else "not_required",
                required_device_mode="not_required", ui_framework="fixture" if target.endswith("native") else None,
                required_resource_ids=["resource-" + target] if target != "api" else []))
        steps = []
        for i, (name, target) in enumerate(zip(["android_assign", "linux_assign", "web_verify"], ["android_native", "linux_native", "web"], strict=True)):
            steps.append({"step_id": name, "target_config_id": target, "capability_id": "cap-" + target,
                "matrix_entry_id": "matrix-" + target, "recipe": {"adapter": target, "test_kind": "gui" if i < 2 else "integration",
                    "expected_case_ids": [CASES[i]], "report_path": "reports/" + name}})
        definition = CrossScenarioDefinition.model_validate({"scenario_id": "assignment-roundtrip",
            "backend": {"origin": ORIGIN, "target_config_id": "api", "credential_ref": "reference-app:manager", "preconfigured": preconfigured},
            "steps": steps})
        entries = [MatrixPlanEntry(matrix_entry_id="matrix-" + c.app_target.value, test_case_id="case-" + c.app_target.value,
            app_target=c.app_target, component_roles=["product", "test"], target_config_id=c.target_config_id, target_config_revision=1) for c in configs]
        matrix = MatrixPlan(required_app_targets=[c.app_target for c in configs], target_configs=configs, entries=entries)
        source_blob = await nodes.import_input(b"controller integration source fixture", "source.tar", "run")
        build_plan = await nodes.import_input(canonical_json({"schema_version": 1,
            "cross_scenarios": [definition.model_dump(mode="json")]}).encode(), "execution-plan.json", "run")
        source = SourceManifest(source_commit="a" * 40, source_tree_oid="b" * 40,
            source_bundle_artifact_version_id=source_blob["id"], source_bundle_digest=source_blob["digest"],
            test_package_artifact_version_id=source_blob["id"], test_package_digest=source_blob["digest"],
            build_plan_artifact_version_id=build_plan["id"], build_plan_digest=build_plan["digest"],
            target_matrix_fingerprint=matrix.fingerprint, required_app_targets=tuple(c.app_target for c in configs))
        artifacts = []
        for c in configs:
            for role in ["product", "test"]:
                artifact = await nodes.import_input(f"fixture frozen {c.app_target.value} {role}".encode(), role + ".tar", "run")
                artifacts.append(BuildArtifact(artifact_id=c.app_target.value + "-" + role, artifact_version_id=artifact["id"],
                    app_target=c.app_target, target_config_id=c.target_config_id, component_role=role, kind=role,
                    digest=artifact["digest"], source_manifest_fingerprint=source.fingerprint,
                    toolchain_fingerprint=canonical_digest("fixture toolchain"), verified_upload=True,
                    metadata={"relative_path": c.app_target.value + "/" + role, "content_digest": canonical_digest([c.app_target.value, role])}))
        platform = freeze_platform_manifest(source, matrix, artifacts)
        binding = bind_matrix(matrix, source, platform, {e.matrix_entry_id: {} for e in entries})
        mappings = {e.matrix_entry_id: {"matrix_entry_id": e.matrix_entry_id, "test_case_id": e.test_case_id,
            "target_config_id": e.target_config_id, "phase": "cross_scenario",
            "framework_case_ids": ["api-smoke"] if e.app_target.value == "api" else [CASES[["android_native", "linux_native", "web"].index(e.app_target.value)]]} for e in entries}
        def seed(tx):
            run = tx.put("run", "run", {"run_id": "run", "execution_state": "running", "input_fingerprint": canonical_digest("run")})
            tx.put("work_item", "integration", {"run_id": "run", "generation": 1, "input_fingerprint": canonical_digest("parent"), "status": "waiting_execution"})
            candidate = tx.put("candidate", "candidate", {"run_id": "run", "run_input_fingerprint": run["input_fingerprint"],
                "fingerprint": platform.fingerprint, "source_manifest": {**source.model_dump(mode="json"), "fingerprint": source.fingerprint},
                "platform_manifest": {**platform.model_dump(mode="json"), "fingerprint": platform.fingerprint},
                "matrix_plan": matrix.model_dump(mode="json"), "matrix_mappings": mappings, "matrix_binding": binding})
            for c in configs[1:]:
                target = c.app_target.value
                boot = canonical_digest([target, "boot"])
                tx.put("node", "node-" + target, {"state": "online", "allowed_app_targets": [target], "boot_fingerprint": boot})
                tx.put("node_capability", "cap-" + target, {"node_id": "node-" + target, "app_target": target,
                    "verification_state": "functional_verified", "functional_result_id": "proof-" + target,
                    "report": {"target_config_fingerprint": c.fingerprint, "boot_fingerprint": boot}})
                tx.put("node_result", "proof-" + target, {"assessment_state": "validated", "verified_checks": [{"fixture_only": True}]})
                tx.put("node_resource", "resource-" + target, {"node_id": "node-" + target, "state": "available",
                    "fencing_token": 1, "owner_job_id": None})
            return {"run": run, "candidate": candidate}
        initial = await store.command("fixture.scenario", "seed", {}, seed)
        run, candidate = initial["run"], initial["candidate"]
        backend = {"tickets": {}, "next_id": 1, "create_count": 0, "timeout_after_create": False, "error_after_create": False,
            "source": source.fingerprint, "product_content_digest": canonical_digest(["api", "product"])}
        calls = []
        app = FastAPI()
        @app.middleware("http")
        async def observe(request, next_call):
            calls.append({"method": request.method, "path": request.url.path, "headers": dict(request.headers)})
            return await next_call(request)
        @app.get("/api/version")
        async def version():
            return {"schema": "tickets-v1", "source": backend["source"], "product_content_digest": backend["product_content_digest"]}
        @app.post("/api/tickets")
        async def create(request: Request):
            if request.headers.get("authorization") != "Bearer reference.manager":
                return JSONResponse({"error": "unauthorized"}, 401)
            body = await request.json()
            identity = backend["next_id"]
            backend["next_id"] += 1
            backend["create_count"] += 1
            ticket = {"id": identity, "title": body["title"], "assignee": None}
            backend["tickets"][identity] = ticket
            if backend["timeout_after_create"]:
                raise httpx.ReadTimeout("fixture: POST applied but acknowledgement was lost")
            if backend["error_after_create"]:
                return JSONResponse({"error": "fixture: response failed after creation"}, 500)
            return JSONResponse(ticket, 201)
        @app.get("/api/tickets/{identity}")
        async def read(identity: int, request: Request):
            if request.headers.get("authorization") != "Bearer reference.manager":
                return JSONResponse({"error": "unauthorized"}, 401)
            return JSONResponse(backend["tickets"].get(identity, {"error": "not_found"}), 200 if identity in backend["tickets"] else 404)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), headers={"Authorization": "Bearer OWNER_TOKEN_MUST_NOT_LEAK"},
                                    cookies={"owner": "COOKIE_MUST_NOT_LEAK"}) as client:
            resolver = (lambda _ref, origin: {"audience": "agentflow-reference-application", "origin": origin,
                "bearer_token": credential_token}) if credential else None
            coordinator = CrossScenarioCoordinator(store, nodes, client, resolver)
            yield ScenarioFixture(coordinator, store, nodes, run, candidate, definition, backend, calls)
    finally:
        await store.close()


async def test_ordered_native_scene_publishes_checks_only_after_web_and_keeps_application_credentials_separate(tmp_path):
    async with fixture(tmp_path) as env:
        scene = await env.start()
        assert scene["status"] == "waiting_node" and len(scene["job_ids"]) == 1
        assert scene["frame_context"]["ticket_id"] == "1"
        assert scene["frame_context"]["candidate_fingerprint"] == env.candidate["fingerprint"]
        assert await env.store.list("check") == []
        for index, target in enumerate(["android_native", "linux_native", "web"]):
            assert scene["current_step"] == index and len(scene["job_ids"]) == index + 1
            job = await env.store.read("node_job", scene["job_ids"][-1])
            assert job["app_target"] == target
            assert job["recipe"]["scenario_inputs"]["ticket_title"] == scene["frame_context"]["namespace"]
            assert job["recipe"]["service_target_config_ids"] == {"api": "api"}
            assert (await env.coordinator.advance(scene["id"]))["current_step"] == index
            await env.receipt(scene)
            scene = await env.coordinator.advance(scene["id"])
            if index < 2:
                assert await env.store.list("check") == []
        assert scene["status"] == "completed" and scene["quality_result"] == "passed"
        assert len(scene["check_ids"]) == len(await env.store.list("check")) == 3
        assert env.backend["tickets"][1]["assignee"] == "manager"
        assert [c["path"] for c in env.requests if c["method"] != "GET"] == ["/api/tickets"]
        assert all("OWNER_TOKEN" not in json.dumps(call) and "COOKIE_MUST_NOT_LEAK" not in json.dumps(call) for call in env.requests)
        assert all("cookie" not in call["headers"] for call in env.requests)
        assert all(call["headers"].get("authorization") is None for call in env.requests if call["path"] == "/api/version")
        assert "reference.manager" not in canonical_json(await env.store.read("cross_scenario", scene["id"]))
        assert (await env.start())["check_ids"] == scene["check_ids"]
        assert env.backend["create_count"] == 1


@pytest.mark.parametrize("gap", ["not_preconfigured", "credential_missing", "owner_credential", "backend_mismatch", "capability_missing", "resource_unavailable"])
async def test_missing_frozen_backend_or_native_precondition_blocks_before_api_post(tmp_path, gap):
    async with fixture(tmp_path, preconfigured=gap != "not_preconfigured", credential=gap != "credential_missing",
                       credential_token="OWNER_TOKEN" if gap == "owner_credential" else "reference.manager") as env:
        if gap == "backend_mismatch":
            env.backend["source"] = canonical_digest("other source")
        elif gap == "capability_missing":
            await env.patch("node_capability", "cap-linux_native", {"verification_state": "static_verified"})
        elif gap == "resource_unavailable":
            await env.patch("node_resource", "resource-linux_native", {"state": "quarantined"})
        scene = await env.start()
        assert scene["status"] == "blocked"
        assert env.backend["create_count"] == 0
        assert await env.store.list("node_job") == []
        assert await env.store.list("check") == []


async def test_uncertain_api_creation_never_reposts_even_after_restart(tmp_path):
    async with fixture(tmp_path) as env:
        env.backend["timeout_after_create"] = True
        scene = await env.start()
        assert scene["status"] == "execution_unknown"
        assert scene["create_intent"]["body"]["title"] == scene["frame_context"]["namespace"]
        assert env.backend["create_count"] == 1
        env.coordinator = CrossScenarioCoordinator(env.store, env.nodes, env.coordinator.http_client, env.coordinator.credential_resolver)
        for _ in range(3):
            assert (await env.start())["status"] == "execution_unknown"
        assert env.backend["create_count"] == 1
        assert await env.store.list("node_job") == []


async def test_http_server_error_after_write_is_an_unknown_creation_outcome(tmp_path):
    async with fixture(tmp_path) as env:
        env.backend["error_after_create"] = True
        scene = await env.start()
        assert scene["status"] == "execution_unknown"
        assert (await env.start())["status"] == "execution_unknown"
        assert env.backend["create_count"] == 1
        assert await env.store.list("node_job") == []


@pytest.mark.parametrize("after_commit", [False, True])
async def test_ticket_confirmation_database_loss_never_duplicates_creation(tmp_path, monkeypatch, after_commit):
    async with fixture(tmp_path) as env:
        original = env.store.command
        failed = False
        async def fault(scope, key, payload, handler):
            nonlocal failed
            if scope == "cross_scenario.transition" and payload.get("status") == "ready" and "create_receipt" in payload and not failed:
                failed = True
                if after_commit:
                    await original(scope, key, payload, handler)
                raise OSError("fixture: creation confirmation DB ACK lost")
            return await original(scope, key, payload, handler)
        monkeypatch.setattr(env.store, "command", fault)
        with pytest.raises(OSError):
            await env.start()
        scene = (await env.store.list("cross_scenario"))[0]
        if not after_commit:
            intent = {**scene["create_intent"], "deadline_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
            scene = await env.patch("cross_scenario", scene["id"], {"create_intent": intent})
        scene = await env.coordinator.advance(scene["id"])
        assert scene["status"] == ("waiting_node" if after_commit else "execution_unknown")
        assert env.backend["create_count"] == 1


@pytest.mark.parametrize("after_commit", [False, True])
async def test_domain_error_recording_a_creation_receipt_cannot_reopen_the_post_path(tmp_path, monkeypatch, after_commit):
    from agentflow.common import DomainError
    async with fixture(tmp_path) as env:
        original = env.store.command
        failed = False
        async def fault(scope, key, payload, handler):
            nonlocal failed
            if scope == "cross_scenario.transition" and payload.get("status") == "ready" and "create_receipt" in payload and not failed:
                failed = True
                if after_commit:
                    await original(scope, key, payload, handler)
                raise DomainError("invalid_json", "fixture: receipt command returned a domain error")
            return await original(scope, key, payload, handler)
        monkeypatch.setattr(env.store, "command", fault)
        scene = await env.start()
        scene = await env.coordinator.advance(scene["id"])
        assert scene["status"] == ("waiting_node" if after_commit else "execution_unknown")
        assert env.backend["create_count"] == 1


@pytest.mark.parametrize("after_commit", [False, True])
async def test_node_enqueue_database_loss_replays_the_same_durable_dispatch(tmp_path, monkeypatch, after_commit):
    async with fixture(tmp_path) as env:
        original = env.store.command
        failed = False
        async def fault(scope, key, payload, handler):
            nonlocal failed
            if scope.startswith("node_job_enqueue:") and not failed:
                failed = True
                if after_commit:
                    await original(scope, key, payload, handler)
                raise OSError("fixture: node enqueue DB ACK lost")
            return await original(scope, key, payload, handler)
        monkeypatch.setattr(env.store, "command", fault)
        with pytest.raises(OSError):
            await env.start()
        scene = (await env.store.list("cross_scenario"))[0]
        assert scene["status"] == "dispatching"
        scene = await env.coordinator.advance(scene["id"])
        assert scene["status"] == "waiting_node"
        assert len(scene["job_ids"]) == len(await env.store.list("node_job")) == 1
        assert env.backend["create_count"] == 1


@pytest.mark.parametrize("failure", ["failed", "rejected", "wrong_case", "missing_service", "native_effect_missing", "other_backend"])
async def test_unverified_previous_native_step_never_authorizes_linux_or_publishes_checks(tmp_path, failure):
    async with fixture(tmp_path) as env:
        scene = await env.start()
        await env.receipt(scene, quality="failed" if failure == "failed" else "passed",
            assessment="rejected" if failure == "rejected" else "validated", wrong_case=failure == "wrong_case",
            missing_service=failure == "missing_service", apply_native_effect=failure != "native_effect_missing")
        if failure == "other_backend":
            env.backend["product_content_digest"] = canonical_digest("different backend product")
        scene = await env.coordinator.advance(scene["id"])
        assert scene["status"] in {"blocked", "failed"}
        assert len(await env.store.list("node_job")) == 1
        assert await env.store.list("check") == []


async def test_scene_definition_must_exist_in_exact_source_frozen_build_plan(tmp_path):
    async with fixture(tmp_path) as env:
        changed = env.definition.model_dump(mode="json")
        changed["steps"][0]["recipe"]["report_path"] = "different/path.txt"
        scene = await env.coordinator.start(env.run, env.candidate, changed, "changed")
        assert scene["status"] == "blocked" and scene["blocking_code"] == "scenario_definition_unbound"
        assert env.backend["create_count"] == 0


async def test_concurrent_start_creates_one_ticket_and_one_android_job(tmp_path):
    async with fixture(tmp_path) as env:
        results = await asyncio.gather(env.start(), env.start(), env.start())
        assert len({scene["id"] for scene in results}) == 1
        assert env.backend["create_count"] == 1
        assert len(await env.store.list("node_job")) == 1


async def test_independent_coordinator_instances_do_not_duplicate_the_post(tmp_path):
    async with fixture(tmp_path) as env:
        other = CrossScenarioCoordinator(env.store, env.nodes, env.coordinator.http_client, env.coordinator.credential_resolver)
        results = await asyncio.gather(env.start(), other.start(env.run, env.candidate, env.definition, "scene", parent_work_item_id="integration"))
        assert len({scene["id"] for scene in results}) == 1
        assert env.backend["create_count"] == 1
        assert len(await env.store.list("node_job")) == 1


@pytest.mark.parametrize("after_commit", [False, True])
async def test_api_intent_database_ack_loss_never_sends_an_unrecorded_or_second_post(tmp_path, monkeypatch, after_commit):
    async with fixture(tmp_path) as env:
        original = env.store.command
        failed = False
        async def fault(scope, key, payload, handler):
            nonlocal failed
            if scope == "cross_scenario.transition" and payload.get("status") == "creating" and not failed:
                failed = True
                if after_commit:
                    await original(scope, key, payload, handler)
                raise OSError("fixture: API intent acknowledgement lost")
            return await original(scope, key, payload, handler)
        monkeypatch.setattr(env.store, "command", fault)
        with pytest.raises(OSError):
            await env.start()
        assert env.backend["create_count"] == 0
        scene = (await env.store.list("cross_scenario"))[0]
        if after_commit:
            intent = {**scene["create_intent"], "deadline_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
            scene = await env.patch("cross_scenario", scene["id"], {"create_intent": intent})
        scene = await env.coordinator.advance(scene["id"])
        assert scene["status"] == ("execution_unknown" if after_commit else "waiting_node")
        assert env.backend["create_count"] == (0 if after_commit else 1)


async def test_unknown_native_execution_stays_unknown_and_can_still_be_cancelled(tmp_path):
    async with fixture(tmp_path) as env:
        scene = await env.start()
        identity = scene["job_ids"][0]
        await env.patch("node_job", identity, {"state": "execution_unknown"})
        scene = await env.coordinator.advance(scene["id"])
        assert scene["status"] == "execution_unknown"
        assert len(await env.store.list("node_job")) == 1
        await env.patch("run", "run", {"execution_state": "cancelling"})
        scene = await env.coordinator.advance(scene["id"])
        assert scene["status"] == "cancelling"
        assert (await env.store.read("node_job", identity))["state"] == "stopping"
        assert await env.store.list("check") == []


async def test_stale_run_cancels_old_scene_job_instead_of_advancing(tmp_path):
    async with fixture(tmp_path) as env:
        scene = await env.start()
        await env.patch("run", "run", {"input_fingerprint": canonical_digest("new generation")})
        scene = await env.coordinator.advance(scene["id"])
        assert scene["status"] == "blocked" and scene["terminal"]
        assert (await env.store.read("node_job", scene["job_ids"][0]))["state"] == "cancelled"
        assert await env.store.list("check") == []


async def test_previous_raw_report_is_reverified_before_final_checks(tmp_path):
    async with fixture(tmp_path) as env:
        scene = await env.start()
        first = None
        for _ in range(2):
            result = await env.receipt(scene)
            if first is None:
                first = result["verified_checks"][0]["raw_report_artifact_version_id"]
            scene = await env.coordinator.advance(scene["id"])
        await env.receipt(scene)
        artifact = await env.store.read("node_artifact", first)
        env.nodes.artifacts.object_path(artifact["digest"]).unlink()
        scene = await env.coordinator.advance(scene["id"])
        assert scene["status"] == "blocked"
        assert await env.store.list("check") == []


async def test_cancelled_parent_uses_node_cancel_and_does_not_release_quarantined_resource(tmp_path):
    async with fixture(tmp_path) as env:
        scene = await env.start()
        identity = scene["job_ids"][0]
        await env.patch("node_job", identity, {"state": "running", "resource_leases": [{"resource_id": "resource-android_native", "fencing_token": 2}]})
        await env.patch("node_resource", "resource-android_native", {"state": "leased", "owner_job_id": identity, "fencing_token": 2})
        await env.patch("run", "run", {"execution_state": "cancelling"})
        scene = await env.coordinator.advance(scene["id"])
        assert scene["status"] == "cancelling"
        assert (await env.store.read("node_job", identity))["state"] == "stopping"
        assert (await env.store.read("node_resource", "resource-android_native"))["state"] == "quarantined"
        await env.patch("node_job", identity, {"state": "cancelled"})
        scene = await env.coordinator.advance(scene["id"])
        assert scene["status"] == "cancelled"
        assert await env.store.list("check") == []


@pytest.mark.parametrize("after_commit", [False, True])
async def test_final_publication_database_ack_loss_publishes_each_check_once(tmp_path, monkeypatch, after_commit):
    async with fixture(tmp_path) as env:
        scene = await env.start()
        for _ in range(2):
            await env.receipt(scene)
            scene = await env.coordinator.advance(scene["id"])
        await env.receipt(scene)
        original = env.store.command
        failed = False
        async def fault(scope, key, payload, handler):
            nonlocal failed
            if scope == "cross_scenario.complete" and not failed:
                failed = True
                if after_commit:
                    await original(scope, key, payload, handler)
                raise OSError("fixture: final scene DB ACK lost")
            return await original(scope, key, payload, handler)
        monkeypatch.setattr(env.store, "command", fault)
        with pytest.raises(OSError):
            await env.coordinator.advance(scene["id"])
        result = await env.coordinator.advance(scene["id"])
        assert result["status"] == "completed"
        assert len(result["check_ids"]) == len(await env.store.list("check")) == 3
        assert env.backend["create_count"] == 1

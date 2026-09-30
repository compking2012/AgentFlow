"""Owner HTTP boundary tests; seeded native receipts are explicit control fixtures."""

from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from agentflow.common import canonical_digest, utc_now
from agentflow.control.api import create_app
from agentflow.control.execution_pipeline import ProjectExecutionSpec
from agentflow.execution.manifests import (
    BuildArtifact,
    MatrixPlan,
    MatrixPlanEntry,
    SourceManifest,
    bind_matrix,
    execution_key,
    freeze_platform_manifest,
)
from agentflow.execution.models import (
    CapabilityReport,
    DisplayObservation,
    JobClaimRequest,
    NodeIdentity,
    TargetConfig,
)
from agentflow.execution.service import NodeService
from agentflow.settings import Settings
from agentflow.storage import Store


@pytest_asyncio.fixture
async def owner_nodes(tmp_path, request):
    options = getattr(request, "param", {})
    settings = Settings(data_dir=tmp_path / "controller")
    store = Store(settings.data_dir)
    await store.start()
    service = NodeService(store, settings.data_dir, "https://127.0.0.1:9443")
    app = create_app(settings, store=store, node_service=service)
    config = TargetConfig(target_config_id="api-config", app_target="api", os_name="fixture",
        os_version_constraint="1", cpu_architecture="fixture", required_display_protocol="not_required",
        required_device_mode="not_required", required_resource_ids=["workspace-one"])
    spec = ProjectExecutionSpec.model_validate({"targets": [{"target_config_id": config.target_config_id,
        "build": {"adapter": "api", "output_paths": {"product": "bundle/product", "test": "bundle/test"}},
        "unit": {"adapter": "api", "test_kind": "unit", "unit_project": "bundle/test/unit.mjs", "expected_case_ids": ["unit"]},
        "integration": {"adapter": "api", "test_kind": "api", "expected_case_ids": ["api-create", "api-read"]}}]})
    if options.get("no_integration"):
        spec.targets[0].integration = None
    phases = [phase for phase in ("unit", "integration") if getattr(spec.targets[0], phase) is not None]
    entries = [MatrixPlanEntry(matrix_entry_id=phase, test_case_id=phase, app_target="api",
        target_config_id=config.target_config_id, target_config_revision=1, component_roles=["product", "test"])
        for phase in phases]
    if options.get("scenario_entry"):
        entries.append(MatrixPlanEntry(matrix_entry_id="scenario-integration", test_case_id="cross-case", app_target="api",
            target_config_id=config.target_config_id, target_config_revision=1, component_roles=["product", "test"]))
    matrix = MatrixPlan(required_app_targets=["api"], target_configs=[config], entries=entries)
    source_blob = await service.import_input(b"explicit source transport fixture", "source.tar", "run")
    plan_blob = await service.import_input(spec.model_dump_json().encode(), "execution-plan.json", "run")
    source = SourceManifest(source_commit="a" * 40, source_tree_oid="b" * 40,
        source_bundle_artifact_version_id=source_blob["id"], source_bundle_digest=source_blob["digest"],
        test_package_artifact_version_id=source_blob["id"], test_package_digest=source_blob["digest"],
        build_plan_artifact_version_id=plan_blob["id"], build_plan_digest=plan_blob["digest"],
        target_matrix_fingerprint=matrix.fingerprint, required_app_targets=("api",))
    components = []
    for role in ("product", "test"):
        blob = await service.import_input(("explicit " + role + " fixture").encode(), role + ".tar", "run")
        components.append(BuildArtifact(artifact_id=role, artifact_version_id=blob["id"], app_target="api",
            target_config_id=config.target_config_id, component_role=role, kind=role, digest=blob["digest"],
            source_manifest_fingerprint=source.fingerprint, toolchain_fingerprint=canonical_digest("fixture-toolchain"),
            verified_upload=True, metadata={"relative_path": "bundle/" + role, "content_digest": blob["digest"]}))
    platform = freeze_platform_manifest(source, matrix, components)
    binding = bind_matrix(matrix, source, platform, {entry.matrix_entry_id: {} for entry in entries})
    boot = canonical_digest("explicit-fixture-boot")
    report = CapabilityReport(app_target="api", target_config_fingerprint=config.fingerprint,
        os_name="fixture", os_version="1", architecture="fixture", boot_fingerprint=boot,
        display=DisplayObservation(session_fingerprint=boot), tools=[], state="static_verified", observed_at=utc_now())

    def seed(tx):
        tx.put("node", "node", {"state": "online", "boot_fingerprint": boot, "allowed_app_targets": ["api"],
            "config_revision": 1, "certificate_fingerprint": canonical_digest("control-fixture-certificate")})
        tx.put("node_capability", "capability", {"node_id": "node", "app_target": "api",
            "verification_state": "static_verified", "functional_result_id": None,
            "observed_at": report.observed_at, "environment_fingerprint": report.environment_fingerprint,
            "report": report.model_dump(mode="json")})
        tx.put("run", "run", {"execution_state": "running", "input_fingerprint": canonical_digest("run-input")})
        mappings = {phase: {"matrix_entry_id": phase, "test_case_id": phase, "phase": phase,
            "target_config_id": config.target_config_id, "framework_case_ids": getattr(spec.targets[0], phase).expected_case_ids}
            for phase in phases}
        if options.get("scenario_entry"):
            mappings["scenario-integration"] = {"matrix_entry_id": "scenario-integration", "test_case_id": "cross-case",
                "phase": "integration", "scenario_id": "cross-audit", "target_config_id": config.target_config_id,
                "framework_case_ids": ["cross-case"]}
        return tx.put("candidate", "candidate", {"run_id": "run", "run_input_fingerprint": canonical_digest("run-input"),
            "fingerprint": platform.fingerprint, "source_manifest": {**source.model_dump(mode="json"), "fingerprint": source.fingerprint},
            "platform_manifest": {**platform.model_dump(mode="json"), "fingerprint": platform.fingerprint},
            "matrix_plan": matrix.model_dump(mode="json"), "matrix_binding": binding, "recipes": spec.model_dump(mode="json"),
            "matrix_mappings": mappings})

    candidate = await store.command("fixture", "seed-owner-nodes", {}, seed)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=settings.origin) as client:
        bootstrap = await client.post("/api/v1/session", json={"bootstrap_token": app.state.tokens.bootstrap_code},
            headers={"Origin": settings.origin, "Idempotency-Key": "owner-bootstrap"})
        headers = {"Authorization": "Bearer " + bootstrap.json()["owner_token"], "Origin": settings.origin,
                   "Idempotency-Key": "owner-command"}
        yield app, client, headers, service, candidate, source_blob
    await store.close()


def probe_request(candidate):
    return {"candidate_id": "candidate", "expected_candidate_fingerprint": candidate["fingerprint"],
            "target_config_id": "api-config", "capability_id": "capability"}


async def test_owner_can_register_resources_and_queue_exact_frozen_probe(owner_nodes):
    _app, client, headers, service, candidate, _blob = owner_nodes
    payload = {"kind": "workspace", "resource_id": "workspace-one", "identity_fingerprint": canonical_digest("owner-selected-workspace")}
    resource = await client.post("/api/v1/executor_nodes/node/resources", json=payload, headers=headers)
    assert resource.status_code == 201, resource.text
    assert resource.json()["state"] == "available"
    assert (await client.post("/api/v1/executor_nodes/node/resources", json=payload, headers=headers)).json() == resource.json()
    response = await client.post("/api/v1/executor_nodes/node/functional_probes", json=probe_request(candidate), headers=headers)
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["kind"] == "capability_probe" and job["capability_id"] == "capability"
    assert job["recipe"]["test_kind"] == "api"
    assert job["matrix_entry_ids"] == ["integration"]
    assert job["matrix_entries"][0]["framework_case_ids"] == ["api-create", "api-read"]
    assert job["platform_artifact_manifest"]["fingerprint"] == candidate["fingerprint"]
    assert job["required_resource_ids"] == ["workspace-one"]
    assert (await client.post("/api/v1/executor_nodes/node/functional_probes", json=probe_request(candidate), headers=headers)).json() == job
    assert len(await service.store.list("node_job")) == 1


async def test_probe_and_confirmation_cannot_accept_owner_supplied_passes_or_wrong_target(owner_nodes):
    _app, client, headers, service, candidate, _blob = owner_nodes
    for extra in ({"quality_result": "passed"}, {"recipe": {}}, {"source_manifest": {}}):
        response = await client.post("/api/v1/executor_nodes/node/functional_probes", json={**probe_request(candidate), **extra}, headers=headers)
        assert response.status_code == 422
    for change in ({"expected_candidate_fingerprint": canonical_digest("stale")}, {"target_config_id": "other-config"}):
        response = await client.post("/api/v1/executor_nodes/node/functional_probes", json={**probe_request(candidate), **change}, headers=headers)
        assert response.status_code == 409
    response = await client.post("/api/v1/executor_nodes/node/capabilities/capability/confirm",
        json={"result_id": "unverified"}, headers=headers)
    assert response.status_code == 409
    assert (await service.store.read("node_capability", "capability"))["verification_state"] == "static_verified"
    assert await service.store.list("node_job") == []


async def test_owner_confirmation_requires_validated_probe_result_and_is_idempotent(owner_nodes):
    _app, client, headers, service, candidate, _blob = owner_nodes
    response = await client.post("/api/v1/executor_nodes/node/functional_probes", json=probe_request(candidate), headers=headers)
    job = response.json()
    await service.register_resource("node", "workspace", canonical_digest("owner-selected-workspace"), "resource", "workspace-one")
    capability = await service.store.read("node_capability", "capability")
    identity = NodeIdentity(node_id="node", certificate_fingerprint=canonical_digest("control-fixture-certificate"),
                            certificate_serial="control-fixture")
    assignment = (await service.claim_job(identity, JobClaimRequest(operation_id="claim-probe", node_revision=1,
        boot_fingerprint=capability["report"]["boot_fingerprint"], capability_ids=["capability"],
        available_resource_ids=["workspace-one"])))["assignment"]
    assert assignment["job_id"] == job["id"]
    # Real bounded upload and production report parsing. This intentionally tests
    # the controller protocol, not an assertion that a native OS was executed.
    raw = b'<testsuite><testcase name="api-create" fullname="api-create"/><testcase name="api-read" fullname="api-read"/></testsuite>'
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    upload = await service.begin_upload(identity, job["id"], assignment["attempt_token"],
        {"name": "functional-report.xml", "size": len(raw), "digest": digest}, "upload-report")
    await service.append_chunk(identity, job["id"], upload["id"], assignment["attempt_token"], 0, raw, digest, "upload-chunk")
    artifact = await service.complete_upload(identity, job["id"], upload["id"], assignment["attempt_token"], "upload-complete")
    route = "/api/v1/executor_nodes/node/capabilities/capability/confirm"
    assert (await client.post(route, json={"result_id": "not-yet-validated"}, headers=headers)).status_code == 409
    key = execution_key("integration", "api-config", candidate["fingerprint"])
    outcome = await service.submit_job_result(identity, job["id"], {
        "expected_revision": assignment["revision"], "operation_id": "submit-real-upload",
        "job_kind": "capability_probe", "app_target": "api", "fencing_token": assignment["fencing_token"],
        "input_fingerprint": assignment["input_fingerprint"], "execution_status": "completed", "quality_result": "passed",
        "observed_source_manifest_fingerprint": assignment["source_manifest"]["fingerprint"],
        "observed_platform_artifact_manifest_fingerprint": candidate["fingerprint"],
        "observed_test_package_digest": assignment["test_package_digest"], "artifact_version_ids": [artifact["id"]],
        "finished_at": utc_now(), "checks": [{"matrix_entry_id": "integration", "test_case_id": "integration",
            "raw_report_artifact_version_id": artifact["id"], "report_format": "junit", "quality_result": "passed",
            "platform_artifact_manifest_fingerprint": candidate["fingerprint"],
            "matrix_binding_fingerprint": assignment["matrix_binding_fingerprint"],
            "environment_fingerprint": capability["environment_fingerprint"], "exit_code": 0,
            "expected_execution_keys": [key], "actual_execution_keys": [key], "target_config_id": "api-config",
            "target_config_revision": 1, "matrix_plan_fingerprint": assignment["matrix_plan_fingerprint"],
            "observed_components": [{"component_id": value["artifact_id"], "actual_digest": value["digest"]}
                                    for value in candidate["platform_manifest"]["artifacts"]]}]}, assignment["attempt_token"])
    assert outcome["assessment_state"] == "validated"
    result = await service.store.read("node_result", outcome["result_id"])
    assert result["verified_checks"][0]["normalized_report"]["raw_digest"] == digest
    confirmed = await client.post(route, json={"result_id": outcome["result_id"]}, headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["verification_state"] == "functional_verified"
    replays = await asyncio.gather(*(client.post(route, json={"result_id": outcome["result_id"]}, headers=headers)
                                     for _ in range(4)))
    assert all(response.status_code == 200 and response.json() == confirmed.json() for response in replays)
    viewed = await client.get("/api/v1/executor_jobs/" + job["id"], headers=headers)
    assert viewed.json()["result"]["assessment_state"] == "validated"
    assert "attempt_token" not in json.dumps(viewed.json())


async def test_owner_artifact_read_and_routes_keep_audience_boundary(owner_nodes):
    app, client, headers, _service, candidate, blob = owner_nodes
    route = "/api/v1/executor_artifacts/" + blob["id"]
    assert (await client.get(route)).status_code == 401
    attempt = app.state.tokens.issue("agentflow_attempt", {"llm:chat"}, "attempt", 60)
    assert (await client.get(route, headers={"Authorization": "Bearer " + attempt})).status_code == 403
    assert (await client.get(route, headers=headers)).json()["digest"] == blob["digest"]
    downloaded = await client.get(route + "?download=true", headers=headers)
    assert downloaded.content == b"explicit source transport fixture"
    assert downloaded.headers["content-type"] == "application/octet-stream"
    assert (await client.post("/api/v1/executor_nodes/node/functional_probes", json=probe_request(candidate),
                             headers={"Origin": app.state.settings.origin, "Idempotency-Key": str(uuid4())})).status_code == 401


async def test_mutated_candidate_recipe_cannot_replace_frozen_reference_tests(owner_nodes):
    _app, client, headers, service, candidate, _blob = owner_nodes
    def mutate(tx):
        current = tx.get("candidate", "candidate")
        current["recipes"]["targets"][0]["integration"]["expected_case_ids"] = ["easier-case"]
        return tx.put("candidate", "candidate", current, current["revision"])
    await service.store.command("fixture", "change-case", {}, mutate)
    response = await client.post("/api/v1/executor_nodes/node/functional_probes", json=probe_request(candidate), headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "functional_plan_unbound"


@pytest.mark.parametrize("owner_nodes", [{"no_integration": True}], indirect=True)
async def test_unit_only_candidate_reports_missing_functional_recipe(owner_nodes):
    _app, client, headers, service, candidate, _blob = owner_nodes
    response = await client.post("/api/v1/executor_nodes/node/functional_probes", json=probe_request(candidate), headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "functional_recipe_missing"
    assert await service.store.list("node_job") == []


@pytest.mark.parametrize("owner_nodes", [{"scenario_entry": True}], indirect=True)
async def test_base_functional_probe_excludes_cross_scenario_entries(owner_nodes):
    _app, client, headers, _service, candidate, _blob = owner_nodes
    response = await client.post("/api/v1/executor_nodes/node/functional_probes", json=probe_request(candidate), headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["matrix_entry_ids"] == ["integration"]
    assert response.json()["matrix_entries"][0]["framework_case_ids"] == ["api-create", "api-read"]

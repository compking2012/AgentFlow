"""Separate management and execution routers share one controller domain service."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentflow.common import DomainError, canonical_digest
from agentflow.control.api import bearer, body, command_key
from agentflow.control.execution_pipeline import ProjectExecutionSpec
from agentflow.execution.manifests import MatrixPlan, PlatformManifest, SourceManifest, file_digest
from agentflow.execution.models import Digest, JobLimits


class ResourceRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["device", "simulator", "desktop_session", "display", "port", "workspace", "test_data_namespace"]
    identity_fingerprint: Digest
    resource_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class FunctionalProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=128)
    expected_candidate_fingerprint: Digest
    target_config_id: str = Field(min_length=1, max_length=128)
    capability_id: str = Field(min_length=1, max_length=128)


class FunctionalConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_id: str = Field(min_length=1, max_length=128)


async def _active_node(service, node_id):
    node = await service.store.read("node", node_id)
    if not node or node.get("state") == "revoked":
        raise DomainError("node_missing", "An active paired node is required", 404)
    return node


async def _functional_inputs(service, node_id: str, request: FunctionalProbeRequest):
    node = await _active_node(service, node_id)
    capability = await service.store.read("node_capability", request.capability_id)
    candidate = await service.store.read("candidate", request.candidate_id)
    if not candidate:
        raise DomainError("candidate_missing", "Build and freeze a candidate before requesting a functional probe", 404)
    run = await service.store.read("run", candidate["run_id"])
    if (not run or run["execution_state"] not in {"running", "paused"}
            or run["input_fingerprint"] != candidate["run_input_fingerprint"]
            or candidate["fingerprint"] != request.expected_candidate_fingerprint):
        raise DomainError("stale_candidate", "The requested candidate no longer matches its active run")
    if not candidate.get("platform_manifest") or not candidate.get("matrix_binding"):
        raise DomainError("candidate_not_built", "Verified platform products and test packages must be frozen first")
    source = SourceManifest.model_validate({k: v for k, v in candidate["source_manifest"].items() if k != "fingerprint"})
    platform = PlatformManifest.model_validate({k: v for k, v in candidate["platform_manifest"].items() if k != "fingerprint"})
    matrix = MatrixPlan.model_validate(candidate["matrix_plan"])
    binding = candidate["matrix_binding"]
    if (source.fingerprint != candidate["source_manifest"]["fingerprint"]
            or platform.fingerprint != candidate["fingerprint"]
            or platform.source_manifest.fingerprint != source.fingerprint
            or source.target_matrix_fingerprint != matrix.fingerprint
            or platform.target_matrix_fingerprint != matrix.fingerprint
            or binding.get("fingerprint") != matrix.fingerprint
            or binding.get("source_manifest", {}).get("fingerprint") != source.fingerprint
            or binding.get("platform_artifact_manifest", {}).get("fingerprint") != platform.fingerprint
            or canonical_digest({k: v for k, v in binding.items() if k != "binding_fingerprint"}) != binding.get("binding_fingerprint")):
        raise DomainError("candidate_identity_mismatch", "Frozen source, products and required matrix do not agree")
    config = next((value for value in matrix.target_configs if value.target_config_id == request.target_config_id), None)
    if (not config or not capability or capability["node_id"] != node_id
            or capability["app_target"] != config.app_target.value
            or capability.get("report", {}).get("target_config_fingerprint") != config.fingerprint
            or capability.get("report", {}).get("boot_fingerprint") != node.get("boot_fingerprint")
            or capability.get("verification_state") not in {"static_verified", "functional_verified"}):
        raise DomainError("probe_capability_mismatch", "Choose this node's current static capability for the exact target")
    spec = ProjectExecutionSpec.model_validate(candidate["recipes"])
    plan_artifact = await service.store.read("node_artifact", source.build_plan_artifact_version_id)
    if not plan_artifact or plan_artifact.get("digest") != source.build_plan_digest:
        raise DomainError("functional_plan_unbound", "The probe needs its exact source-frozen build plan")
    plan_path = service.artifacts.object_path(plan_artifact["digest"])
    if not plan_path.is_file() or plan_path.is_symlink() or plan_path.stat().st_size > 4 * 1024 * 1024 or file_digest(plan_path) != source.build_plan_digest:
        raise DomainError("functional_plan_unbound", "Frozen build plan is missing, corrupt or too large")
    if ProjectExecutionSpec.model_validate_json(plan_path.read_bytes()) != spec:
        raise DomainError("functional_plan_unbound", "Candidate recipes differ from the source-frozen build plan")
    target = next((value for value in spec.targets if value.target_config_id == config.target_config_id), None)
    if not target or target.integration is None or target.integration.adapter != config.app_target or target.integration.test_kind == "unit":
        raise DomainError("functional_recipe_missing", "A frozen integration/GUI reference recipe is required")
    entries = [entry for entry in candidate["matrix_mappings"].values()
               if entry["target_config_id"] == config.target_config_id and entry["phase"] == "integration"
               and not entry.get("scenario_id")]
    required_ids = {entry.matrix_entry_id for entry in matrix.entries
                    if entry.target_config_id == config.target_config_id and entry.required}
    if (not entries or any(entry["matrix_entry_id"] not in required_ids for entry in entries)
            or {case for entry in entries for case in entry["framework_case_ids"]} != set(target.integration.expected_case_ids)):
        raise DomainError("functional_case_mismatch", "Probe must retain every frozen integration case for the target")
    return candidate, config, target.integration, entries, matrix


def owner_node_router(service) -> APIRouter:
    router = APIRouter()

    @router.post("/api/v1/executor_pairings", status_code=201)
    async def create_pairing(request: Request):
        return await service.create_pairing(await body(request), command_key(request))

    @router.get("/api/v1/executor_pairings")
    async def pairings():
        return {"items": await service.store.list("node_pairing")}

    @router.post("/api/v1/executor_nodes/{node_id}/revoke")
    async def revoke(node_id: str, request: Request):
        payload = await body(request)
        return await service.revoke_node(node_id, payload["expected_revision"], payload["reason"], command_key(request))

    @router.get("/api/v1/executor_nodes/{node_id}/probes")
    async def probes(node_id: str):
        return {"items": [p for p in await service.store.list("node_capability") if p.get("node_id") == node_id]}

    @router.post("/api/v1/executor_nodes/{node_id}/resources", status_code=201)
    async def register_resource(node_id: str, request: Request):
        payload = ResourceRegistration.model_validate(await body(request))
        await _active_node(service, node_id)
        return await service.register_resource(node_id, payload.kind, payload.identity_fingerprint,
                                               command_key(request), resource_id=payload.resource_id)

    @router.post("/api/v1/executor_nodes/{node_id}/functional_probes", status_code=201)
    async def functional_probe(node_id: str, request: Request):
        payload = FunctionalProbeRequest.model_validate(await body(request))
        candidate, config, recipe, entries, matrix = await _functional_inputs(service, node_id, payload)
        settings = request.app.state.settings
        limits = JobLimits(maximum_active_seconds=settings.node_active_seconds,
                           maximum_output_bytes=settings.node_output_limit_bytes)
        return await service.enqueue_functional_probe(candidate["run_id"], capability_id=payload.capability_id,
            target_config=config, source_manifest=candidate["source_manifest"],
            platform_manifest=candidate["platform_manifest"], recipe=recipe, matrix_entries=entries,
            matrix_plan_fingerprint=matrix.fingerprint,
            matrix_binding_fingerprint=candidate["matrix_binding"]["binding_fingerprint"],
            required_resource_ids=config.required_resource_ids, limits=limits,
            idempotency_key=command_key(request))

    @router.post("/api/v1/executor_nodes/{node_id}/capabilities/{capability_id}/confirm")
    async def confirm_capability(node_id: str, capability_id: str, request: Request):
        payload = FunctionalConfirmation.model_validate(await body(request))
        await _active_node(service, node_id)
        cap = await service.store.read("node_capability", capability_id)
        result = await service.store.read("node_result", payload.result_id)
        if not cap or cap["node_id"] != node_id or not result or result.get("node_id") != node_id:
            raise DomainError("functional_evidence_missing", "The capability and verified result must belong to this node")
        return await service.confirm_functional_capability(capability_id, payload.result_id, command_key(request))

    @router.get("/api/v1/executor_jobs")
    async def jobs():
        # Attempt tokens are derived on claim and never returned by this read model.
        return {"items": await service.store.list("node_job")}

    @router.get("/api/v1/executor_jobs/{job_id}")
    async def owner_job(job_id: str):
        job = await service.store.read("node_job", job_id)
        if not job:
            raise DomainError("not_found", "Unknown node job", 404)
        return {**job, "result": await service.store.read("node_result", job["result_id"]) if job.get("result_id") else None}

    @router.get("/api/v1/executor_artifacts/{artifact_id}")
    async def owner_artifact(artifact_id: str, download: bool = False):
        artifact = await service.store.read("node_artifact", artifact_id)
        if not artifact or artifact.get("state") != "complete":
            raise DomainError("not_found", "Unknown complete node artifact", 404)
        if not download:
            return artifact
        path = service.artifacts.object_path(artifact["digest"])
        if file_digest(path) != artifact["digest"]:
            raise DomainError("corrupt_evidence", "Node artifact content does not match its frozen digest")
        return FileResponse(path, media_type="application/octet-stream", filename="node-artifact.bin")

    @router.post("/api/v1/executor_jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, request: Request):
        payload = await body(request)
        return await service.cancel_job(job_id, payload.get("reason", "Owner cancellation"), command_key(request))

    return router


def create_executor_app(service, *, max_body_bytes: int = 4 * 1024 * 1024) -> FastAPI:
    app = FastAPI(title="AgentFlow authenticated node ingress", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.exception_handler(DomainError)
    async def domain_error(_request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.exception_handler(ValidationError)
    async def validation_error(_request, _exc):
        return JSONResponse({"error": {"code": "invalid_request", "message": "Node request failed validation"}}, status_code=422)

    @app.middleware("http")
    async def execution_boundary(request, call_next):
        try:
            if request.headers.get("host") != urlsplit(service.executor_origin).netloc:
                raise DomainError("invalid_host", "Execution authority does not match", 403)
            if request.headers.get("origin") is not None:
                raise DomainError("browser_not_allowed", "Node credentials cannot be used as browser authority", 403)
            if not request.url.path.startswith("/executor/v1/"):
                raise DomainError("not_found", "Only node execution routes exist on this listener", 404)
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                command_key(request)
                content = bytearray()
                async for chunk in request.stream():
                    content.extend(chunk)
                    if len(content) > max_body_bytes:
                        raise DomainError("body_too_large", "Node request exceeds transfer limit", 413)
                request._body = bytes(content)
            pairing = request.url.path.startswith("/executor/v1/pairings/") and request.url.path.endswith("/redeem")
            if not pairing:
                peer = request.scope.get("extensions", {}).get("agentflow.peer_certificate_der")
                request.state.node_identity = await service.authenticate_peer_certificate(peer)
            response = await call_next(request)
        except DomainError as exc:
            response = JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.post("/executor/v1/pairings/{pairing_id}/redeem")
    async def redeem(pairing_id: str, request: Request):
        return await service.redeem_pairing(pairing_id, await body(request), command_key(request))

    @app.post("/executor/v1/nodes/{node_id}/heartbeat")
    async def node_heartbeat(node_id: str, request: Request):
        identity = request.state.node_identity
        if identity.node_id != node_id:
            raise DomainError("node_identity_mismatch", "Certificate belongs to another node", 403)
        return await service.heartbeat(identity, await body(request), command_key(request))

    @app.post("/executor/v1/nodes/{node_id}/capabilities")
    async def capability_report(node_id: str, request: Request):
        identity = request.state.node_identity
        if identity.node_id != node_id:
            raise DomainError("node_identity_mismatch", "Certificate belongs to another node", 403)
        return await service.submit_capability_report(identity, await body(request), command_key(request))

    @app.post("/executor/v1/nodes/{node_id}/cleanup")
    async def cleanup(node_id: str, request: Request):
        identity = request.state.node_identity
        if identity.node_id != node_id:
            raise DomainError("node_identity_mismatch", "Certificate belongs to another node", 403)
        data = await body(request)
        if set(data) != {"payload", "signature"} or not isinstance(data["payload"], dict):
            raise DomainError("invalid_cleanup", "A signed cleanup payload is required", 422)
        return await service.submit_cleanup_receipt(identity, data["payload"], data["signature"], command_key(request))

    @app.post("/executor/v1/jobs/claim")
    async def claim(request: Request):
        return await service.claim_job(request.state.node_identity, await body(request), command_key(request))

    @app.get("/executor/v1/jobs/{job_id}")
    async def job(job_id: str, request: Request):
        return await service.get_job(request.state.node_identity, job_id)

    @app.post("/executor/v1/jobs/{job_id}/heartbeat")
    async def heartbeat(job_id: str, request: Request):
        return await service.renew_job(request.state.node_identity, job_id, await body(request), bearer(request), command_key(request))

    @app.post("/executor/v1/jobs/{job_id}/results")
    async def result(job_id: str, request: Request):
        return await service.submit_job_result(request.state.node_identity, job_id, await body(request), bearer(request), command_key(request))

    @app.post("/executor/v1/jobs/{job_id}/uploads")
    async def begin(job_id: str, request: Request):
        return await service.begin_upload(request.state.node_identity, job_id, bearer(request), await body(request), command_key(request))

    async def upload_context(upload_id: str, request: Request):
        upload = await service.store.read("node_upload", upload_id)
        if upload is None:
            raise DomainError("not_found", "Unknown upload", 404)
        await service.get_job(request.state.node_identity, upload["job_id"])
        return upload

    @app.get("/executor/v1/uploads/{upload_id}")
    async def upload_status(upload_id: str, request: Request):
        upload = await upload_context(upload_id, request)
        await service._authorized_job(request.state.node_identity, upload["job_id"], bearer(request))
        return {k: v for k, v in upload.items() if k not in {"path", "temporary_path"}}

    @app.put("/executor/v1/uploads/{upload_id}")
    async def append(upload_id: str, request: Request):
        upload = await upload_context(upload_id, request)
        try:
            offset = int(request.headers["upload-offset"])
        except (KeyError, ValueError):
            raise DomainError("invalid_offset", "Upload-Offset is required", 422) from None
        return await service.append_chunk(request.state.node_identity, upload["job_id"], upload_id,
            bearer(request), offset, await request.body(), request.headers.get("x-chunk-digest", ""), command_key(request))

    @app.post("/executor/v1/uploads/{upload_id}/complete")
    async def complete(upload_id: str, request: Request):
        upload = await upload_context(upload_id, request)
        return await service.complete_upload(request.state.node_identity, upload["job_id"], upload_id,
                                               bearer(request), command_key(request))

    @app.get("/executor/v1/jobs/{job_id}/artifacts/{artifact_id}")
    async def download(job_id: str, artifact_id: str, request: Request, offset: int = 0, size: int = 1024 * 1024):
        await service._authorized_job(request.state.node_identity, job_id, bearer(request))
        value = await service.read_input_chunk(request.state.node_identity, job_id, artifact_id, offset, size)
        return Response(value["data"], media_type="application/octet-stream", headers={
            "Upload-Offset": str(value["offset"]), "X-Artifact-Size": str(value["total_size"]),
            "X-Artifact-Digest": value["digest"], "X-Chunk-Digest": value["chunk_digest"]})

    return app

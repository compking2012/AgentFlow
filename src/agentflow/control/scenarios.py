"""Bounded API → Android UI → GTK UI → Web scenario coordination.

This coordinator consumes a preconfigured frozen reference backend. It neither
deploys that backend nor substitutes API assignment writes for native controls.
An ambiguous ticket-creation outcome is never retried with another POST.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from agentflow.common import DomainError, canonical_digest, canonical_json, utc_now
from agentflow.execution.manifests import (
    MatrixPlan,
    PlatformManifest,
    SourceManifest,
    execution_key,
    file_digest,
    target_artifacts,
)
from agentflow.execution.models import TargetConfig
from agentflow.testing.adapters import BuildRecipe


def _origin(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
            or parts.path not in {"", "/"} or parts.query or parts.fragment):
        raise ValueError("A credential-free local/LAN HTTP(S) origin is required")
    try:
        address = ipaddress.ip_address(parts.hostname)
        networks = [ipaddress.ip_network(value) for value in ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"]]
        allowed = address.is_loopback or any(address.version == network.version and address in network for network in networks)
    except ValueError:
        allowed = parts.hostname == "localhost"
    if not allowed:
        raise ValueError("Use localhost or an explicit private IP for the preconfigured backend")
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise ValueError("Invalid backend port")
    return value.rstrip("/")


class ScenarioBackend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str
    target_config_id: str
    credential_ref: str = Field(pattern=r"^reference-app:[A-Za-z0-9_.-]{1,80}$")
    preconfigured: bool = False

    _validate_origin = field_validator("origin")(_origin)


class ReferenceApplicationCredential(BaseModel):
    """Only the public test application's manager credential is accepted in v1.

    Explicit audience and origin prevent using an owner/model credential resolver
    by accident. General application authentication is outside this fixture scope.
    """
    model_config = ConfigDict(extra="forbid")
    audience: Literal["agentflow-reference-application"]
    origin: str
    bearer_token: SecretStr

    _validate_origin = field_validator("origin")(_origin)

    @model_validator(mode="after")
    def reference_only(self):
        if self.bearer_token.get_secret_value() != "reference.manager":
            raise ValueError("Only the public reference application's manager credential is permitted")
        return self


class CrossScenarioStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: Literal["android_assign", "linux_assign", "web_verify"]
    target_config_id: str
    capability_id: str | None = None
    matrix_entry_id: str
    recipe: BuildRecipe


class CrossScenarioDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    scenario_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    kind: Literal["api_android_linux_web_assignment"] = "api_android_linux_web_assignment"
    backend: ScenarioBackend
    steps: list[CrossScenarioStep] = Field(min_length=3, max_length=3)
    max_active_seconds: int = Field(default=1800, ge=30, le=7200)

    @model_validator(mode="after")
    def ordered_native_steps(self):
        if [s.step_id for s in self.steps] != ["android_assign", "linux_assign", "web_verify"]:
            raise ValueError("The scenario requires Android, then Linux, then Web")
        if len({s.matrix_entry_id for s in self.steps}) != 3:
            raise ValueError("Each scenario step needs a distinct frozen matrix entry")
        for step, target in zip(self.steps, ["android_native", "linux_native", "web"], strict=True):
            if step.recipe.adapter.value != target or step.recipe.test_kind not in {"gui", "integration"}:
                raise ValueError("Native UI steps cannot be replaced by API or unit-test recipes")
            if not step.recipe.expected_case_ids or step.recipe.scenario_inputs:
                raise ValueError("Freeze explicit case IDs; scene inputs are assigned by the coordinator")
        return self


class CrossScenarioCoordinator:
    def __init__(self, store, nodes, http_client=None, credential_resolver=None):
        self.store, self.nodes = store, nodes
        self.http_client, self.credential_resolver = http_client, credential_resolver
        self._locks: dict[str, asyncio.Lock] = {}

    async def start(self, run, candidate, definition, key, *, parent_work_item_id=None):
        definition = CrossScenarioDefinition.model_validate(definition)
        payload = {"run_id": run["id"], "candidate_id": candidate["id"],
            "candidate_fingerprint": candidate["fingerprint"], "definition": definition.model_dump(mode="json"),
            "parent_work_item_id": parent_work_item_id}
        identity = str(uuid5(NAMESPACE_URL, f"cross-scenario:{run['id']}:{candidate['fingerprint']}:{definition.scenario_id}:{key}"))
        def create(tx):
            current = tx.get("run", run["id"])
            actual = tx.get("candidate", candidate["id"])
            if (not current or not actual or current["input_fingerprint"] != run["input_fingerprint"]
                    or actual["fingerprint"] != candidate["fingerprint"] or actual["run_id"] != run["id"]):
                raise DomainError("stale_scenario", "Scenario inputs changed before creation")
            parent = tx.get("work_item", parent_work_item_id) if parent_work_item_id else None
            if parent_work_item_id and (not parent or parent["run_id"] != run["id"]):
                raise DomainError("scenario_parent_missing", "Scenario parent must belong to its run")
            namespace = "scene-" + identity.replace("-", "")
            row = tx.put("cross_scenario", identity, {**payload, "run_input_fingerprint": run["input_fingerprint"],
                "parent_generation": parent["generation"] if parent else None,
                "parent_input_fingerprint": parent["input_fingerprint"] if parent else None,
                "definition_fingerprint": canonical_digest(payload["definition"]),
                "status": "created", "terminal": False, "current_step": 0, "job_ids": [], "step_evidence": [],
                "check_ids": [], "blocking_reason": None, "blocking_code": None, "selected_capabilities": {},
                "frame_context": {"scene_id": identity, "namespace": namespace, "ticket_title": namespace,
                    "ticket_id": None, "candidate_fingerprint": candidate["fingerprint"]},
                "created_at": utc_now(), "deadline_at": (datetime.now(UTC) + timedelta(seconds=definition.max_active_seconds)).isoformat()})
            tx.event("scenario.created", {"scene_id": identity, "scenario_id": definition.scenario_id}, run_id=run["id"])
            return row
        scene = await self.store.command("cross_scenario.start", key, payload, create)
        return await self.advance(scene["id"])

    async def _change(self, scene, changes, action):
        def update(tx):
            current = tx.get("cross_scenario", scene["id"])
            if current["revision"] != scene["revision"]:
                raise DomainError("scenario_revision_conflict", "Another coordinator advanced this scene")
            if action in {"create_intent", "dispatch_intent"}:
                run = tx.get("run", scene["run_id"])
                candidate = tx.get("candidate", scene["candidate_id"])
                if (run["execution_state"] != "running" or run["input_fingerprint"] != scene["run_input_fingerprint"]
                        or candidate["fingerprint"] != scene["candidate_fingerprint"]):
                    raise DomainError("stale_scenario", "Run changed before authorizing a scene action")
            row = tx.put("cross_scenario", scene["id"], {**current, **changes}, current["revision"])
            tx.event("scenario." + action, {"scene_id": scene["id"], "status": row["status"],
                "step": row["current_step"]}, run_id=scene["run_id"])
            return row
        return await self.store.command("cross_scenario.transition", f"{scene['id']}:{scene['revision']}:{action}:{uuid4()}", changes, update)

    async def _block(self, scene, code, message, *, terminal=False, status="blocked"):
        return await self._change(scene, {"status": status, "terminal": terminal,
            "blocking_code": code, "blocking_reason": message}, status)

    async def _credential(self, backend):
        if self.credential_resolver is None:
            raise DomainError("application_credential_missing", "Configure a dedicated reference-application credential resolver")
        try:
            value = self.credential_resolver(backend.credential_ref, backend.origin)
            if inspect.isawaitable(value):
                value = await value
            credential = ReferenceApplicationCredential.model_validate(value)
            if credential.origin != backend.origin:
                raise ValueError("credential origin mismatch")
            return credential.bearer_token.get_secret_value()
        except Exception as exc:
            raise DomainError("application_credential_invalid", "Application credential audience or origin is not valid") from exc

    async def _http(self, method, backend, path, *, token=None, body=None):
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        # Construct directly: do not inherit client cookies, default Authorization,
        # model auth, or owner credentials from an injected HTTP client's defaults.
        request = httpx.Request(method, backend.origin + path, headers=headers, json=body,
            extensions={"timeout": {"connect": 5, "read": 10, "write": 10, "pool": 5}})
        async def send(client):
            async with asyncio.timeout(15):
                response = await client.send(request, auth=None, follow_redirects=False, stream=True)
                try:
                    raw = bytearray()
                    async for block in response.aiter_bytes():
                        raw.extend(block)
                        if len(raw) > 65536:
                            raise DomainError("scenario_response_limit", "Reference response exceeds its bounded size")
                    if not raw:
                        return response.status_code, {}
                    try:
                        value = json.loads(raw)
                    except ValueError as exc:
                        raise DomainError("scenario_invalid_response", "Reference service did not return JSON") from exc
                    if not isinstance(value, dict):
                        raise DomainError("scenario_invalid_response", "Reference service response must be an object")
                    return response.status_code, value
                finally:
                    await response.aclose()
        if self.http_client is not None:
            return await send(self.http_client)
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            return await send(client)

    async def _validate_inputs(self, scene):
        run = await self.store.read("run", scene["run_id"])
        candidate = await self.store.read("candidate", scene["candidate_id"])
        if (not run or not candidate or run["input_fingerprint"] != scene["run_input_fingerprint"]
                or candidate["run_input_fingerprint"] != run["input_fingerprint"]
                or candidate["fingerprint"] != scene["candidate_fingerprint"]):
            raise DomainError("stale_scenario", "Run or frozen candidate changed")
        if scene["parent_work_item_id"]:
            parent = await self.store.read("work_item", scene["parent_work_item_id"])
            if (not parent or parent["generation"] != scene["parent_generation"]
                    or parent["input_fingerprint"] != scene["parent_input_fingerprint"]):
                raise DomainError("stale_scenario", "Parent work generation changed")
            if parent["status"] in {"cancel_requested", "cancelled"}:
                return run, candidate, "cancel"
        if run["execution_state"] in {"cancelling", "cancelled"}:
            return run, candidate, "cancel"
        if run["execution_state"] == "paused":
            return run, candidate, "pause"
        if run["execution_state"] not in {"running"}:
            raise DomainError("scenario_run_inactive", "Scenario requires an active run")
        return run, candidate, "continue"

    def _frozen(self, candidate, definition):
        source = SourceManifest.model_validate({k: v for k, v in candidate["source_manifest"].items() if k != "fingerprint"})
        platform = PlatformManifest.model_validate({k: v for k, v in (candidate.get("platform_manifest") or {}).items() if k != "fingerprint"})
        matrix = MatrixPlan.model_validate(candidate["matrix_plan"])
        if (source.fingerprint != candidate["source_manifest"]["fingerprint"] or platform.fingerprint != candidate["fingerprint"]
                or platform.source_manifest.fingerprint != source.fingerprint or platform.target_matrix_fingerprint != matrix.fingerprint
                or source.target_matrix_fingerprint != matrix.fingerprint):
            raise DomainError("scenario_manifest_mismatch", "Source, platform and matrix identities must agree")
        configs = {config.target_config_id: config for config in matrix.target_configs}
        backend_config = configs.get(definition.backend.target_config_id)
        if not backend_config or backend_config.app_target.value != "api":
            raise DomainError("scenario_backend_missing", "Backend must name a frozen API target configuration")
        products = [a for a in target_artifacts(candidate["platform_manifest"], backend_config) if a["kind"] in {"product", "service"}]
        if len(products) != 1:
            raise DomainError("scenario_backend_ambiguous", "Exactly one frozen API product is required")
        product = products[0]
        digest = product.get("metadata", {}).get("content_digest")
        if not digest or not product.get("verified_upload"):
            raise DomainError("scenario_backend_unverified", "API product must have verified unpacked content identity")
        for step in definition.steps:
            config = configs.get(step.target_config_id)
            mapping = candidate["matrix_mappings"].get(step.matrix_entry_id)
            entry = next((e for e in matrix.entries if e.matrix_entry_id == step.matrix_entry_id), None)
            if (not config or config.app_target != step.recipe.adapter or not mapping or not entry or not entry.required
                    or entry.target_config_id != config.target_config_id or mapping["target_config_id"] != config.target_config_id
                    or mapping["test_case_id"] != entry.test_case_id
                    or set(mapping["framework_case_ids"]) != set(step.recipe.expected_case_ids)):
                raise DomainError("scenario_case_scope_mismatch", "Scenario case mapping must exactly match its frozen required matrix entry")
            artifacts = target_artifacts(candidate["platform_manifest"], config)
            if not {"product", "test"}.issubset({a["kind"] for a in artifacts}) or any(not a["verified_upload"] for a in artifacts):
                raise DomainError("scenario_products_missing", "Every scene client needs verified frozen product and test packages")
        return source, matrix, configs, product

    async def _declared(self, candidate, definition):
        source = candidate["source_manifest"]
        artifact = await self.store.read("node_artifact", source["build_plan_artifact_version_id"])
        if not artifact or artifact["digest"] != source["build_plan_digest"]:
            raise DomainError("scenario_definition_unbound", "Scene definition needs its exact frozen build-plan artifact")
        path = self.nodes.artifacts.object_path(artifact["digest"])
        if file_digest(path) != artifact["digest"] or path.stat().st_size > 1024 * 1024:
            raise DomainError("scenario_definition_unbound", "Frozen scene plan is absent, corrupt or too large")
        document = json.loads(path.read_bytes())
        matches = [value for value in document.get("cross_scenarios", []) if value.get("scenario_id") == definition.scenario_id]
        if len(matches) != 1 or CrossScenarioDefinition.model_validate(matches[0]) != definition:
            raise DomainError("scenario_definition_unbound", "Scene recipe and order differ from the source-frozen definition")

    async def _capability(self, step: CrossScenarioStep, config: TargetConfig):
        for cap in await self.store.list("node_capability"):
            if (step.capability_id and cap["id"] != step.capability_id) or cap.get("app_target") != config.app_target.value:
                continue
            node = await self.store.read("node", cap["node_id"])
            proof = await self.store.read("node_result", cap.get("functional_result_id") or "missing")
            if (cap.get("verification_state") != "functional_verified" or not node or node.get("state") != "online"
                    or config.app_target.value not in node.get("allowed_app_targets", [])
                    or cap.get("report", {}).get("target_config_fingerprint") != config.fingerprint
                    or cap.get("report", {}).get("boot_fingerprint") != node.get("boot_fingerprint")
                    or not proof or proof.get("assessment_state") != "validated" or not proof.get("verified_checks")):
                continue
            ready = True
            for identity in config.required_resource_ids:
                resource = await self.store.read("node_resource", identity)
                if not resource or resource.get("node_id") != node["id"] or resource.get("state") != "available":
                    ready = False
            if ready:
                return cap["id"]
        raise DomainError("scenario_capability_missing", f"No matching verified available node/resources for {step.step_id}")

    async def _identity(self, backend, source, product):
        status, observed = await self._http("GET", backend, "/api/version")
        if (status != 200 or observed.get("schema") != "tickets-v1" or observed.get("source") != source.fingerprint
                or observed.get("product_content_digest") != product["metadata"]["content_digest"]):
            raise DomainError("scenario_backend_identity_mismatch", "Preconfigured backend does not match the frozen source and API product")
        return observed

    async def _ticket(self, scene, backend, token, expected_assignee):
        frame = scene["frame_context"]
        status, ticket = await self._http("GET", backend, "/api/tickets/" + frame["ticket_id"], token=token)
        if (status != 200 or str(ticket.get("id")) != frame["ticket_id"] or ticket.get("title") != frame["ticket_title"]
                or ticket.get("assignee") != expected_assignee):
            raise DomainError("scenario_ticket_mismatch", "The same ticket does not contain the expected preceding native state")
        return ticket

    async def _cancel(self, scene, reason, final_status="cancelled"):
        reason = scene.get("cancel_reason") or reason
        known = [j["id"] for j in await self.store.list("node_job")
                 if j.get("run_id") == scene["run_id"] and (j.get("recipe") or {}).get("scenario_inputs", {}).get("scene_id") == scene["id"]]
        identities = sorted(set(scene["job_ids"]) | set(known))
        for identity in identities:
            await self.nodes.cancel_job(identity, reason, f"scenario-cancel:{scene['id']}:{identity}")
        jobs = [await self.store.read("node_job", identity) for identity in identities]
        pending = any(j["state"] not in {"completed", "failed", "cancelled"} for j in jobs)
        return await self._change(scene, {"status": "cancelling" if pending else final_status,
            "terminal": not pending, "blocking_reason": reason, "cancel_reason": reason,
            "cancel_final_status": final_status, "cancelled_job_ids": identities}, "cancel")

    def _recipe(self, scene, definition, index):
        step = definition.steps[index]
        frame = scene["frame_context"]
        return BuildRecipe.model_validate({**step.recipe.model_dump(mode="json"),
            "service_urls": {"api": definition.backend.origin},
            "service_target_config_ids": {"api": definition.backend.target_config_id},
            "scenario_inputs": {"scene_id": scene["id"], "namespace": frame["namespace"],
                "ticket_id": frame["ticket_id"], "ticket_title": frame["ticket_title"],
                "expected_assignee": "member" if index == 0 else "manager"}})

    async def advance(self, scene_id):
        async with self._locks.setdefault(scene_id, asyncio.Lock()):
            scene = await self.store.read("cross_scenario", scene_id)
            if scene is None:
                raise DomainError("scenario_missing", "Unknown cross-client scenario", 404)
            if scene["terminal"]:
                run = await self.store.read("run", scene["run_id"])
                if run and run["execution_state"] in {"cancelling", "cancelled"} and scene["status"] == "execution_unknown" and scene["job_ids"]:
                    return await self._cancel(scene, "Parent cancelled an uncertain native execution")
                return scene
            try:
                _, candidate, action = await self._validate_inputs(scene)
            except DomainError as exc:
                if scene["job_ids"]:
                    return await self._cancel(scene, exc.message, "blocked")
                return await self._block(scene, exc.code, exc.message, terminal=True)
            if action == "cancel" or scene["status"] == "cancelling":
                return await self._cancel(scene, "Parent cancelled or scenario stopped", scene.get("cancel_final_status", "cancelled"))
            if action == "pause":
                return scene
            if datetime.now(UTC) > datetime.fromisoformat(scene["deadline_at"]):
                return await self._cancel(scene, "Scenario execution deadline exceeded", "failed")
            if scene["status"] == "creating":
                if datetime.now(UTC) > datetime.fromisoformat(scene["create_intent"]["deadline_at"]):
                    return await self._block(scene, "scenario_create_unknown", "Ticket creation outcome is unknown; reconcile the intent before any new scenario", terminal=True, status="execution_unknown")
                return scene
            definition = CrossScenarioDefinition.model_validate(scene["definition"])
            try:
                if not self.nodes or not definition.backend.preconfigured:
                    raise DomainError("scenario_backend_not_preconfigured", "Preconfigure the frozen backend lifecycle and node ingress before starting this scenario")
                source, matrix, configs, product = self._frozen(candidate, definition)
                await self._declared(candidate, definition)
                token = await self._credential(definition.backend)
                if not scene["frame_context"]["ticket_id"]:
                    selected = {step.step_id: await self._capability(step, configs[step.target_config_id]) for step in definition.steps}
                    observed = await self._identity(definition.backend, source, product)
                    frame = {**scene["frame_context"], "source_manifest_fingerprint": source.fingerprint,
                        "backend_origin": definition.backend.origin, "backend_target_config_id": definition.backend.target_config_id,
                        "backend_product_content_digest": product["metadata"]["content_digest"], "backend_product_artifact_id": product["artifact_id"]}
                    scene = await self._change(scene, {"status": "creating", "selected_capabilities": selected,
                        "frame_context": frame, "create_intent": {"method": "POST", "path": "/api/tickets",
                            "namespace": frame["namespace"], "body": {"title": frame["ticket_title"]},
                            "backend_identity": observed, "started_at": utc_now(),
                            "deadline_at": (datetime.now(UTC) + timedelta(seconds=20)).isoformat()}}, "create_intent")
                    try:
                        status, ticket = await self._http("POST", definition.backend, "/api/tickets", token=token,
                                                        body={"title": frame["ticket_title"]})
                    except (httpx.HTTPError, TimeoutError, DomainError):
                        return await self._block(scene, "scenario_create_unknown", "Ticket POST may have taken effect; do not retry it blindly", terminal=True, status="execution_unknown")
                    if status >= 500 or status == 408:
                        return await self._block(scene, "scenario_create_unknown", "Backend error leaves the ticket creation outcome unknown", terminal=True, status="execution_unknown")
                    if status != 201:
                        return await self._block(scene, "scenario_create_rejected", "Ticket creation did not return an accepted creation receipt", terminal=True, status="failed")
                    ticket_id = ticket.get("id")
                    if isinstance(ticket_id, bool) or not isinstance(ticket_id, int) or ticket_id <= 0 or ticket.get("title") != frame["ticket_title"]:
                        return await self._block(scene, "scenario_create_unknown", "Ticket creation receipt lacks the exact new identity", terminal=True, status="execution_unknown")
                    frame = {**frame, "ticket_id": str(ticket_id)}
                    scene = await self._change(scene, {"status": "ready", "frame_context": frame,
                        "frame_fingerprint": canonical_digest(frame), "blocking_code": None, "blocking_reason": None,
                        "create_receipt": ticket}, "ticket_created")
                await self._identity(definition.backend, source, product)
                index = scene["current_step"]
                if len(scene["job_ids"]) > index:
                    job = await self.store.read("node_job", scene["job_ids"][index])
                    if job["state"] == "execution_unknown":
                        return await self._block(scene, "scenario_node_unknown", "Previous native execution is unknown; no downstream step can run", terminal=True, status="execution_unknown")
                    if job["state"] in {"failed", "cancelled"}:
                        return await self._block(scene, "scenario_step_failed", "A required native/UI step failed or was cancelled", terminal=True, status="failed")
                    if job["state"] != "completed":
                        return scene
                    evidence = await self._verified(scene, candidate, definition.steps[index], job)
                    ticket = await self._ticket(scene, definition.backend, token, "member" if index == 0 else "manager")
                    scene = await self._change(scene, {"status": "ready", "current_step": index + 1,
                        "step_evidence": [*scene["step_evidence"], {**evidence, "ticket_observation": ticket}]}, "step_verified")
                    index += 1
                if index == len(definition.steps):
                    return await self._complete(scene, candidate)
                step = definition.steps[index]
                if not scene.get("pending_dispatch"):
                    await self._ticket(scene, definition.backend, token, None if index == 0 else "member" if index == 1 else "manager")
                    cap = await self._capability(step, configs[step.target_config_id])
                    scene = await self._change(scene, {"status": "dispatching", "pending_dispatch": {
                        "step_id": step.step_id, "capability_id": cap}}, "dispatch_intent")
                cap = scene["pending_dispatch"]["capability_id"]
                recipe = self._recipe(scene, definition, index)
                mapping = candidate["matrix_mappings"][step.matrix_entry_id]
                job = await self.nodes.enqueue_job(scene["run_id"], kind="test", target_config=configs[step.target_config_id],
                    source_manifest=candidate["source_manifest"], platform_manifest=candidate["platform_manifest"], recipe=recipe,
                    capability_id=cap, matrix_entry_ids=[step.matrix_entry_id], matrix_entries=[mapping],
                    matrix_plan_fingerprint=matrix.fingerprint, matrix_binding_fingerprint=candidate["matrix_binding"]["binding_fingerprint"],
                    parent_work_item_id=scene["parent_work_item_id"], required_resource_ids=configs[step.target_config_id].required_resource_ids,
                    idempotency_key=f"scenario:{scene['id']}:{step.step_id}")
                return await self._change(scene, {"status": "waiting_node", "job_ids": [*scene["job_ids"], job["id"]],
                    "selected_capabilities": {**scene["selected_capabilities"], step.step_id: cap},
                    "blocking_code": None, "blocking_reason": None, "pending_dispatch": None}, "job_queued")
            except DomainError as exc:
                current = await self.store.read("cross_scenario", scene_id)
                if exc.code == "scenario_revision_conflict":
                    return current
                # Once an API intent is durable, an error recording its receipt
                # must never reopen the POST path. The DB may already contain a
                # confirmation even when the caller received a domain error.
                if current.get("create_intent") and not current["frame_context"].get("ticket_id"):
                    return await self._block(current, "scenario_create_unknown", "Ticket creation confirmation is uncertain; do not repeat the POST", terminal=True, status="execution_unknown")
                if scene["status"] == "creating" and current["frame_context"].get("ticket_id"):
                    return current
                terminal = scene["status"] == "waiting_node" or bool(scene["step_evidence"])
                if terminal and scene["job_ids"]:
                    return await self._cancel(scene, exc.message, "blocked")
                return await self._block(scene, exc.code, exc.message, terminal=terminal)
            except (httpx.HTTPError, TimeoutError):
                return await self._block(scene, "scenario_backend_unreachable", "Preconfigured backend is unavailable; no downstream action was authorized")
            except (ValueError, KeyError):
                return await self._block(scene, "scenario_frozen_data_invalid", "Frozen scene inputs do not match their schema", terminal=True)

    async def _verified(self, scene, candidate, step, job):
        result = await self.store.read("node_result", job.get("result_id") or "missing")
        definition = CrossScenarioDefinition.model_validate(scene["definition"])
        index = next(i for i, value in enumerate(definition.steps) if value.step_id == step.step_id)
        actual_recipe = BuildRecipe.model_validate(job["recipe"])
        frame = scene["frame_context"]
        if (job["state"] != "completed" or job["quality_result"] != "passed" or not result or result.get("assessment_state") != "validated"
                or result.get("job_id") != job["id"] or job.get("run_id") != scene["run_id"]
                or not job.get("node_id") or result.get("node_id") != job["node_id"]
                or job.get("capability_id") != scene["selected_capabilities"][step.step_id]
                or job["target_config"]["target_config_id"] != step.target_config_id
                or job["platform_artifact_manifest"]["fingerprint"] != scene["candidate_fingerprint"]
                or job["source_manifest"]["fingerprint"] != frame["source_manifest_fingerprint"]
                or job.get("matrix_entry_ids") != [step.matrix_entry_id]
                or actual_recipe != self._recipe(scene, definition, index)):
            raise DomainError("scenario_result_unverified", "Native result does not identify this exact frozen scene")
        observations = result.get("request", {}).get("service_observations", [])
        if not any(o.get("service_name") == "api" and o.get("component_id") == frame["backend_product_artifact_id"]
                   and o.get("source_manifest_fingerprint") == frame["source_manifest_fingerprint"]
                   and o.get("product_content_digest") == frame["backend_product_content_digest"] for o in observations):
            raise DomainError("scenario_service_unverified", "Node result did not verify the shared frozen backend identity")
        checks = result.get("verified_checks", [])
        if len(checks) != 1 or checks[0].get("matrix_entry_id") != step.matrix_entry_id:
            raise DomainError("scenario_check_missing", "Scene step requires its exact verified framework check")
        check = checks[0]
        expected_format = {"android_assign": "instrumentation", "linux_assign": "junit", "web_verify": "playwright"}[step.step_id]
        normalized = check.get("normalized_report", {})
        cases = normalized.get("cases", [])
        mapping = candidate["matrix_mappings"][step.matrix_entry_id]
        key = execution_key(mapping["test_case_id"], step.target_config_id, candidate["fingerprint"])
        if (check.get("report_format") != expected_format
                or normalized.get("execution_status") != "completed" or normalized.get("quality_result") != "passed" or not cases
                or any(c.get("status") != "passed" for c in cases)
                or {c.get("case_id") for c in cases} != set(step.recipe.expected_case_ids)
                or check.get("platform_artifact_manifest_fingerprint") != candidate["fingerprint"]
                or check.get("target_config_id") != step.target_config_id
                or check.get("target_config_revision") != job["target_config"]["revision"]
                or check.get("matrix_binding_fingerprint") != candidate["matrix_binding"]["binding_fingerprint"]
                or check.get("expected_execution_keys") != [key] or check.get("actual_execution_keys") != [key]):
            raise DomainError("scenario_check_mismatch", "Raw native cases or candidate bindings do not match the required scene")
        artifact = await self.store.read("node_artifact", check.get("raw_report_artifact_version_id") or "missing")
        if not artifact or file_digest(self.nodes.artifacts.object_path(artifact["digest"])) != artifact["digest"]:
            raise DomainError("scenario_report_missing", "Original scene report is missing or corrupt")
        return {"step_id": step.step_id, "job_id": job["id"], "node_result_id": result["id"],
            "matrix_entry_id": step.matrix_entry_id, "execution_key": key, "executed_case_count": len(cases),
            "raw_report_artifact_id": artifact["id"], "raw_report_digest": artifact["digest"]}

    async def _complete(self, scene, candidate):
        # Re-read all earlier reports at the final boundary: a partial scene never
        # publishes passing check rows, and a removed report cannot be forgotten.
        definition = CrossScenarioDefinition.model_validate(scene["definition"])
        observations = []
        for step, identity in zip(definition.steps, scene["job_ids"], strict=True):
            job = await self.store.read("node_job", identity)
            verified = await self._verified(scene, candidate, step, job)
            observations.extend([("node_job", job), ("node_result", await self.store.read("node_result", job["result_id"])),
                ("node_artifact", await self.store.read("node_artifact", verified["raw_report_artifact_id"]))])
        report = await self.nodes.import_input(canonical_json({"frame_context": scene["frame_context"],
            "frame_fingerprint": scene["frame_fingerprint"], "steps": scene["step_evidence"],
            "backend_lifecycle": "externally_preconfigured"}).encode(), "cross-scenario.json", scene["run_id"],
            idempotency_key="scene-report:" + scene["id"])
        def complete(tx):
            current = tx.get("cross_scenario", scene["id"])
            run = tx.get("run", scene["run_id"])
            actual = tx.get("candidate", scene["candidate_id"])
            if (current["revision"] != scene["revision"] or run["execution_state"] != "running"
                    or run["input_fingerprint"] != scene["run_input_fingerprint"] or actual["fingerprint"] != scene["candidate_fingerprint"]):
                raise DomainError("stale_scenario", "Scene changed before publishing checks")
            for kind, observed in observations:
                now = tx.get(kind, observed["id"])
                if not now or now["revision"] != observed["revision"]:
                    raise DomainError("stale_scenario", "Scene evidence changed before publishing checks")
            if scene["parent_work_item_id"]:
                parent = tx.get("work_item", scene["parent_work_item_id"])
                if (parent["generation"] != scene["parent_generation"] or parent["input_fingerprint"] != scene["parent_input_fingerprint"]
                        or parent["status"] in {"cancel_requested", "cancelled"}):
                    raise DomainError("stale_scenario", "Parent changed before publishing checks")
            identities = []
            for evidence in scene["step_evidence"]:
                identity = str(uuid5(NAMESPACE_URL, f"scene-check:{scene['id']}:{evidence['matrix_entry_id']}"))
                tx.put("check", identity, {**evidence, "run_id": scene["run_id"], "work_item_id": scene["parent_work_item_id"],
                    "scenario_id": scene["id"], "candidate_fingerprint": scene["candidate_fingerprint"],
                    "frame_fingerprint": scene["frame_fingerprint"], "scenario_report_artifact_id": report["id"],
                    "execution_status": "completed", "quality_result": "passed", "evidence_verified": True,
                    "assertion_count": None, "framework_case_evidence": True})
                identities.append(identity)
            row = tx.put("cross_scenario", scene["id"], {**current, "status": "completed", "terminal": True,
                "quality_result": "passed", "check_ids": identities, "report_artifact_id": report["id"],
                "completed_at": utc_now()}, current["revision"])
            tx.event("scenario.completed", {"scene_id": scene["id"], "check_ids": identities}, run_id=scene["run_id"])
            return row
        return await self.store.command("cross_scenario.complete", scene["id"], {"report": report["digest"],
            "frame": scene["frame_fingerprint"]}, complete)

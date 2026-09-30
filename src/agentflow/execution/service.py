"""Controller-side node commands. FastAPI routes live in the root application."""
from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.execution.manifests import execution_key, file_digest, service_components, target_artifacts
from agentflow.execution.models import (
    CapabilityReport,
    JobClaimRequest,
    JobHeartbeatRequest,
    JobKind,
    JobLimits,
    JobResultRequest,
    NodeIdentity,
    PairingCreateRequest,
    PairingRedeemRequest,
    TargetConfig,
    new_id,
)
from agentflow.execution.pki import (
    NodeCertificateAuthority,
    fingerprint,
    public_key_fingerprint,
    verify_receipt,
)
from agentflow.execution.transport import ArtifactTransport
from agentflow.testing.reports import (
    parse_instrumentation,
    parse_junit,
    parse_playwright,
    parse_xcresult_export,
)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _future(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _fresh_capability_observation(value: str | None) -> bool:
    try:
        age = datetime.now(UTC) - _time(value)
    except (AttributeError, TypeError, ValueError):
        return False
    return timedelta(minutes=-2) <= age < timedelta(hours=1)


def _wire(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _manifest(value: dict | None) -> dict | None:
    if value is None:
        return None
    value = _wire(value)
    if value.get("state") != "frozen":
        raise DomainError("manifest_not_frozen", "Node input must be an immutable frozen manifest")
    body = {k: v for k, v in value.items() if k not in {"fingerprint", "id"}}
    digest = canonical_digest(body)
    if value.get("fingerprint") and value["fingerprint"] != digest:
        raise DomainError("manifest_fingerprint_mismatch", "Manifest contents do not match its identity")
    return {**body, "fingerprint": digest}


class NodeService:
    def __init__(self, store, data_dir: Path, executor_origin: str = "https://localhost:8443",
                 *, lease_seconds: int = 60):
        origin = urlsplit(executor_origin)
        if origin.scheme != "https" or not origin.hostname or origin.username or origin.query or origin.fragment:
            raise ValueError("execution origin must be a credential-free HTTPS origin")
        self.store = store
        self.data_dir = Path(data_dir)
        self.executor_origin = executor_origin.rstrip("/")
        self.pki = NodeCertificateAuthority(self.data_dir / "nodes" / "pki", origin.hostname)
        self.artifacts = ArtifactTransport(store, self.data_dir / "nodes" / "artifacts")
        self.lease_seconds = max(10, min(lease_seconds, 300))

    @staticmethod
    def _require_node(tx, identity: NodeIdentity) -> dict:
        row = tx.get("node", identity.node_id)
        if not row or row["state"] == "revoked" or not hmac.compare_digest(
                row["certificate_fingerprint"], identity.certificate_fingerprint):
            raise DomainError("node_unauthorized", "Node is not paired or its certificate has been revoked", 401)
        return row

    async def authenticate_peer_certificate(self, der: bytes | None) -> NodeIdentity:
        if not der:
            raise DomainError("node_certificate_required", "A verified TLS client certificate is required", 401)
        node_id, cert = self.pki.verify_node_certificate(der)
        row = await self.store.read("node", node_id)
        digest = fingerprint(der)
        if not row or row["state"] == "revoked" or not hmac.compare_digest(row["certificate_fingerprint"], digest):
            raise DomainError("node_unauthorized", "Certificate is not active for this node", 401)
        return NodeIdentity(node_id=node_id, certificate_fingerprint=digest, certificate_serial=str(cert.serial_number))

    @staticmethod
    def _node_view(row: dict) -> dict:
        return {k: v for k, v in row.items() if k != "certificate_pem"} | {"revision": row["config_revision"]}

    async def list_nodes(self) -> list[dict]:
        caps, jobs, resources = (await self.store.list("node_capability"), await self.store.list("node_job"),
                                 await self.store.list("node_resource"))
        return [self._node_view(n) | {
            "capabilities": [c for c in caps if c["node_id"] == n["id"]],
            "active_job_ids": [j["id"] for j in jobs if j.get("node_id") == n["id"] and j["state"] in {"leased", "running", "stopping", "execution_unknown"}],
            "quarantined_resource_ids": [r["id"] for r in resources if r["node_id"] == n["id"] and r["state"] == "quarantined"]}
            for n in await self.store.list("node")]

    async def import_input(self, value: Path | bytes, name: str, run_id: str | None,
                            idempotency_key: str | None = None) -> dict:
        """Trusted controller import; result is a node-downloadable immutable artifact."""
        if not name or Path(name).name != name:
            raise DomainError("unsafe_artifact_name", "Input name must be a plain file name", 422)
        temporary = None
        if isinstance(value, bytes):
            if len(value) > 512 * 1024 * 1024:
                raise DomainError("artifact_limit", "Input artifact exceeds limit", 413)
            temporary = self.artifacts.directory / "staging" / new_id()
            temporary.write_bytes(value)
            path = temporary
        else:
            path = Path(value)
        from agentflow.execution.manifests import file_digest
        digest = file_digest(path)
        from uuid import UUID
        artifact_id = str(UUID(hex=canonical_digest({"run_id": run_id, "name": name, "digest": digest,
                                                     "key": idempotency_key})[7:39]))
        try:
            return await self.artifacts.register_input(path, digest, artifact_id=artifact_id)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    async def create_pairing(self, request: PairingCreateRequest | dict, idempotency_key: str) -> dict:
        req = PairingCreateRequest.model_validate(request)
        pairing_id = new_id()

        def create(tx):
            row = tx.put("node_pairing", pairing_id, {**req.model_dump(mode="json"),
                                                       "pairing_id": pairing_id, "state": "pending",
                                                       "expires_at": _future(req.expires_in_seconds), "node_id": None,
                                                       "controller_certificate_fingerprint": self.pki.controller_fingerprint,
                                                       "executor_origin": self.executor_origin})
            tx.event("node.pairing.created", {"pairing_id": pairing_id, "node_label": req.node_label})
            return row
        row = await self.store.command("node_pairing_create", idempotency_key, req.model_dump(mode="json"), create)
        # The single-use secret is derived, never stored in the command receipt or audit event.
        return {"pairing": row, "single_use_code": self.pki.pairing_code(row["id"])}

    async def redeem_pairing(self, pairing_id: str, request: PairingRedeemRequest | dict,
                             idempotency_key: str) -> dict:
        req = PairingRedeemRequest.model_validate(request)
        if not hmac.compare_digest(req.controller_certificate_fingerprint, self.pki.controller_fingerprint):
            raise DomainError("controller_pin_mismatch", "Gateway certificate pin does not match", 403)
        if not hmac.compare_digest(req.single_use_code, self.pki.pairing_code(pairing_id)):
            raise DomainError("invalid_pairing_code", "Invalid pairing credential", 403)
        csr = self.pki.parse_csr(req.csr_pem)
        pub_digest = public_key_fingerprint(csr.public_key())
        node_id = new_id()
        cert = self.pki.issue_node(node_id, csr)
        cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
        cert_digest = fingerprint(cert.public_bytes(serialization.Encoding.DER))
        payload = {"pairing_id": pairing_id, "csr_fingerprint": fingerprint(req.csr_pem.encode()),
                   "public_key_fingerprint": pub_digest, "controller_pin": req.controller_certificate_fingerprint}

        def consume(tx):
            pair = tx.get("node_pairing", pairing_id)
            if not pair or pair["state"] != "pending" or _time(pair["expires_at"]) <= datetime.now(UTC):
                raise DomainError("pairing_unavailable", "Pairing is expired, revoked or already consumed", 403)
            if not hmac.compare_digest(pair["expected_node_public_key_fingerprint"], pub_digest):
                raise DomainError("node_key_mismatch", "CSR key differs from the owner-confirmed key", 403)
            tx.put("node", node_id, {"node_id": node_id, "label": pair["node_label"], "location": pair["location"],
                                     "config_revision": 1, "state": "paired", "certificate_pem": cert_pem,
                                     "certificate_fingerprint": cert_digest,
                                     "certificate_expires_at": cert.not_valid_after_utc.isoformat(),
                                     "allowed_app_targets": pair["allowed_app_targets"], "capabilities": [],
                                     "heartbeat_sequence": 0, "boot_fingerprint": None, "last_heartbeat_at": None,
                                     "active_job_ids": [], "quarantined_resource_ids": []})
            tx.put("node_pairing", pairing_id, {**pair, "state": "consumed", "node_id": node_id},
                   expected_revision=pair["revision"])
            tx.event("node.paired", {"node_id": node_id, "pairing_id": pairing_id})
            return {"node_id": node_id, "node_revision": 1, "node_certificate_pem": cert_pem,
                    "controller_ca_certificate_pem": self.pki.ca_pem,
                    "certificate_expires_at": cert.not_valid_after_utc.isoformat(),
                    "node_audience": "agentflow_node", "node_scopes": ["node:heartbeat", "job:claim", "job:read"]}
        return await self.store.command(f"node_pairing_redeem:{pairing_id}", idempotency_key, payload, consume)

    async def revoke_node(self, node_id: str, expected_revision: int, reason: str, idempotency_key: str) -> dict:
        def revoke(tx):
            row = tx.get("node", node_id)
            if not row:
                raise DomainError("node_missing", "Node does not exist", 404)
            if row["config_revision"] != expected_revision:
                raise DomainError("revision_conflict", "Node configuration changed")
            quarantined = []
            for candidate in tx.list("node_resource"):
                r = tx.get("node_resource", candidate["id"])
                if r and r["node_id"] == node_id and r["state"] == "leased":
                    tx.put("node_resource", r["id"], {**r, "state": "quarantined", "quarantine_reason": "node_revoked"},
                           expected_revision=r["revision"])
                    quarantined.append(r["id"])
            updated = tx.put("node", node_id, {**row, "state": "revoked", "config_revision": expected_revision + 1,
                                                "quarantined_resource_ids": quarantined}, expected_revision=row["revision"])
            tx.event("node.revoked", {"node_id": node_id, "reason": reason})
            return self._node_view(updated)
        return await self.store.command(f"node_revoke:{node_id}", idempotency_key,
                                        {"expected_revision": expected_revision, "reason": reason}, revoke)

    async def heartbeat(self, identity: NodeIdentity, request: dict, idempotency_key: str) -> dict:
        sequence = request.get("heartbeat_sequence", 0)
        if not isinstance(sequence, int) or sequence < 1:
            raise DomainError("invalid_heartbeat", "Monotonic positive heartbeat sequence required", 422)

        def update(tx):
            row = self._require_node(tx, identity)
            if request.get("expected_revision") != row["config_revision"]:
                raise DomainError("revision_conflict", "Node configuration changed")
            if sequence <= row["heartbeat_sequence"]:
                raise DomainError("heartbeat_replay", "Heartbeat sequence did not advance")
            active = request.get("active_job_ids", [])
            directives = []
            for job_id in active:
                job = tx.get("node_job", job_id)
                if not job or job.get("node_id") != identity.node_id:
                    directives.append({"job_id": job_id, "action": "quarantine", "reason": "unknown_job"})
                elif job["state"] in {"stopping", "cancelled", "execution_unknown"}:
                    directives.append({"job_id": job_id, "action": "stop", "reason": job["state"]})
                elif job.get("boot_fingerprint") != request.get("boot_fingerprint"):
                    directives.append({"job_id": job_id, "action": "quarantine", "reason": "boot_identity_changed"})
            tx.put("node", identity.node_id, {**row, "state": "online", "heartbeat_sequence": sequence,
                                              "boot_fingerprint": request.get("boot_fingerprint"),
                                              "last_heartbeat_at": utc_now(), "active_job_ids": active},
                   expected_revision=row["revision"])
            return {"node_id": identity.node_id, "revision": row["config_revision"],
                    "accepted_heartbeat_sequence": sequence, "server_time": utc_now(), "directives": directives}
        return await self.store.command(f"node_heartbeat:{identity.node_id}", idempotency_key, request, update)

    async def register_capability(self, node_id: str, report: CapabilityReport | dict,
                                   idempotency_key: str) -> dict:
        """Refresh identical observed environments; preserve, but never invent, functional proof."""
        req = CapabilityReport.model_validate(report)
        capability_id = new_id()

        def create(tx):
            node = tx.get("node", node_id)
            if not node or node["state"] == "revoked" or req.app_target not in node["allowed_app_targets"]:
                raise DomainError("target_not_allowed", "Node cannot register this target", 403)
            try:
                age = datetime.now(UTC) - _time(req.observed_at)
            except (TypeError, ValueError) as exc:
                raise DomainError("capability_time_invalid", "Static observation needs a UTC timestamp") from exc
            if age < timedelta(minutes=-2) or age >= timedelta(hours=1):
                raise DomainError("capability_observation_expired", "Submit a fresh static probe; old/future timestamps cannot renew capability")
            previous = [cap for cap in tx.list("node_capability") if cap["node_id"] == node_id
                        and cap["app_target"] == req.app_target.value
                        and cap["report"]["target_config_fingerprint"] == req.target_config_fingerprint
                        and cap["verification_state"] != "superseded"]
            reusable = [cap for cap in previous if req.state == "static_verified"
                        and cap["verification_state"] in {"static_verified", "functional_verified"}
                        and cap["environment_fingerprint"] == req.environment_fingerprint
                        and cap["report"]["boot_fingerprint"] == req.boot_fingerprint
                        and (cap["verification_state"] != "functional_verified" or self._functional_proof_intact(tx, cap))]
            existing = max(reusable, key=lambda cap: (cap["verification_state"] == "functional_verified", cap["observed_at"])) if reusable else None
            selected_id = existing["id"] if existing else capability_id
            observation_id = canonical_digest({"node_id": node_id, "probe_id": req.probe_id})[7:]
            if existing and not existing.get("last_observation_id"):
                legacy_id = canonical_digest({"node_id": node_id, "probe_id": existing["report"]["probe_id"]})[7:]
                if not tx.get("node_capability_observation", legacy_id):
                    tx.put("node_capability_observation", legacy_id, {"node_id": node_id, "capability_id": selected_id,
                        "report": existing["report"], "environment_fingerprint": existing["environment_fingerprint"],
                        "recorded_at": utc_now(), "imported_previous_observation": True})
            observed = tx.get("node_capability_observation", observation_id)
            if observed:
                if observed["capability_id"] != selected_id or observed["report"] != req.model_dump(mode="json"):
                    raise DomainError("capability_observation_conflict", "A probe identity cannot be reused with different evidence")
            else:
                tx.put("node_capability_observation", observation_id, {"node_id": node_id, "capability_id": selected_id,
                    "report": req.model_dump(mode="json"), "environment_fingerprint": req.environment_fingerprint,
                    "recorded_at": utc_now()})
            for old in previous:
                if existing and old["id"] == existing["id"]:
                    continue
                tx.put("node_capability", old["id"], {**old, "prior_verification_state": old["verification_state"],
                    "verification_state": "superseded", "superseded_by_capability_id": selected_id,
                    "superseded_at": utc_now()}, old["revision"])
            if existing:
                refreshed = tx.put("node_capability", existing["id"], {**existing, "report": req.model_dump(mode="json"),
                    "observed_at": req.observed_at, "last_observation_id": observation_id,
                    "static_refresh_count": existing.get("static_refresh_count", 0) + 1}, existing["revision"])
                tx.event("node.capability.refreshed", {"capability_id": existing["id"], "observation_id": observation_id,
                    "functional_result_id": existing.get("functional_result_id")})
                return refreshed
            created = tx.put("node_capability", capability_id, {"capability_id": capability_id, "node_id": node_id,
                       "app_target": req.app_target.value, "verification_state": req.state,
                       "environment_fingerprint": req.environment_fingerprint, "report": req.model_dump(mode="json"),
                       "permitted_job_kinds": ["capability_probe", "build", "install", "test"],
                       "observed_at": req.observed_at, "functional_result_id": None,
                       "first_observation_id": observation_id, "last_observation_id": observation_id,
                       "static_refresh_count": 0})
            tx.event("node.capability.observed", {"capability_id": capability_id, "observation_id": observation_id,
                "superseded_capability_ids": [cap["id"] for cap in previous]})
            return created
        return await self.store.command(f"node_capability:{node_id}", idempotency_key, req.model_dump(mode="json"), create)

    def _functional_proof_intact(self, tx, cap: dict, result_id: str | None = None) -> bool:
        result = tx.get("node_result", result_id or cap.get("functional_result_id")) if (result_id or cap.get("functional_result_id")) else None
        job = tx.get("node_job", result["job_id"]) if result else None
        if (not result or result.get("assessment_state") != "validated" or not job
                or job.get("node_id") != cap["node_id"] or job.get("kind") != "capability_probe"
                or job.get("capability_id") != cap["id"] or job.get("quality_result") != "passed"
                or not job.get("source_manifest") or not job.get("platform_artifact_manifest")
                or job.get("environment_fingerprint") != cap["environment_fingerprint"]
                or not result.get("verified_checks")):
            return False
        verified = set()
        for check in result["verified_checks"]:
            if check.get("quality_result") != "passed" or check.get("environment_fingerprint") != cap["environment_fingerprint"]:
                return False
            artifact_id = check.get("raw_report_artifact_version_id")
            if artifact_id in verified:
                continue
            artifact = tx.get("node_artifact", artifact_id) if artifact_id else None
            if not artifact or artifact.get("state") != "complete" or artifact.get("job_id") != job["id"]:
                return False
            try:
                if file_digest(self.artifacts.object_path(artifact["digest"])) != artifact["digest"]:
                    return False
            except (OSError, DomainError):
                return False
            verified.add(artifact_id)
        return True

    async def submit_capability_report(self, identity: NodeIdentity, report: CapabilityReport | dict,
                                       idempotency_key: str) -> dict:
        node = await self.store.read("node", identity.node_id)
        if not node or node["state"] == "revoked" or node["certificate_fingerprint"] != identity.certificate_fingerprint:
            raise DomainError("node_unauthorized", "Inactive node identity", 401)
        return await self.register_capability(identity.node_id, report, idempotency_key)

    async def confirm_functional_capability(self, capability_id: str, result_id: str,
                                            idempotency_key: str) -> dict:
        """Controller-only promotion after an actual frozen reference fixture has passed."""
        def confirm(tx):
            cap, result = tx.get("node_capability", capability_id), tx.get("node_result", result_id)
            job = tx.get("node_job", result["job_id"]) if result else None
            if (not cap or not result or not job or result["assessment_state"] != "validated"
                    or job["kind"] != "capability_probe" or job["capability_id"] != capability_id
                    or not job.get("source_manifest") or not job.get("platform_artifact_manifest")
                    or not result["verified_checks"] or job["quality_result"] != "passed"):
                raise DomainError("functional_evidence_missing", "A validated frozen fixture probe is required")
            node = tx.get("node", cap["node_id"])
            if (cap["verification_state"] not in {"static_verified", "functional_verified"}
                    or cap.get("superseded_by_capability_id") or not node or node["state"] == "revoked"
                    or cap["report"]["boot_fingerprint"] != node.get("boot_fingerprint")
                    or not _fresh_capability_observation(cap.get("observed_at"))
                    or not self._functional_proof_intact(tx, cap, result_id)):
                raise DomainError("functional_environment_stale", "A fresh unchanged environment and intact original test evidence are required")
            if job["app_target"] != "api":
                recipe = job.get("recipe") or {}
                required_format = {"web": "playwright", "ios_native": "xcresult", "macos_native": "xcresult",
                                   "android_native": "instrumentation", "windows_native": "junit", "linux_native": "junit"}[job["app_target"]]
                if (recipe.get("test_kind", "integration") == "unit"
                        or any(check.get("report_format") != required_format for check in result["verified_checks"])):
                    raise DomainError("functional_ui_evidence_missing", "Web/native capability requires its actual GUI framework execution; unit results cannot promote it")
            updated = tx.put("node_capability", capability_id,
                {**cap, "verification_state": "functional_verified", "functional_result_id": result_id,
                 "functional_verified_at": utc_now(), "functional_environment_fingerprint": cap["environment_fingerprint"]},
                expected_revision=cap["revision"])
            tx.event("node.capability.functional_verified", {"capability_id": capability_id, "result_id": result_id})
            return updated
        return await self.store.command(f"node_capability_confirm:{capability_id}", idempotency_key,
                                        {"result_id": result_id}, confirm)

    async def register_resource(self, node_id: str, kind: str, identity_fingerprint: str,
                                 idempotency_key: str, resource_id: str | None = None) -> dict:
        if kind not in {"device", "simulator", "desktop_session", "display", "port", "workspace", "test_data_namespace"}:
            raise DomainError("invalid_resource", "Unknown resource kind", 422)
        resource_id = resource_id or new_id()
        payload = {"node_id": node_id, "kind": kind, "identity_fingerprint": identity_fingerprint}

        def create(tx):
            node = tx.get("node", node_id)
            if not node or node["state"] == "revoked":
                raise DomainError("node_missing", "Active paired node required", 404)
            return tx.put("node_resource", resource_id, {**payload, "resource_id": resource_id, "state": "available",
                          "fencing_token": 0, "current_lease_id": None, "owner_job_id": None,
                          "expires_at": None, "quarantine_reason": None, "last_cleanup_receipt_id": None})
        return await self.store.command(f"node_resource:{node_id}", idempotency_key, payload, create)

    async def enqueue_job(self, run_id: str | None, *, kind: JobKind | str,
                          target_config: TargetConfig | dict, idempotency_key: str,
                          source_manifest=None, platform_manifest=None, recipe=None,
                          required_resource_ids=(), capability_id=None, matrix_entry_ids=(),
                          matrix_plan_fingerprint=None, matrix_binding_fingerprint=None,
                          parent_work_item_id=None, input_fingerprint=None, limits=None,
                          download_artifact_version_ids=(), probe_id=None, matrix_entries=()) -> dict:
        kind, target = JobKind(kind), TargetConfig.model_validate(target_config)
        resources = sorted(set(required_resource_ids) | set(target.required_resource_ids))
        source, platform = _manifest(source_manifest), _manifest(platform_manifest)
        if kind != JobKind.PROBE and not source:
            raise DomainError("source_required", "Build/install/test needs a frozen source manifest", 422)
        if kind == JobKind.BUILD and platform is not None:
            raise DomainError("cyclic_build_input", "Build must not depend on its future platform manifest", 422)
        if kind in {JobKind.INSTALL, JobKind.TEST} or (kind == JobKind.PROBE and platform):
            if not platform or not matrix_plan_fingerprint or not matrix_binding_fingerprint:
                raise DomainError("platform_manifest_required", "Formal install/test requires frozen platform and matrix binding", 422)
            if platform["source_manifest"]["fingerprint"] != source["fingerprint"]:
                raise DomainError("source_mismatch", "Product/test artifacts belong to a different source")
            if matrix_plan_fingerprint != source["target_matrix_fingerprint"] or platform["target_matrix_fingerprint"] != matrix_plan_fingerprint:
                raise DomainError("matrix_plan_mismatch", "Formal execution must use the matrix plan frozen into source and platform artifacts")
        if kind == JobKind.TEST and not matrix_entry_ids:
            raise DomainError("matrix_required", "Formal tests must name their frozen required entries", 422)
        limit = JobLimits.model_validate(limits or {})
        inputs = {"kind": kind.value, "target_config": target.model_dump(mode="json"),
                  "source_manifest": source, "platform_artifact_manifest": platform,
                  "recipe": _wire(recipe) if recipe else None, "matrix_entry_ids": list(matrix_entry_ids),
                  "matrix_plan_fingerprint": matrix_plan_fingerprint,
                  "matrix_binding_fingerprint": matrix_binding_fingerprint,
                  "matrix_entries": list(matrix_entries)}
        if kind == JobKind.TEST or (kind == JobKind.PROBE and platform):
            if {e.get("matrix_entry_id") for e in matrix_entries} != set(matrix_entry_ids):
                raise DomainError("matrix_definition_required", "Controller-frozen case definitions must match assigned entries")
            for entry in matrix_entries:
                if not entry.get("test_case_id") or not entry.get("framework_case_ids"):
                    raise DomainError("case_mapping_required", "Each assigned entry needs its frozen raw framework case mapping")
        calculated = canonical_digest(inputs)
        if input_fingerprint and input_fingerprint != calculated:
            raise DomainError("input_fingerprint_mismatch", "Supplied fingerprint does not match frozen node inputs")
        job_id, attempt_id = new_id(), new_id()
        downloads = list(download_artifact_version_ids)
        if source:
            downloads += [source.get(k) for k in ["source_bundle_artifact_version_id", "test_package_artifact_version_id",
                                                  "build_plan_artifact_version_id"] if source.get(k)]
        if platform:
            downloads += [a["artifact_version_id"] for a in target_artifacts(platform, target)]
        test_id = source.get("test_package_artifact_version_id") if source else None
        test_digest = source.get("test_package_digest") if source else None
        if platform:
            test_artifacts = [a for a in target_artifacts(platform, target) if a.get("kind") in {"test", "test_runner"}]
            if len(test_artifacts) != 1:
                raise DomainError("test_artifact_ambiguous", "One exact prebuilt test package is required per target")
            test_id, test_digest = test_artifacts[0]["artifact_version_id"], test_artifacts[0]["digest"]
        body = {**inputs, "job_id": job_id, "attempt_id": attempt_id, "run_id": run_id,
                "parent_work_item_id": parent_work_item_id, "app_target": target.app_target.value,
                "input_fingerprint": calculated, "state": "queued", "quality_result": "unknown",
                "capability_id": capability_id, "required_resource_ids": resources,
                "node_id": None, "boot_fingerprint": None, "process_fingerprint": None,
                "fencing_token": 0, "lease_revision": 0, "lease_expires_at": None, "resource_leases": [],
                "limits": limit.model_dump(mode="json"), "download_artifact_version_ids": sorted(set(downloads)),
                "created_at": utc_now(), "probe_id": probe_id, "result_id": None,
                "test_package_artifact_version_id": test_id, "test_package_digest": test_digest}

        def create(tx):
            if run_id:
                run = tx.get("run", run_id)
                if not run or run.get("execution_state") not in {"running", "paused"}:
                    raise DomainError("run_not_active", "Node work requires an active controller run")
                body["parent_run_fingerprint"] = run.get("input_fingerprint")
            if parent_work_item_id:
                item = tx.get("work_item", parent_work_item_id)
                if not item or item.get("run_id") != run_id:
                    raise DomainError("work_item_missing", "Parent work item must belong to the run")
                body["parent_generation"] = item.get("generation")
                body["parent_input_fingerprint"] = item.get("input_fingerprint")
            row = tx.put("node_job", job_id, body)
            tx.event("node.job.queued", {"job_id": job_id, "kind": kind.value}, run_id=run_id)
            return row
        return await self.store.command(f"node_job_enqueue:{run_id}", idempotency_key,
                                        {"run_id": run_id, "inputs": inputs, "resources": body["required_resource_ids"]}, create)

    async def enqueue_functional_probe(self, run_id: str, *, capability_id: str,
                                       target_config, source_manifest, platform_manifest, recipe,
                                       matrix_entries, matrix_plan_fingerprint: str,
                                       matrix_binding_fingerprint: str, idempotency_key: str,
                                       required_resource_ids=(), limits=None) -> dict:
        """Owner/controller starts a real frozen-fixture probe, not a capability flag update."""
        return await self.enqueue_job(run_id, kind=JobKind.PROBE, target_config=target_config,
            source_manifest=source_manifest, platform_manifest=platform_manifest, recipe=recipe,
            capability_id=capability_id, matrix_entries=matrix_entries,
            matrix_entry_ids=[entry["matrix_entry_id"] for entry in matrix_entries],
            matrix_plan_fingerprint=matrix_plan_fingerprint, matrix_binding_fingerprint=matrix_binding_fingerprint,
            required_resource_ids=required_resource_ids, limits=limits, probe_id=new_id(),
            idempotency_key=idempotency_key)

    @staticmethod
    def _parent_current(tx, job: dict, *, allow_paused: bool = False) -> bool:
        if job.get("run_id"):
            run = tx.get("run", job["run_id"])
            if (not run or run.get("execution_state") not in ({"running", "paused"} if allow_paused else {"running"})
                    or (not job.get("parent_work_item_id") and run.get("input_fingerprint") != job.get("parent_run_fingerprint"))):
                return False
        if job.get("parent_work_item_id"):
            item = tx.get("work_item", job["parent_work_item_id"])
            if (not item or item.get("generation") != job.get("parent_generation")
                    or item.get("input_fingerprint") != job.get("parent_input_fingerprint")
                    or item.get("status") in {"cancel_requested", "cancelled"}):
                return False
        return True

    def _assignment(self, job: dict) -> dict:
        token = self.pki.attempt_token({"node_id": job["node_id"], "job_id": job["job_id"],
                                       "attempt_id": job["attempt_id"], "fence": job["fencing_token"],
                                       "input_fingerprint": job["input_fingerprint"], "expires_at": job["lease_expires_at"]})
        return {**job, "attempt_token": token, "directive": "start_new"}

    async def claim_job(self, identity: NodeIdentity, request: JobClaimRequest | dict,
                         idempotency_key: str | None = None) -> dict:
        req = JobClaimRequest.model_validate(request)
        live_node = await self.store.read("node", identity.node_id)
        if not live_node or live_node["state"] == "revoked" or live_node["certificate_fingerprint"] != identity.certificate_fingerprint:
            raise DomainError("node_unauthorized", "Inactive node identity", 401)
        candidates = await self.store.list("node_job")
        payload = req.model_dump(mode="json")

        def claim(tx):
            node = self._require_node(tx, identity)
            if node["config_revision"] != req.node_revision:
                raise DomainError("revision_conflict", "Node configuration changed")
            if node.get("boot_fingerprint") != req.boot_fingerprint:
                raise DomainError("boot_mismatch", "Node must register its current boot heartbeat first")
            if req.resume_job_id:
                job = tx.get("node_job", req.resume_job_id)
                if not job or job["node_id"] != identity.node_id:
                    raise DomainError("job_forbidden", "Job belongs to another node", 403)
                if (job.get("process_fingerprint") != req.observed_process_fingerprint
                        or not job["lease_expires_at"] or _time(job["lease_expires_at"]) <= datetime.now(UTC)):
                    raise DomainError("execution_unknown", "Cannot verify the existing process/lease; do not restart")
                return {"assignment": {**self._assignment(job), "directive": "observe_existing"}}
            capabilities = []
            for cid in req.capability_ids:
                cap = tx.get("node_capability", cid)
                if not cap or cap["node_id"] != identity.node_id:
                    raise DomainError("capability_forbidden", "Capability does not belong to this node", 403)
                capabilities.append(cap)
            for candidate in sorted(candidates, key=lambda x: (x["created_at"], x["id"])):
                job = tx.get("node_job", candidate["id"])
                if (not job or job["state"] != "queued" or job["app_target"] not in node["allowed_app_targets"]
                        or not self._parent_current(tx, job)):
                    continue
                cap = next((c for c in capabilities if c["app_target"] == job["app_target"]
                            and (not job["capability_id"] or job["capability_id"] == c["id"])
                            and c["report"]["target_config_fingerprint"] == canonical_digest(job["target_config"])
                            and c["report"]["boot_fingerprint"] == req.boot_fingerprint
                            and c["verification_state"] in {"static_verified", "functional_verified"}
                            and _fresh_capability_observation(c.get("observed_at"))), None)
                if cap is None:
                    continue
                if job["kind"] == "test" and cap["verification_state"] != "functional_verified":
                    continue
                resources = []
                ready = True
                for rid in job["required_resource_ids"]:
                    row = tx.get("node_resource", rid)
                    if not row or row["node_id"] != identity.node_id or rid not in req.available_resource_ids or row["state"] != "available":
                        ready = False
                        break
                    resources.append(row)
                if not ready:
                    continue
                active_count = sum(j.get("node_id") == identity.node_id and j["state"] in {
                    "leased", "running", "stopping", "execution_unknown"} for j in tx.list("node_job"))
                if active_count >= node.get("maximum_active_jobs", 1):
                    return {"assignment": None}
                expires = _future(self.lease_seconds)
                leases = []
                for resource in resources:
                    lease_id, fence = new_id(), resource["fencing_token"] + 1
                    updated = tx.put("node_resource", resource["id"], {**resource, "state": "leased",
                                     "owner_job_id": job["id"], "current_lease_id": lease_id,
                                     "fencing_token": fence, "expires_at": expires}, expected_revision=resource["revision"])
                    leases.append({"lease_id": lease_id, "resource_id": resource["id"],
                                   "resource_revision": updated["revision"], "fencing_token": fence, "expires_at": expires})
                updated = tx.put("node_job", job["id"], {**job, "node_id": identity.node_id,
                                  "state": "leased", "boot_fingerprint": req.boot_fingerprint,
                                  "fencing_token": job["fencing_token"] + 1, "lease_revision": 1,
                                  "lease_expires_at": expires, "resource_leases": leases,
                                  "resource_kinds": {r["id"]: r["kind"] for r in resources},
                                  "capability_id": cap["id"], "environment_fingerprint": cap["environment_fingerprint"]},
                                 expected_revision=job["revision"])
                tx.event("node.job.claimed", {"job_id": job["id"], "node_id": identity.node_id}, run_id=job["run_id"])
                return {"assignment": self._assignment(updated)}
            return {"assignment": None}
        return await self.store.command(f"node_claim:{identity.node_id}", idempotency_key or req.operation_id, payload, claim)

    async def get_job(self, identity: NodeIdentity, job_id: str) -> dict:
        node = await self.store.read("node", identity.node_id)
        if not node or node["state"] == "revoked" or node["certificate_fingerprint"] != identity.certificate_fingerprint:
            raise DomainError("node_unauthorized", "Inactive node certificate", 401)
        job = await self.store.read("node_job", job_id)
        if not job or job["node_id"] != identity.node_id:
            raise DomainError("job_forbidden", "Job is outside this node identity", 403)
        return job

    async def _authorized_job(self, identity: NodeIdentity, job_id: str, token: str) -> dict:
        job = await self.get_job(identity, job_id)
        self.pki.verify_attempt_token(token, {"node_id": identity.node_id, "job_id": job_id,
                                            "attempt_id": job["attempt_id"], "fence": job["fencing_token"],
                                            "input_fingerprint": job["input_fingerprint"]})
        if job["state"] not in {"leased", "running", "stopping"}:
            raise DomainError("job_not_active", "Job no longer accepts execution writes")
        return job

    async def renew_job(self, identity: NodeIdentity, job_id: str, request: JobHeartbeatRequest | dict,
                         attempt_token: str, idempotency_key: str) -> dict:
        req = JobHeartbeatRequest.model_validate(request)
        await self._authorized_job(identity, job_id, attempt_token)

        def renew(tx):
            self._require_node(tx, identity)
            job = tx.get("node_job", job_id)
            self._check_lease(job, identity, req.fencing_token, req.input_fingerprint)
            if req.expected_revision != job["lease_revision"] or req.boot_fingerprint != job["boot_fingerprint"]:
                raise DomainError("lease_changed", "Lease or boot identity changed")
            if req.process_fingerprint and job.get("process_fingerprint") not in {None, req.process_fingerprint} and not job.get("process_exited"):
                raise DomainError("process_changed", "Heartbeat cannot substitute another process")
            if req.observed_state == "preparing" and job.get("process_fingerprint"):
                raise DomainError("process_changed", "An execution cannot return to a pre-process state")
            if not self._parent_current(tx, job):
                return {"job_id": job_id, "directive": {"job_id": job_id, "action": "stop", "reason": "parent_input_or_run_changed"}}
            if job["state"] == "stopping":
                return {"job_id": job_id, "directive": {"job_id": job_id, "action": "stop", "reason": "cancel_requested"}}
            received = {r.resource_id: r for r in req.resource_fences}
            if set(received) != set(job["required_resource_ids"]):
                raise DomainError("resource_fence_mismatch", "Heartbeat must identify every leased resource")
            expires, leases = _future(self.lease_seconds), []
            for old in job["resource_leases"]:
                row = tx.get("node_resource", old["resource_id"])
                incoming = received[old["resource_id"]]
                if (not row or row["state"] != "leased" or row["owner_job_id"] != job_id
                        or row["fencing_token"] != incoming.fencing_token or row["current_lease_id"] != incoming.lease_id):
                    raise DomainError("resource_fence_mismatch", "Resource is stale or quarantined")
                updated = tx.put("node_resource", row["id"], {**row, "expires_at": expires}, expected_revision=row["revision"])
                leases.append({**old, "resource_revision": updated["revision"], "expires_at": expires})
            updated = tx.put("node_job", job_id, {**job, "state": "leased" if req.observed_state == "preparing" else "running",
                             "process_fingerprint": req.process_fingerprint,
                             "lease_revision": job["lease_revision"] + 1, "lease_expires_at": expires,
                             "resource_leases": leases, "last_activity_at": req.last_activity_at,
                             "process_exited": req.observed_state == "process_exited"}, expected_revision=job["revision"])
            return {"job_id": job_id, "job_revision": updated["revision"], "lease_revision": updated["lease_revision"],
                    "fencing_token": updated["fencing_token"], "expires_at": expires, "resource_leases": leases,
                    "attempt_token": self._assignment(updated)["attempt_token"],
                    "directive": {"job_id": job_id, "action": "continue_observing", "reason": "lease_valid"}}
        return await self.store.command(f"node_job_renew:{job_id}", idempotency_key, req.model_dump(mode="json"), renew)

    @staticmethod
    def _check_lease(job, identity: NodeIdentity, fence: int, inputs: str) -> None:
        if not job or job["node_id"] != identity.node_id:
            raise DomainError("job_forbidden", "Job belongs to another node", 403)
        if job["fencing_token"] != fence or job["input_fingerprint"] != inputs:
            raise DomainError("stale_execution", "Execution fence or input fingerprint changed")
        if not job["lease_expires_at"] or _time(job["lease_expires_at"]) <= datetime.now(UTC):
            raise DomainError("lease_expired", "Lease has expired; resource must be reconciled")

    async def begin_upload(self, identity: NodeIdentity, job_id: str, attempt_token: str,
                            request: dict, idempotency_key: str) -> dict:
        job = await self._authorized_job(identity, job_id, attempt_token)
        return await self.artifacts.begin(identity.node_id, job_id, request["digest"], request["size"],
                                          request["name"], idempotency_key, job["limits"]["maximum_output_bytes"])

    async def append_chunk(self, identity: NodeIdentity, job_id: str, upload_id: str, attempt_token: str,
                            offset: int, data: bytes, chunk_digest: str, idempotency_key: str) -> dict:
        await self._authorized_job(identity, job_id, attempt_token)
        return await self.artifacts.append(upload_id, identity.node_id, job_id, offset, data, chunk_digest, idempotency_key)

    async def complete_upload(self, identity: NodeIdentity, job_id: str, upload_id: str, attempt_token: str,
                              idempotency_key: str) -> dict:
        await self._authorized_job(identity, job_id, attempt_token)
        return await self.artifacts.complete(upload_id, identity.node_id, job_id, idempotency_key)

    async def read_input_chunk(self, identity: NodeIdentity, job_id: str, artifact_id: str,
                               offset: int = 0, length: int = 1024 * 1024) -> dict:
        job = await self.get_job(identity, job_id)
        return await self.artifacts.read_chunk(artifact_id, job["download_artifact_version_ids"], offset, length)

    async def submit_job_result(self, identity: NodeIdentity, job_id: str, request: JobResultRequest | dict,
                                 attempt_token: str, idempotency_key: str | None = None) -> dict:
        req = JobResultRequest.model_validate(request)
        job = await self.get_job(identity, job_id)
        # Exact result retransmissions can be acknowledged after the job terminal transition.
        if job.get("result_id"):
            old = await self.store.read("node_result", job["result_id"])
            if old and old["request_digest"] == canonical_digest(req.model_dump(mode="json")):
                return {"result_id": old["id"], "job_id": job_id, "assessment_state": old["assessment_state"]}
            raise DomainError("result_conflict", "Job already has a different final result")
        await self._authorized_job(identity, job_id, attempt_token)
        artifacts = {}
        for aid in req.artifact_version_ids:
            artifact = await self.store.read("node_artifact", aid)
            if not artifact or artifact.get("job_id") != job_id or artifact["state"] != "complete":
                raise DomainError("unverified_artifact", "Results must reference this job's verified completed uploads")
            artifacts[aid] = artifact
        failures, verified_checks, verified_builds = [], [], []
        source = job.get("source_manifest")
        platform = job.get("platform_artifact_manifest")
        if req.job_kind.value != job["kind"] or req.app_target.value != job["app_target"]:
            failures.append("job_kind_or_target_mismatch")
        requires_observation = req.execution_status == "completed" or bool(req.built_artifacts or req.checks)
        if source and (requires_observation or req.observed_source_manifest_fingerprint) and req.observed_source_manifest_fingerprint != source["fingerprint"]:
            failures.append("source_fingerprint_mismatch")
        if platform and (requires_observation or req.observed_platform_artifact_manifest_fingerprint) and req.observed_platform_artifact_manifest_fingerprint != platform["fingerprint"]:
            failures.append("platform_fingerprint_mismatch")
        if source and (requires_observation or req.observed_test_package_digest) and req.observed_test_package_digest != job["test_package_digest"]:
            failures.append("test_package_mismatch")
        for claim in req.built_artifacts:
            artifact = artifacts.get(claim.get("artifact_version_id"))
            if (not artifact or artifact["digest"] != claim.get("digest")
                    or claim.get("source_manifest_fingerprint") != (source or {}).get("fingerprint")
                    or claim.get("app_target") != job["app_target"]
                    or claim.get("target_config_id") != job["target_config"]["target_config_id"]
                    or claim.get("environment_fingerprint") != job.get("environment_fingerprint")
                    or claim.get("kind") not in {"application", "test_runner", "api_service", "test_data"}
                    or not claim.get("component_id") or not claim.get("relative_path") or not claim.get("content_digest")):
                failures.append("build_artifact_unverified")
            else:
                verified_builds.append({**claim, "build_job_id": job_id, "build_node_id": identity.node_id,
                                        "verified_upload": True})
        seen_entries = set()
        for check in req.checks:
            entry = check.get("matrix_entry_id")
            artifact = artifacts.get(check.get("raw_report_artifact_version_id"))
            if entry in seen_entries or (job["matrix_entry_ids"] and entry not in job["matrix_entry_ids"]):
                failures.append("unexpected_or_duplicate_matrix_entry")
                continue
            seen_entries.add(entry)
            if not artifact:
                failures.append("raw_report_missing")
                continue
            declared = next((e for e in job.get("matrix_entries", []) if e["matrix_entry_id"] == entry), None)
            expected = set(declared.get("framework_case_ids", [])) if declared else None
            if not declared or not expected:
                failures.append("undeclared_case_mapping")
                continue
            parser = {"junit": parse_junit, "playwright": parse_playwright, "xcresult": parse_xcresult_export,
                      "instrumentation": parse_instrumentation}.get(
                check.get("report_format", check.get("runner_name")))
            if parser is None:
                failures.append("unsupported_report_format")
                continue
            try:
                parsed = parser(self.artifacts.object_path(artifact["digest"]), expected)
            except (DomainError, ValueError) as exc:
                failures.append(f"raw_report_invalid:{getattr(exc, 'code', 'invalid_report')}")
                continue
            if check.get("quality_result") == "passed" and parsed.quality_result != "passed":
                failures.append("claimed_pass_disagrees_with_raw_report")
            if check.get("platform_artifact_manifest_fingerprint") != (platform or {}).get("fingerprint"):
                failures.append("check_candidate_mismatch")
            if check.get("matrix_binding_fingerprint") != job.get("matrix_binding_fingerprint"):
                failures.append("check_binding_mismatch")
            if check.get("environment_fingerprint") != job.get("environment_fingerprint"):
                failures.append("check_environment_mismatch")
            if check.get("exit_code") != 0 and parsed.quality_result == "passed":
                failures.append("runner_exit_mismatch")
            required_key = execution_key(declared["test_case_id"], job["target_config"]["target_config_id"], platform["fingerprint"])
            # A partial framework report is valid failure evidence, not proof
            # that every planned case ran. Derive coverage from the raw report
            # so neither an empty nor a forged full key can claim success.
            actual_keys = [] if parsed.missing_case_ids else [required_key]
            if not check.get("expected_execution_keys"):
                failures.append("missing_execution_keys")
            if (check.get("test_case_id") != declared["test_case_id"]
                    or check.get("expected_execution_keys") != [required_key]
                    or check.get("actual_execution_keys") != actual_keys):
                failures.append("execution_key_scope_mismatch")
            if (check.get("target_config_id") != job["target_config"]["target_config_id"]
                    or check.get("target_config_revision") != job["target_config"]["revision"]
                    or check.get("matrix_plan_fingerprint") != job.get("matrix_plan_fingerprint")):
                failures.append("check_target_or_plan_mismatch")
            if platform:
                expected_components = {a.get("component_id", a.get("artifact_id")): a["digest"]
                                       for a in target_artifacts(platform, job["target_config"])}
                observed = {c.get("component_id"): c.get("actual_digest", c.get("digest"))
                            for c in check.get("observed_components", [])}
                if not expected_components or observed != expected_components:
                    failures.append("observed_components_mismatch")
                expected_services = [{"service_name": name, "component_id": component.get("component_id", component.get("artifact_id")),
                    "source_manifest_fingerprint": source["fingerprint"],
                    "product_content_digest": component.get("content_digest") or component.get("metadata", {}).get("content_digest")}
                    for name, component in service_components(platform, job.get("recipe") or {}).items()]
                if sorted(check.get("service_observations", []), key=lambda row: row.get("service_name", "")) != sorted(expected_services, key=lambda row: row["service_name"]):
                    failures.append("service_identity_mismatch")
            verified_checks.append({**check, "normalized_report": parsed.model_dump(mode="json"),
                                    "quality_result": parsed.quality_result, "node_id": identity.node_id, "job_id": job_id})
        if requires_observation and (req.job_kind == JobKind.TEST or (req.job_kind == JobKind.PROBE and platform)) and set(job["matrix_entry_ids"]) != seen_entries:
            failures.append("missing_required_matrix_entries")
        if req.quality_result == "passed" and req.job_kind == JobKind.BUILD and not verified_builds:
            failures.append("no_verified_builds")
        if req.quality_result == "passed" and any(check["quality_result"] != "passed" for check in verified_checks):
            failures.append("overall_pass_disagrees_with_raw_checks")
        result_id = new_id()

        def accept(tx):
            self._require_node(tx, identity)
            current = tx.get("node_job", job_id)
            self._check_lease(current, identity, req.fencing_token, req.input_fingerprint)
            if not self._parent_current(tx, current, allow_paused=True):
                raise DomainError("stale_parent_input", "Controller work item or run changed before result acceptance")
            if current["revision"] != req.expected_revision or current["state"] not in {"leased", "running", "stopping"}:
                raise DomainError("revision_conflict", "Job changed before result acceptance")
            if current["state"] == "stopping":
                failures.append("cancel_requested")
            for lease in current["resource_leases"]:
                resource = tx.get("node_resource", lease["resource_id"])
                if (not resource or resource["owner_job_id"] != job_id or resource["fencing_token"] != lease["fencing_token"]
                        or resource["state"] != "leased"):
                    failures.append("resource_lease_not_current")
            assessment = "rejected" if failures else "validated"
            row = tx.put("node_result", result_id, {"job_id": job_id, "node_id": identity.node_id,
                         "request_digest": canonical_digest(req.model_dump(mode="json")),
                         "assessment_state": assessment, "errors": failures, "request": req.model_dump(mode="json"),
                         "verified_checks": verified_checks if not failures else [],
                         "verified_build_artifacts": verified_builds if not failures else [], "received_at": utc_now()})
            state = "cancelled" if current["state"] == "stopping" else "completed" if not failures and req.execution_status == "completed" else "failed"
            tx.put("node_job", job_id, {**current, "state": state, "quality_result": req.quality_result.value if not failures else "unknown",
                                       "result_id": result_id}, expected_revision=current["revision"])
            # Expiry/completion alone never proves a GUI resource is safe to reuse.
            for lease in current["resource_leases"]:
                resource = tx.get("node_resource", lease["resource_id"])
                if resource and resource["owner_job_id"] == job_id:
                    tx.put("node_resource", resource["id"], {**resource, "state": "quarantined",
                           "quarantine_reason": "cleanup_receipt_required"}, expected_revision=resource["revision"])
            tx.event("node.job.result", {"job_id": job_id, "result_id": result_id, "assessment_state": assessment},
                     run_id=current["run_id"])
            return {"result_id": row["id"], "job_id": job_id, "assessment_state": assessment}
        return await self.store.command(f"node_job_result:{job_id}", idempotency_key or req.operation_id,
                                        req.model_dump(mode="json"), accept)

    async def submit_cleanup_receipt(self, identity: NodeIdentity, payload: dict, signature: str,
                                      idempotency_key: str) -> dict:
        job = await self.get_job(identity, payload.get("job_id", ""))
        node = await self.store.read("node", identity.node_id)
        verify_receipt(node["certificate_pem"], payload, signature)
        if payload.get("node_id") != identity.node_id or payload.get("alive_process_count") != 0:
            raise DomainError("cleanup_not_verified", "Receipt does not confirm stopped processes")
        if payload.get("method") not in {"process_stop_verified", "session_reset_verified", "device_reset_verified", "workspace_cleanup_verified"}:
            raise DomainError("cleanup_not_verified", "Unsupported cleanup evidence method", 422)
        receipt_id = new_id()

        def release(tx):
            self._require_node(tx, identity)
            resource = tx.get("node_resource", payload["resource_id"])
            if (not resource or resource["node_id"] != identity.node_id or resource["owner_job_id"] != job["id"]
                    or resource["fencing_token"] != payload.get("fencing_token")
                    or resource["current_lease_id"] != payload.get("lease_id")):
                raise DomainError("stale_cleanup", "Cleanup is for an old resource owner")
            receipt = tx.put("node_cleanup_receipt", receipt_id, {**payload, "signature": signature, "verified": True})
            tx.put("node_resource", resource["id"], {**resource, "state": "available", "current_lease_id": None,
                   "owner_job_id": None, "expires_at": None, "quarantine_reason": None,
                   "last_cleanup_receipt_id": receipt_id}, expected_revision=resource["revision"])
            current = tx.get("node_job", job["id"])
            remaining = [r for r in tx.list("node_resource") if r.get("owner_job_id") == job["id"]]
            if current["state"] in {"execution_unknown", "stopping"} and not remaining:
                tx.put("node_job", job["id"], {**current,
                    "state": "cancelled" if current.get("stop_reason") else "failed",
                    "quality_result": "unknown", "cleanup_verified": True}, expected_revision=current["revision"])
                tx.event("node.job.cleanup_resolved", {"job_id": job["id"]}, run_id=current.get("run_id"))
            tx.event("node.resource.cleaned", {"resource_id": resource["id"], "receipt_id": receipt_id})
            return receipt
        return await self.store.command(f"node_cleanup:{job['id']}", idempotency_key,
                                        {"payload": payload, "signature": signature}, release)

    async def cancel_job(self, job_id: str, reason: str, idempotency_key: str) -> dict:
        def cancel(tx):
            job = tx.get("node_job", job_id)
            if not job:
                raise DomainError("job_missing", "Job not found", 404)
            if job["state"] in {"completed", "failed", "cancelled"}:
                return job
            state = "cancelled" if job["state"] == "queued" else "stopping"
            updated = tx.put("node_job", job_id, {**job, "state": state, "stop_reason": reason}, expected_revision=job["revision"])
            for lease in job["resource_leases"]:
                resource = tx.get("node_resource", lease["resource_id"])
                if resource and resource["owner_job_id"] == job_id:
                    tx.put("node_resource", resource["id"], {**resource, "state": "quarantined", "quarantine_reason": reason},
                           expected_revision=resource["revision"])
            tx.event("node.job.stop_requested", {"job_id": job_id, "reason": reason}, run_id=job["run_id"])
            return updated
        return await self.store.command(f"node_job_cancel:{job_id}", idempotency_key, {"reason": reason}, cancel)

    async def reconcile_expired_leases(self) -> list[str]:
        expired = [j for j in await self.store.list("node_job") if j.get("lease_expires_at")
                   and j["state"] in {"leased", "running", "stopping"}
                   and _time(j["lease_expires_at"]) <= datetime.now(UTC)]
        changed = []
        for candidate in expired:
            def quarantine(tx, candidate=candidate):
                job = tx.get("node_job", candidate["id"])
                if job["state"] not in {"leased", "running", "stopping"} or _time(job["lease_expires_at"]) > datetime.now(UTC):
                    return {"changed": False}
                tx.put("node_job", job["id"], {**job, "state": "execution_unknown"}, expected_revision=job["revision"])
                for lease in job["resource_leases"]:
                    resource = tx.get("node_resource", lease["resource_id"])
                    if resource and resource["owner_job_id"] == job["id"]:
                        tx.put("node_resource", resource["id"], {**resource, "state": "quarantined",
                               "quarantine_reason": "lease_expired_stop_unconfirmed"}, expected_revision=resource["revision"])
                tx.event("node.job.execution_unknown", {"job_id": job["id"]}, run_id=job["run_id"])
                return {"changed": True}
            result = await self.store.command(f"node_lease_expire:{candidate['id']}", candidate["lease_expires_at"], {}, quarantine)
            if result["changed"]:
                changed.append(candidate["id"])
        return changed

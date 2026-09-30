"""One owner action prepares a real, authenticated local Web/API executor."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import platform
import shutil
import socket
import ssl
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.node_routes import create_executor_app
from agentflow.control.tls import PeerCertificateH11Protocol
from agentflow.execution.manifests import (
    BuildArtifact,
    MatrixPlan,
    MatrixPlanEntry,
    SourceManifest,
    bind_matrix,
    file_digest,
    freeze_platform_manifest,
)
from agentflow.execution.models import JobLimits, TargetConfig, ToolRequirement, new_id
from agentflow.execution.pki import _private_write, create_node_key_and_csr
from agentflow.execution.service import NodeService
from agentflow.repository import RepositoryAdapter
from agentflow.runtime.launcher import atomic_json
from agentflow.testing.adapters import BuildRecipe
from node_agent.client import enroll_node
from node_agent.daemon import NodeDaemon
from node_agent.local_isolation import LocalIsolatedExecutor


class _LocalServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


class LocalExecutionService:
    def __init__(self, store, data_dir: Path, *, node_service=None, reference_root: Path | None = None,
                 trusted_project_execution: bool = False, protected_ports=(), package_fetch_timeout_seconds=30):
        self.store, self.data_dir = store, Path(data_dir).resolve()
        if node_service is not None and node_service.store is not store:
            raise DomainError("local_executor_store_mismatch", "Local and LAN execution must share the controller store")
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(128)
        self.port = self._socket.getsockname()[1]
        self.origin = f"https://127.0.0.1:{self.port}"
        self.nodes = NodeService(store, self.data_dir, self.origin)
        self._loopback_certificate()
        self.reference_root = Path(reference_root).resolve() if reference_root else Path(str(
            resources.files("agentflow").joinpath("resources", "local_reference"))).resolve()
        self.protected_ports = sorted(set([self.port, *protected_ports]))
        self.trusted_project_execution = trusted_project_execution
        self.package_fetch_timeout_seconds = package_fetch_timeout_seconds
        self._server = None
        self._server_task = None
        self._daemon: NodeDaemon | None = None
        self._worker_task = None
        self._worker_error: Exception | None = None
        self._prepare_task = None
        self._prepare_lock = asyncio.Lock()
        self._daemon_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._targets: list[TargetConfig] = []
        self._closed = False
        self._pending_targets: set[str] = set()
        self._active_targets: set[str] = set()
        self._current_fixture_digest = None
        self._resume_attempted_targets: set[str] = set()

    def _loopback_certificate(self):
        # Keep an existing LAN gateway certificate and its pin unchanged. Both
        # listeners share the controller CA and database, with distinct server pins.
        pki = self.nodes.pki
        certificate, key = pki.directory / "local-server.pem", pki.directory / "local-server.key"
        if certificate.exists() != key.exists():
            raise DomainError("local_executor_pki_incomplete", "Local execution certificate state needs recovery")
        if not certificate.exists():
            private = ec.generate_private_key(ec.SECP256R1())
            issued = pki._issue(private.public_key(), "AgentFlow managed local execution",
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))], [ExtendedKeyUsageOID.SERVER_AUTH], days=365)
            _private_write(key, private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
            _private_write(certificate, issued.public_bytes(serialization.Encoding.PEM))
        pki.server_cert_path, pki.server_key_path = certificate, key
        pki.server_certificate = x509.load_pem_x509_certificate(certificate.read_bytes())

    async def start(self) -> dict:
        if self._closed:
            raise DomainError("local_executor_closed", "Local execution service has closed")
        if not self._server_task:
            self._server = _LocalServer(uvicorn.Config(create_executor_app(self.nodes), proxy_headers=False,
                access_log=False, log_level="warning", lifespan="off", http=PeerCertificateH11Protocol,
                ssl_certfile=str(self.nodes.pki.server_cert_path), ssl_keyfile=str(self.nodes.pki.server_key_path),
                ssl_ca_certs=str(self.nodes.pki.directory / "ca.pem"), ssl_cert_reqs=ssl.CERT_OPTIONAL))
            self._server_task = asyncio.create_task(self._server.serve(sockets=[self._socket]), name="managed-local-mtls")
        for _ in range(200):
            if self._server.started:
                return await self.status()
            if self._server_task.done():
                await self._server_task
                break
            await asyncio.sleep(.025)
        raise DomainError("local_executor_listener_failed", "The private execution listener did not start")

    async def resume_pending(self, *, wait=False) -> dict:
        """Reconnect an enrolled executor once per required target per application start.

        Explicit prepare remains the retry action after a reported preparation
        failure. Recovery never creates a replacement product, candidate or job.
        """
        if self._resume_attempted_targets == {"api", "web"}:
            return await self.status()
        local = await self.store.read("local_execution", "managed-local")
        node = await self.store.read("node", local["node_id"]) if local and local.get("node_id") else None
        if not node or node["state"] == "revoked":
            return await self.status()
        registered = {canonical_digest(target): target["app_target"] for target in local.get("target_configs", [])
                      if target.get("app_target") in {"api", "web"} & set(node.get("allowed_app_targets", []))}
        runs, work, jobs, candidates = await asyncio.gather(*(self.store.list(kind) for kind in
            ("run", "work_item", "node_job", "candidate")))
        runs = {run["id"]: run for run in runs if run.get("execution_state") == "running"}
        work = {item["id"]: item for item in work}
        jobs = {job["id"]: job for job in jobs}
        requested = set()
        for job in jobs.values():
            run = runs.get(job.get("run_id"))
            if (not run or job.get("state") != "queued" or job.get("node_id") not in {None, local["node_id"]}
                    or canonical_digest(job.get("target_config")) not in registered):
                continue
            if job.get("parent_work_item_id"):
                item = work.get(job["parent_work_item_id"])
                if (not item or item.get("run_id") != run["id"]
                        or item.get("generation") != job.get("parent_generation")
                        or item.get("input_fingerprint") != job.get("parent_input_fingerprint")
                        or item.get("status") in {"cancel_requested", "cancelled"}):
                    continue
            elif run.get("input_fingerprint") != job.get("parent_run_fingerprint"):
                continue
            requested.add(registered[canonical_digest(job["target_config"])])
        candidates = {candidate["id"]: candidate for candidate in candidates}
        for item in work.values():
            run, candidate = runs.get(item.get("run_id")), candidates.get(item.get("candidate_id"))
            if (item.get("status") != "waiting_execution" or not run or not candidate
                    or candidate.get("run_id") != run["id"]
                    or candidate.get("run_input_fingerprint") != run.get("input_fingerprint")):
                continue
            phase = item.get("execution_phase")
            builds = [jobs.get(identity) for identity in candidate.get("build_job_ids", [])]
            prerequisites = builds + [jobs.get(identity) for identity in candidate.get("phase_jobs", {}).get("install", [])]
            if (phase in candidate.get("phase_jobs", {})
                    or any(not job or job.get("state") != "completed" or job.get("quality_result") != "passed"
                           for job in prerequisites)):
                continue
            phase_targets = {target["target_config_id"] for target in candidate.get("recipes", {}).get("targets", [])
                             if target.get(phase) is not None}
            for target in candidate.get("matrix_plan", {}).get("target_configs", []):
                app_target = registered.get(canonical_digest(target))
                if app_target and (not builds or target.get("target_config_id") in phase_targets):
                    requested.add(app_target)
        requested.difference_update(self._resume_attempted_targets)
        if not requested:
            return await self.status()
        self._resume_attempted_targets.update(requested)
        reference = await self.store.read("local_execution_reference", local["reference_id"]) if local.get("reference_id") else None
        reference_source = (reference or {}).get("source_manifest", {}).get("fingerprint")
        if any((job.get("node_id") == local["node_id"] or reference_source
                and (job.get("source_manifest") or {}).get("fingerprint") == reference_source)
               and job.get("state") in {"execution_unknown", "stopping"}
               for job in jobs.values()):
            await self._update(state="blocked", phase="recovery", error_code="local_execution_unknown",
                               message="先前本机进程尚未确认结束，未自动启动新的作业")
            return await self.status(required_targets=requested)
        return await self.prepare(wait=wait, required_targets=sorted(requested))

    async def _update(self, **changes):
        def update(tx):
            row = tx.get("local_execution", "managed-local")
            body = row or {"installation_id": new_id(), "state": "not_prepared", "created_at": utc_now()}
            updated = tx.put("local_execution", "managed-local", {**body, **changes, "updated_at": utc_now()},
                             row["revision"] if row else None)
            tx.event("local_execution.updated", {key: updated.get(key) for key in ("state", "phase", "job_id", "message")})
            return updated
        return await self.store.command("local_execution.update", new_id(), changes, update)

    async def _ready_targets(self, row=None, caps=None):
        if self._stop.is_set() or not self._daemon or not self._worker_task or self._worker_task.done():
            return []
        row = row or await self.store.read("local_execution", "managed-local") or {}
        caps = caps or [await self.store.read("node_capability", identity) for identity in self._daemon.capability_ids]
        expected_fixture = self._current_fixture_digest or row.get("fixture_digest")
        ready = []
        for cap in caps:
            if not cap or cap.get("verification_state") != "functional_verified":
                continue
            proof = row.get("validated_targets", {}).get(cap["app_target"], {})
            trusted = (proof.get("result_id") == cap.get("functional_result_id") and proof.get("fixture_digest") == expected_fixture)
            trusted |= (not row.get("validated_targets") and row.get("fixture_digest") == expected_fixture
                        and cap.get("functional_result_id") in row.get("result_ids", []))
            if trusted:
                ready.append(cap["app_target"])
        return sorted(ready)

    async def status(self, *, required_targets=None) -> dict:
        row = await self.store.read("local_execution", "managed-local")
        view = {"state": "not_prepared", "phase": "not_prepared", "target_configs": [],
                "node_id": None, "capability_ids": [], "message": "本机执行环境尚未准备"} if not row else {
                    key: row.get(key) for key in ("state", "phase", "target_configs", "node_id", "capability_ids", "message",
                                                 "job_id", "reference_id", "result_ids", "error_code", "updated_at")}
        view["preparing"] = bool(self._prepare_task and not self._prepare_task.done())
        for key in ("target_configs", "capability_ids", "result_ids"):
            view[key] = view.get(key) or []
        view["isolation"] = "macos_seatbelt_required"
        view["ready_targets"] = await self._ready_targets(row)
        if view["state"] in {"ready", "partial"}:
            if set(view["ready_targets"]) == {"api", "web"}:
                view["state"] = "ready"
            elif view["ready_targets"]:
                view["state"] = "partial"
            elif self._daemon is None:
                view.update(state="not_prepared", phase="reconnect", message="启动本机执行器后即可复用已验证环境")
            else:
                view.update(state="blocked", phase="revalidation", message="本机环境变化或执行器停止，需要重新准备")
        if required_targets is not None:
            view["required_targets"] = sorted(set(required_targets))
            if set(required_targets).issubset(view["ready_targets"]):
                view.update(state="ready", error_code=None, message="所需本机执行环境已通过真实验证")
            elif not view["preparing"] and view["state"] == "partial":
                view["state"] = "blocked"
        return view

    async def prepare(self, *, wait=False, required_targets=("api", "web")) -> dict:
        requested = set(required_targets)
        if not requested or not requested.issubset({"api", "web"}):
            raise DomainError("invalid_local_targets", "Local preparation supports explicit API and/or Web targets")
        await self.start()
        async with self._prepare_lock:
            self._pending_targets.update(requested - self._active_targets)
            if not self._prepare_task or self._prepare_task.done():
                await self._update(state="preparing", phase="preflight", error_code=None,
                                   job_id=None, message="正在检查本机开发与测试环境")
                self._prepare_task = asyncio.create_task(self._prepare_batches(), name="managed-local-prepare")
            task = self._prepare_task
        if wait:
            while not task.done():
                view = await self.status(required_targets=requested)
                if view["state"] == "ready":
                    return view
                await asyncio.sleep(.1)
            await asyncio.shield(task)
        return await self.status(required_targets=requested)

    async def _prepare_batches(self):
        try:
            while self._pending_targets and not self._stop.is_set():
                requested = set(self._pending_targets)
                self._pending_targets.clear()
                self._active_targets = requested
                await self._prepare(requested)
        finally:
            self._active_targets.clear()

    def _fixture(self):
        root = self.reference_root
        descriptor = root / "manifest.json"
        if root.is_symlink() or not descriptor.is_file():
            raise DomainError("local_reference_missing", "The installed package is missing its local verification fixture")
        manifest = json.loads(descriptor.read_text())
        expected = set()
        for item in manifest["files"]:
            relative = Path(item["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise DomainError("local_reference_invalid", "Bundled reference paths are invalid")
            path = root / relative
            if path.is_symlink() or not path.is_file() or file_digest(path) != item["digest"]:
                raise DomainError("local_reference_changed", "The bundled verification fixture differs from its manifest")
            expected.add(relative.as_posix())
        actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() and path != descriptor}
        if actual != expected:
            raise DomainError("local_reference_changed", "Unexpected files appeared in the bundled verification fixture")
        return manifest, canonical_digest(manifest)

    async def _ensure_daemon(self):
        state = await self.store.read("local_execution", "managed-local")
        installation = state["installation_id"]
        directory = self.data_dir / "nodes/managed-local" / installation
        configuration = directory / "node.json"
        if configuration.exists():
            saved = json.loads(configuration.read_text())
            node = await self.store.read("node", saved["node_id"])
            if not node or node["state"] == "revoked":
                raise DomainError("local_executor_repair_required", "本机执行器已撤销，需要重新启用后才能运行新产品")
            if saved["controller_fingerprint"] != self.nodes.pki.controller_fingerprint:
                raise DomainError("local_executor_identity_changed", "The local gateway identity changed; automatic credential reuse is blocked")
            atomic_json(configuration, {**saved, "origin": self.origin})
            node_id = saved["node_id"]
        else:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            _, fingerprint = create_node_key_and_csr(directory, "AgentFlow managed local Web/API")
            pair = await self.nodes.create_pairing({"node_label": "本机 Web/API 执行器", "location": "controller_host",
                "expected_node_public_key_fingerprint": fingerprint, "allowed_app_targets": ["web", "api"]},
                "managed-local-pair:" + installation)
            paired = await enroll_node(directory, origin=self.origin, pairing_id=pair["pairing"]["id"],
                single_use_code=pair["single_use_code"], controller_fingerprint=self.nodes.pki.controller_fingerprint,
                label="AgentFlow managed local Web/API")
            node_id = paired["node_id"]
        resource_id = str(uuid5(NAMESPACE_URL, f"agentflow:local-workspace:{node_id}"))
        existing_resource = await self.store.read("node_resource", resource_id)
        if not existing_resource:
            await self.nodes.register_resource(node_id, "workspace", canonical_digest({"node_id": node_id, "kind": "private-job-workspace"}),
                                               "managed-workspace:" + node_id, resource_id=resource_id)
        self._targets = [TargetConfig(target_config_id=str(uuid5(NAMESPACE_URL, f"agentflow:local-target:{installation}:{kind}")),
            app_target=kind, os_name=platform.system(), os_version_constraint="*", cpu_architecture=platform.machine(),
            required_display_protocol="not_required", required_device_mode="not_required", required_resource_ids=[resource_id],
            sdk_requirements=[ToolRequirement(name="node", version_constraint=">=22.13"), ToolRequirement(name="npm", version_constraint=">=10")],
            build_backend=ToolRequirement(name="npm", version_constraint=">=10"),
            test_backend=ToolRequirement(name="node", version_constraint=">=22.13")) for kind in ("api", "web")]
        async with self._daemon_lock:
            if self._daemon is None:
                self._daemon = NodeDaemon(directory, self._targets, resource_ids=[resource_id], trusted_project_mode=False)
                self._daemon.runner.executor = LocalIsolatedExecutor(self.data_dir, protected_ports=self.protected_ports,
                    package_fetch_timeout_seconds=self.package_fetch_timeout_seconds)
                reports = await self._daemon.initialize()
            else:
                reports = await self._daemon.refresh_capabilities()
            unknown = [entry for entry in self._daemon.journal.recoverable() if entry["state"] != "received"]
            if unknown:
                raise DomainError("local_execution_unknown", "先前本机进程尚未确认结束，未创建新的验证作业")
            current_resource = await self.store.read("node_resource", resource_id)
            if current_resource["state"] == "quarantined":
                for pending in self._daemon.journal.pending_cleanups():
                    await self._daemon._cleanup(pending["assignment"], pending["result"])
                current_resource = await self.store.read("node_resource", resource_id)
                if current_resource["state"] == "quarantined":
                    raise DomainError("local_workspace_cleanup_required", "先前本机作业的清理尚未确认，已阻止新作业")
        await self._update(node_id=node_id, target_configs=[target.model_dump(mode="json") for target in self._targets],
                           capability_ids=list(self._daemon.capability_ids))
        if not self._worker_task or self._worker_task.done():
            self._worker_error = None
            self._worker_task = asyncio.create_task(self._work(), name="managed-local-node")
        return reports

    async def _work(self):
        while not self._stop.is_set():
            try:
                async with self._daemon_lock:
                    result = await self._daemon.run_once()
                if result.get("state") == "execution_unknown":
                    self._worker_error = DomainError("local_execution_unknown", "先前进程状态需要核对，未重复启动")
                    await self._update(state="blocked", phase="recovery", error_code="local_execution_unknown",
                                       message="先前进程状态需要核对，未重复启动")
                    return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._worker_error = error
                await self._update(state="blocked", phase="execution", error_code=getattr(error, "code", "local_worker_error"),
                                   message=str(error)[:2000])
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=.2)
            except TimeoutError:
                pass

    def _recipes(self, target, manifest):
        common = {"adapter": target.app_target, "project_path": "web_api", "product_path": "web_api/bundle/product"}
        build = BuildRecipe(**common, output_paths={"product": "web_api/bundle/product", "test": "web_api/bundle/tests"})
        unit = BuildRecipe(**common, test_project_path="web_api/bundle/tests", test_kind="unit",
            unit_project="web_api/bundle/tests/unit/domain.test.mjs", report_path="reports/unit.xml",
            expected_case_ids=manifest["unit_case_ids"])
        kind = target.app_target.value
        integration = BuildRecipe(**common, test_project_path="web_api/bundle/tests", test_kind="api" if kind == "api" else "gui",
            framework_config=f"web_api/bundle/tests/playwright.{kind}.config.mjs", report_path=f"reports/{kind}.json",
            expected_case_ids=manifest[f"{kind}_case_ids"])
        return build, unit, integration

    async def _reference(self, manifest, fixture_digest, caps, targets):
        state = await self.store.read("local_execution", "managed-local")
        existing = await self.store.read("local_execution_reference", state["reference_id"]) if state.get("reference_id") else None
        environments = {cap["report"]["target_config_fingerprint"]: cap["environment_fingerprint"] for cap in caps}
        if existing:
            prior_jobs = [job for job in await self.store.list("node_job")
                          if (job.get("source_manifest") or {}).get("fingerprint") == existing["source_manifest"]["fingerprint"]]
            if any(job["state"] in {"execution_unknown", "stopping"} for job in prior_jobs):
                raise DomainError("local_execution_unknown", "先前验证作业的执行或清理状态不确定，未创建替代作业")
            reusable = (existing["fixture_digest"] == fixture_digest and existing.get("environment_fingerprints") == environments
                        and all(job["state"] not in {"failed", "cancelled"}
                                and (job["state"] != "completed" or job.get("quality_result") == "passed") for job in prior_jobs))
            if reusable:
                recipes = {entry["target_config_id"]: tuple(BuildRecipe.model_validate(entry[phase]) for phase in
                    ("build", "unit", "integration")) for entry in existing["build_plan"]["targets"]}
                return (existing, SourceManifest.model_validate({k: v for k, v in existing["source_manifest"].items() if k != "fingerprint"}),
                        MatrixPlan.model_validate(existing["matrix_plan"]), existing["matrix_mappings"], recipes)
            if any(job["state"] in {"queued", "leased", "running"} for job in prior_jobs):
                raise DomainError("local_reference_inputs_changed", "先前验证作业仍未结束，不能以新的环境替换它")
        entries, mappings, recipes = [], {}, {}
        for target in targets:
            recipes[target.target_config_id] = self._recipes(target, manifest)
            for phase, recipe in zip(("unit", "integration"), recipes[target.target_config_id][1:], strict=True):
                identity = str(uuid5(NAMESPACE_URL, f"agentflow:local-reference:{fixture_digest}:{target.target_config_id}:{phase}"))
                entry = MatrixPlanEntry(matrix_entry_id=identity, test_case_id=f"local-reference:{target.app_target}:{phase}",
                    app_target=target.app_target, component_roles=["product", "test"], target_config_id=target.target_config_id,
                    target_config_revision=target.revision)
                entries.append(entry)
                mappings[identity] = {"matrix_entry_id": identity, "test_case_id": entry.test_case_id,
                    "framework_case_ids": recipe.expected_case_ids, "phase": phase, "target_config_id": target.target_config_id}
        kinds = [target.app_target for target in targets]
        plan = MatrixPlan(matrix_id=str(uuid5(NAMESPACE_URL, "agentflow:local-reference-matrix:" + fixture_digest + ":" + ",".join(kinds))),
                          required_app_targets=kinds, target_configs=targets, entries=entries)
        identity = canonical_digest({"fixture": fixture_digest, "matrix": plan.fingerprint})[7:]
        repository = self.data_dir / "workspaces" / ("local-reference-" + identity)
        adapter = RepositoryAdapter(git_path=shutil.which("git") or "git")
        if not repository.exists():
            repository.mkdir(parents=True, mode=0o700)
            for item in manifest["files"]:
                target = repository / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(self.reference_root / item["path"], target)
            adapter._run(None, ["init", "-b", "main", str(repository)])
            adapter._run(repository, ["add", "."])
            adapter._run(repository, ["-c", "user.name=AgentFlow", "-c", "user.email=agentflow@localhost", "commit", "-m", "Frozen local executor reference"])
        commit = adapter._run(repository, ["rev-parse", "HEAD"]).decode().strip()
        expected_files = {item["path"]: item["digest"] for item in manifest["files"]}
        observed_files = {value.decode() for value in adapter._run(repository, ["ls-tree", "-r", "--name-only", "-z", commit]).split(b"\0") if value}
        if observed_files != set(expected_files):
            raise DomainError("local_reference_changed", "The cached reference source tree changed")
        import hashlib
        for name, digest in expected_files.items():
            if "sha256:" + hashlib.sha256(adapter._run(repository, ["show", f"{commit}:{name}"])).hexdigest() != digest:
                raise DomainError("local_reference_changed", "The cached reference source differs from the bundled fixture")
        tree = adapter._integrity(repository, commit)
        archive = adapter._run(repository, ["archive", "--format=tar", commit])
        source_blob = await self.nodes.import_input(archive, "local-reference-source.tar", None)
        build_plan = {"schema_version": 1, "targets": [{"target_config_id": target.target_config_id,
            "build": recipes[target.target_config_id][0].model_dump(mode="json"),
            "unit": recipes[target.target_config_id][1].model_dump(mode="json"),
            "integration": recipes[target.target_config_id][2].model_dump(mode="json")} for target in targets]}
        build_blob = await self.nodes.import_input(json.dumps(build_plan).encode(), "local-reference-plan.json", None)
        source = SourceManifest(source_commit=commit, source_tree_oid=tree,
            source_bundle_artifact_version_id=source_blob["id"], source_bundle_digest=source_blob["digest"],
            test_package_artifact_version_id=source_blob["id"], test_package_digest=source_blob["digest"],
            build_plan_artifact_version_id=build_blob["id"], build_plan_digest=build_blob["digest"],
            target_matrix_fingerprint=plan.fingerprint, required_app_targets=tuple(kinds))
        def save(tx):
            return tx.put("local_execution_reference", new_id(), {"repository_path": str(repository), "commit_oid": commit,
                "fixture_digest": fixture_digest, "source_manifest": {**source.model_dump(mode="json"), "fingerprint": source.fingerprint},
                "matrix_plan": plan.model_dump(mode="json"), "matrix_mappings": mappings, "build_plan": build_plan,
                "environment_fingerprints": environments, "created_at": utc_now()})
        record = await self.store.command("local_execution.reference", new_id(), {"source": source.fingerprint}, save)
        return record, source, plan, mappings, recipes

    async def _wait_job(self, job, phase):
        await self._update(phase=phase, job_id=job["id"], message={"build": "正在真实构建验证样本", "unit": "正在执行样本单元测试",
                           "integration": "正在执行浏览器与接口功能验证"}.get(phase, "正在准备本机执行环境"))
        for _ in range(7200):
            current = await self.store.read("node_job", job["id"])
            if current["state"] in {"completed", "failed", "cancelled", "execution_unknown"}:
                result = await self.store.read("node_result", current["result_id"]) if current.get("result_id") else None
                if current["state"] != "completed" or current["quality_result"] != "passed" or not result or result["assessment_state"] != "validated":
                    summary = (result or {}).get("request", {}).get("summary", "")
                    raise DomainError("local_reference_failed", f"本机验证未通过：{phase}。{summary}")
                return current, result
            if self._stop.is_set() or (self._worker_task and self._worker_task.done()):
                if self._worker_error is not None:
                    raise self._worker_error
                raise DomainError("local_worker_stopped", "本机执行器已停止，未重复启动旧作业")
            await asyncio.sleep(.1)
        await self.nodes.cancel_job(job["id"], "Local environment preparation timed out", new_id())
        raise DomainError("local_preparation_timeout", "本机环境准备超时，作业已请求停止")

    async def _prepare(self, requested):
        try:
            manifest, fixture_digest = self._fixture()
            self._current_fixture_digest = fixture_digest
            reports = await self._ensure_daemon()
            previous = await self.store.read("local_execution", "managed-local")
            all_caps = [await self.store.read("node_capability", identity) for identity in self._daemon.capability_ids]
            already_ready = set(await self._ready_targets(previous, all_caps))
            available = {report["app_target"] for report in reports if report["state"] == "static_verified"}
            selected = requested - already_ready
            targets = [target for target in self._targets if target.app_target in selected & available]
            caps = [cap for cap in all_caps if cap["app_target"] in selected & available]
            if not targets:
                complete = requested.issubset(already_ready)
                reasons = sorted({reason for report in reports if report["app_target"] in requested for reason in report["blocking_reasons"]})
                await self._update(state="ready" if already_ready == {"api", "web"} else "partial" if already_ready else "blocked",
                    phase="ready" if complete else "blocked", message="所需执行环境已就绪" if complete else "本机缺少所需环境：" + "; ".join(reasons),
                    error_code=None if complete else "local_tools_unavailable")
                return
            reference, source, matrix, mappings, recipes = await self._reference(manifest, fixture_digest, caps, targets)
            await self._update(reference_id=reference["id"], result_ids=[])
            builds, result_ids = [], []
            limits = JobLimits(maximum_active_seconds=600, maximum_output_bytes=256 * 1024 * 1024,
                               maximum_memory_bytes=4 * 1024 ** 3, maximum_processes=128)
            for target in targets:
                job = await self.nodes.enqueue_job(None, kind="build", target_config=target, source_manifest=source,
                    recipe=recipes[target.target_config_id][0], idempotency_key=f"local-build:{reference['id']}:{target.target_config_id}", limits=limits)
                _, result = await self._wait_job(job, "build")
                result_ids.append(result["id"])
                for artifact in result["verified_build_artifacts"]:
                    builds.append(BuildArtifact(artifact_id=artifact["component_id"], artifact_version_id=artifact["artifact_version_id"],
                        app_target=target.app_target, target_config_id=target.target_config_id, component_role=artifact["component_role"],
                        kind="product" if artifact["kind"] == "application" else "test", digest=artifact["digest"],
                        source_manifest_fingerprint=source.fingerprint, toolchain_fingerprint=artifact["environment_fingerprint"],
                        verified_upload=True, metadata={"relative_path": artifact["relative_path"], "content_digest": artifact["content_digest"]}))
            from agentflow.execution.manifests import PlatformManifest
            if reference.get("platform_manifest"):
                platform_manifest = PlatformManifest.model_validate(reference["platform_manifest"])
                binding = reference["matrix_binding"]
            else:
                platform_manifest = freeze_platform_manifest(source, matrix, builds)
                binding = bind_matrix(matrix, source, platform_manifest, {entry.matrix_entry_id: {} for entry in matrix.entries})
                def freeze(tx):
                    current = tx.get("local_execution_reference", reference["id"])
                    return tx.put("local_execution_reference", current["id"], {**current,
                        "platform_manifest": platform_manifest.model_dump(mode="json"), "matrix_binding": binding}, current["revision"])
                await self.store.command("local_execution.platform", reference["id"], {"platform": platform_manifest.fingerprint}, freeze)
            validated = dict(previous.get("validated_targets", {}))
            for target, cap in zip(targets, caps, strict=True):
                for phase, recipe in zip(("unit", "integration"), recipes[target.target_config_id][1:], strict=True):
                    entries = [entry for entry in mappings.values() if entry["target_config_id"] == target.target_config_id and entry["phase"] == phase]
                    job = await self.nodes.enqueue_functional_probe(None, capability_id=cap["id"], target_config=target,
                        source_manifest=source, platform_manifest=platform_manifest, recipe=recipe, matrix_entries=entries,
                        matrix_plan_fingerprint=matrix.fingerprint, matrix_binding_fingerprint=binding["binding_fingerprint"],
                        idempotency_key=f"local-probe:{reference['id']}:{target.target_config_id}:{phase}", limits=limits)
                    _, result = await self._wait_job(job, phase)
                    result_ids.append(result["id"])
                    if phase == "integration":
                        await self.nodes.confirm_functional_capability(cap["id"], result["id"], "local-confirm:" + result["id"])
                        validated[target.app_target.value] = {"capability_id": cap["id"], "result_id": result["id"], "fixture_digest": fixture_digest}
                        await self._update(validated_targets=validated, fixture_digest=fixture_digest, result_ids=result_ids)
            ready = set(await self._ready_targets())
            complete = requested.issubset(ready)
            await self._update(state="ready" if ready == {"api", "web"} else "partial", phase="ready" if complete else "blocked",
                fixture_digest=fixture_digest, result_ids=result_ids,
                capability_ids=list(self._daemon.capability_ids), job_id=None, error_code=None,
                message="所需本机开发与测试环境已通过真实验证" if complete else "部分本机执行环境尚未具备所需条件")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            ready = await self._ready_targets()
            await self._update(state="partial" if ready else "blocked", phase="blocked", error_code=getattr(error, "code", "local_preparation_failed"),
                               message=str(error)[:2000])

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        cancelled_preparation = bool(self._prepare_task and not self._prepare_task.done())
        if cancelled_preparation:
            self._prepare_task.cancel()
            await asyncio.gather(self._prepare_task, return_exceptions=True)
        if self._daemon:
            for entry in self._daemon.journal.recoverable():
                job = await self.store.read("node_job", entry["job_id"])
                if job and job["state"] in {"queued", "leased", "running"}:
                    await self.nodes.cancel_job(job["id"], "Managed local executor stopping", "local-stop:" + job["id"])
        if self._worker_task:
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)
        if self._daemon:
            await self._daemon.runner.executor.sandbox.close()
            await self._daemon.close()
        if cancelled_preparation:
            await self._update(state="not_prepared", phase="stopped", error_code=None,
                               message="本机执行器已停止，后续准备会先核对已有作业")
        if self._server:
            self._server.should_exit = True
        if self._server_task:
            await asyncio.wait_for(self._server_task, 10)
        self._socket.close()

"""Run frozen inputs through actual platform tools and collect immutable evidence."""
from __future__ import annotations

import asyncio
import hashlib
import json
import tarfile
from pathlib import Path

import httpx

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.execution.capabilities import probe_target
from agentflow.execution.manifests import file_digest, service_components, target_artifacts, tree_digest
from agentflow.execution.models import JobKind, JobLimits, TargetConfig, new_id
from agentflow.execution.process import CommandSpec, ProcessExecutor
from agentflow.execution.transport import safe_extract_tar
from agentflow.testing.adapters import BuildRecipe, contained, plan_execution
from agentflow.testing.reports import (
    parse_instrumentation,
    parse_junit,
    parse_playwright,
    parse_xcresult_export,
)
from node_agent.journal import NodeJournal


def pack_artifact(workspace: Path, relative_path: str, destination: Path) -> dict:
    source = contained(workspace, relative_path)
    if not source.exists() or source.is_symlink():
        raise DomainError("build_output_missing", f"Expected built output {relative_path} is absent or linked")
    destination.parent.mkdir(parents=True, exist_ok=True)
    paths = [source, *sorted(source.rglob("*"))] if source.is_dir() else [source]
    rows = []
    with tarfile.open(destination, "w", format=tarfile.PAX_FORMAT) as archive:
        for path in paths:
            actual = path.resolve()
            # npm .bin links may be materialized as regular files only if the referent stays in this package.
            if path.is_symlink() and (not actual.is_relative_to(source) or not actual.is_file()):
                raise DomainError("unsafe_build_output", "Linked output escapes the package or links a directory")
            if not (actual.is_dir() or actual.is_file()):
                raise DomainError("unsafe_build_output", "Built output contains an unapproved link/special file")
            info = archive.gettarinfo(str(actual), arcname=path.relative_to(workspace).as_posix())
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if actual.is_dir() or actual.stat().st_mode & 0o111 else 0o644
            if actual.is_file():
                with actual.open("rb") as stream:
                    archive.addfile(info, stream)
                rows.append({"path": path.relative_to(source).as_posix() if source.is_dir() else source.name,
                             "size": actual.stat().st_size, "digest": file_digest(actual),
                             "executable": bool(actual.stat().st_mode & 0o111)})
            else:
                archive.addfile(info)
    content_digest = canonical_digest(rows) if source.is_dir() else file_digest(source)
    return {"path": str(destination), "digest": file_digest(destination), "content_digest": content_digest,
            "relative_path": relative_path, "size": destination.stat().st_size}


def _materialize(archive: Path, workspace: Path) -> None:
    """Merge independently verified packages, permitting only byte-identical overlap."""
    stage = workspace.parent / f"extract-{new_id()}"
    safe_extract_tar(archive, stage)
    import shutil
    try:
        for p in sorted(stage.rglob("*")):
            target = contained(workspace, p.relative_to(stage).as_posix())
            if p.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif target.exists():
                if target.is_symlink() or file_digest(target) != file_digest(p):
                    raise DomainError("package_collision", "Frozen packages contain conflicting files")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, target)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


class NodeRunner:
    def __init__(self, work_root: Path, journal: NodeJournal, *, trusted_project_mode: bool = False,
                 executor: ProcessExecutor | None = None):
        self.work_root = work_root.resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.journal = journal
        self.trusted_project_mode = trusted_project_mode
        self.executor = executor or ProcessExecutor()

    async def run(self, assignment: dict, input_files: dict[str, Path], cancel: asyncio.Event | None = None,
                  on_process_event=None) -> dict:
        disposition = self.journal.record_assignment(assignment)
        received = self.journal.get(assignment["job_id"])
        if disposition != "start_new" and received["state"] != "received":
            saved = self.journal.get(assignment["job_id"])
            return {"disposition": disposition, "result": saved["result"]}
        job_id = assignment["job_id"]
        # Job identity, not a caller-provided filesystem path, controls the workspace.
        directory = self.work_root / hashlib.sha256(job_id.encode()).hexdigest()
        workspace, evidence = directory / "workspace", directory / "evidence"
        workspace.mkdir(parents=True, exist_ok=True)
        evidence.mkdir(parents=True, exist_ok=True)
        self.journal.mark_starting(job_id)
        target = TargetConfig.model_validate(assignment["target_config"])
        limits = JobLimits.model_validate(assignment["limits"])
        probe = await probe_target(target)
        augment = getattr(self.executor, "augment_capability_report", None)
        if augment:
            probe = augment(probe)
        (evidence / "capability-report.json").write_text(probe.model_dump_json(indent=2))
        artifact_files = [{"path": str(evidence / "capability-report.json"), "format": "capability"}]
        result = {"execution_status": "failed", "quality_result": "unknown", "summary": "",
                  "state": "blocked", "built_artifacts": [], "checks": [], "artifact_files": artifact_files,
                  "process_results": [], "cleanup_verified": True, "finished_at": None,
                  "observed_source_manifest_fingerprint": None, "observed_platform_artifact_manifest_fingerprint": None,
                  "observed_test_package_digest": None, "observed_components": [],
                  "service_observations": [],
                  "observed_environment_fingerprint": probe.environment_fingerprint}
        try:
            if probe.state != "static_verified":
                raise DomainError("environment_blocked", "; ".join(probe.blocking_reasons))
            if assignment["kind"] == "capability_probe" and not assignment.get("recipe"):
                result.update(execution_status="completed", state="static_verified",
                              summary="Static host probe complete; functional platform support remains unverified")
            else:
                if not self.trusted_project_mode and not getattr(self.executor, "supports_verified_isolation", False):
                    raise DomainError("isolation_unverified", "An approved trusted-project mode or verified external runner isolation is required")
                recipe = BuildRecipe.model_validate(assignment.get("recipe"))
                source = assignment.get("source_manifest")
                platform = assignment.get("platform_artifact_manifest")
                if not source:
                    raise DomainError("source_required", "Frozen source input is required")
                if canonical_digest({k: v for k, v in source.items() if k != "fingerprint"}) != source["fingerprint"]:
                    raise DomainError("source_manifest_mismatch", "Source manifest failed its content identity check")
                for part in ("source_bundle", "test_package", "build_plan"):
                    self._input(input_files, source[f"{part}_artifact_version_id"], source[f"{part}_digest"])
                result["observed_source_manifest_fingerprint"] = source["fingerprint"]
                if assignment["kind"] == "build":
                    archive = self._input(input_files, source["source_bundle_artifact_version_id"], source["source_bundle_digest"])
                    _materialize(archive, workspace)
                    result["observed_test_package_digest"] = source["test_package_digest"]
                else:
                    if not platform:
                        raise DomainError("platform_required", "Formal execution requires frozen platform artifacts")
                    if canonical_digest({k: v for k, v in platform.items() if k != "fingerprint"}) != platform["fingerprint"]:
                        raise DomainError("platform_manifest_mismatch", "Platform manifest failed its content identity check")
                    for component in target_artifacts(platform, target):
                        archive = self._input(input_files, component["artifact_version_id"], component["digest"])
                        _materialize(archive, workspace)
                        result["observed_components"].append({"component_id": component.get("component_id", component.get("artifact_id")),
                                                               "actual_digest": file_digest(archive)})
                        if component["artifact_version_id"] == assignment["test_package_artifact_version_id"]:
                            result["observed_test_package_digest"] = file_digest(archive)
                    result["observed_platform_artifact_manifest_fingerprint"] = platform["fingerprint"]
                    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=10) as client:
                        for name, component in service_components(platform, recipe.model_dump()).items():
                            url = recipe.service_urls[name]
                            from urllib.parse import urlsplit
                            parts = urlsplit(url)
                            if parts.scheme not in {"http", "https"} or parts.username or parts.password or parts.query or parts.fragment:
                                raise DomainError("invalid_service_url", "Service bindings require credential-free HTTP(S) origins")
                            async with client.stream("GET", url.rstrip("/") + "/api/version") as response:
                                raw = bytearray()
                                if response.status_code != 200:
                                    raise DomainError("service_identity_unverified", "Bound service did not return its identity")
                                async for block in response.aiter_bytes():
                                    raw.extend(block)
                                    if len(raw) > 65536:
                                        raise DomainError("service_identity_unverified", "Bound service identity exceeded its size limit")
                            observed = json.loads(raw)
                            expected_content = component.get("content_digest") or component.get("metadata", {}).get("content_digest")
                            if observed.get("source") != source["fingerprint"] or observed.get("product_content_digest") != expected_content:
                                raise DomainError("service_identity_mismatch", "External service is not the source and product frozen in this candidate")
                            result["service_observations"].append({"service_name": name, "component_id": component.get("component_id", component.get("artifact_id")),
                                "source_manifest_fingerprint": observed["source"], "product_content_digest": observed["product_content_digest"]})
                effective_kind = JobKind.TEST if assignment["kind"] == "capability_probe" else JobKind(assignment["kind"])
                plan = plan_execution(effective_kind, target, recipe, workspace)
                if plan.report_path:
                    plan.report_path.parent.mkdir(parents=True, exist_ok=True)
                # Frozen packages remain immutable through formal testing, including when frameworks rebuild implicitly.
                before = {}
                if platform:
                    for component in target_artifacts(platform, target):
                        relative = component.get("relative_path") or component.get("metadata", {}).get("relative_path")
                        content_digest = component.get("content_digest") or component.get("metadata", {}).get("content_digest")
                        if not relative or not content_digest:
                            raise DomainError("build_identity_incomplete", "Platform artifact must bind its unpacked content identity")
                        path = contained(workspace, relative)
                        actual = tree_digest(path) if path.is_dir() else file_digest(path)
                        if actual != content_digest:
                            raise DomainError("installed_artifact_mismatch", "Actual unpacked product/test differs from frozen content")
                        before[relative] = actual
                for index, command in enumerate(plan.commands):
                    command.environment["AGENTFLOW_SOURCE_FINGERPRINT"] = source["fingerprint"]
                    if recipe.runtime_port:
                        command.environment["AGENTFLOW_WEB_PORT"] = str(recipe.runtime_port)
                    if target.app_target == "web":
                        browser = next((tool for tool in probe.tools if tool.name == "browser" and tool.available), None)
                        if browser and browser.path:
                            command.environment["AGENTFLOW_BROWSER_EXECUTABLE"] = browser.path
                    for name, value in recipe.scenario_inputs.items():
                        command.environment[f"AGENTFLOW_CROSS_{name.upper()}"] = value
                        command.environment[f"AGENTFLOW_SCENARIO_{name.upper()}"] = value
                    async def on_start(pid, process_fingerprint):
                        self.journal.record_process(job_id, pid, process_fingerprint)
                        if on_process_event:
                            await on_process_event("starting", process_fingerprint)
                    from urllib.parse import urlsplit
                    for service_name, url in recipe.service_urls.items():
                        parsed = urlsplit(url)
                        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                            raise DomainError("invalid_service_url", "Service endpoints must be credential-free HTTP(S) URLs")
                        if service_name not in {"api", "web"}:
                            raise DomainError("invalid_service_name", "Unsupported reference service binding")
                        command.environment[f"AGENTFLOW_{service_name.upper()}_URL"] = url
                    if recipe.product_path:
                        command.environment["AGENTFLOW_APP_PATH"] = str(contained(workspace, recipe.product_path))
                    command_result = await self.executor.execute(command, evidence / f"command-{index}", limits, cancel,
                                                                 on_start=on_start)
                    result["process_results"].append(command_result.model_dump(mode="json"))
                    if on_process_event:
                        await on_process_event("process_exited", command_result.process_fingerprint)
                    result["cleanup_verified"] = result["cleanup_verified"] and command_result.cleanup_verified
                    artifact_files += [{"path": command_result.stdout_path, "format": "log"},
                                       {"path": command_result.stderr_path, "format": "log"}]
                    isolation_report = evidence / f"command-{index}" / "isolation.json"
                    if isolation_report.is_file():
                        artifact_files.append({"path": str(isolation_report), "format": "isolation"})
                    if command_result.execution_status != "completed":
                        if effective_kind == JobKind.TEST and command_result.reason is None and command_result.exit_code is not None:
                            # An assertion failure is still a completed framework run; parse its actual failure report.
                            continue
                        result["execution_status"] = "cancelled" if command_result.execution_status == "cancelled" else "failed"
                        raise DomainError("tool_execution_failed", command_result.reason or f"Tool exit {command_result.exit_code}")
                for relative, expected in before.items():
                    path = contained(workspace, relative)
                    actual = tree_digest(path) if path.is_dir() else file_digest(path)
                    if actual != expected:
                        raise DomainError("frozen_artifact_changed", "Formal test changed/recompiled frozen product or test input")
                if effective_kind == JobKind.BUILD:
                    outputs = recipe.output_paths or {k: v for k, v in {
                        "product": recipe.product_path, "test": recipe.test_product_path}.items() if v}
                    if not {"product", "test"}.issubset(outputs):
                        raise DomainError("missing_build_outputs", "Build recipe must collect product and prebuilt tests")
                    for role, relative in outputs.items():
                        if role not in {"product", "test", "service", "data"}:
                            raise DomainError("invalid_output_role", "Unknown build output role")
                        item = pack_artifact(workspace, relative, evidence / f"built-{role}.tar")
                        result["built_artifacts"].append({**item, "component_id": new_id(), "component_role": role,
                            "kind": {"product": "application", "test": "test_runner", "service": "api_service", "data": "test_data"}[role],
                            "app_target": assignment["app_target"], "target_config_id": target.target_config_id,
                            "source_manifest_fingerprint": source["fingerprint"],
                            "environment_fingerprint": probe.environment_fingerprint, "architecture": probe.architecture})
                        artifact_files.append({"path": item["path"], "format": "build_artifact"})
                    result.update(execution_status="completed", quality_result="passed", state="completed")
                elif effective_kind == JobKind.TEST:
                    raw = plan.report_path
                    if plan.report_format == "instrumentation":
                        raw = Path(result["process_results"][-1]["stdout_path"])
                    elif plan.report_format == "xcresult":
                        export = await self.executor.execute(CommandSpec(argv=("xcrun", "xcresulttool", "get",
                            "test-results", "tests", "--path", str(raw)), cwd=workspace, label="export xcresult test records"),
                            evidence / "xcresult-export", limits, cancel, on_start=on_start)
                        result["process_results"].append(export.model_dump(mode="json"))
                        result["cleanup_verified"] = result["cleanup_verified"] and export.cleanup_verified
                        if on_process_event:
                            await on_process_event("process_exited", export.process_fingerprint)
                        if export.execution_status != "completed":
                            raise DomainError("xcresult_export_failed", "Installed xcresulttool did not produce supported raw JSON")
                        raw = Path(export.stdout_path)
                    parser = {"junit": parse_junit, "playwright": parse_playwright, "xcresult": parse_xcresult_export,
                              "instrumentation": parse_instrumentation}[plan.report_format]
                    if not raw:
                        raise DomainError("missing_report", "Test did not provide a raw framework report")
                    report = parser(raw, set(recipe.expected_case_ids) or None)
                    if report.quality_result == "passed" and any(p["exit_code"] != 0 for p in result["process_results"]):
                        raise DomainError("runner_exit_mismatch", "Runner failed despite an apparently passing test subset")
                    artifact_files.append({"path": str(raw), "format": plan.report_format})
                    result["checks"].append({"report_format": plan.report_format, "raw_path": str(raw),
                                              "normalized_report": report.model_dump(mode="json")})
                    result.update(execution_status="completed" if report.execution_status == "completed" else "failed",
                                  quality_result=report.quality_result, state="completed")
                else:
                    result.update(execution_status="completed", quality_result="passed", state="installed",
                                  summary="Frozen product/test package staged and declared install commands completed")
        except DomainError as exc:
            result.update(summary=f"{exc.code}: {exc.message}", state="blocked" if exc.code in {
                "environment_blocked", "isolation_unverified"} else "failed")
        except Exception as exc:
            result.update(summary=f"node_execution_error: {type(exc).__name__}: {exc}", state="failed")
        result["finished_at"] = utc_now()
        summary = evidence / "execution-receipt.json"
        summary.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        result["artifact_files"].append({"path": str(summary), "format": "execution_receipt"})
        self.journal.record_result(job_id, result)
        return {"disposition": "finished", "result": result}

    @staticmethod
    def _input(files: dict[str, Path], artifact_id: str, digest: str) -> Path:
        path = files.get(artifact_id)
        if not path or file_digest(path) != digest:
            raise DomainError("input_artifact_mismatch", "Downloaded input is missing or has the wrong digest")
        return path

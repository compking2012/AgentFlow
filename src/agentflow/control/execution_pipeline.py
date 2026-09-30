"""Source → platform builds → frozen matrix tests, with no Agent slot held while waiting."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentflow.common import DomainError, canonical_digest
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
from agentflow.repository import RepositoryAdapter
from agentflow.runtime.failures import runtime_failure_code
from agentflow.testing.adapters import BuildRecipe


class TargetRecipes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_config_id: str
    build: BuildRecipe
    install: BuildRecipe | None = None
    unit: BuildRecipe | None = None
    integration: BuildRecipe | None = None

    @model_validator(mode="after")
    def concrete_cases(self):
        if self.unit is None and self.integration is None:
            raise ValueError("At least one execution phase is required")
        if any(not recipe.expected_case_ids for recipe in (self.unit, self.integration) if recipe):
            raise ValueError("Execution recipes require explicit raw framework case IDs")
        if (self.unit and self.unit.test_kind != "unit") or (self.integration and self.integration.test_kind == "unit"):
            raise ValueError("Unit and integration recipes must retain their distinct test kinds")
        return self


class ProjectExecutionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(default=1, ge=1, le=1)
    targets: list[TargetRecipes] = Field(min_length=1)
    cross_scenarios: list[CrossScenarioDefinition] = Field(default_factory=list, max_length=16)


def reference_credentials(reference: str, origin: str) -> dict:
    if reference != "reference-app:manager":
        raise DomainError("reference_credential_missing", "Configure a dedicated application credential resolver")
    return {"audience": "agentflow-reference-application", "origin": origin, "bearer_token": "reference.manager"}


class ExecutionPipeline:
    def __init__(self, store, workflow, nodes, source_resolver):
        self.store, self.workflow, self.nodes = store, workflow, nodes
        self.source_resolver = source_resolver
        self.repository = RepositoryAdapter()
        self.scenarios = CrossScenarioCoordinator(store, nodes, credential_resolver=reference_credentials)

    def _limits(self):
        return {"maximum_output_bytes": self.workflow.settings.node_output_limit_bytes,
                "maximum_active_seconds": self.workflow.settings.node_active_seconds}

    def _archive(self, repository: Path, commit: str, destination: Path) -> None:
        # Read exact Git blobs; export-ignore, filters and a changing worktree cannot alter the source bundle.
        rows = self.repository._run(repository, ["ls-tree", "-r", "-z", commit]).split(b"\0")
        total = 0
        with tarfile.open(destination, "w") as archive:
            for row in rows:
                if not row:
                    continue
                header, path = row.split(b"\t", 1)
                mode, kind, oid = header.split()
                if kind != b"blob" or mode not in {b"100644", b"100755"}:
                    raise DomainError("unsupported_source_entry", "Source archives require regular files; linked/submodule entries need explicit handling")
                name = path.decode("utf-8")
                if name.startswith("/") or ".." in Path(name).parts:
                    raise DomainError("unsafe_source_path", "Invalid source path")
                content = self.repository._run(repository, ["cat-file", "blob", oid.decode()])
                total += len(content)
                if total > 256 * 1024 * 1024:
                    raise DomainError("source_too_large", "Source bundle exceeds the local execution limit", 413)
                entry = tarfile.TarInfo(name)
                entry.mode, entry.mtime, entry.uid, entry.gid = (0o755 if mode == b"100755" else 0o644), 0, 0, 0
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))

    async def begin(self, claim: dict):
        run, work, attempt = claim["run"], claim["work_item"], claim["attempt"]
        if not self.nodes:
            raise DomainError("executor_not_configured", "Configure the authenticated local node ingress and execution environments")
        phase = "unit" if work["step"] == "unit_test_execution" else "integration"
        plan_record = await self.store.read("plan", run["plan_id"])
        phases = [name for name, stage in [("unit", "unit_test_execution"), ("integration", "integration_test_execution")]
                  if stage in plan_record.get("actual_steps", ["unit_test_execution", "integration_test_execution"])]
        candidates = [c for c in await self.store.list("candidate")
                      if c["run_id"] == run["id"] and c["run_input_fingerprint"] == run["input_fingerprint"]]
        if candidates:
            candidate = candidates[0]
        else:
            repository, commit = await self.source_resolver(run, work)
            configs = [TargetConfig.model_validate(c) for c in plan_record.get("target_configs", [])]
            by_id = {c.target_config_id: c for c in configs}
            planned_cases: dict[tuple[str, str], set[str]] = {}
            plan_sources = []
            planning_work = {item['id']: item for item in await self.store.list('work_item')}
            for artifact in await self.store.list("artifact"):
                if ((artifact.get("run_id") != run["id"] and artifact["id"] not in plan_record.get("reused_inputs", [])) or artifact.get("stale")
                        or artifact.get("step") not in {"unit_test_plan", "integration_test_strategy"}
                        or not artifact.get("name", "").endswith(".json")):
                    continue
                content = json.loads(await self.workflow.artifacts.read(artifact["digest"]))
                content = content.get("result", content)
                cases = content.get('test_cases', [])
                if cases:
                    producer = planning_work.get(artifact.get('work_item_id'), {})
                    plan_sources.append({'artifact_id': artifact['id'], 'revision': artifact['revision'],
                        'digest': artifact['digest'], 'generation': artifact.get('generation'),
                        'work_item_id': artifact.get('work_item_id'), 'work_revision': producer.get('revision'),
                        'accepted': producer.get('status') == 'completed'
                            and artifact['id'] in producer.get('artifact_ids', [])
                            and producer.get('generation') == artifact.get('generation')
                            and producer.get('step') == artifact.get('step')})
                for case in cases:
                    planned_cases.setdefault((case["target_config_id"], case["phase"]), set()).update(case["framework_case_ids"])
            try:
                manifest = await asyncio.to_thread(self.repository._run, repository,
                    ['ls-tree', '-z', commit, '--', 'agentflow.project.json'])
                if manifest:
                    if len(list(filter(None, manifest.split(b'\0')))) != 1 or manifest.split(b' ', 2)[0] not in {b'100644', b'100755'}:
                        raise ValueError('manifest_not_regular_file')
                    raw_spec = await asyncio.to_thread(self.repository._run, repository,
                                                       ["show", f"{commit}:agentflow.project.json"])
                    spec = ProjectExecutionSpec.model_validate_json(raw_spec)
                    spec_source = {'kind': 'committed_manifest', 'source_commit': commit,
                                   'spec_fingerprint': canonical_digest(spec.model_dump(mode='json'))}
                else:
                    from agentflow.control.starter_execution import starter_execution_spec
                    generated, spec_source = await starter_execution_spec(self.repository, repository, commit,
                        plan_record, configs, phases, planned_cases, plan_sources)
                    spec = ProjectExecutionSpec.model_validate(generated)
                    spec_source['spec_fingerprint'] = canonical_digest(spec.model_dump(mode='json'))
            except (OSError, ValueError) as exc:
                raise DomainError("execution_plan_missing", "需要有效的已提交执行清单；无效文件不会被自动替换。") from exc
            if run["purpose"] != "code_delivery" and plan_record.get("existing_suite_spec"):
                spec = spec.model_copy(update={"targets": [t for t in spec.targets if t.target_config_id in by_id],
                    "cross_scenarios": [s for s in spec.cross_scenarios if "integration" in phases
                        and {s.backend.target_config_id, *[step.target_config_id for step in s.steps]} <= set(by_id)]})
            if {t.target_config_id for t in spec.targets} != set(by_id) or len(spec.targets) != len(by_id):
                raise DomainError("target_scope_mismatch", "Execution recipes must exactly cover the owner-approved target configurations")
            scenario_cases: dict[str, set[str]] = {}
            if len({s.scenario_id for s in spec.cross_scenarios}) != len(spec.cross_scenarios):
                raise DomainError("duplicate_scenario", "Scenario IDs must be unique within the frozen execution plan")
            for scenario in spec.cross_scenarios:
                if scenario.backend.target_config_id not in by_id:
                    raise DomainError("scenario_backend_missing", "Scenario backend is outside the declared targets")
                for step in scenario.steps:
                    if step.target_config_id not in by_id:
                        raise DomainError("scenario_target_missing", "Scenario client is outside the declared targets")
                    if by_id[step.target_config_id].app_target != step.recipe.adapter:
                        raise DomainError("scenario_adapter_mismatch", "Scenario recipe must match its approved target configuration")
                    scenario_cases.setdefault(step.target_config_id, set()).update(step.recipe.expected_case_ids)
            for target in spec.targets:
                for stage in phases:
                    recipe = getattr(target, stage)
                    if recipe is None:
                        if planned_cases.get((target.target_config_id, stage)):
                            raise DomainError("missing_test_phase", "An execution phase required by the accepted test plan has no recipe")
                        continue
                    actual_cases = set(recipe.expected_case_ids) | (scenario_cases.get(target.target_config_id, set()) if stage == "integration" else set())
                    if plan_record.get("existing_suite_spec") and run["purpose"] != "code_delivery":
                        expected = actual_cases
                    else:
                        expected = planned_cases.get((target.target_config_id, stage), set())
                    if actual_cases != expected:
                        raise DomainError("test_plan_changed", "Test code cannot shrink or replace cases from the accepted independent test plans")
            if any(not any(getattr(target, phase) is not None for phase in phases) for target in spec.targets):
                raise DomainError('missing_test_phase', '每个必选目标都必须保留至少一个已计划的执行阶段。')
            if any(not any(getattr(target, phase) is not None for target in spec.targets) for phase in phases):
                raise DomainError('missing_test_phase', '本轮要求的单元或集成阶段不能成为空检查。')
            entries, mappings = [], {}
            for target in spec.targets:
                config = by_id[target.target_config_id]
                if any(recipe.adapter != config.app_target for recipe in [target.build, target.install, target.unit, target.integration] if recipe):
                    raise DomainError("adapter_mismatch", "Recipe adapter differs from its declared application target")
                for stage in phases:
                    if getattr(target, stage) is None:
                        continue
                    identity = str(uuid5(NAMESPACE_URL, f"{run['id']}:{target.target_config_id}:{stage}"))
                    recipe = getattr(target, stage)
                    entries.append(MatrixPlanEntry(matrix_entry_id=identity, test_case_id=identity,
                        app_target=config.app_target, component_roles=["product", "test"],
                        target_config_id=config.target_config_id, target_config_revision=config.revision))
                    mappings[identity] = {"matrix_entry_id": identity, "test_case_id": identity,
                        "phase": stage, "target_config_id": config.target_config_id,
                        "framework_case_ids": recipe.expected_case_ids}
            for scenario in spec.cross_scenarios:
                for step in scenario.steps:
                    if step.matrix_entry_id in mappings:
                        raise DomainError("duplicate_scenario_entry", "Every scenario step needs a unique frozen matrix entry")
                    config = by_id[step.target_config_id]
                    entries.append(MatrixPlanEntry(matrix_entry_id=step.matrix_entry_id, test_case_id=step.matrix_entry_id,
                        app_target=config.app_target, component_roles=["product", "test"],
                        target_config_id=config.target_config_id, target_config_revision=config.revision))
                    mappings[step.matrix_entry_id] = {"matrix_entry_id": step.matrix_entry_id,
                        "test_case_id": step.matrix_entry_id, "phase": "integration", "scenario_id": scenario.scenario_id,
                        "target_config_id": config.target_config_id, "framework_case_ids": step.recipe.expected_case_ids}
            matrix = MatrixPlan(required_app_targets=sorted({c.app_target for c in configs}), target_configs=configs, entries=entries)
            candidate_id = str(uuid4())
            directory = self.workflow.settings.data_dir / "candidates" / candidate_id
            directory.mkdir(parents=True, mode=0o700)
            source_archive = directory / "source.tar"
            await asyncio.to_thread(self._archive, repository, commit, source_archive)
            source_blob = await self.nodes.import_input(source_archive, "source.tar", run["id"])
            build_blob = await self.nodes.import_input(spec.model_dump_json().encode(), "execution-plan.json", run["id"])
            tree = (await asyncio.to_thread(self.repository._run, repository, ["rev-parse", f"{commit}^{{tree}}"])) .decode().strip()
            source = SourceManifest(source_commit=commit, source_tree_oid=tree,
                source_bundle_artifact_version_id=source_blob["id"], source_bundle_digest=source_blob["digest"],
                test_package_artifact_version_id=source_blob["id"], test_package_digest=source_blob["digest"],
                build_plan_artifact_version_id=build_blob["id"], build_plan_digest=build_blob["digest"],
                target_matrix_fingerprint=matrix.fingerprint, required_app_targets=tuple(matrix.required_app_targets))
            payload = {"run_id": run["id"], "run_input_fingerprint": run["input_fingerprint"],
                "source_repository": str(repository), "source_commit": commit, "tree_oid": tree,
                "source_manifest": {**source.model_dump(mode="json"), "fingerprint": source.fingerprint},
                "matrix_plan": matrix.model_dump(mode="json"), "matrix_mappings": mappings,
                "recipes": spec.model_dump(mode="json"), "platform_manifest": None, "matrix_binding": None,
                'execution_spec_source': spec_source,
                "state": "source_frozen", "fingerprint": source.fingerprint, "build_job_ids": [],
                "phase_jobs": {}, "required_review_ids": [], "required_approval_ids": []}
            def save(tx):
                current = tx.get("work_item", work["id"])
                if current["attempt_id"] != attempt["id"] or current["status"] != "running":
                    raise DomainError("stale_candidate", "Work changed before source freeze")
                if spec_source['kind'] == 'verified_builtin_node_web_api':
                    latest_plan = tx.get('plan', plan_record['id'])
                    if not latest_plan or latest_plan['revision'] != plan_record['revision']:
                        raise DomainError('stale_candidate', '独立测试计划配置在冻结期间发生变化。')
                    for source in plan_sources:
                        artifact = tx.get('artifact', source['artifact_id'])
                        producer = tx.get('work_item', source['work_item_id'])
                        if (not artifact or artifact['revision'] != source['revision'] or artifact.get('stale')
                                or not producer or producer['revision'] != source['work_revision']
                                or producer.get('status') != 'completed'):
                            raise DomainError('stale_candidate', '已接受测试计划在冻结期间发生变化，未生成候选。')
                row = tx.put("candidate", candidate_id, payload)
                tx.put("target_matrix", candidate_id, {"run_id": run["id"], "plan": matrix.model_dump(mode="json"),
                    "plan_fingerprint": matrix.fingerprint, "state": "planned", "required_count": len(entries)})
                tx.event("candidate.source_frozen", {"candidate_id": candidate_id, "source_commit": commit}, run_id=run["id"])
                return row
            candidate = await self.store.command("candidate.freeze", f"{run['id']}:{run['input_fingerprint']}", payload, save)

        def waiting(tx):
            current = tx.get("work_item", work["id"])
            if current["attempt_id"] != attempt["id"]:
                raise DomainError("stale_execution", "Attempt changed before dispatch")
            tx.put("work_item", work["id"], {**current, "status": "waiting_execution",
                "candidate_id": candidate["id"], "execution_phase": phase}, current["revision"])
            old = tx.get("attempt", attempt["id"])
            tx.put("attempt", old["id"], {**old, "status": "waiting_execution"}, old["revision"])
            tx.event("execution.waiting", {"work_item_id": work["id"], "candidate_id": candidate["id"], "phase": phase}, run_id=run["id"])
            return {"candidate_id": candidate["id"]}
        await self.store.command("execution.begin", attempt["id"], {"candidate_id": candidate["id"], "phase": phase}, waiting)
        await self.advance(work["id"])

    async def reconcile(self):
        for item in await self.store.list("work_item"):
            if item["status"] in {"waiting_execution", "cancel_requested"} and item.get("candidate_id"):
                try:
                    await self.advance(item["id"])
                except DomainError as exc:
                    code = 'test_not_passed' if exc.code in {'test_not_passed', 'invalid_evidence'} else runtime_failure_code(exc.code)
                    await self.workflow.block_attempt(item["attempt_id"], exc.message, str(uuid4()), failure_code=code)

    async def advance(self, work_id: str):
        work = await self.store.read("work_item", work_id)
        run = await self.store.read("run", work["run_id"])
        candidate = await self.store.read("candidate", work["candidate_id"])
        if run["input_fingerprint"] != candidate["run_input_fingerprint"]:
            raise DomainError("stale_candidate", "Candidate is not for the current run inputs")
        if run["execution_state"] == "paused":
            return
        all_job_ids = candidate["build_job_ids"] + [j for group in candidate["phase_jobs"].values() for j in group]
        if work["status"] == "cancel_requested" or run["execution_state"] == "cancelling":
            scenes = [s for s in await self.store.list("cross_scenario") if s.get("parent_work_item_id") == work["id"]]
            for scene in scenes:
                await self.scenarios.advance(scene["id"])
            for job_id in all_job_ids:
                job = await self.store.read("node_job", job_id)
                if job["state"] not in {"completed", "failed", "cancelled"}:
                    await self.nodes.cancel_job(job_id, "Parent run cancelled", f"parent-cancel:{job_id}")
            observed = [await self.store.read("node_job", j) for j in all_job_ids]
            scene_state = [await self.store.read("cross_scenario", s["id"]) for s in scenes]
            if (any(j["state"] in {"leased", "running", "stopping", "execution_unknown"} for j in observed)
                    or any(s["status"] in {"cancelling", "execution_unknown"} for s in scene_state)):
                return
            await self._finish(work, [], cancelled=True)
            return
        matrix = MatrixPlan.model_validate(candidate["matrix_plan"])
        source = SourceManifest.model_validate({k: v for k, v in candidate["source_manifest"].items() if k != "fingerprint"})
        spec = ProjectExecutionSpec.model_validate(candidate["recipes"])
        configs = {c.target_config_id: c for c in matrix.target_configs}
        if not candidate["build_job_ids"]:
            jobs = []
            for target in spec.targets:
                config = configs[target.target_config_id]
                job = await self.nodes.enqueue_job(run["id"], kind="build", target_config=config,
                    source_manifest=candidate["source_manifest"], recipe=target.build,
                    limits=self._limits(),
                    parent_work_item_id=work["id"], required_resource_ids=config.required_resource_ids,
                    idempotency_key=f"build:{candidate['id']}:{config.target_config_id}")
                jobs.append(job["id"])
            candidate = await self._update(candidate, {"build_job_ids": jobs, "state": "building"}, "builds")
        builds = [await self.store.read("node_job", j) for j in candidate["build_job_ids"]]
        if await self._drain_failed_group(builds):
            return
        if any(j["state"] in {"failed", "execution_unknown", "cancelled"} for j in builds):
            raise DomainError("build_not_passed", "A required platform build failed or is unknown; inspect its evidence")
        if any(j["state"] != "completed" for j in builds):
            return
        if not candidate["platform_manifest"]:
            artifacts = []
            for job in builds:
                result = await self.store.read("node_result", job["result_id"])
                if result["assessment_state"] != "validated" or job["quality_result"] != "passed":
                    raise DomainError("unverified_build", "Required build does not have validated output")
                for claim in result["verified_build_artifacts"]:
                    artifacts.append(BuildArtifact(artifact_id=claim["component_id"],
                        artifact_version_id=claim["artifact_version_id"], app_target=job["app_target"],
                        target_config_id=job["target_config"]["target_config_id"], component_role=claim["component_role"],
                        kind={"application": "product", "test_runner": "test", "api_service": "service", "test_data": "data"}[claim["kind"]],
                        digest=claim["digest"], source_manifest_fingerprint=source.fingerprint,
                        toolchain_fingerprint=claim["environment_fingerprint"], verified_upload=True,
                        metadata={"relative_path": claim["relative_path"], "content_digest": claim["content_digest"]}))
            platform = freeze_platform_manifest(source, matrix, artifacts)
            binding = bind_matrix(matrix, source, platform, {entry.matrix_entry_id: {} for entry in matrix.entries})
            candidate = await self._update(candidate, {"platform_manifest": {**platform.model_dump(mode="json"), "fingerprint": platform.fingerprint},
                "matrix_binding": binding, "state": "platform_artifacts_frozen", "fingerprint": platform.fingerprint}, "platform")
        if "install" not in candidate["phase_jobs"]:
            installations = []
            for target in spec.targets:
                if target.install is None:
                    continue
                config = configs[target.target_config_id]
                job = await self.nodes.enqueue_job(run["id"], kind="install", target_config=config,
                    source_manifest=candidate["source_manifest"], platform_manifest=candidate["platform_manifest"],
                    recipe=target.install, matrix_plan_fingerprint=matrix.fingerprint,
                    limits=self._limits(),
                    matrix_binding_fingerprint=candidate["matrix_binding"]["binding_fingerprint"],
                    required_resource_ids=config.required_resource_ids, parent_work_item_id=work["id"],
                    idempotency_key=f"install:{candidate['id']}:{config.target_config_id}")
                installations.append(job["id"])
            candidate = await self._update(candidate, {"phase_jobs": {**candidate["phase_jobs"], "install": installations}}, "install")
        installations = [await self.store.read("node_job", identity) for identity in candidate["phase_jobs"]["install"]]
        if await self._drain_failed_group(installations):
            return
        if any(job["state"] in {"failed", "execution_unknown", "cancelled"} for job in installations):
            raise DomainError("installation_failed", "A required native installation did not complete")
        if any(job["state"] != "completed" for job in installations):
            return
        phase = work["execution_phase"]
        if phase not in candidate["phase_jobs"]:
            jobs = []
            for target in spec.targets:
                config = configs[target.target_config_id]
                selected = [m for m in candidate["matrix_mappings"].values()
                            if m["phase"] == phase and m["target_config_id"] == config.target_config_id and not m.get("scenario_id")]
                if getattr(target, phase) is None:
                    if selected:
                        raise DomainError('missing_test_phase', '冻结矩阵仍要求此阶段，不能省略执行。')
                    continue
                job = await self.nodes.enqueue_job(run["id"], kind="test", target_config=config,
                    source_manifest=candidate["source_manifest"], platform_manifest=candidate["platform_manifest"],
                    recipe=getattr(target, phase), matrix_entry_ids=[m["matrix_entry_id"] for m in selected],
                    limits=self._limits(),
                    matrix_entries=selected, matrix_plan_fingerprint=matrix.fingerprint,
                    matrix_binding_fingerprint=candidate["matrix_binding"]["binding_fingerprint"],
                    parent_work_item_id=work["id"], required_resource_ids=config.required_resource_ids,
                    idempotency_key=f"test:{candidate['id']}:{phase}:{config.target_config_id}")
                jobs.append(job["id"])
            candidate = await self._update(candidate, {"phase_jobs": {**candidate["phase_jobs"], phase: jobs}, "state": "testing"}, phase)
        jobs = [await self.store.read("node_job", j) for j in candidate["phase_jobs"][phase]]
        reported_failures = set()
        for job in jobs:
            if job['state'] != 'failed' or not job.get('result_id'):
                continue
            result = await self.store.read('node_result', job['result_id'])
            verified = (result or {}).get('verified_checks', [])
            if (result and result.get('job_id') == job['id'] and result.get('assessment_state') == 'validated'
                    and verified and any(c['normalized_report']['quality_result'] != 'passed' for c in verified)):
                reported_failures.add(job['id'])
        unresolved = [job for job in jobs if job['id'] not in reported_failures]
        if await self._drain_failed_group(unresolved):
            return
        if any(j["state"] in {"failed", "execution_unknown", "cancelled"} for j in unresolved):
            raise DomainError("test_not_passed", "A required platform test failed or is unknown; inspect original reports")
        if any(j["state"] != "completed" for j in unresolved):
            return
        scene_checks = []
        if phase == "integration" and all(j["quality_result"] == "passed" for j in jobs):
            for definition in spec.cross_scenarios:
                scene = await self.scenarios.start(run, candidate, definition,
                    f"scene:{candidate['id']}:{definition.scenario_id}", parent_work_item_id=work["id"])
                if scene["status"] in {"failed", "execution_unknown", "cancelled"}:
                    raise DomainError("cross_scenario_failed", scene["blocking_reason"] or "Required cross-platform scenario did not pass")
                if scene["status"] != "completed":
                    return
                scene_checks.extend(scene["check_ids"])
        await self._finish(work, jobs, extra_check_ids=scene_checks)

    async def _drain_failed_group(self, jobs):
        if not any(j["state"] in {"failed", "execution_unknown", "cancelled"} for j in jobs):
            return False
        pending = [j for j in jobs if j["state"] in {"queued", "leased", "running", "stopping"}]
        for job in pending:
            await self.nodes.cancel_job(job["id"], "A required peer job failed", f"peer-failure:{job['id']}")
        return bool(pending)

    async def _update(self, candidate, changes, action):
        def update(tx):
            current = tx.get("candidate", candidate["id"])
            value = tx.put("candidate", current["id"], {**current, **changes}, candidate["revision"])
            matrix = tx.get("target_matrix", candidate["id"])
            if matrix and changes.get("platform_manifest"):
                tx.put("target_matrix", matrix["id"], {**matrix, "state": "bound_to_platform_manifest",
                    "binding": changes["matrix_binding"], "candidate_fingerprint": changes["fingerprint"]}, matrix["revision"])
            tx.event("candidate.updated", {"candidate_id": value["id"], "state": value["state"]}, run_id=value["run_id"])
            return value
        return await self.store.command("candidate.update", f"{candidate['id']}:{action}", changes, update)

    async def _finish(self, work, jobs, cancelled=False, extra_check_ids=()):
        candidate = await self.store.read("candidate", work["candidate_id"])
        checks = []
        incomplete = False
        for job in jobs:
            result = await self.store.read("node_result", job["result_id"])
            if result["assessment_state"] != "validated":
                raise DomainError("invalid_evidence", "Node result did not pass controller verification")
            for check in result["verified_checks"]:
                normalized = check["normalized_report"]
                incomplete |= normalized['execution_status'] != 'completed' or bool(normalized.get('missing_case_ids'))
                entry = candidate["matrix_mappings"][check["matrix_entry_id"]]
                checks.append({"execution_key": execution_key(entry["test_case_id"], entry["target_config_id"], candidate["fingerprint"]),
                    "candidate_fingerprint": candidate["fingerprint"], "evidence_verified": True,
                    "execution_status": normalized["execution_status"], "quality_result": normalized["quality_result"],
                    "executed_case_count": len(normalized.get("cases", [])), "assertion_count": None,
                    "assertion_count_status": "not_reported_by_framework", "framework_case_evidence": True,
                    "raw_report_artifact_id": check["raw_report_artifact_version_id"], "node_result_id": result["id"],
                    "work_item_id": work["id"], "run_id": work["run_id"], "matrix_entry_id": entry["matrix_entry_id"]})
        record_ids = []
        def save(tx):
            for check in checks:
                identity = str(uuid5(NAMESPACE_URL, f"{work['attempt_id']}:{check['execution_key']}"))
                tx.put("check", identity, check)
                record_ids.append(identity)
            return {"check_ids": record_ids}
        result = await self.store.command("checks.collect", work["attempt_id"], {"checks": checks}, save)
        for check_id in extra_check_ids:
            check = await self.store.read("check", check_id)
            if (not check or check.get("candidate_fingerprint") != candidate["fingerprint"]
                    or check.get("run_id") != work["run_id"] or not check.get("evidence_verified")):
                raise DomainError("invalid_scenario_evidence", "Scenario check does not match the current frozen candidate")
            checks.append(check)
        report = await self.workflow.artifacts.put_bytes(json.dumps({"checks": checks}, ensure_ascii=False).encode())
        quality = ("failed" if any(c["quality_result"] == "failed" for c in checks)
                   else "passed" if checks and all(c["quality_result"] == "passed" for c in checks) else "unknown")
        await self.workflow.finish_attempt(work["attempt_id"], {"fencing_token": work["fencing_token"],
            "input_fingerprint": work["input_fingerprint"], "execution_status": "cancelled" if cancelled else "completed",
            "quality_result": quality, 'runtime_failure_code': 'test_incomplete' if incomplete else 'test_failed' if quality == 'failed' else None,
            "verified_check_ids": result["check_ids"] + list(extra_check_ids)}, f"node-finish:{work['attempt_id']}",
            verified_artifacts=[{"digest": report["id"], "name": "platform-results.json", "media_type": "application/json"}])

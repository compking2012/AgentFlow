"""Controller integration: real Store/Git/files/NodeService, injected node receipts.

The receipt helpers explicitly stand at NodeService's already-validated-result
boundary. They do not execute a native toolchain or establish platform support.
Raw JUnit files are nevertheless parsed by the production report parser.
"""

from __future__ import annotations

import asyncio
import json
import tarfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from xml.sax.saxutils import quoteattr

import pytest

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.execution_pipeline import ExecutionPipeline
from agentflow.control.scheduler import Scheduler
from agentflow.control.service import WorkflowService
from agentflow.execution.manifests import execution_key
from agentflow.execution.models import TargetConfig
from agentflow.execution.service import NodeService
from agentflow.repository import RepositoryAdapter
from agentflow.settings import Settings
from agentflow.storage import LocalArtifactStore, Store
from agentflow.testing.reports import parse_junit


def cross_scene_spec(spec):
    spec["cross_scenarios"] = [{"scenario_id": "controller-glue-fixture", "backend": {
        "origin": "http://127.0.0.1:43210", "target_config_id": "target-0", "preconfigured": True,
        "credential_ref": "reference-app:manager"}, "steps": [{"step_id": name,
            "target_config_id": f"target-{index}", "matrix_entry_id": f"scene-entry-{index}",
            "recipe": {"adapter": target, "test_kind": "integration", "expected_case_ids": [f"cross::{index}"]}}
            for index, (name, target) in enumerate(zip(
                ["android_assign", "linux_assign", "web_verify"], ["android_native", "linux_native", "web"], strict=True), 1)]}]


def cross_scene_plan(plans):
    for index in range(1, 4):
        plans["integration"][index]["framework_case_ids"].append(f"cross::{index}")


@dataclass
class PipelineFixture:
    pipeline: ExecutionPipeline
    workflow: WorkflowService
    store: Store
    nodes: NodeService
    settings: Settings
    repository: RepositoryAdapter
    source: Path
    snapshot: dict
    configs: list[TargetConfig]
    run_id: str = "run"

    async def claim(self, key=None):
        return await self.workflow.claim_next(self.run_id, "controller-fixture", key or str(uuid4()))

    async def candidate(self):
        values = [c for c in await self.store.list("candidate") if c["run_id"] == self.run_id]
        assert len(values) == 1
        return values[0]

    async def build_receipt(self, job_id, *, state="completed", quality="passed", assessment="validated",
                            omit_test_artifact=False):
        job = await self.store.read("node_job", job_id)
        claims = []
        for role, kind in [("product", "application"), ("test", "test_runner")]:
            if omit_test_artifact and role == "test":
                continue
            content = f"controller receipt fixture: {job_id}/{role}".encode()
            artifact = await self.nodes.import_input(content, f"{role}.tar", self.run_id)
            claims.append({"component_id": f"{job_id}:{role}", "artifact_version_id": artifact["id"],
                "component_role": role, "kind": kind, "digest": artifact["digest"],
                "environment_fingerprint": canonical_digest({"fixture-environment": job["target_config"]}),
                "relative_path": f"build/{role}.tar", "content_digest": artifact["digest"],
                "source_manifest_fingerprint": job["source_manifest"]["fingerprint"]})
        await self._receipt(job_id, state, quality, assessment, builds=claims)

    async def test_receipt(self, job_id, *, fail=False, assessment="validated"):
        job = await self.store.read("node_job", job_id)
        checks = []
        for entry in job["matrix_entries"]:
            cases = "".join(f"<testcase name={quoteattr(case)} fullname={quoteattr(case)}>{'<failure>fixture assertion</failure>' if fail and i == 0 else ''}</testcase>"
                            for i, case in enumerate(entry["framework_case_ids"]))
            report = await self.nodes.import_input(f"<testsuite>{cases}</testsuite>".encode(), "junit.xml", self.run_id)
            parsed = parse_junit(self.nodes.artifacts.object_path(report["digest"]), set(entry["framework_case_ids"]))
            checks.append({"matrix_entry_id": entry["matrix_entry_id"], "raw_report_artifact_version_id": report["id"],
                "normalized_report": parsed.model_dump(mode="json")})
        await self._receipt(job_id, "completed", "failed" if fail else "passed", assessment, checks=checks)

    async def _receipt(self, job_id, state, quality, assessment, *, builds=(), checks=()):
        result_id = str(uuid4())
        def inject(tx):
            job = tx.get("node_job", job_id)
            tx.put("node_result", result_id, {"job_id": job_id, "node_id": "controller-fixture-only",
                "assessment_state": assessment, "errors": [] if assessment == "validated" else ["fixture_rejected"],
                "verified_build_artifacts": list(builds), "verified_checks": list(checks),
                "received_at": utc_now(), "fixture_scope": "controller_integration_not_native_execution"})
            return tx.put("node_job", job_id, {**job, "state": state, "quality_result": quality,
                "result_id": result_id}, job["revision"])
        await self.store.command("fixture.node-result", result_id, {}, inject)

    async def finish_builds(self):
        for job_id in (await self.candidate())["build_job_ids"]:
            await self.build_receipt(job_id)
        await self.pipeline.reconcile()


@asynccontextmanager
async def fixture(tmp: Path, *, app_targets=("api", "web"), spec_change=None, plan_change=None,
                  agent_concurrency=2):
    settings = Settings(data_dir=tmp / "data", agent_concurrency=agent_concurrency)
    store = Store(settings.data_dir)
    await store.start()
    try:
        artifacts = LocalArtifactStore(settings.data_dir / "artifacts")
        workflow = WorkflowService(store, artifacts, settings)
        project = await workflow.create_project({"name": "Pipeline controller integration fixture",
            "local_path": str(tmp / "repo"), "import_mode": "initialize_managed",
            "dirty_worktree_policy": "require_clean"}, "project")
        repository = RepositoryAdapter()
        source = tmp / "source"
        await repository.clone_snapshot(Path(project["local_path"]), source, project["base_commit"])
        configs = [TargetConfig(target_config_id=f"target-{index}", app_target=target, os_name="Linux",
            os_version_constraint="24.04", cpu_architecture="x86_64",
            required_display_protocol="x11" if target == "linux_native" else "not_required",
            required_device_mode="not_required", ui_framework="fixture-only" if target.endswith("_native") else None)
            for index, target in enumerate(app_targets)]
        spec = {"schema_version": 1, "targets": [{"target_config_id": config.target_config_id,
            "build": {"adapter": config.app_target.value, "output_paths": {"product": "build/app", "test": "build/tests"}},
            "unit": {"adapter": config.app_target.value, "test_kind": "unit", "unit_project": "build/tests/unit.mjs",
                "expected_case_ids": [f"{config.target_config_id}::unit::normal", f"{config.target_config_id}::unit::denied"]},
            "integration": {"adapter": config.app_target.value, "test_kind": "integration",
                "expected_case_ids": [f"{config.target_config_id}::integration::persists"]}} for config in configs]}
        plan_cases = {phase: [{"case_id": f"requirement-{i}-{phase}", "requirement_id": f"requirement-{i}",
            "target_config_id": target["target_config_id"], "phase": phase,
            "framework_case_ids": list(target[phase]["expected_case_ids"])} for i, target in enumerate(spec["targets"])]
            for phase in ("unit", "integration")}
        if spec_change:
            spec_change(spec)
        if plan_change:
            plan_change(plan_cases)
        (source / "agentflow.project.json").write_text(json.dumps(spec))
        (source / "feature.txt").write_text("frozen implementation\n")
        (source / "export-ignored.txt").write_text("still part of the exact commit\n")
        (source / ".gitattributes").write_text("export-ignored.txt export-ignore\n")
        executable = source / "run.sh"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        snapshot = await repository.freeze_workspace(source, project["base_commit"], "frozen pipeline fixture")
        blobs = {phase: await artifacts.put_bytes(json.dumps({"result": {"test_cases": cases}}).encode())
                 for phase, cases in plan_cases.items()}
        fingerprint = canonical_digest({"controller-fixture": 1})
        def seed(tx):
            tx.put("plan", "plan", {"project_id": project["id"], "state": "started",
                "target_configs": [c.model_dump(mode="json") for c in configs], "app_targets": list(app_targets),
                "actual_steps": ["unit_test_execution", "integration_test_execution"]})
            tx.put("run", "run", {"run_id": "run", "plan_id": "plan", "project_id": project["id"],
                "iteration_id": "iteration", "goal": "controller integration fixture", "purpose": "diagnostic",
                "execution_state": "running", "quality_result": "unknown", "input_fingerprint": fingerprint,
                "delivery_ids": [], "blocking_reasons": []})
            common = {"run_id": "run", "project_id": project["id"], "generation": 1, "required": True,
                "quality_result": "passed", "input_fingerprint": fingerprint, "policy_fingerprint": fingerprint,
                "fencing_token": 0, "write_paths": [], "approval_required": False, "artifact_ids": [], "attempt_id": None}
            tx.put("work_item", "code-work", {**common, "step": "integration_test_implementation",
                "role": "integration_test", "status": "completed", "dependencies": [],
                "attempt_id": "code-snapshot", "fencing_token": 1})
            tx.put("attempt", "code-snapshot", {"run_id": "run", "iteration_id": "iteration", "work_item_id": "code-work",
                "generation": 1, "fencing_token": 1, "input_fingerprint": fingerprint, "status": "completed",
                "fixture_scope": "controller_integration_not_agent_execution"})
            tx.put("code_snapshot", "code-snapshot", {"run_id": "run", "work_item_id": "code-work", "generation": 1,
                "repository_path": str(source), "commit_oid": snapshot["commit_oid"], "tree_oid": snapshot["tree_oid"],
                "base_oid": snapshot["base_oid"], "stale": False})
            for phase, step in [("unit", "unit_test_plan"), ("integration", "integration_test_strategy")]:
                artifact_id = f"{phase}-plan-artifact"
                tx.put("artifact", artifact_id, {"run_id": "run", "work_item_id": f"{phase}-plan-work",
                    "step": step, "name": f"{phase}-plan.json", "media_type": "application/json",
                    "digest": blobs[phase]["id"], "stale": False, "generation": 1})
                tx.put("work_item", f"{phase}-plan-work", {**common, "step": step, "role": f"{phase}_test",
                    "status": "completed", "dependencies": [], "artifact_ids": [artifact_id]})
            tx.put("work_item", "unit-work", {**common, "step": "unit_test_execution", "role": "unit_test",
                "status": "pending", "quality_result": "unknown",
                "dependencies": ["code-work", "unit-plan-work", "integration-plan-work"]})
            tx.put("work_item", "integration-work", {**common, "step": "integration_test_execution", "role": "integration_test",
                "status": "pending", "quality_result": "unknown", "dependencies": ["unit-work"]})
            return {}
        await store.command("fixture.pipeline", "seed", {}, seed)
        nodes = NodeService(store, settings.data_dir, "https://127.0.0.1:9443")
        async def resolve_source(_run, _work):
            return source, snapshot["commit_oid"]
        pipeline = ExecutionPipeline(store, workflow, nodes, resolve_source)
        yield PipelineFixture(pipeline, workflow, store, nodes, settings, repository, source, snapshot, configs)
    finally:
        await store.close()


async def test_source_archive_reads_exact_commit_blobs_despite_dirty_worktree_and_export_ignore(tmp_path):
    async with fixture(tmp_path) as env:
        (env.source / "feature.txt").write_text("later uncommitted replacement\n")
        (env.source / "untracked-secret.txt").write_text("must not be archived\n")
        (env.source / "agentflow.project.json").write_text("not valid JSON in later worktree")
        await env.pipeline.begin(await env.claim())
        candidate = await env.candidate()
        assert candidate["source_commit"] == env.snapshot["commit_oid"]
        assert candidate["source_manifest"]["source_tree_oid"] == env.snapshot["tree_oid"]
        artifact = await env.store.read("node_artifact", candidate["source_manifest"]["source_bundle_artifact_version_id"])
        with tarfile.open(env.nodes.artifacts.object_path(artifact["digest"])) as archive:
            assert archive.extractfile("feature.txt").read() == b"frozen implementation\n"
            assert archive.extractfile("export-ignored.txt").read() == b"still part of the exact commit\n"
            assert archive.getmember("run.sh").mode == 0o755
            assert "untracked-secret.txt" not in archive.getnames()
            assert all(member.mtime == 0 and member.uid == 0 and member.gid == 0 for member in archive.getmembers())
            assert json.load(archive.extractfile("agentflow.project.json"))["schema_version"] == 1
        assert (await env.workflow.run_detail("run"))["active_attempt_count"] == 0


@pytest.mark.parametrize("change", ["remove_case", "replace_case", "add_case"])
async def test_implementation_cannot_shrink_replace_or_extend_the_independent_case_plan(tmp_path, change):
    def mutate(spec):
        cases = spec["targets"][0]["unit"]["expected_case_ids"]
        if change == "remove_case":
            cases.pop()
        elif change == "replace_case":
            cases[-1] = "unplanned::replacement"
        else:
            cases.append("unplanned::extra")
    async with fixture(tmp_path, spec_change=mutate) as env:
        with pytest.raises(DomainError) as caught:
            await env.pipeline.begin(await env.claim())
        assert caught.value.code == "test_plan_changed"
        assert await env.store.list("candidate") == []
        assert await env.store.list("node_job") == []


@pytest.mark.parametrize("change", ["missing_target", "duplicate_target", "extra_target"])
async def test_execution_spec_must_cover_every_exact_owner_target_configuration(tmp_path, change):
    def mutate(spec):
        if change == "missing_target":
            spec["targets"].pop()
        else:
            duplicate = json.loads(json.dumps(spec["targets"][0]))
            if change == "extra_target":
                duplicate["target_config_id"] = "unapproved-target"
            spec["targets"].append(duplicate)
    async with fixture(tmp_path, spec_change=mutate) as env:
        with pytest.raises(DomainError) as caught:
            await env.pipeline.begin(await env.claim())
        assert caught.value.code == "target_scope_mismatch"
        assert await env.store.list("node_job") == []


async def test_stale_independent_test_plan_cannot_authorize_frozen_cases(tmp_path):
    async with fixture(tmp_path) as env:
        def mark_stale(tx):
            old = tx.get("artifact", "unit-plan-artifact")
            return tx.put("artifact", old["id"], {**old, "stale": True}, old["revision"])
        await env.store.command("fixture.stale-plan", "mark", {}, mark_stale)
        with pytest.raises(DomainError) as caught:
            await env.pipeline.begin(await env.claim())
        assert caught.value.code == "test_plan_changed"
        assert await env.store.list("node_job") == []


async def test_recipe_cannot_substitute_a_different_application_adapter(tmp_path):
    def change(spec):
        spec["targets"][0]["unit"]["adapter"] = "web"
    async with fixture(tmp_path, app_targets=("api",), spec_change=change) as env:
        with pytest.raises(DomainError) as caught:
            await env.pipeline.begin(await env.claim())
        assert caught.value.code == "adapter_mismatch"
        assert await env.store.list("node_job") == []


async def test_source_build_freeze_unit_integration_gate_and_repeated_reconcile(tmp_path):
    async with fixture(tmp_path) as env:
        unit_claim = await env.claim()
        await env.pipeline.begin(unit_claim)
        candidate = await env.candidate()
        assert len(candidate["build_job_ids"]) == 2
        assert candidate["platform_manifest"] is None and candidate["phase_jobs"] == {}
        assert (await env.store.read("work_item", "unit-work"))["status"] == "waiting_execution"
        assert (await env.claim())["attempt"] is None
        await env.pipeline.begin(unit_claim)
        await env.pipeline.reconcile()
        assert len(await env.store.list("node_job")) == 2
        await env.build_receipt(candidate["build_job_ids"][0])
        await env.pipeline.reconcile()
        assert (await env.candidate())["platform_manifest"] is None
        await env.build_receipt(candidate["build_job_ids"][1])
        await env.pipeline.reconcile()
        candidate = await env.candidate()
        assert candidate["platform_manifest"]["source_manifest"]["fingerprint"] == candidate["source_manifest"]["fingerprint"]
        assert candidate["fingerprint"] == candidate["platform_manifest"]["fingerprint"]
        matrix = await env.store.read("target_matrix", candidate["id"])
        assert matrix["candidate_fingerprint"] == candidate["fingerprint"]
        assert matrix["state"] == "bound_to_platform_manifest"
        assert set(candidate["phase_jobs"]) - {"install"} == {"unit"}
        assert candidate["phase_jobs"].get("install", []) == []
        assert len(candidate["phase_jobs"]["unit"]) == 2
        await env.test_receipt(candidate["phase_jobs"]["unit"][0])
        await env.pipeline.reconcile()
        assert (await env.claim())["attempt"] is None
        await env.test_receipt(candidate["phase_jobs"]["unit"][1])
        await env.pipeline.reconcile()
        unit = await env.store.read("work_item", "unit-work")
        assert unit["status"] == "completed" and unit["quality_result"] == "passed"
        integration = await env.claim()
        assert integration["work_item"]["step"] == "integration_test_execution"
        await env.pipeline.begin(integration)
        candidate = await env.candidate()
        assert set(candidate["phase_jobs"]) - {"install"} == {"unit", "integration"}
        assert candidate["phase_jobs"].get("install", []) == []
        assert len(await env.store.list("node_job")) == 6
        for job_id in candidate["phase_jobs"]["integration"]:
            job = await env.store.read("node_job", job_id)
            assert job["platform_artifact_manifest"]["fingerprint"] == candidate["fingerprint"]
            assert job["source_manifest"]["fingerprint"] == candidate["source_manifest"]["fingerprint"]
            await env.test_receipt(job_id)
        await env.pipeline.reconcile()
        await env.pipeline.reconcile()
        checks = await env.store.list("check")
        assert len(checks) == 4
        assert all(check["evidence_verified"] and check["executed_case_count"] > 0 for check in checks)
        for check in checks:
            mapping = candidate["matrix_mappings"][check["matrix_entry_id"]]
            assert check["candidate_fingerprint"] == candidate["fingerprint"]
            assert check["execution_key"] == execution_key(mapping["test_case_id"], mapping["target_config_id"], candidate["fingerprint"])
            report = await env.store.read("node_artifact", check["raw_report_artifact_id"])
            assert env.nodes.artifacts.object_path(report["digest"]).is_file()
        assert (await env.store.read("work_item", "integration-work"))["quality_result"] == "passed"
        assert len(await env.store.list("candidate")) == 1
        assert len(await env.store.list("node_job")) == 6


@pytest.mark.parametrize("state,quality,assessment,omit_test", [
    ("failed", "unknown", "rejected", False),
    ("execution_unknown", "unknown", "rejected", False),
    ("completed", "failed", "validated", False),
    ("completed", "passed", "rejected", False),
    ("completed", "passed", "validated", True),
])
async def test_failed_unknown_unverified_or_incomplete_build_never_dispatches_tests(tmp_path, state, quality, assessment, omit_test):
    async with fixture(tmp_path, app_targets=("api",)) as env:
        await env.pipeline.begin(await env.claim())
        candidate = await env.candidate()
        await env.build_receipt(candidate["build_job_ids"][0], state=state, quality=quality,
                                assessment=assessment, omit_test_artifact=omit_test)
        await env.pipeline.reconcile()
        candidate = await env.candidate()
        assert candidate["platform_manifest"] is None and candidate["phase_jobs"] == {}
        assert (await env.store.read("work_item", "unit-work"))["status"] == "blocked"
        assert (await env.claim())["attempt"] is None
        assert await env.store.list("check") == []


async def test_real_raw_failing_unit_report_blocks_integration(tmp_path):
    async with fixture(tmp_path, app_targets=("api",)) as env:
        await env.pipeline.begin(await env.claim())
        await env.finish_builds()
        candidate = await env.candidate()
        await env.test_receipt(candidate["phase_jobs"]["unit"][0], fail=True)
        await env.pipeline.reconcile()
        unit = await env.store.read("work_item", "unit-work")
        assert unit["status"] == "completed" and unit["quality_result"] == "failed"
        assert (await env.claim())["attempt"] is None
        assert "integration" not in (await env.candidate())["phase_jobs"]
        assert (await env.store.list("check"))[0]["quality_result"] == "failed"


async def test_rejected_node_assessment_cannot_become_verified_unit_evidence(tmp_path):
    async with fixture(tmp_path, app_targets=("api",)) as env:
        await env.pipeline.begin(await env.claim())
        await env.finish_builds()
        candidate = await env.candidate()
        await env.test_receipt(candidate["phase_jobs"]["unit"][0], assessment="rejected")
        await env.pipeline.reconcile()
        assert (await env.store.read("work_item", "unit-work"))["status"] == "blocked"
        assert (await env.claim())["attempt"] is None
        assert await env.store.list("check") == []


@pytest.mark.parametrize('assertion_failed', [False, True])
async def test_partial_test_report_is_collected_and_explained_without_opening_the_next_gate(tmp_path, assertion_failed):
    from agentflow.control.presentation import RunPresentationService
    async with fixture(tmp_path, app_targets=('api',)) as env:
        await env.pipeline.begin(await env.claim())
        await env.finish_builds()
        candidate = await env.candidate()
        job = await env.store.read('node_job', candidate['phase_jobs']['unit'][0])
        entry = job['matrix_entries'][0]
        present, missing = entry['framework_case_ids']
        raw = ('<testsuite><testcase name=' + quoteattr(present) + '>'
               + ('<failure>wrong select assertion</failure>' if assertion_failed else '') + '</testcase></testsuite>')
        artifact = await env.nodes.import_input(raw.encode(), 'partial.xml', env.run_id)
        report = parse_junit(env.nodes.artifacts.object_path(artifact['digest']), set(entry['framework_case_ids']))
        await env._receipt(job['id'], 'failed', report.quality_result, 'validated', checks=[{
            'matrix_entry_id': entry['matrix_entry_id'], 'raw_report_artifact_version_id': artifact['id'],
            'normalized_report': report.model_dump(mode='json')}])
        await env.pipeline.reconcile()
        work = await env.store.read('work_item', 'unit-work')
        assert work['status'] == 'completed' and work['quality_result'] != 'passed'
        assert (await env.claim())['attempt'] is None
        assert 'integration' not in (await env.candidate())['phase_jobs']
        view = RunPresentationService(env.store, env.workflow.artifacts, env.settings)
        flow = await view.workflow('run')
        stage = next(stage for stage in flow['stages'] if stage['step'] == 'unit_test_execution')
        assert '用例' in stage['tasks'][0]['blocking_reason']
        assert '缺少执行前提' not in stage['tasks'][0]['blocking_reason']
        document = await view.document(work)
        text = (await env.workflow.artifacts.read(document['digest'])).decode()
        assert missing in text and present in text
        quality = await view.quality('run')
        assert quality['unit_tests']['coverage_complete'] is False
        assert quality['unit_tests']['pass_rate'] is None


async def test_partial_test_failure_does_not_cancel_other_platform_reports(tmp_path):
    async with fixture(tmp_path) as env:
        await env.pipeline.begin(await env.claim())
        await env.finish_builds()
        candidate = await env.candidate()
        first, other = candidate['phase_jobs']['unit']
        job = await env.store.read('node_job', first)
        entry = job['matrix_entries'][0]
        raw = '<testsuite><testcase name=' + quoteattr(entry['framework_case_ids'][0]) + '><failure>fixture assertion</failure></testcase></testsuite>'
        artifact = await env.nodes.import_input(raw.encode(), 'partial.xml', env.run_id)
        report = parse_junit(env.nodes.artifacts.object_path(artifact['digest']), set(entry['framework_case_ids']))
        await env._receipt(first, 'failed', 'failed', 'validated', checks=[{
            'matrix_entry_id': entry['matrix_entry_id'], 'raw_report_artifact_version_id': artifact['id'],
            'normalized_report': report.model_dump(mode='json')}])
        await env.pipeline.reconcile()
        assert (await env.store.read('node_job', other))['state'] == 'queued'
        assert (await env.store.read('work_item', 'unit-work'))['status'] == 'waiting_execution'
        await env.test_receipt(other)
        await env.pipeline.reconcile()
        assert (await env.store.read('work_item', 'unit-work'))['quality_result'] == 'failed'
        assert len(await env.store.list('check')) == 2
        assert (await env.claim())['attempt'] is None


async def test_waiting_execution_releases_agent_capacity_for_independent_work(tmp_path):
    async with fixture(tmp_path, app_targets=("api",), agent_concurrency=1) as env:
        await env.pipeline.begin(await env.claim())
        def add_independent(tx):
            unit = tx.get("work_item", "unit-work")
            return tx.put("work_item", "independent-research", {**{k: v for k, v in unit.items() if k not in {"id", "revision"}},
                "step": "research", "role": "research", "status": "pending", "dependencies": [],
                "attempt_id": None, "candidate_id": None, "execution_phase": None})
        await env.store.command("fixture.parallel", "add", {}, add_independent)
        next_claim = await env.claim()
        assert next_claim["work_item"]["id"] == "independent-research"
        assert (await env.store.read("work_item", "unit-work"))["status"] == "waiting_execution"
        assert (await env.workflow.run_detail("run"))["active_attempt_count"] == 1


@pytest.mark.parametrize("boundary", ["enqueue", "platform", "checks", "finish"])
@pytest.mark.parametrize("after_commit", [False, True])
async def test_database_fault_boundaries_resume_without_duplicate_jobs_or_checks(tmp_path, monkeypatch, boundary, after_commit):
    async with fixture(tmp_path, app_targets=("api",)) as env:
        claim = await env.claim()
        if boundary != "enqueue":
            await env.pipeline.begin(claim)
            for job in (await env.candidate())["build_job_ids"]:
                await env.build_receipt(job)
        if boundary in {"checks", "finish"}:
            await env.pipeline.reconcile()
            for job in (await env.candidate())["phase_jobs"]["unit"]:
                await env.test_receipt(job)
        original = env.store.command
        faulted = False
        async def fault(scope, key, payload, handler):
            nonlocal faulted
            matches = {"enqueue": scope.startswith("node_job_enqueue:"),
                "platform": scope == "candidate.update" and key.endswith(":platform"),
                "checks": scope == "checks.collect", "finish": scope == "attempt.finish"}[boundary]
            if matches and not faulted:
                faulted = True
                if after_commit:
                    await original(scope, key, payload, handler)
                raise OSError("fixture: controller database acknowledgement lost")
            return await original(scope, key, payload, handler)
        monkeypatch.setattr(env.store, "command", fault)
        with pytest.raises(OSError):
            if boundary == "enqueue":
                await env.pipeline.begin(claim)
            else:
                await env.pipeline.reconcile()
        assert faulted
        await env.pipeline.reconcile()
        await env.pipeline.reconcile()
        candidate = await env.candidate()
        assert len(candidate["build_job_ids"]) == 1
        assert len(await env.store.list("candidate")) == 1
        if boundary == "enqueue":
            assert len(await env.store.list("node_job")) == 1
        else:
            assert len(candidate["phase_jobs"]["unit"]) == 1
            assert len(await env.store.list("node_job")) == 2
        if boundary in {"checks", "finish"}:
            assert len(await env.store.list("check")) == 1
            assert (await env.store.read("work_item", "unit-work"))["quality_result"] == "passed"


async def test_scheduler_dispatches_execution_with_real_source_resolution_without_model_calls(tmp_path):
    async with fixture(tmp_path, app_targets=("api",)) as env:
        # Execution dispatch should not touch a model/runtime at all.
        scheduler = Scheduler(env.workflow, env.store, None, None, env.settings, node_service=env.nodes)
        await scheduler._dispatch(await env.claim())
        candidate = await env.candidate()
        assert candidate["source_commit"] == env.snapshot["commit_oid"]
        assert (await env.store.read("work_item", "unit-work"))["status"] == "waiting_execution"
        assert len(await env.store.list("node_job")) == 1
        assert await env.store.list("task_authorization") == []


@pytest.mark.parametrize("after_commit", [False, True])
async def test_scheduler_keeps_durable_node_wait_recoverable_after_enqueue_ack_loss(tmp_path, monkeypatch, after_commit):
    async with fixture(tmp_path, app_targets=("api",)) as env:
        scheduler = Scheduler(env.workflow, env.store, None, None, env.settings, node_service=env.nodes)
        original = env.store.command
        faulted = False
        async def fault(scope, key, payload, handler):
            nonlocal faulted
            if scope.startswith("node_job_enqueue:") and not faulted:
                faulted = True
                if after_commit:
                    await original(scope, key, payload, handler)
                raise OSError("fixture: node enqueue acknowledgement lost")
            return await original(scope, key, payload, handler)
        monkeypatch.setattr(env.store, "command", fault)
        await scheduler._dispatch(await env.claim())
        assert faulted
        assert (await env.store.read("work_item", "unit-work"))["status"] == "waiting_execution"
        await scheduler.execution.reconcile()
        await scheduler.execution.reconcile()
        assert len(await env.store.list("node_job")) == 1
        assert len((await env.candidate())["build_job_ids"]) == 1


async def test_scheduler_restart_reconciles_waiting_node_jobs_without_model_or_agent_slot(tmp_path):
    class NoModelRuntime:
        closed = False

        async def execute_task(self, _task):
            pytest.fail("Node reconciliation must not start a model task")

        async def resume_task(self, _task):
            pytest.fail("waiting_execution must not resume a model task")

        async def cancel(self, _attempt_id):
            pytest.fail("No model attempt should occupy a slot")

        async def close(self):
            self.closed = True

    async with fixture(tmp_path, app_targets=("api",), agent_concurrency=1) as env:
        await env.pipeline.begin(await env.claim())
        candidate = await env.candidate()
        await env.build_receipt(candidate["build_job_ids"][0])
        runtime = NoModelRuntime()
        scheduler = Scheduler(env.workflow, env.store, runtime, None, env.settings, node_service=env.nodes)
        try:
            await scheduler.start()
            async with asyncio.timeout(5):
                while "unit" not in (await env.candidate())["phase_jobs"]:
                    await asyncio.sleep(0.01)
            assert scheduler._active == {}
            assert (await env.workflow.run_detail("run"))["active_attempt_count"] == 0
            assert (await env.store.read("work_item", "unit-work"))["status"] == "waiting_execution"
            assert len(await env.store.list("candidate")) == 1
            assert len(await env.store.list("node_job")) == 2
        finally:
            await scheduler.close()
        assert runtime.closed


@pytest.mark.parametrize("parallel", [False, True])
async def test_scheduler_uses_latest_source_chain_and_refuses_to_drop_parallel_branch(tmp_path, parallel):
    async with fixture(tmp_path, app_targets=("api",)) as env:
        sibling = tmp_path / "second-snapshot"
        base = env.snapshot["base_oid"] if parallel else env.snapshot["commit_oid"]
        await env.repository.clone_snapshot(env.source, sibling, base)
        (sibling / "second-feature.txt").write_text("independent source contribution\n")
        snapshot = await env.repository.freeze_workspace(sibling, base, "second fixture source")
        def add_snapshot(tx):
            previous = tx.get("work_item", "code-work")
            tx.put("work_item", "second-code-work", {**{k: v for k, v in previous.items() if k not in {"id", "revision"}},
                "dependencies": [] if parallel else ["code-work"], "attempt_id": "second-code-snapshot"})
            tx.put("attempt", "second-code-snapshot", {"run_id": "run", "iteration_id": "iteration", "work_item_id": "second-code-work",
                "generation": 1, "fencing_token": 1, "input_fingerprint": previous['input_fingerprint'],
                "status": "completed", "fixture_scope": "controller_integration_not_agent_execution"})
            tx.put("code_snapshot", "second-code-snapshot", {"run_id": "run", "work_item_id": "second-code-work",
                "generation": 1, "repository_path": str(sibling), "commit_oid": snapshot["commit_oid"],
                "tree_oid": snapshot["tree_oid"], "base_oid": snapshot["base_oid"], "stale": False})
            unit = tx.get("work_item", "unit-work")
            tx.put("work_item", unit["id"], {**unit, "dependencies": [*unit["dependencies"], "second-code-work"]}, unit["revision"])
            return {}
        await env.store.command("fixture.source-chain", "add", {}, add_snapshot)
        scheduler = Scheduler(env.workflow, env.store, None, None, env.settings, node_service=env.nodes)
        await scheduler._dispatch(await env.claim())
        if parallel:
            unit = await env.store.read("work_item", "unit-work")
            assert unit["status"] == "blocked" and unit['runtime_failure_code'] == 'assembly_required'
            assert '提交关系' in unit['blocking_reason']
            assert await env.store.list("candidate") == []
            assert await env.store.list("node_job") == []
        else:
            assert (await env.candidate())["source_commit"] == snapshot["commit_oid"]
            assert (await env.store.read("work_item", "unit-work"))["status"] == "waiting_execution"


async def test_archive_rejects_committed_symlink_instead_of_reading_external_file(tmp_path):
    async with fixture(tmp_path, app_targets=("api",)) as env:
        secret = tmp_path / "outside.txt"
        secret.write_text("external control data")
        (env.source / "linked.txt").symlink_to(secret)
        # Imported Git history can contain a link even though our own snapshot
        # collector correctly refuses to create an escaping link.
        env.repository._run(env.source, ["add", "linked.txt"])
        env.repository._run(env.source, ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                         "commit", "-m", "fixture linked source"])
        commit = env.repository._run(env.source, ["rev-parse", "HEAD"]).decode().strip()
        with pytest.raises(DomainError) as caught:
            env.pipeline._archive(env.source, commit, tmp_path / "linked.tar")
        assert caught.value.code == "unsupported_source_entry"


async def test_multiple_configs_for_same_application_keep_their_own_test_package(tmp_path):
    async with fixture(tmp_path, app_targets=("api", "api")) as env:
        await env.pipeline.begin(await env.claim())
        await env.finish_builds()
        candidate = await env.candidate()
        assert (await env.store.read("work_item", "unit-work"))["status"] == "waiting_execution"
        assert len(candidate["phase_jobs"]["unit"]) == 2
        packages = set()
        for identity in candidate["phase_jobs"]["unit"]:
            job = await env.store.read("node_job", identity)
            matching = [artifact for artifact in candidate["platform_manifest"]["artifacts"]
                        if artifact["target_config_id"] == job["target_config"]["target_config_id"] and artifact["kind"] == "test"]
            assert job["test_package_artifact_version_id"] == matching[0]["artifact_version_id"]
            packages.add(job["test_package_artifact_version_id"])
        assert len(packages) == 2


@pytest.mark.parametrize('scene_status', ['waiting_execution', 'completed', 'failed'])
async def test_pipeline_freezes_cross_entries_and_waits_for_scene_evidence(tmp_path, scene_status):
    # Isolated coordinator boundary test; no native execution is claimed.
    async with fixture(tmp_path, app_targets=('api', 'android_native', 'linux_native', 'web'),
                       spec_change=cross_scene_spec, plan_change=cross_scene_plan) as env:
        await env.pipeline.begin(await env.claim())
        candidate = await env.candidate()
        assert len(candidate['matrix_plan']['entries']) == 11
        build_plan = await env.store.read('node_artifact', candidate['source_manifest']['build_plan_artifact_version_id'])
        frozen = json.loads(env.nodes.artifacts.object_path(build_plan['digest']).read_bytes())
        assert frozen['cross_scenarios'][0]['scenario_id'] == 'controller-glue-fixture'
        await env.finish_builds()
        for identity in (await env.candidate())['phase_jobs']['unit']:
            await env.test_receipt(identity)
        await env.pipeline.reconcile()
        await env.pipeline.begin(await env.claim())
        candidate = await env.candidate()
        for identity in candidate['phase_jobs']['integration']:
            job = await env.store.read('node_job', identity)
            assert all(not entry.get('scenario_id') for entry in job['matrix_entries'])
            await env.test_receipt(identity)
        observed = []
        async def scene_start(run, candidate, definition, key, **kwargs):
            observed.append(definition.scenario_id)
            check_ids = []
            if scene_status == 'completed':
                for step in definition.steps:
                    blob = await env.nodes.import_input(b'<testsuite><testcase name="fixture"/></testsuite>',
                                                        'fixture-scene.xml', run['id'])
                    identity = step.matrix_entry_id
                    def save(tx, identity=identity, blob=blob):
                        return tx.put('check', identity, {'run_id': run['id'], 'work_item_id': 'integration-work',
                            'candidate_fingerprint': candidate['fingerprint'], 'evidence_verified': True,
                            'quality_result': 'passed', 'execution_status': 'completed',
                            'raw_report_artifact_id': blob['id'], 'fixture_scope': 'pipeline-coordinator-contract'})
                    await env.store.command('fixture.scene-check', identity, {}, save)
                    check_ids.append(identity)
            return {'status': scene_status, 'check_ids': check_ids, 'blocking_reason': 'fixture scene failure'}
        env.pipeline.scenarios = SimpleNamespace(start=scene_start)
        await env.pipeline.reconcile()
        work = await env.store.read('work_item', 'integration-work')
        assert observed == ['controller-glue-fixture']
        assert work['status'] == {'completed': 'completed', 'failed': 'blocked',
                                  'waiting_execution': 'waiting_execution'}[scene_status]
        assert (await env.store.read('run', 'run'))['execution_state'] == (
            'completed' if scene_status == 'completed' else 'running')
        if scene_status == 'completed':
            report = await env.store.read('artifact', work['artifact_ids'][0])
            contents = json.loads(await env.workflow.artifacts.read(report['digest']))
            assert len(contents['checks']) == 7


async def test_baseline_suite_subset_is_explicit_and_never_available_for_delivery(tmp_path):
    async with fixture(tmp_path) as env:
        def select(tx):
            plan = tx.get('plan', 'plan')
            return tx.put('plan', 'plan', {**plan, 'existing_suite_spec': {'schema_version': 1},
                'target_configs': plan['target_configs'][:1]}, plan['revision'])
        await env.store.command('fixture.subset', 'subset', {}, select)
        await env.pipeline.begin(await env.claim())
        assert len((await env.candidate())['recipes']['targets']) == 1
    async with fixture(tmp_path / 'delivery') as env:
        def delivery(tx):
            plan = tx.get('plan', 'plan')
            tx.put('plan', 'plan', {**plan, 'existing_suite_spec': {'schema_version': 1},
                'target_configs': plan['target_configs'][:1]}, plan['revision'])
            run = tx.get('run', 'run')
            return tx.put('run', 'run', {**run, 'purpose': 'code_delivery'}, run['revision'])
        await env.store.command('fixture.delivery', 'delivery', {}, delivery)
        with pytest.raises(DomainError, match='exactly cover'):
            await env.pipeline.begin(await env.claim())


@pytest.mark.parametrize('stop_reason', [None, 'uncertain_job', 'unreviewed_scope', 'human_wait', 'unknown_model',
    'missing_raw', 'corrupt_raw', 'changed_raw_digest', 'changed_revision', 'run_quota', 'iteration_quota',
    'restored_budget', 'monetary_limit', 'unlimited'])
async def test_product_failure_repair_preserves_frozen_tests_and_rechecks_entire_matrix(tmp_path, stop_reason):
    from agentflow.control.product_repair import ProductTestRepair
    from agentflow.models.budget import BudgetLedger, account_id
    maximum = 0 if stop_reason == 'unlimited' else 20
    async with fixture(tmp_path) as env:
        def authorized_product(tx):
            plan = tx.get('plan', 'plan')
            tx.put('plan', 'plan', {**plan, 'product_contract': {'stack': 'node_web_api', 'product_id': 'product'},
                'authorized_rework_steps': ['implementation']}, plan['revision'])
            run = tx.get('run', 'run')
            return tx.put('run', 'run', {**run, 'purpose': 'code_delivery', 'budget_limit': {
                'currency': 'USD', 'limit_micros': 0, 'max_model_requests': maximum, 'cost_mode': 'request_limited'}}, run['revision'])
        await env.store.command('fixture.product', 'authorize', {}, authorized_product)
        await BudgetLedger(env.store).setup_accounts('run', 'iteration', 0, 0, run_max_requests=maximum, iteration_max_requests=maximum)
        await env.pipeline.begin(await env.claim())
        await env.finish_builds()
        old_candidate = await env.candidate()
        for job_id in old_candidate['phase_jobs']['unit']:
            await env.test_receipt(job_id, fail=True)
        await env.pipeline.reconcile()
        frozen_source = old_candidate['source_commit']
        prior_code = await env.store.read('work_item', 'code-work')
        prior_plan = await env.store.read('artifact', 'unit-plan-artifact')
        old_reports = await env.store.list('check')
        def change(tx):
            if stop_reason == 'uncertain_job':
                job = tx.get('node_job', old_candidate['phase_jobs']['unit'][0])
                tx.put('node_job', job['id'], {**job, 'state': 'execution_unknown'}, job['revision'])
            elif stop_reason == 'unreviewed_scope':
                plan = tx.get('plan', 'plan')
                tx.put('plan', 'plan', {**plan, 'authorized_rework_steps': []}, plan['revision'])
            elif stop_reason == 'human_wait':
                work = tx.get('work_item', 'unit-work')
                tx.put('work_item', work['id'], {**work, 'status': 'waiting_approval'}, work['revision'])
            elif stop_reason == 'unknown_model':
                tx.put('model_invocation', 'lost-response', {'run_id': 'run', 'state': 'uncertain'})
            elif stop_reason in {'run_quota', 'iteration_quota', 'restored_budget'}:
                kind = 'iteration' if stop_reason == 'iteration_quota' else 'run'
                account = tx.get('budget_account', account_id(kind, kind))
                changes = ({'restore_uncertain': True} if stop_reason == 'restored_budget'
                           else {'request_count': account['max_requests']})
                tx.put('budget_account', account['id'], {**account, **changes}, account['revision'])
            elif stop_reason == 'monetary_limit':
                run = tx.get('run', 'run')
                tx.put('run', 'run', {**run, 'budget_limit': {**run['budget_limit'], 'cost_mode': 'priced'}}, run['revision'])
            elif stop_reason == 'changed_raw_digest':
                result = tx.get('node_result', tx.get('node_job', old_candidate['phase_jobs']['unit'][0])['result_id'])
                result['verified_checks'][0]['normalized_report']['raw_digest'] = 'sha256:' + '0' * 64
                tx.put('node_result', result['id'], result, result['revision'])
            return {}
        await env.store.command('fixture.stop', str(stop_reason), {}, change)
        coordinator = ProductTestRepair(env.store, env.workflow)
        if stop_reason in {'missing_raw', 'corrupt_raw'}:
            result = await env.store.read('node_result', (await env.store.read('node_job', old_candidate['phase_jobs']['unit'][0]))['result_id'])
            report = await env.store.read('node_artifact', result['verified_checks'][0]['raw_report_artifact_version_id'])
            path = env.nodes.artifacts.object_path(report['digest'])
            if stop_reason == 'missing_raw':
                path.unlink()
            else:
                path.chmod(0o600)
                path.write_bytes(b'corrupt raw report')
        if stop_reason == 'changed_revision':
            verify = coordinator._failure_evidence
            async def change_after_verification(run_id):
                evidence = await verify(run_id)
                def touch(tx):
                    job = tx.get('node_job', old_candidate['phase_jobs']['unit'][0])
                    return tx.put('node_job', job['id'], {**job, 'revised_after_hash': True}, job['revision'])
                await env.store.command('fixture.touch', 'after-hash', {}, touch)
                return evidence
            coordinator._failure_evidence = change_after_verification
        result = await coordinator.attempt({'id': 'product', 'run_id': 'run'})
        assert result['scheduled'] == (stop_reason in {None, 'unlimited'})
        if stop_reason not in {None, 'unlimited'}:
            assert not await env.store.list('product_test_repair')
            return
        repair = await env.store.read('work_item', result['repair_id'])
        assert repair['write_paths'] == ['src', 'public']
        snapshot = await env.store.read('code_snapshot', repair['payload']['repair_base_snapshot_id'])
        assert snapshot['commit_oid'] == frozen_source
        assert await env.store.read('work_item', 'code-work') == prior_code
        assert await env.store.read('artifact', 'unit-plan-artifact') == prior_plan
        assert await env.store.list('check') == [{**c, 'stale': True, 'revision': c['revision'] + 1} for c in old_reports]
        assert (await env.store.read('run', 'run'))['input_fingerprint'] != old_candidate['run_input_fingerprint']
        assert (await env.store.read('work_item', 'unit-work'))['generation'] == 2
        assert (await env.store.read('work_item', 'integration-work'))['generation'] == 2
        assert not (await coordinator.attempt({'id': 'product', 'run_id': 'run'}))['scheduled']
        claim = await env.claim()
        assert claim['work_item']['id'] == repair['id']
        scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
        repository, commit = await scheduler._source(claim['run'], claim['work_item'])
        assert commit == frozen_source and repository == env.source


@pytest.mark.parametrize('mode', ['retry', 'continue'])
async def test_owner_integration_retry_preserves_unit_and_replaces_only_affected_evidence(tmp_path, mode):
    from agentflow.control.presentation import RunPresentationService
    from agentflow.control.recovery import RunRecoveryService
    from agentflow.models.budget import BudgetLedger
    async with fixture(tmp_path) as env:
        limit = {'currency': 'USD', 'limit_micros': 0, 'max_model_requests': 0, 'cost_mode': 'request_limited'}
        def budget(tx):
            run = tx.get('run', 'run')
            tx.put('iteration', 'iteration', {'project_id': run['project_id'], 'budget_limit': limit})
            if mode == 'continue':
                work = tx.get('work_item', 'integration-work')
                tx.put('work_item', 'delivery-work', {**{k: v for k, v in work.items() if k not in {'id', 'revision'}}, 'key': 'delivery', 'step': 'delivery',
                    'role': 'system', 'dependencies': ['integration-work'], 'status': 'pending', 'attempt_id': None})
            return tx.put('run', 'run', {**run, 'budget_limit': limit}, run['revision'])
        await env.store.command('fixture.budget', 'budget', {}, budget)
        await BudgetLedger(env.store).setup_accounts('run', 'iteration', 0, 0, run_max_requests=0, iteration_max_requests=0)
        await env.pipeline.begin(await env.claim())
        await env.finish_builds()
        candidate = await env.candidate()
        for job_id in candidate['phase_jobs']['unit']:
            await env.test_receipt(job_id)
        await env.pipeline.reconcile()
        unit = await env.store.read('work_item', 'unit-work')
        old_unit_checks = [c for c in await env.store.list('check') if c['work_item_id'] == 'unit-work']
        assert unit['status'] == 'completed'
        await env.pipeline.begin(await env.claim())
        candidate = await env.candidate()
        for job_id in candidate['phase_jobs']['integration']:
            await env.test_receipt(job_id, fail=True)
        await env.pipeline.reconcile()
        old_integration_checks = [c for c in await env.store.list('check') if c['work_item_id'] == 'integration-work']
        presentation = RunPresentationService(env.store, env.workflow.artifacts, env.settings)
        run = await env.store.read('run', 'run')
        _, reports = await presentation._verified_reports(run)
        assert {r['phase'] for r in reports} == {'unit', 'integration'}
        recovery = RunRecoveryService(env.store, env.workflow)
        if mode == 'continue':
            run = await env.workflow.control_run('run', {'expected_revision': run['revision'], 'action': 'cancel',
                'reason': 'Owner cancels after the integration result'}, 'cancel-integration')
        result = await recovery.recover('run', {'expected_revision': run['revision'], 'mode': mode,
            **({'work_item_id': 'integration-work'} if mode == 'retry' else {})}, 'retry-integration')
        assert result['affected_work_item_ids'] == (['delivery-work', 'integration-work'] if mode == 'continue' else ['integration-work'])
        assert await env.store.read('work_item', 'unit-work') == unit
        assert [c for c in await env.store.list('check') if c['work_item_id'] == 'unit-work'] == old_unit_checks
        assert all([(await env.store.read('check', c['id']))['stale'] for c in old_integration_checks])
        current = [c for c in await env.store.list('candidate') if not c.get('stale')][0]
        assert current['id'] != candidate['id'] and current['fingerprint'] == candidate['fingerprint']
        assert current['build_job_ids'] == candidate['build_job_ids']
        assert current['phase_jobs']['unit'] == candidate['phase_jobs']['unit']
        assert 'integration' not in current['phase_jobs']
        _, reports = await presentation._verified_reports(result['run'])
        assert {r['phase'] for r in reports} == {'unit'}
        await env.pipeline.begin(await env.claim())
        current = await env.store.read('candidate', current['id'])
        assert set(current['phase_jobs']['integration']).isdisjoint(candidate['phase_jobs']['integration'])
        for job_id in current['phase_jobs']['integration']:
            await env.test_receipt(job_id)
        await env.pipeline.reconcile()
        _, reports = await presentation._verified_reports(await env.store.read('run', 'run'))
        assert {r['phase'] for r in reports} == {'unit', 'integration'}
        assert all(r['report']['quality_result'] == 'passed' for r in reports)
        assert (await env.store.read('work_item', 'integration-work'))['quality_result'] == 'passed'


@pytest.mark.parametrize('node_state', ['queued', 'completed', 'execution_unknown'])
async def test_continue_paused_execution_collects_existing_jobs_without_new_attempts(tmp_path, node_state):
    from agentflow.control.recovery import RunRecoveryService
    from agentflow.models.budget import BudgetLedger
    async with fixture(tmp_path) as env:
        limit = {'currency': 'USD', 'limit_micros': 0, 'max_model_requests': 0, 'cost_mode': 'request_limited'}
        def budget(tx):
            run = tx.get('run', 'run')
            tx.put('iteration', 'iteration', {'project_id': run['project_id'], 'budget_limit': limit})
            return tx.put('run', 'run', {**run, 'budget_limit': limit}, run['revision'])
        await env.store.command('fixture.budget', 'budget', {}, budget)
        await BudgetLedger(env.store).setup_accounts('run', 'iteration', 0, 0, run_max_requests=0, iteration_max_requests=0)
        await env.pipeline.begin(await env.claim())
        candidate = await env.candidate()
        run = await env.store.read('run', 'run')
        await env.workflow.control_run('run', {'expected_revision': run['revision'], 'action': 'pause', 'reason': 'fixture'}, 'pause')
        if node_state == 'completed':
            for identity in candidate['build_job_ids']:
                await env.build_receipt(identity)
        elif node_state == 'execution_unknown':
            def unknown(tx):
                job = tx.get('node_job', candidate['build_job_ids'][0])
                return tx.put('node_job', job['id'], {**job, 'state': 'execution_unknown'}, job['revision'])
            await env.store.command('fixture.unknown', 'unknown', {}, unknown)
        await env.pipeline.reconcile()
        work = await env.store.read('work_item', 'unit-work')
        attempts = await env.store.list('attempt')
        assert work['status'] == 'waiting_execution'
        run = await env.store.read('run', 'run')
        service = RunRecoveryService(env.store, env.workflow)
        options = await service.options('run')
        assert options['continue']['eligible'] == (node_state != 'execution_unknown')
        if node_state == 'execution_unknown':
            with pytest.raises(DomainError):
                await service.recover('run', {'expected_revision': run['revision'], 'mode': 'continue'}, 'continue')
            return
        result = await service.recover('run', {'expected_revision': run['revision'], 'mode': 'continue'}, 'continue')
        assert result['execution'] == 'resume_scheduling'
        assert await env.store.read('work_item', 'unit-work') == work
        assert await env.store.list('attempt') == attempts
        await env.pipeline.reconcile()
        assert (await env.candidate())['build_job_ids'] == candidate['build_job_ids']


async def test_slow_cleanup_does_not_block_scheduler_start_or_node_progress(tmp_path, monkeypatch):
    """A backed-up janitor must not hold startup, execution slots or reconciliation."""
    entered, release = asyncio.Event(), asyncio.Event()
    cleanup_entries = 0

    class NoModelRuntime:
        async def execute_task(self, _task):
            pytest.fail('Node progress must not create model work')

        async def resume_task(self, _task):
            pytest.fail('Node progress must not resume model work')

        async def close(self):
            pass

    async def slow_cleanup():
        nonlocal cleanup_entries
        cleanup_entries += 1
        entered.set()
        await release.wait()

    async with fixture(tmp_path, app_targets=('api',), agent_concurrency=1) as env:
        await env.pipeline.begin(await env.claim())
        candidate = await env.candidate()
        await env.build_receipt(candidate['build_job_ids'][0])
        scheduler = Scheduler(env.workflow, env.store, NoModelRuntime(), None, env.settings, node_service=env.nodes)
        monkeypatch.setattr(scheduler, '_maintain', slow_cleanup)
        starting = asyncio.create_task(scheduler.start())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(asyncio.shield(starting), 1)
            async with asyncio.timeout(5):
                while 'unit' not in (await env.candidate())['phase_jobs']:
                    await asyncio.sleep(.01)
            assert not release.is_set()
            assert len(await env.store.list('node_job')) == 2
            assert (await env.store.read('work_item', 'unit-work'))['status'] == 'waiting_execution'
            scheduler._schedule_maintenance()
            scheduler._schedule_maintenance()
            await asyncio.sleep(0)
            assert cleanup_entries == 1
            await asyncio.wait_for(scheduler.close(), 2)
            assert scheduler._maintenance_task.done()
        finally:
            release.set()
            await starting
            await scheduler.close()

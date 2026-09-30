"""Transactional software-work lifecycle; external effects are kept outside DB callbacks."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.domain.planning import (
    CODING_STEPS,
    EXECUTION_STEPS,
    PREREQUISITES,
    ROLES,
    STEPS,
    WorkSpec,
    descendants,
    output_fingerprint,
    paths_conflict,
    select_steps,
    validate_graph,
)
from agentflow.runtime.failures import runtime_failure_code


def new_id() -> str:
    return str(uuid4())


def required(tx, kind: str, identity: str) -> dict:
    value = tx.get(kind, identity)
    if value is None:
        raise DomainError("not_found", f"Unknown {kind}", 404)
    return value


def ensure_revision(value: dict, expected: int) -> None:
    if value["revision"] != expected:
        raise DomainError("revision_conflict", "The object changed; reload before applying this command")


class WorkflowService:
    def __init__(self, store, artifacts, settings, profile_reader=None):
        self.store = store
        self.artifacts = artifacts
        self.settings = settings
        self.profile_reader = profile_reader

    async def _git(self, path: Path, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args,
            cwd=path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"PATH": __import__("os").environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"},
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise DomainError("git_timeout", "Repository operation timed out", 504) from None
        if proc.returncode:
            raise DomainError("invalid_repository", "Repository operation failed", 422)
        return out.decode().strip()

    async def create_project(self, payload: dict, key: str) -> dict:
        path = Path(payload["local_path"]).expanduser()
        if not path.is_absolute() or path.is_symlink():
            raise DomainError("invalid_path", "Choose an absolute, non-symlink project directory", 422)
        path = path.resolve()
        data = self.settings.data_dir.resolve()
        if path == Path(path.anchor) or path == data or path in data.parents or data in path.parents:
            raise DomainError("protected_path", "Project and control data directories must be separate", 422)
        if payload.get("dirty_worktree_policy", "require_clean") != "require_clean":
            raise DomainError("unsupported_policy", "Import an explicit committed baseline with require_clean", 422)
        normalized = {**payload, "local_path": str(path)}

        def intent(tx):
            from agentflow.control.workspace_retention import _referenced
            for retiring in tx.list('workspace_retirement_intent'):
                if retiring.get('state') == 'retiring':
                    trash = retiring.get('trash_path') or str(Path(retiring['path']).with_name(
                        '.retired-' + canonical_digest(retiring['attempt_id']).split(':')[1]))
                    if any(_referenced(location, [str(path)]) for location in (retiring['path'], trash)):
                        raise DomainError('workspace_retiring', '该临时工作区正在回收，请选择项目源码目录。')
            for project in tx.list("project"):
                if project["local_path"] == str(path):
                    raise DomainError("project_exists", "This repository is already registered")
            return tx.put("project_intent", new_id(), {"payload": normalized, "project_id": new_id()})

        record = await self.store.command("project_intent", key, normalized, intent)
        project_id = record["project_id"]
        existing = await self.store.read("project", project_id)
        if existing:
            return existing
        if normalized["import_mode"] == "initialize_managed":
            if not path.exists():
                path.mkdir(parents=True)
            marker = path / ".agentflow-init"
            git_marker = path / ".git" / "agentflow-init"
            if git_marker.exists():
                marker = git_marker
            if not marker.exists():
                if any(path.iterdir()):
                    raise DomainError("directory_not_empty", "Managed project initialization requires an empty directory")
                marker.write_text(record["id"])
            if marker.read_text() != record["id"]:
                raise DomainError("initialization_conflict", "Directory belongs to another initialization")
            if not (path / ".git").exists():
                await self._git(path, "init", "-b", "main")
            if marker != git_marker:
                marker.replace(git_marker)
            try:
                await self._git(path, "rev-parse", "--verify", "HEAD")
            except DomainError:
                await self._git(path, "-c", "user.name=AgentFlow", "-c", "user.email=agentflow@localhost",
                                "commit", "--allow-empty", "-m", "Initialize managed project")
        if not path.is_dir():
            raise DomainError("invalid_path", "Project directory does not exist", 422)
        top = await self._git(path, "rev-parse", "--show-toplevel")
        if Path(top).resolve() != path:
            raise DomainError("invalid_repository", "Select the repository root", 422)
        if await self._git(path, "status", "--porcelain", "--untracked-files=all"):
            raise DomainError("dirty_worktree", "Commit or separately preserve local changes before importing")
        commit = await self._git(path, "rev-parse", "HEAD")
        try:
            base_ref = await self._git(path, "symbolic-ref", "HEAD")
        except DomainError:
            base_ref = "HEAD"

        def commit_project(tx):
            value = tx.put("project", project_id, {**normalized, "base_commit": commit,
                "base_ref": base_ref, "created_at": utc_now()})
            tx.event("project.created", {"project_id": project_id})
            return value

        value = await self.store.command("project_commit", key, normalized, commit_project)
        if normalized["import_mode"] == "initialize_managed":
            git_marker.unlink(missing_ok=True)
        return value

    async def create_plan(self, payload: dict, key: str) -> dict:
        request_limit = (payload.get('budget_limit') or {}).get('max_model_requests')
        if type(request_limit) is not int or request_limit < 0:
            raise DomainError('invalid_budget', '模型调用上限必须为非负整数，0 表示不限次数。', 422)
        chosen = select_steps(payload["selection"])
        stage_refs = payload.get('stage_input_versions', {})
        if (not isinstance(stage_refs, dict) or any(stage not in chosen or not isinstance(refs, list)
                for stage, refs in stage_refs.items())):
            raise DomainError('invalid_stage_inputs', 'Stage inputs must name selected workflow steps', 422)
        if "delivery" in chosen and payload["purpose"] != "code_delivery":
            raise DomainError("invalid_scope", "Only code_delivery runs may include delivery", 422)
        profiles = await self.profile_reader() if self.profile_reader else []
        profile_ids = {p.get("model_profile_id", p.get("id")): p for p in profiles}
        observed_project = await self.store.read("project", payload["project_id"])
        if not observed_project:
            raise DomainError("not_found", "Unknown project", 404)
        repository = Path(observed_project["local_path"])
        baseline = await self._git(repository, "rev-parse", "HEAD")
        try:
            base_ref = await self._git(repository, "symbolic-ref", "HEAD")
        except DomainError:
            base_ref = "HEAD"
        selected_delivery = None
        if payload.get('source_commit') or payload.get('source_ref'):
            source_commit, source_ref = payload.get('source_commit'), payload.get('source_ref')
            if (not isinstance(source_commit, str) or len(source_commit) != 40
                    or any(c not in '0123456789abcdef' for c in source_commit)
                    or not isinstance(source_ref, str) or not source_ref.startswith('refs/heads/')):
                raise DomainError('invalid_source_baseline', 'Select an exact confirmed delivery commit and branch', 422)
            for delivery in await self.store.list('delivery'):
                parent = await self.store.read('run', delivery['run_id']) if delivery.get('run_id') else None
                if (parent and parent['project_id'] == observed_project['id'] and delivery.get('confirmed_at')
                        and delivery.get('commit_oid') == source_commit and delivery.get('delivery_ref') == source_ref):
                    selected_delivery = delivery
                    break
            if not selected_delivery or await self._git(repository, 'rev-parse', '--verify', source_ref) != source_commit:
                raise DomainError('unconfirmed_source_baseline', 'The selected delivery is not current in this project')
            baseline, base_ref = source_commit, source_ref
        existing_suite = None
        if payload["purpose"] != "code_delivery" and set(chosen) <= EXECUTION_STEPS:
            from agentflow.control.execution_pipeline import ProjectExecutionSpec
            try:
                existing_suite = ProjectExecutionSpec.model_validate_json(
                    await self._git(repository, "show", f"{baseline}:agentflow.project.json")).model_dump(mode="json")
            except (DomainError, ValueError):
                pass
        plan_id = new_id()

        def plan(tx):
            project = required(tx, "project", payload["project_id"])
            ensure_revision(project, observed_project["revision"])
            gaps = []
            reused = []
            available = set()
            bound_versions = {}
            mapped_inputs = {}
            all_refs = list(payload.get('input_versions', [])) + [ref for refs in stage_refs.values() for ref in refs]
            for ref in all_refs:
                if not isinstance(ref, dict):
                    raise DomainError('invalid_input', 'Artifact references must be objects', 422)
                artifact_id = ref.get("artifact_version_id") or ref.get("id") or ref.get("object_id")
                artifact = tx.get("artifact", artifact_id) if artifact_id else None
                if (not self._input_current(tx, artifact, project['id'])
                        or ref.get("fingerprint", artifact["digest"]) != artifact["digest"]
                        or ref.get('revision', artifact['revision']) != artifact['revision']):
                    gaps.append({"code": "invalid_input", "message": "Referenced input is missing or stale"})
                else:
                    available.add(artifact["step"])
                    if artifact['id'] not in reused:
                        reused.append(artifact["id"])
                    bound_versions[artifact['id']] = {'revision': artifact['revision'], 'digest': artifact['digest']}
            for stage, refs in stage_refs.items():
                mapped_inputs[stage] = list(dict.fromkeys(ref.get('artifact_version_id') or ref.get('id')
                    or ref.get('object_id') for ref in refs if isinstance(ref, dict)))
            if selected_delivery:
                current_delivery = tx.get('delivery', selected_delivery['id'])
                if current_delivery != selected_delivery:
                    raise DomainError('stale_delivery', 'Delivery changed during iteration planning')
            for step in chosen:
                for previous in PREREQUISITES[step]:
                    direct_input = (step == chosen[0] and (step in {"goal", "research", "prd"}
                        or (project["import_mode"] == "snapshot_existing" and step == "code_review")))
                    if previous not in chosen and previous not in available and step not in available and not direct_input and not existing_suite:
                        gaps.append({"code": "missing_input", "step": step, "message": f"Supply valid {previous} output"})
            if set(chosen) & EXECUTION_STEPS:
                configs = payload.get("target_configs", [])
                if not configs:
                    gaps.append({"code": "missing_target_config", "message": "Explicit target/tool/device requirements are required"})
                declared = {c["app_target"] for c in configs}
                if set(payload.get("app_targets", [])) - declared:
                    gaps.append({"code": "missing_target_config", "message": "Every target needs a concrete configuration"})
            bindings = payload.get("runtime_bindings", {})
            needs_role = any(s not in CODING_STEPS | EXECUTION_STEPS | {"delivery"} for s in chosen)
            needs_coding = bool(set(chosen) & CODING_STEPS)
            if needs_coding and bindings.get("coding_backend_id") != str(uuid5(NAMESPACE_URL, "agentflow:backend:codex_exec")):
                gaps.append({"code": "missing_coding_backend", "message": "Select the registered Codex execution backend"})
            if payload["purpose"] == "code_delivery":
                if not EXECUTION_STEPS <= set(chosen):
                    gaps.append({"code": "required_delivery_tests", "message": "Code delivery requires both unit and integration execution in this run"})
                if base_ref == "HEAD":
                    gaps.append({"code": "detached_base", "message": "Select a named local branch before code delivery"})
            for name, needed in [("role_model_profile_id", needs_role), ("coding_model_profile_id", needs_coding)]:
                if needed:
                    profile = profile_ids.get(bindings.get(name))
                    if not profile or profile.get("acceptance_status") != "accepted":
                        gaps.append({"code": "pending_model_confirmation", "message": f"An explicitly accepted {name} is required"})
                    elif profile.get("credential_status") != "configured":
                        gaps.append({"code": "missing_credentials", "message": "Configure the model credential reference"})
            specs = []
            previous_key = None
            for step in chosen:
                specs.append({"key": step, "step": step, "role": ROLES[step],
                              "dependencies": [previous_key] if previous_key else [], "required": True})
                previous_key = step
                if step in {"unit_test_implementation", "integration_test_implementation"}:
                    review_key = step + ":review"
                    specs.append({"key": review_key, "step": "code_review", "role": "review",
                                  "dependencies": [step], "required": True})
                    previous_key = review_key
            input_fingerprint = canonical_digest({"request": payload, "base": baseline, "reused": reused})
            parent = required(tx, "run", payload["parent_run_id"]) if payload.get("parent_run_id") else None
            if parent and parent["project_id"] != project["id"]:
                raise DomainError("parent_project_mismatch", "Parent run belongs to a different project")
            if parent and payload.get("iteration_id") not in {None, parent["iteration_id"]}:
                raise DomainError("iteration_mismatch", "Continuation must retain its parent iteration budget")
            iteration_id = (parent["iteration_id"] if parent else payload.get("iteration_id")) or new_id()
            if parent or payload.get("iteration_id"):
                iteration = required(tx, "iteration", iteration_id)
                if iteration["project_id"] != project["id"]:
                    raise DomainError("iteration_mismatch", "Iteration belongs to another project")
            value = tx.put("plan", plan_id, {**payload, "plan_id": plan_id, "actual_steps": chosen,
                "work_specs": specs, "reused_inputs": reused, "missing_inputs": gaps,
                'reused_input_versions': bound_versions, 'stage_reused_inputs': mapped_inputs,
                'source_delivery_id': selected_delivery['id'] if selected_delivery else None,
                "state": "missing_inputs" if gaps else "ready", "iteration_id": iteration_id,
                "input_fingerprint": input_fingerprint, "base_commit": baseline, "base_ref": base_ref,
                "existing_suite_spec": existing_suite,
                "model_profile_revisions": {identity: p.get("revision", 1) for identity, p in profile_ids.items()
                                            if identity in bindings.values()},
                "created_at": utc_now(), "started_run_id": None})
            tx.event("plan.created", {"plan_id": plan_id, "state": value["state"]})
            return value

        return await self.store.command("plan.create", key, payload, plan)

    @staticmethod
    def _input_current(tx, artifact, project_id):
        if not artifact or artifact.get('stale'):
            return False
        if artifact.get('source_kind') in {'owner_input', 'static_diagnosis'}:
            return (artifact.get('project_id') == project_id and artifact.get('generation') == 0
                    and not artifact.get('run_id') and not artifact.get('work_item_id')
                    and artifact.get('step') in {'goal', 'research'}
                    and artifact.get('quality_result') == 'not_applicable')
        source = tx.get('work_item', artifact['work_item_id']) if artifact.get('work_item_id') else None
        run = tx.get('run', artifact['run_id']) if artifact.get('run_id') else None
        return bool(source and run and run.get('project_id') == project_id
                    and source.get('project_id') == project_id and source.get('run_id') == run['id']
                    and source.get('generation') == artifact.get('generation')
                    and source.get('input_fingerprint') == artifact.get('input_fingerprint')
                    and source.get('status') == 'completed' and source.get('quality_result') != 'failed')

    async def start_run(self, payload: dict, key: str) -> dict:
        run_id = new_id()
        profiles = await self.profile_reader() if self.profile_reader else []
        profile_ids = {p.get("model_profile_id", p.get("id")): p for p in profiles}
        observed_plan = await self.store.read('plan', payload['plan_id'])
        if observed_plan and observed_plan.get('source_delivery_id'):
            project = await self.store.read('project', observed_plan['project_id'])
            if await self._git(Path(project['local_path']), 'rev-parse', '--verify', observed_plan['base_ref']) != observed_plan['base_commit']:
                raise DomainError('stale_source_baseline', 'The confirmed source branch changed after planning')

        def start(tx):
            plan = required(tx, "plan", payload["plan_id"])
            ensure_revision(plan, payload["expected_revision"])
            if plan["state"] != "ready":
                raise DomainError("plan_not_ready", "Resolve the plan's missing inputs before starting", details=plan["missing_inputs"])
            if plan["started_run_id"]:
                raise DomainError("plan_started", "This immutable plan has already started")
            for identity, revision in plan.get("model_profile_revisions", {}).items():
                profile = profile_ids.get(identity)
                stored = tx.get("model_profile", identity)
                if (not profile or profile.get("acceptance_status") != "accepted"
                        or profile.get("credential_status") != "configured" or profile.get("revision", 1) != revision
                        or (stored and (stored["revision"] != revision or stored.get("acceptance_status") != "accepted"))):
                    raise DomainError("stale_model_profile", "Model configuration changed after plan preview")
            for identity in plan["reused_inputs"]:
                artifact = required(tx, 'artifact', identity)
                frozen = plan.get('reused_input_versions', {}).get(identity)
                if (not self._input_current(tx, artifact, plan['project_id'])
                        or (frozen and (artifact['revision'] != frozen['revision'] or artifact['digest'] != frozen['digest']))):
                    raise DomainError("stale_input", "Referenced artifact changed after plan preview")
            iteration_id = plan["iteration_id"]
            if tx.get("iteration", iteration_id) is None:
                tx.put("iteration", iteration_id, {"project_id": plan["project_id"],
                    "budget_limit": plan["budget_limit"], "created_at": utc_now()})
            item_ids = {spec["key"]: new_id() for spec in plan["work_specs"]}
            policy = canonical_digest({"approvals": plan["approval_steps"], "targets": plan.get("target_configs", [])})
            for spec in plan["work_specs"]:
                identity = item_ids[spec["key"]]
                tx.put("work_item", identity, {**spec, "run_id": run_id, "project_id": plan["project_id"],
                    "dependencies": [item_ids[d] for d in spec["dependencies"]], "generation": 1,
                    "status": "pending", "quality_result": "unknown", "fencing_token": 0,
                    "input_fingerprint": plan["input_fingerprint"], "policy_fingerprint": policy,
                    "approval_required": spec["step"] in plan["approval_steps"], "artifact_ids": [],
                    "write_paths": ["."] if spec["step"] in CODING_STEPS else [], "attempt_id": None})
            run = tx.put("run", run_id, {"run_id": run_id, "plan_id": plan["id"], "project_id": plan["project_id"],
                "iteration_id": iteration_id, "purpose": plan["purpose"], "goal": plan["goal"],
                'display_name': plan.get('product_contract', {}).get('display_name') or plan['goal'][:100],
                "parent_run_id": plan.get("parent_run_id"),
                "execution_state": "running", "quality_result": "unknown", "blocking_reasons": [],
                "runtime_bindings": plan["runtime_bindings"], "budget_limit": plan["budget_limit"],
                "base_commit": plan["base_commit"], "base_ref": plan["base_ref"],
                "input_fingerprint": plan["input_fingerprint"], "delivery_ids": [], "created_at": utc_now()})
            tx.put("plan", plan["id"], {**plan, "state": "started", "started_run_id": run_id}, plan["revision"])
            tx.event("run.started", {"run_id": run_id}, run_id=run_id)
            return run

        return await self.store.command("run.start", key, payload, start)

    async def control_run(self, run_id: str, payload: dict, key: str) -> dict:
        def control(tx):
            run = required(tx, "run", run_id)
            ensure_revision(run, payload["expected_revision"])
            action = payload["action"]
            if action == 'resume':
                from agentflow.control.product_management import guard_product_run
                guard_product_run(tx, run)
            if run["execution_state"] == "publishing":
                raise DomainError("delivery_in_progress", "Git publication is being reconciled; retry after it completes")
            if action not in {"pause", "resume", "cancel"}:
                raise DomainError("invalid_action", "Use the revision command to modify work", 422)
            if run["execution_state"] in {"completed", "cancelled"}:
                raise DomainError("terminal_run", "This run has ended")
            state = {"pause": "paused", "resume": "running", "cancel": "cancelling"}[action]
            if action == "cancel":
                active = False
                for item in tx.list("work_item"):
                    if item["run_id"] != run_id or item["status"] == "completed":
                        continue
                    active |= item["status"] in {"running", "waiting_execution", "execution_unknown", "cancel_requested"}
                    new_status = "cancel_requested" if item["status"] in {"running", "waiting_execution", "execution_unknown", "cancel_requested"} else "cancelled"
                    tx.put("work_item", item["id"], {**item, "status": new_status}, item["revision"])
                if not active:
                    state = "cancelled"
            updated = tx.put("run", run_id, {**run, "execution_state": state}, run["revision"])
            tx.event(f"run.{action}", {"reason": payload["reason"]}, run_id=run_id)
            return updated
        return await self.store.command("run.control", key, {"run_id": run_id, **payload}, control)

    async def add_work_items(self, run_id: str, specs: list[WorkSpec], expected_revision: int, key: str) -> dict:
        validate_graph(specs)
        payload = {"run_id": run_id, "expected_revision": expected_revision,
                   "specs": [s.__dict__ for s in specs]}
        def add(tx):
            run = required(tx, "run", run_id)
            ensure_revision(run, expected_revision)
            if run["execution_state"] not in {"running", "paused"}:
                raise DomainError("invalid_state", "Run cannot accept work items")
            mapping = {s.key: new_id() for s in specs}
            rows = []
            for spec in specs:
                if spec.step not in STEPS or spec.step == "delivery":
                    raise DomainError("invalid_work", "Delegated work cannot bypass the delivery gate", 422)
                paths = list(spec.write_paths)
                if any(Path(p).is_absolute() or ".." in Path(p).parts for p in paths):
                    raise DomainError("invalid_write_scope", "Write paths must remain in the workspace", 422)
                rows.append(tx.put("work_item", mapping[spec.key], {"run_id": run_id,
                    "project_id": run["project_id"], "key": spec.key, "step": spec.step, "role": spec.role,
                    "dependencies": [mapping[d] for d in spec.dependencies], "generation": 1,
                    "status": "pending", "quality_result": "unknown", "fencing_token": 0,
                    "input_fingerprint": run["input_fingerprint"], "policy_fingerprint": canonical_digest(spec.payload),
                    "approval_required": spec.payload.get("approval_required", False),
                    "artifact_ids": [], "write_paths": paths, "attempt_id": None, "required": spec.required,
                    "payload": spec.payload}))
            tx.put("run", run_id, run, run["revision"])
            tx.event("work.expanded", {"work_item_ids": list(mapping.values())}, run_id=run_id)
            return {"items": rows}
        return await self.store.command("work.expand", key, payload, add)

    async def claim_next(self, run_id: str, worker_id: str, key: str) -> dict:
        def claim(tx):
            run = required(tx, "run", run_id)
            if run["execution_state"] != "running":
                return {"attempt": None}
            all_items = tx.list("work_item")
            active = [i for i in all_items if i["status"] in {"running", "cancel_requested", "execution_unknown"}]
            if len(active) >= self.settings.agent_concurrency:
                return {"attempt": None}
            by_id = {i["id"]: i for i in all_items}
            ready, visiting = {}, set()

            def completed_ancestry(identity):
                if identity in ready:
                    return ready[identity]
                parent = by_id.get(identity)
                if (not parent or parent.get('run_id') != run_id or parent.get('archived')
                        or identity in visiting or parent.get('status') != 'completed'
                        or parent.get('quality_result') in {'failed', 'inconclusive'}
                        or parent.get('step') in EXECUTION_STEPS | {'code_review'}
                        and parent.get('quality_result') != 'passed'):
                    return False
                visiting.add(identity)
                ready[identity] = all(completed_ancestry(dependency) for dependency in parent['dependencies'])
                visiting.remove(identity)
                return ready[identity]

            for item in all_items:
                if item["run_id"] != run_id or item["status"] not in {"pending", "pending_delivery"}:
                    continue
                # An accepted contribution may be retained while an earlier
                # producer reopens. Its descendants must wait for that entire
                # prerequisite chain before resolving the new complete source.
                if not all(completed_ancestry(d) for d in item['dependencies']):
                    continue
                from agentflow.control.review_contract_binding import repair_batch
                try:
                    repair_batch(tx, item)
                except DomainError as error:
                    # A rejected auxiliary binding is a concrete work failure,
                    # not a reason to leave it pending and abort every scheduler pass.
                    tx.put('work_item', item['id'], {**item, 'status': 'blocked', 'quality_result': 'unknown',
                        'runtime_failure_code': error.code, 'blocking_reason': error.message,
                        'failure_diagnostic': {'code': error.code, 'message': error.message, 'details': error.details}}, item['revision'])
                    tx.event('work.claim_rejected', {'work_item_id': item['id'], 'code': error.code}, run_id=run_id)
                    self._recompute_run(tx, run_id)
                    run = required(tx, 'run', run_id)
                    continue
                if any(a["project_id"] == item["project_id"] and paths_conflict(a["write_paths"], item["write_paths"])
                       for a in active):
                    continue
                parent_refs = [(d, by_id[d]["generation"], by_id[d].get("output_fingerprint")) for d in item["dependencies"]]
                fingerprint = canonical_digest({"base": run["input_fingerprint"], "generation": item["generation"],
                                                "parents": parent_refs, "payload": item.get("payload", {})})
                attempt_id = new_id()
                fence = item["fencing_token"] + 1
                attempt = tx.put("attempt", attempt_id, {"work_item_id": item["id"], "run_id": run_id,
                    "iteration_id": run["iteration_id"], "generation": item["generation"], "fencing_token": fence,
                    "input_fingerprint": fingerprint, "worker_id": worker_id, "status": "running",
                    "started_at": utc_now(), "last_heartbeat": time.time(), "execution_status": None})
                current = tx.put("work_item", item["id"], {**item, "status": "running", "attempt_id": attempt_id,
                    "input_fingerprint": fingerprint, "fencing_token": fence}, item["revision"])
                tx.event("attempt.claimed", {"attempt_id": attempt_id, "work_item_id": item["id"],
                         "role": item["role"], "fencing_token": fence}, run_id=run_id)
                return {"attempt": attempt, "work_item": current, "run": run}
            return {"attempt": None}
        return await self.store.command("work.claim", key, {"run_id": run_id, "worker_id": worker_id}, claim)

    async def finish_attempt(self, attempt_id: str, payload: dict, key: str, *, verified_artifacts: list[dict],
                             planning_batch: dict | None = None, review_disposition_result: dict | None = None) -> dict:
        """Only the controller collector calls this with locally verified immutable artifacts."""
        verified_artifacts = [dict(item) for item in verified_artifacts]
        if review_disposition_result is not None:
            candidates = [record for record in verified_artifacts if record.get('name') == 'openhands_final.json']
            if len(candidates) != 1 or json.loads(await self.artifacts.read(candidates[0]['digest'])) != review_disposition_result:
                raise DomainError('review_contract_stale', '归属分析与冻结原始结果不一致。')
            from agentflow.control.review_contract_repair import ReviewContractRepair
            attempt = await self.store.read('attempt', attempt_id)
            work = await self.store.read('work_item', attempt['work_item_id'])
            await ReviewContractRepair(self.store, self).prepare_disposition(work, review_disposition_result)
        if planning_batch is not None:
            source = planning_batch.get('result_digest')
            if source not in {item['digest'] for item in verified_artifacts}:
                raise DomainError('planning_result_mismatch', '规划批次未绑定已收集的原始结果。')
            original = json.loads(await self.artifacts.read(source))
            content = original.get('result', original)
            if not isinstance(content, dict) or content.get('parallel_work', []) != planning_batch.get('proposals'):
                raise DomainError('planning_result_mismatch', '规划批次与不可变结果不一致。')
        for artifact in verified_artifacts:
            metadata = await self.artifacts.verify(artifact["digest"])
            if not metadata["size"]:
                raise DomainError("empty_artifact", "Empty output cannot satisfy a work item")
        if verified_artifacts and not any(item.get('readable') for item in verified_artifacts):
            from agentflow.control.readable import (
                document_sources,
                output_contract,
                render_document,
                test_report_markdown,
            )
            attempt = await self.store.read('attempt', attempt_id)
            work = await self.store.read('work_item', attempt['work_item_id']) if attempt else None
            if work:
                run = await self.store.read('run', work['run_id'])
                plan = await self.store.read('plan', run['plan_id']) if run and run.get('plan_id') else None
                language = (plan or {}).get('product_contract', {}).get('language', 'zh-CN')
                rendering_key = canonical_digest({'attempt': attempt_id, 'sources': verified_artifacts})
                cached = await self.store.read('human_output', rendering_key)
                if not cached:
                    sources = await document_sources(self.artifacts, verified_artifacts)
                    text = render_document({**work, 'generation': attempt['generation']}, sources, language=language)
                    if work['step'] in EXECUTION_STEPS:
                        reports, seen = [], set()
                        for check_id in payload.get('verified_check_ids', []):
                            check = await self.store.read('check', check_id)
                            if not check or check.get('run_id') != work['run_id'] or not check.get('node_result_id'):
                                continue
                            result = await self.store.read('node_result', check['node_result_id'])
                            for verified in (result or {}).get('verified_checks', []):
                                raw_id = verified.get('raw_report_artifact_version_id')
                                if (verified.get('matrix_entry_id') == check.get('matrix_entry_id')
                                        and raw_id not in seen and verified.get('normalized_report')):
                                    reports.append(verified['normalized_report'])
                                    seen.add(raw_id)
                        if reports:
                            text += '\n' + test_report_markdown(
                                'Test case results' if language == 'en' else '用例执行详情', reports, language=language)
                    document = await self.artifacts.put_bytes(text.encode('utf-8'))
                    metadata = {'digest': document['id'], 'name': output_contract(work, language=language)['artifact_name'] + '.md',
                        'media_type': 'text/markdown', 'readable': True,
                        'source_digests': [item['digest'] for item in verified_artifacts]}
                    cached = await self.store.command('human_output.prepare', rendering_key,
                        {'attempt': attempt_id, 'sources': verified_artifacts},
                        lambda tx: tx.put('human_output', rendering_key, {'artifact': metadata}))
                verified_artifacts.append(cached['artifact'])
        def finish(tx):
            attempt = required(tx, "attempt", attempt_id)
            item = required(tx, "work_item", attempt["work_item_id"])
            if (item["attempt_id"] != attempt_id or item["fencing_token"] != payload["fencing_token"]
                    or item["input_fingerprint"] != payload["input_fingerprint"]
                    or item["generation"] != attempt["generation"]):
                raise DomainError("stale_attempt", "Result is not for the current work version")
            recollection = None
            if payload.get('recollection_id'):
                from agentflow.control.coding_result_recovery import guard_recollection, verify_evidence
                recollection = guard_recollection(tx, payload['recollection_id'], attempt_id=attempt_id,
                                                   work_item_id=item['id'])
                verify_evidence(self.settings.data_dir, recollection)
                if payload['execution_status'] != 'completed' or payload['quality_result'] != 'unknown':
                    raise DomainError('invalid_recollection_result', '重新收集不能宣称代码已经通过审查或测试。')
            if not recollection and item["status"] not in {"running", "waiting_execution", "cancel_requested"}:
                raise DomainError("attempt_not_running", "Result cannot change a finished attempt")
            status, quality = payload["execution_status"], payload["quality_result"]
            if status != "completed" and quality == "passed":
                raise DomainError("invalid_result", "Failed or unknown execution cannot pass", 422)
            if item["step"] in EXECUTION_STEPS and status == "completed":
                if not payload.get("verified_check_ids"):
                    raise DomainError("missing_test_evidence", "Execution requires independently verified checks")
                for check_id in payload["verified_check_ids"]:
                    check = required(tx, "check", check_id)
                    if not check.get("evidence_verified") or (quality == "passed" and check.get("quality_result") != "passed"):
                        raise DomainError("invalid_test_evidence", "A required test is not verified and passing")
            if status == "completed" and not verified_artifacts:
                raise DomainError("missing_artifact", "Execution completion requires collected output")
            if item["status"] == "cancel_requested":
                status, quality = "cancelled", "unknown"
            if review_disposition_result is not None and status == 'completed':
                from agentflow.control.review_contract_repair import ReviewContractRepair
                if item['step'] != 'review_disposition':
                    raise DomainError('review_contract_binding_invalid', '当前任务不能安排审查返工。')
                ReviewContractRepair(self.store, self).apply_disposition(tx, {'work_item_id': item['id']}, review_disposition_result)
            if planning_batch is not None and status == 'completed':
                from agentflow.domain.expansion import StageExpander
                if item['step'] not in {'goal', 'development_plan'} or item.get('parent_stage_id'):
                    raise DomainError('invalid_planning_producer', '只有当前规划阶段可以提交并行任务计划。')
                expanded = StageExpander(self.store).apply_batch(tx, item['run_id'], planning_batch['proposals'],
                    producer={'work_item_id': item['id'], 'attempt_id': attempt_id,
                              'fencing_token': item['fencing_token'], 'input_fingerprint': item['input_fingerprint']},
                    contract=planning_batch.get('contract'))
                tx.put('planning_commit', attempt_id, {'run_id': item['run_id'], 'work_item_id': item['id'],
                    'attempt_id': attempt_id, 'generation': item['generation'],
                    'result_digest': planning_batch['result_digest'], 'batch_digest': canonical_digest(planning_batch),
                    'stage_ids': [entry['stage']['id'] for entry in expanded], 'created_at': utc_now()})
                tx.event('planning.committed', {'attempt_id': attempt_id,
                    'stage_ids': [entry['stage']['id'] for entry in expanded]}, run_id=item['run_id'])
            artifacts = []
            for artifact in verified_artifacts:
                artifacts.append(tx.put("artifact", new_id(), {**artifact, "step": item["step"],
                    "work_item_id": item["id"], "run_id": item["run_id"], "generation": item["generation"],
                    "input_fingerprint": item["input_fingerprint"], "stale": False, "created_at": utc_now()}))
            output = output_fingerprint(item, artifacts)
            complete = status == "completed"
            approval_satisfied = (item.get("approved_fingerprint") is not None
                                  and payload.get("satisfied_approval_fingerprint") == item["approved_fingerprint"])
            state = "waiting_approval" if complete and item["approval_required"] and not approval_satisfied else "completed" if complete else status
            if recollection:
                item = {k: v for k, v in item.items() if k not in {'blocking_reason', 'runtime_failure_code'}}
            updated = tx.put("work_item", item["id"], {**item, "status": state, "quality_result": quality,
                **({'failure_diagnostic': payload['failure_diagnostic']} if payload.get('failure_diagnostic') else {}),
                "artifact_ids": [a["id"] for a in artifacts], "output_fingerprint": output}, item["revision"])
            tx.put("attempt", attempt_id, {**attempt, "status": status, "execution_status": status,
                "quality_result": quality, "finished_at": attempt.get('finished_at') if recollection else utc_now(),
                **({'result_recollection_id': payload['recollection_id'], 'recollected_at': utc_now()} if recollection else {}),
                "summary": payload.get("summary", ""),
                **({'failure_diagnostic': payload['failure_diagnostic']} if payload.get('failure_diagnostic') else {}),
                "runtime_failure_code": runtime_failure_code(payload.get("runtime_failure_code"))}, attempt["revision"])
            if state == "waiting_approval":
                tx.put("approval", new_id(), {"run_id": item["run_id"], "work_item_id": item["id"],
                    "fingerprint": output, "generation": item["generation"], "decision": None, "stale": False})
            tx.event("attempt.finished", {"attempt_id": attempt_id, "execution_status": status,
                     "quality_result": quality, "work_status": state}, run_id=item["run_id"])
            if recollection:
                tx.event('attempt.result_recollected', {'attempt_id': attempt_id,
                    'recollection_id': payload['recollection_id']}, run_id=item['run_id'])
            self._recompute_run(tx, item["run_id"])
            return updated
        result = await self.store.command("attempt.finish", key, {"attempt_id": attempt_id, **payload,
            "artifacts": verified_artifacts, **({'planning_batch': planning_batch} if planning_batch is not None else {}),
            **({'review_disposition_result': review_disposition_result} if review_disposition_result is not None else {})}, finish)
        # Presentation files are recoverable copies of immutable artifacts. A
        # filesystem problem cannot rewrite execution evidence or approvals.
        from agentflow.control.presentation import RunPresentationService
        try:
            await RunPresentationService(self.store, self.artifacts, self.settings).document(result)
        except Exception:
            logging.getLogger(__name__).exception('Readable output copy needs recovery; execution evidence retained')
        return result

    def _recompute_run(self, tx, run_id: str) -> None:
        run = required(tx, "run", run_id)
        items = [i for i in tx.list("work_item") if i["run_id"] == run_id and i.get("required", True)]
        active = any(i["status"] in {"running", "cancel_requested", "waiting_execution", "execution_unknown"} for i in items)
        changes = {}
        if run["execution_state"] == "cancelling" and not active:
            changes["execution_state"] = "cancelled"
        elif items and all(i["status"] == "completed" for i in items):
            changes["execution_state"] = "completed"
            changes["quality_result"] = ("passed" if run.get("delivery_ids") else "failed" if any(i["quality_result"] == "failed" for i in items)
                                         else "passed" if all(i["quality_result"] == "passed" for i in items) else "unknown")
        elif any(i["status"] in {"failed", "execution_unknown", "blocked"} or i["quality_result"] == "failed"
                 or (i["status"] == "completed" and i["step"] in EXECUTION_STEPS and i["quality_result"] != "passed") for i in items):
            changes["blocking_reasons"] = [f"{i['step']}: {i['status']}" for i in items
                                            if i["status"] in {"failed", "execution_unknown", "blocked"} or i["quality_result"] == "failed"
                                            or (i["status"] == "completed" and i["step"] in EXECUTION_STEPS and i["quality_result"] != "passed")]
        elif run.get('blocking_reasons'):
            changes['blocking_reasons'] = []
        if changes:
            tx.put("run", run_id, {**run, **changes}, run["revision"])

    async def decide(self, approval_id: str, payload: dict, key: str) -> dict:
        def decision(tx):
            approval = required(tx, "approval", approval_id)
            ensure_revision(approval, payload["expected_revision"])
            item = required(tx, "work_item", approval["work_item_id"])
            if (approval["stale"] or approval["decision"] is not None or item["status"] != "waiting_approval"
                    or payload["expected_fingerprint"] != approval["fingerprint"]
                    or item["output_fingerprint"] != approval["fingerprint"]):
                raise DomainError("stale_approval", "Approval no longer matches the current output")
            accepted = payload["decision"] == "approve"
            if not accepted and (not payload.get("reason") or not payload.get("change_expectation")):
                raise DomainError("rejection_reason_required", "Describe the required correction", 422)
            value = tx.put("approval", approval_id, {**approval, "decision": payload["decision"],
                "reason": payload.get("reason", ""), "decided_at": utc_now(), "actor": "owner"}, approval["revision"])
            tx.put("approval_decision", new_id(), {"approval_id": approval_id, **payload, "actor": "owner"})
            if accepted:
                state = "pending_delivery" if approval.get("kind") == "delivery" else "completed"
                tx.put("work_item", item["id"], {**item, "status": state,
                    "approved_fingerprint": approval["fingerprint"]}, item["revision"])
            else:
                root = payload.get("return_to_work_item_id") or item["id"]
                target = required(tx, "work_item", root)
                if target["run_id"] != item["run_id"]:
                    raise DomainError("invalid_return_target", "Cannot return work to another run")
                all_items = [i for i in tx.list("work_item") if i["run_id"] == item["run_id"]]
                run = required(tx, "run", item["run_id"])
                if run.get("delivery_ids") and target["step"] != "retrospective":
                    raise DomainError("delivered_run", "Start a new iteration to change already delivered code")
                if item["id"] not in descendants(all_items, {root}):
                    raise DomainError("invalid_return_target", "Return target must be the producer or an ancestor")
                affected = descendants(all_items, self._expanded_roots(all_items, {root}))
                if any(i["status"] in {"running", "cancel_requested", "execution_unknown", "waiting_execution"}
                       for i in all_items if i["id"] in affected):
                    raise DomainError("active_work", "Confirm affected attempts have stopped before returning to an earlier stage")
                affected = self._invalidate(tx, all_items, {root}, payload["change_expectation"])
                if target["step"] != "retrospective":
                    tx.put("run", run["id"], {**run, "quality_result": "unknown", "blocking_reasons": [],
                        "input_fingerprint": canonical_digest({"previous": run["input_fingerprint"],
                            "approval_id": approval_id, "affected": sorted(affected), "change": payload["change_expectation"]})}, run["revision"])
            tx.event("approval.decided", {"approval_id": approval_id, "decision": payload["decision"]}, run_id=item["run_id"])
            self._recompute_run(tx, item["run_id"])
            return value
        return await self.store.command("approval.decide", key, {"approval_id": approval_id, **payload}, decision)

    @staticmethod
    def _expanded_roots(items: list[dict], roots: set[str]) -> set[str]:
        expanded = set(roots)
        while True:
            added = {i["id"] for i in items if i.get("parent_stage_id") in expanded} - expanded
            if not added:
                return expanded
            expanded |= added

    def _invalidate(self, tx, all_items: list[dict], roots: set[str], reason: str, *,
                    expand_roots: bool = True, preserve_stage_ids: frozenset[str] = frozenset(),
                    inherit_model_settings: bool = True) -> set[str]:
        from agentflow.control.recovery import inherit_recovery_model_binding
        correction_roots = self._expanded_roots(all_items, roots) if expand_roots else roots
        affected = descendants(all_items, correction_roots)
        replanning = any(i["step"] in {'goal', 'development_plan'} and i["id"] in affected for i in all_items)
        reset_groups = {i["id"] for i in all_items if replanning and i["id"] in affected
                        and i.get("kind") == "aggregation" and i["id"] not in preserve_stage_ids}
        for item in all_items:
            if item["id"] not in affected:
                continue
            tx.put("work_revision", new_id(), {"work_item_id": item["id"], "snapshot": item, "reason": reason})
            revised = {**item, "generation": item["generation"] + 1,
                "status": "pending", "quality_result": "unknown", "fencing_token": item["fencing_token"] + 1,
                "attempt_id": None, "artifact_ids": [], "output_fingerprint": None, "approved_fingerprint": None,
                "payload": {**item.get("payload", {})}}
            # Descendants must regenerate their own stage output from updated
            # inputs. A producer's repair request is not their assigned task.
            if item['id'] in correction_roots:
                revised['payload']['change_expectation'] = reason
            else:
                revised['payload'].pop('change_expectation', None)
            revised["payload"].pop("repair_base_snapshot_id", None)
            revised["payload"].pop("recovery_checkpoint_id", None)
            revised["payload"].pop("recovery_instruction", None)
            revised["payload"].pop("bounded_coding_recovery", None)
            revised["payload"].pop("coding_step_checkpoint_id", None)
            revised["payload"].pop("role_output_checkpoint_id", None)
            revised["payload"].pop("planning_recovery_diagnostic", None)
            contract_id = revised['payload'].get('review_contract_task')
            if contract_id:
                contract = tx.get('review_contract_repair', contract_id)
                original_base = (contract or {}).get('work_specs', {}).get(item['id'], {}).get('payload', {}).get('repair_base_snapshot_id')
                if original_base:
                    revised['payload']['repair_base_snapshot_id'] = original_base
            for field in ('blocking_reason', 'runtime_failure_code', 'failure_diagnostic', 'candidate_id', 'execution_phase'):
                revised.pop(field, None)
            if item["id"] in reset_groups:
                revised.update(kind="stage", dependencies=item["original_dependencies"], write_paths=item["original_write_paths"])
                for field in ("expanded_child_ids", "expansion_fingerprint", "original_dependencies", "original_write_paths"):
                    revised.pop(field, None)
            if item.get("parent_stage_id") in reset_groups:
                revised.update(status="superseded", required=False, archived=True)
            if inherit_model_settings and not revised.get('archived'):
                inherit_recovery_model_binding(tx, item, revised, reason)
            else:
                # Explicit recovery issues its own selection receipt after invalidation.
                revised['payload'].pop('recovery_model_binding', None)
            tx.put("work_item", item["id"], revised, item["revision"])
        for approval in tx.list("approval"):
            if approval["work_item_id"] in affected and not approval["stale"]:
                tx.put("approval", approval["id"], {**approval, "stale": True}, approval["revision"])
        for artifact in tx.list("artifact"):
            if artifact.get("work_item_id") in affected and not artifact.get("stale"):
                tx.put("artifact", artifact["id"], {**artifact, "stale": True}, artifact["revision"])
        for kind in ('code_snapshot', 'check', 'review'):
            for record in tx.list(kind):
                if record.get('work_item_id') in affected and not record.get('stale'):
                    tx.put(kind, record['id'], {**record, 'stale': True}, record['revision'])
        return affected

    async def revise(self, run_id: str, payload: dict, key: str) -> dict:
        def revise(tx):
            run = required(tx, "run", run_id)
            ensure_revision(run, payload["expected_revision"])
            from agentflow.control.product_management import guard_product_run
            guard_product_run(tx, run)
            if run["execution_state"] == "publishing":
                raise DomainError("delivery_in_progress", "Wait for the bounded Git publication to finish")
            if run.get("delivery_ids"):
                raise DomainError("delivered_run", "Create a new iteration to revise delivered code")
            roots = set(payload["work_item_ids"])
            all_items = [i for i in tx.list("work_item") if i["run_id"] == run_id]
            if not roots or not roots <= {i["id"] for i in all_items}:
                raise DomainError("invalid_revision", "Revision must name existing work in this run", 422)
            if any(i["status"] in {"running", "cancel_requested", "execution_unknown", "waiting_execution"}
                   for i in all_items if i["id"] in descendants(all_items, self._expanded_roots(all_items, roots))):
                raise DomainError("active_work", "Pause and confirm affected attempts have stopped before revision")
            affected = self._invalidate(tx, all_items, roots, payload["reason"])
            tx.put("run", run_id, {**run, "execution_state": "running", "quality_result": "unknown",
                "blocking_reasons": [], "input_fingerprint": canonical_digest({"prior": run["input_fingerprint"],
                "reason": payload["reason"], "affected": sorted(affected)})}, run["revision"])
            tx.event("run.revised", {"affected_work_items": sorted(affected)}, run_id=run_id)
            return {"affected_work_items": sorted(affected)}
        return await self.store.command("run.revise", key, {"run_id": run_id, **payload}, revise)

    async def block_attempt(self, attempt_id: str, reason: str, key: str, *, failure_code: str | None = None,
                            failure_diagnostic: dict | None = None) -> dict:
        from agentflow.runtime.failures import runtime_failure_code
        if failure_code is not None and runtime_failure_code(failure_code) is None:
            raise DomainError('invalid_failure_code', '执行失败类型无效，未修改工作状态。')
        diagnostic = {'runtime_failure_code': failure_code} if failure_code is not None else {}
        if failure_diagnostic is not None:
            if not isinstance(failure_diagnostic, dict) or not isinstance(failure_diagnostic.get('code'), str):
                raise DomainError('invalid_failure_diagnostic', '执行诊断格式无效。')
            diagnostic['failure_diagnostic'] = failure_diagnostic
        def block(tx):
            attempt = required(tx, "attempt", attempt_id)
            item = required(tx, "work_item", attempt["work_item_id"])
            if item["attempt_id"] != attempt_id or item["fencing_token"] != attempt["fencing_token"]:
                raise DomainError("stale_attempt", "Attempt no longer owns this work")
            run = required(tx, 'run', item['run_id'])
            if (item.get('status') in {'completed', 'failed', 'cancelled', 'waiting_approval', 'pending_delivery'}
                    or attempt.get('status') in {'completed', 'failed', 'cancelled'}
                    or run.get('execution_state') == 'cancelled'):
                # An asynchronous failure callback cannot replace an accepted
                # outcome, reconciled receipt, or the owner's cancellation.
                return item
            tx.put("attempt", attempt_id, {**attempt, "status": "blocked", "summary": reason, **diagnostic}, attempt["revision"])
            updated = tx.put("work_item", item["id"], {**item, "status": "blocked", "blocking_reason": reason, **diagnostic}, item["revision"])
            tx.event("attempt.blocked", {"attempt_id": attempt_id, "reason": reason, **diagnostic}, run_id=item["run_id"])
            self._recompute_run(tx, item["run_id"])
            return updated
        return await self.store.command("attempt.block", key, {"attempt_id": attempt_id, "reason": reason, **diagnostic}, block)

    async def run_detail(self, run_id: str) -> dict:
        run = await self.store.read("run", run_id)
        if run is None:
            raise DomainError("not_found", "Unknown run", 404)
        items = [i for i in await self.store.list("work_item") if i["run_id"] == run_id and not i.get("archived")]
        approvals = [a for a in await self.store.list("approval") if a["run_id"] == run_id and not a["stale"]]
        return {**run, "work_items": items,
                "active_attempt_count": sum(i["status"] == "running" for i in items),
                "pending_approval_count": sum(a["decision"] is None for a in approvals),
                "completed_work_count": sum(i["status"] == "completed" for i in items),
                "total_work_count": len(items)}

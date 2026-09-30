from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from agentflow.adapters.codex import CodexExecAdapter
from agentflow.adapters.openhands import OpenHandsRoleAdapter
from agentflow.common import DomainError, canonical_digest
from agentflow.models.service import ModelService

from .contracts import TaskEnvelope
from .sandbox import default_sandbox
from .supervisor import Supervisor
from .workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class RuntimeService:
    CODING_STEPS = {"implementation", "unit_test_implementation", "integration_test_implementation", "coding", "repair",
                    'review_unit_migration', 'review_integration_migration'}

    def __init__(self, store, data_dir: Path, model_service: ModelService, *, sandbox=None, codex_executable=None,
                 settings=None):
        self.store = store
        self.data_dir = Path(data_dir).resolve()
        self.models = model_service
        self.supervisor = Supervisor(store, self.data_dir,
            handshake_timeout=settings.agent_startup_timeout_seconds if settings else 60)
        self.sandbox = sandbox or default_sandbox(self.data_dir / "sandbox_profiles",
            probe_timeout_seconds=settings.isolation_probe_timeout_seconds if settings else 30)
        self.workspaces = WorkspaceManager(self.data_dir)
        self.codex = CodexExecAdapter(self.supervisor, self.sandbox, executable=codex_executable)
        self.openhands = OpenHandsRoleAdapter(self.supervisor, self.sandbox)

    async def probe(self) -> list[dict]:
        return [await self.codex.probe(), await self.openhands.probe()]

    async def _envelope(self, task: dict, *, resume: bool = False, backend: str | None = None):
        coding = backend == "codex_exec" if backend else (
            task["role"] == "development" or task.get("step") in self.CODING_STEPS
        )
        profile_id = task.get("profile_id", task.get("model_profile_id"))
        profile = await self.models.registry.get(profile_id)
        protocol = "responses" if coding else "chat_completions"
        reasoning_effort = task.get('reasoning_effort') if coding else None
        if coding and reasoning_effort != profile.reasoning_effort:
            raise DomainError('reasoning_policy_mismatch', 'Frozen coding task reasoning policy differs from its model profile', 403)
        if not resume:
            profile.assert_accepted(protocol)
            secret = self.models.secret_resolver(profile)
            secret = await secret if inspect.isawaitable(secret) else secret
            if not secret:
                raise DomainError("credential_missing", "Configured model credential is unavailable")
            if task.get("cost_mode", "strict") == "strict":
                if not profile.pricing:
                    raise DomainError("budget_unbounded", "Model pricing bound is not configured")
                profile.pricing.reservation_cost(min(task.get("max_output_tokens", 4096), profile.max_output_tokens))
        workspace_path = Path(task["workspace"])
        workspace = workspace_path.resolve(strict=True)
        if coding:
            self.workspaces.assert_owned(workspace_path, attempt_id=task['attempt_id'])
        expected_artifacts = self.data_dir / "attempt_artifacts" / canonical_digest(task["attempt_id"]).split(":")[1]
        artifacts = Path(task.get("artifact_dir") or expected_artifacts).resolve()
        if artifacts != expected_artifacts or not artifacts.is_relative_to(self.data_dir / "attempt_artifacts"):
            raise DomainError("unsafe_artifact_directory", "Artifacts must use this attempt's controller-owned staging", 403)
        artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
        writes = [Path(path) if Path(path).is_absolute() else workspace / path for path in task.get("allowed_write_paths", [])]
        # Globs are not silently interpreted as a broad workspace grant.
        if any(any(marker in str(path) for marker in ["*", "?", "["]) for path in writes):
            raise DomainError("invalid_write_scope", "Write scope must contain explicit files/directories", 422)
        protected = [self.data_dir / name for name in ["state", "auth", "secrets", "nodes", "local_execution", "supervisor", "model_invocations", "workspace_metadata", "repositories"]]
        protected += [Path(path) for path in task.get("protected_paths", [])]
        from agentflow.configuration import configuration_path
        protected += [configuration_path(), configuration_path().parent / 'active-instance.json',
                      configuration_path().parent / 'cli_submissions']
        context_directory = Path(task['context_directory']) if task.get('context_directory') else None
        readable_roots = [Path(path) for path in task.get('allowed_read_paths', [])]
        if context_directory is not None:
            if (context_directory.parent != self.data_dir / 'stage_context'
                    or context_directory.is_symlink() or not context_directory.is_dir()
                    or context_directory.resolve() != context_directory
                    or context_directory not in readable_roots
                    or len(context_directory.name) != 64
                    or any(character not in '0123456789abcdef' for character in context_directory.name)):
                raise DomainError('unsafe_context', 'Task context must use its controller-owned input directory', 403)
        envelope = TaskEnvelope(
            attempt_id=task["attempt_id"], operation_id=task.get("operation_id", task["attempt_id"]),
            work_item_id=task.get("work_item_id", task["attempt_id"]), run_id=task["run_id"],
            iteration_id=task["iteration_id"], role=task["role"], goal=task["goal"],
            planning_contract=task.get('planning_contract'),
            review_phase_contract=task.get('review_phase_contract') if task['role'] == 'review' else None,
            input_fingerprint=task["input_fingerprint"], fencing_token=task.get("fencing_token", 1),
            workspace=workspace, artifact_dir=artifacts, allowed_write_roots=writes if coding else [],
            allowed_read_roots=readable_roots, context_directory=context_directory,
            protected_roots=protected, allow_code_write=coding,
            model_profile_id=profile.model_profile_id, model=profile.accepted_api_model or profile.requested_model,
            proxy_base_url=task.get("model_proxy_url", task.get("proxy_base_url", "http://127.0.0.1:8787/internal/v1/llm")),
            proxy_token=SecretStr(task.get("task_token", "") if not resume else ""),
            max_active_seconds=task.get("deadline_seconds", 300),
            max_output_tokens=min(task.get("max_output_tokens", 4096), profile.max_output_tokens),
            reasoning_effort=reasoning_effort,
            max_tool_calls=task.get("max_tool_calls", 30), max_iterations=task.get("max_iterations", 30),
            max_log_bytes=task.get('max_log_bytes', 16 * 1024 * 1024),
            require_hard_tool_limit=task.get("require_hard_tool_limit", False),
            allowed_web_hosts=task.get("allowed_web_hosts", []), allow_public_web=task.get("allow_public_web", False),
            output_schema=task.get("output_schema", {"type": "object"}),
        )
        if not resume and not envelope.proxy_token.get_secret_value():
            raise DomainError("task_token_missing", "Scoped model proxy token is required", 403)
        return envelope, self.codex if coding else self.openhands

    async def execute_task(self, task: dict[str, Any]) -> dict[str, Any]:
        envelope, adapter = await self._envelope(task)
        try:
            work = await self.store.read('work_item', task.get('work_item_id', task['attempt_id']))
            if work and work.get('payload', {}).get('role_output_checkpoint_id'):
                if getattr(envelope, 'allow_code_write', False):
                    raise DomainError('invalid_role_output_checkpoint', '编码执行不能导入角色草稿。')
                # A resumed or already supervised process owns its existing
                # builder state. Never import over that live/observed namespace.
                if await self.store.read('supervised_attempt', task['attempt_id']) is None:
                    from agentflow.control.recovery import restore_role_output_checkpoint
                    from agentflow.settings import Settings
                    try:
                        await restore_role_output_checkpoint(self.store, Settings(data_dir=self.data_dir), work, envelope)
                    except (DomainError, OSError, ValueError, TypeError, KeyError) as error:
                        raise DomainError('invalid_role_output_checkpoint', '角色草稿恢复检查未通过，Agent 尚未启动。') from error
            await adapter.start(envelope)
        except DomainError as error:
            if error.code in {'isolation_unverified', 'isolation_probe_timeout', 'unsafe_sandbox_roots',
                              'sdk_version_unverified', 'capability_unverified', 'invalid_role_output_checkpoint',
                              'coding_workspace_unwritable'}:
                from agentflow.runtime.prelaunch import record_prelaunch_failure
                try:
                    await record_prelaunch_failure(self.store, self.data_dir, task,
                        phase='envelope_validation' if error.code == 'invalid_role_output_checkpoint' else 'sandbox_validation' if error.code in {'isolation_unverified',
                            'isolation_probe_timeout', 'unsafe_sandbox_roots', 'coding_workspace_unwritable'} else 'adapter_preflight',
                        failure_code=error.code)
                except Exception:
                    logger.exception('Prelaunch failure could not be sealed; recovery must remain conservative')
            raise
        handle = await self.supervisor.wait(envelope.attempt_id)
        if handle.input_fingerprint != envelope.input_fingerprint or handle.fencing_token != envelope.fencing_token:
            raise DomainError("stale_attempt", "Attempt input or fence changed")
        self._check_startup_outcome(handle)
        result = await adapter.collect_artifacts(envelope.attempt_id, envelope)
        if envelope.allow_code_write:
            result["workspace"] = str(envelope.workspace)
            result["artifacts"] = result.get("artifacts", [])
        return result

    @staticmethod
    def _check_startup_outcome(handle):
        # This is only an uncertainty transition. The controller must prove
        # timeout revocation, stopped identity and zero calls before retrying.
        # Avoid collecting absent output as unknown coding usage.
        if handle.state == 'cancelled' and handle.reason == 'launch_not_authorized':
            raise DomainError('execution_unconfirmed', 'Launch handshake was not observed')

    async def collect_completed_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Read a finished worker's output without launching, recovering or waiting."""
        record = await self.store.read('supervised_attempt', task['attempt_id'])
        if not record or record.get('state') != 'completed' or record.get('backend') != 'codex_exec':
            raise DomainError('recollection_not_completed', '仅可重新收集已确认结束的编码执行。')
        envelope, adapter = await self._envelope(task, resume=True, backend='codex_exec')
        return await adapter.collect_artifacts(task['attempt_id'], envelope)

    async def resume_task(self, task: dict[str, Any]) -> dict[str, Any]:
        record = await self.store.read("supervised_attempt", task["attempt_id"])
        if record is None:
            return {"execution_status": "execution_unknown", "quality_result": "unknown", "artifacts": [],
                    "summary": "No persisted launch intent; no new process was started."}
        try:
            envelope, adapter = await self._envelope(task, resume=True, backend=record["backend"])
        except (DomainError, ValueError, KeyError, OSError):
            handle = await self.supervisor.recover(task["attempt_id"])
            return {"execution_status": "execution_unknown", "quality_result": "unknown", "artifacts": [],
                    "stdout_path": handle.stdout_path, "stderr_path": handle.stderr_path,
                    "summary": "Frozen execution configuration is unavailable; existing logs retained and no new process started."}
        handle = await adapter.recover(task["attempt_id"])
        if handle.input_fingerprint != envelope.input_fingerprint or handle.fencing_token != envelope.fencing_token:
            return {"execution_status": "execution_unknown", "quality_result": "unknown", "artifacts": [],
                    "summary": "Frozen input/fence changed; existing logs retained and no new execution started."}
        if handle.state in {"running", "cancelling"}:
            handle = await self.supervisor.wait(task["attempt_id"])
        self._check_startup_outcome(handle)
        return await adapter.collect_artifacts(task["attempt_id"], envelope)

    async def cancel(self, attempt_id: str):
        return await self.supervisor.cancel(attempt_id)

    async def close(self):
        await self.supervisor.close()

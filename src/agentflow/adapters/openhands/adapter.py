from __future__ import annotations

import asyncio
import importlib.metadata
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from agentflow.common import DomainError, canonical_digest
from agentflow.runtime.contracts import LaunchSpec, TaskEnvelope
from agentflow.runtime.failures import known_failure_reason, read_role_failure, runtime_failure_message
from agentflow.runtime.launcher import atomic_json
from agentflow.runtime.supervisor import Supervisor

from .output_builder import export_partial, result_identity


class OpenHandsRoleAdapter:
    def __init__(self, supervisor: Supervisor, sandbox):
        self.supervisor = supervisor
        self.sandbox = sandbox
        self._tasks: dict[str, TaskEnvelope] = {}

    async def probe(self) -> dict:
        try:
            version = importlib.metadata.version("openhands-sdk")
            transport_version = importlib.metadata.version("litellm")
        except importlib.metadata.PackageNotFoundError:
            return {"backend": "openhands_role", "available": False, "reason": "sdk_not_installed", "paid_requests_started": 0}
        return {"backend": "openhands_role", "available": version == "1.49.2" and transport_version == "1.101.0", "version": version,
                "transport_version": transport_version, "validated_transport_version": "1.101.0",
                "validated_sdk_api_version": "1.49.2", "tool_call_enforcement": "hard",
                "read_code_write_document_only": True, "live_verified": False, "paid_requests_started": 0}

    async def start(self, task: TaskEnvelope):
        task = TaskEnvelope.model_validate(task)
        if task.allow_code_write:
            raise DomainError("role_write_scope", "Professional role adapter cannot grant code writes", 403)
        probe = await self.probe()
        if not probe["available"]:
            raise DomainError("sdk_version_unverified", "Installed SDK has not passed this adapter's API contract")
        task.assert_paths()
        task.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        private = self.supervisor.root.parent / "openhands_homes" / canonical_digest(task.attempt_id).split(":")[1]
        private.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = private / "tmp"
        temporary.mkdir(exist_ok=True)
        source_root = Path(__file__).resolve().parents[3]
        task = task.model_copy(update={"allowed_read_roots": [*task.allowed_read_roots, source_root]})
        config_path = private / "task.json"
        prefix, evidence = await self.sandbox.prepare(task, Path(sys.executable).resolve(), private)
        if not evidence["verified"]:
            raise DomainError("isolation_unverified", "Role worker sandbox did not pass")
        atomic_json(config_path, task.model_dump(mode="json"))
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(private), "TMPDIR": str(temporary),
            "PYTHONPATH": str(source_root), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
            "AGENTFLOW_PROXY_TOKEN": task.proxy_token.get_secret_value(),
            "OTEL_SDK_DISABLED": "true", "LITELLM_LOCAL_MODEL_COST_MAP": "true", "DO_NOT_TRACK": "1",
            "LITELLM_LOG": "ERROR", "LOG_LEVEL": "ERROR",
        }
        self._tasks[task.attempt_id] = task
        return await self.supervisor.start(LaunchSpec(
            attempt_id=task.attempt_id, operation_id=task.operation_id, run_id=task.run_id,
            input_fingerprint=task.input_fingerprint, fencing_token=task.fencing_token,
            argv=prefix + [sys.executable, "-m", "agentflow.adapters.openhands.worker", str(config_path)],
            cwd=task.workspace, environment=env, timeout_seconds=task.max_active_seconds,
            max_log_bytes=task.max_log_bytes,
            backend="openhands_role", backend_version=probe["version"],
        ))

    async def inspect(self, attempt_id):
        return await self.supervisor.inspect(attempt_id)

    async def cancel(self, attempt_id):
        return await self.supervisor.cancel(attempt_id)

    async def recover(self, attempt_id):
        return await self.supervisor.recover(attempt_id)

    async def collect_artifacts(self, attempt_id, task: TaskEnvelope | None = None):
        task = task or self._tasks.get(attempt_id)
        if task is None:
            raise DomainError("task_context_missing", "Frozen task needed to validate artifacts")
        handle = await self.supervisor.inspect(attempt_id)
        result_path = task.artifact_dir / "role_result.json"
        if handle.state != "completed" or not result_path.is_file():
            checkpoint = await asyncio.to_thread(export_partial, task.artifact_dir, expected_identity=result_identity(task))
            code = 'execution_unconfirmed' if handle.state == 'execution_unknown' else known_failure_reason(handle.reason)
            if (not code and task.attempt_id == attempt_id and handle.input_fingerprint == task.input_fingerprint
                    and handle.fencing_token == task.fencing_token):
                code = read_role_failure(self.supervisor.root.parent, attempt_id)
            if (handle.state == 'failed' and task.attempt_id == attempt_id
                    and handle.input_fingerprint == task.input_fingerprint and handle.fencing_token == task.fencing_token
                    and code in {None, 'worker_exited', 'worker_internal_error',
                    'model_authentication_failed', 'model_request_failed'} and getattr(self.supervisor, 'store', None)):
                from agentflow.runtime.task_authorization import authorization_failure
                code = await authorization_failure(self.supervisor.store, self.supervisor.root.parent, attempt_id,
                    fencing_token=task.fencing_token, input_fingerprint=task.input_fingerprint) or code
                if code in {None, 'worker_exited', 'worker_internal_error', 'model_authentication_failed', 'model_request_failed'}:
                    from agentflow.models.transport_failures import transport_failure_for_attempt
                    code = await transport_failure_for_attempt(self.supervisor.store, attempt_id,
                        fencing_token=task.fencing_token, input_fingerprint=task.input_fingerprint) or code
            code = code or ('invalid_model_output' if handle.state == 'completed' else 'worker_exited')
            details = None
            if code == 'planning_validation_failed':
                error_path = task.artifact_dir / 'role_error.json'
                if error_path.is_file() and not error_path.is_symlink():
                    details = json.loads(error_path.read_text()).get('failure_details')
            return {"execution_status": "failed" if handle.state == "completed" else handle.state,
                    "quality_result": "unknown", "artifacts": [checkpoint['path']] if checkpoint else [], "runtime_failure_code": code,
                    **({'role_output_checkpoint': checkpoint} if checkpoint else {}),
                    **({"failure_details": details} if details else {}),
                    "summary": runtime_failure_message(code),
                    "logs": [handle.stdout_path, handle.stderr_path]}
        result = json.loads(result_path.read_text())
        Draft202012Validator(task.output_schema).validate(result["result"])
        for value in result.get("artifacts", []):
            path = Path(value).resolve(strict=True)
            if not path.is_relative_to(task.artifact_dir.resolve()) or not path.is_file():
                raise DomainError("invalid_artifact", "Role artifact escaped its staging directory")
        return {**result, "quality_result": "unknown", "logs": [handle.stdout_path, handle.stderr_path]}

    async def collect_usage(self, attempt_id):
        return {"status": "use_proxy_ledger", "attempt_id": attempt_id}

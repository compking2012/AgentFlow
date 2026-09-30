from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.runtime.codex_output import parse_codex_final, write_normalized_final
from agentflow.runtime.codex_usage import observed_tool_usage
from agentflow.runtime.coding_tools import discover_coding_tools
from agentflow.runtime.contracts import LaunchSpec, TaskEnvelope
from agentflow.runtime.events import CodexEventNormalizer
from agentflow.runtime.failures import classify_codex_failure, runtime_failure_message
from agentflow.runtime.supervisor import Supervisor


def _execution_prompt(task: TaskEnvelope) -> str:
    interface = ("\n\nExecution interface: Return one final JSON object matching the exact schema; no extra fields or Markdown fences. "
                 "Use small, focused patches and bounded output. Do not echo long documents, whole files or the full prompt. "
                 "Batch independent inspections; reuse unchanged evidence and preserve all acceptance criteria. "
                 "For reviewed defects, inspect affected code and dependencies, fix precisely, and run focused checks; "
                 "retain all required final gates. ")
    if task.context_directory is not None:
        interface += (
            'AGENTFLOW_CONTEXT_DIR contains the exact controller-authorized read-only input directory. '
            'Use "$AGENTFLOW_CONTEXT_DIR" in shell commands or process.env.AGENTFLOW_CONTEXT_DIR in Node '
            'instead of retyping its hash. Read the relevant input files listed above from that directory. '
            'If a filename is unclear, list that directory; do not search / or unrelated owner directories. ')
    _, marker, contract = task.goal.rpartition('\nFinal response contract:')
    supplied = False
    if marker and '\n' in contract:
        try:
            declared = json.loads(contract.rsplit('\n', 1)[1])
            supplied = isinstance(declared, dict) and canonical_digest(declared) == canonical_digest(task.output_schema)
        except ValueError:
            pass
    if not supplied:
        interface += "Final output schema: " + json.dumps(task.output_schema, separators=(',', ':'))
    return task.goal + interface


class CodexExecAdapter:
    def __init__(self, supervisor: Supervisor, sandbox, executable: str | Path | None = None):
        resolved = executable or shutil.which("codex")
        self.executable = Path(resolved).resolve() if resolved else None
        self.supervisor = supervisor
        self.sandbox = sandbox
        self.events = CodexEventNormalizer()
        self._tasks: dict[str, TaskEnvelope] = {}

    async def probe(self) -> dict:
        if self.executable is None or not self.executable.is_file():
            return {"backend": "codex_exec", "available": False, "reason": "executable_missing", "paid_requests_started": 0}
        outputs = []
        for args in [["--version"], ["exec", "--help"]]:
            process = await asyncio.create_subprocess_exec(
                str(self.executable), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise DomainError("backend_probe_timeout", "Codex offline probe timed out")
            if process.returncode:
                raise DomainError("backend_probe_failed", "Codex help/version probe failed")
            outputs.append(stdout.decode(errors="replace") + stderr.decode(errors="replace"))
        required = ["--json", "--output-schema", "--ephemeral", "--ignore-user-config", "--sandbox"]
        return {
            "backend": "codex_exec", "available": all(flag in outputs[1] for flag in required),
            "version": outputs[0].strip(), "executable": str(self.executable), "checked_at": utc_now(),
            "flags": {flag: flag in outputs[1] for flag in required},
            "tool_call_enforcement": "observed", "sandbox_enforcement": "unverified_until_task_probe",
            "session_resume": "unsupported_ephemeral", "live_verified": False, "paid_requests_started": 0,
            "evidence_digest": canonical_digest(outputs),
        }

    async def start(self, task: TaskEnvelope):
        task = TaskEnvelope.model_validate(task)
        if not task.allow_code_write:
            raise DomainError("coding_not_authorized", "Task did not authorize code writes", 403)
        if task.require_hard_tool_limit:
            raise DomainError("capability_unverified", "Codex JSONL is not a pre-execution tool-count hook")
        task.assert_paths()
        probe = await self.probe()
        if not probe["available"]:
            raise DomainError("capability_unverified", "Required Codex exec interface is unavailable")
        Draft202012Validator.check_schema(task.output_schema)
        private = self.supervisor.root.parent / "codex_homes" / canonical_digest(task.attempt_id).split(":")[1]
        private.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (private / "auth.json").exists():
            raise DomainError("unexpected_authentication", "Per-attempt CODEX_HOME contains authentication state")
        task.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        schema_path = private / "output_schema.json"
        schema_path.write_text(json.dumps(task.output_schema))
        final_path = task.artifact_dir / "codex_final.json"
        temporary = private / "tmp"
        temporary.mkdir(exist_ok=True)
        coding_tools = discover_coding_tools()
        quote = json.dumps
        overrides = {
            "model_provider": quote("agentflow"),
            "model_providers.agentflow.name": quote("AgentFlow task proxy"),
            "model_providers.agentflow.base_url": quote(task.proxy_base_url),
            "model_providers.agentflow.wire_api": quote("responses"),
            "model_providers.agentflow.env_key": quote("AGENTFLOW_PROXY_TOKEN"),
            "model_providers.agentflow.requires_openai_auth": "false",
            "model_providers.agentflow.request_max_retries": "0",
            "model_providers.agentflow.stream_max_retries": "0",
            "approval_policy": quote("never"),
            "shell_environment_policy.inherit": quote("none"),
            "shell_environment_policy.set.PATH": quote(coding_tools.path),
            "shell_environment_policy.set.HOME": quote(str(private)),
            "shell_environment_policy.set.TMPDIR": quote(str(temporary)),
            # zsh here-documents use TMPPREFIX rather than TMPDIR. Keep their
            # temporary files inside the existing private writable directory.
            "shell_environment_policy.set.TMPPREFIX": quote(str(temporary / 'zsh')),
            "shell_environment_policy.set.GIT_CONFIG_NOSYSTEM": quote("1"),
            "shell_environment_policy.set.GIT_CONFIG_GLOBAL": quote("/dev/null"),
            "features.multi_agent": "false",
            "features.spawn_csv": "false",
            "features.shell_snapshot": "false",
            "web_search": quote("disabled"),
            "mcp_servers": "{}",
        }
        if task.reasoning_effort is not None:
            overrides['model_reasoning_effort'] = quote(task.reasoning_effort)
        if task.context_directory is not None:
            overrides['shell_environment_policy.set.AGENTFLOW_CONTEXT_DIR'] = quote(str(task.context_directory))
        # The already verified outer OS sandbox is inherited by the CLI and every
        # tool subprocess. A nested Seatbelt sandbox is rejected by macOS; never
        # grant forbidden-sandbox-reinit, which could replace our restrictive policy.
        command = [str(self.executable), "exec", "--ignore-user-config", "--ephemeral", "--json",
                   "--sandbox", "danger-full-access", "--color", "never", "--model", task.model,
                   "--output-schema", str(schema_path), "--output-last-message", str(final_path), "--cd", str(task.workspace)]
        if "--ignore-rules" in (await self._help_text()):
            command += ["--ignore-rules"]
        for key, value in overrides.items():
            command += ["-c", f"{key}={value}"]
        command += ["-"]
        prefix, isolation = await self.sandbox.prepare(
            task, self.executable, private, runtime_read_roots=coding_tools.read_roots)
        if not isolation.get("verified") or isolation.get("filesystem") != "hard" or isolation.get("network") != "hard":
            raise DomainError("isolation_unverified", "Codex outer sandbox was not verified")
        environment = {
            "PATH": coding_tools.path, "HOME": str(private), "CODEX_HOME": str(private),
            "TMPDIR": str(temporary), "TMPPREFIX": str(temporary / 'zsh'),
            "AGENTFLOW_PROXY_TOKEN": task.proxy_token.get_secret_value(),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "TERM": "dumb", "NO_COLOR": "1",
        }
        if task.context_directory is not None:
            environment['AGENTFLOW_CONTEXT_DIR'] = str(task.context_directory)
        self._tasks[task.attempt_id] = task
        return await self.supervisor.start(LaunchSpec(
            attempt_id=task.attempt_id, operation_id=task.operation_id, run_id=task.run_id,
            input_fingerprint=task.input_fingerprint, fencing_token=task.fencing_token,
            argv=prefix + command, cwd=task.workspace, environment=environment, stdin_text=_execution_prompt(task),
            timeout_seconds=task.max_active_seconds, backend="codex_exec", backend_version=probe["version"],
            max_log_bytes=task.max_log_bytes,
            output_schema=task.output_schema, final_output_path=final_path,
        ))

    async def _help_text(self):
        proc = await asyncio.create_subprocess_exec(str(self.executable), "exec", "--help", stdout=asyncio.subprocess.PIPE, env={"PATH": "/usr/bin:/bin"})
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
        return out.decode(errors="replace")

    async def inspect(self, attempt_id):
        return await self.supervisor.inspect(attempt_id)

    async def cancel(self, attempt_id):
        return await self.supervisor.cancel(attempt_id)

    async def recover(self, attempt_id):
        # Reobserve a verified process/receipt; never invoke `codex resume --last`.
        return await self.supervisor.recover(attempt_id)

    async def collect_artifacts(self, attempt_id: str, task: TaskEnvelope | None = None) -> dict:
        task = task or self._tasks.get(attempt_id)
        if task is None:
            raise DomainError("task_context_missing", "Frozen task context is needed to collect results")
        handle = await self.supervisor.inspect(attempt_id)
        if (task.attempt_id != attempt_id or handle.input_fingerprint != task.input_fingerprint
                or handle.fencing_token != task.fencing_token):
            return {'execution_status': 'execution_unknown', 'quality_result': 'unknown', 'result': None,
                    'artifacts': [], 'errors': ['task_identity_mismatch'],
                    'runtime_failure_code': 'execution_unconfirmed',
                    'summary': runtime_failure_message('execution_unconfirmed'),
                    'logs': [handle.stdout_path, handle.stderr_path]}
        try:
            parsed = self.events.read(Path(handle.stdout_path))
        except OSError:
            parsed = {'events': [], 'errors': ['events_missing']}
        final_path = task.artifact_dir / "codex_final.json"
        result = None
        normalized = False
        # Event names and provider text stay in protected logs. Public errors use
        # only fixed categories, including when an unfamiliar event was received.
        errors = list(dict.fromkeys(error.split(':', 1)[0] for error in parsed['errors']))
        directory = file = None
        try:
            directory = os.open(task.artifact_dir, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
                                | getattr(os, 'O_NOFOLLOW', 0))
            file = os.open('codex_final.json', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                           | getattr(os, 'O_NONBLOCK', 0), dir_fd=directory)
            info = os.fstat(file)
            maximum = 2 * 1024 * 1024
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum
                    or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
                raise ValueError('invalid_final_file')
            raw = os.read(file, maximum + 1)
            if len(raw) > maximum:
                raise ValueError('oversized_final_file')
            try:
                result, normalized = parse_codex_final(raw, task.output_schema)
            except (ValueError, UnicodeError, ValidationError):
                result = None
                errors.append("final_schema_invalid")
        except OSError:
            errors.append("final_output_missing")
        except ValueError:
            errors.append('final_schema_invalid')
        finally:
            for descriptor in (file, directory):
                if descriptor is not None:
                    os.close(descriptor)
        code = classify_codex_failure(state=handle.state, reason=handle.reason, exit_code=handle.exit_code,
                                      events=parsed['events'], errors=errors)
        if handle.state == 'failed' and code in {'worker_exited', 'worker_internal_error'} and getattr(self.supervisor, 'store', None):
            from agentflow.runtime.task_authorization import authorization_failure
            code = await authorization_failure(self.supervisor.store, self.supervisor.root.parent, attempt_id,
                fencing_token=task.fencing_token, input_fingerprint=task.input_fingerprint) or code
            if code in {'worker_exited', 'worker_internal_error'}:
                from agentflow.models.transport_failures import transport_failure_for_attempt
                code = await transport_failure_for_attempt(self.supervisor.store, attempt_id,
                    fencing_token=task.fencing_token, input_fingerprint=task.input_fingerprint) or code
        completed = handle.state == 'completed' and not errors and code is None
        normalization = None
        if completed and normalized:
            try:
                final_path = write_normalized_final(task.artifact_dir, result)
                normalization = {'method': 'schema_validated_json_normalization',
                    'source_name': 'codex_final.json', 'artifact_name': final_path.name,
                    'source_digest': 'sha256:' + hashlib.sha256(raw).hexdigest(),
                    'schema_digest': canonical_digest(task.output_schema)}
            except (OSError, ValueError):
                completed, result = False, None
                errors.append('final_schema_invalid')
                code = 'final_schema_invalid'
        active_seconds = getattr(handle, 'active_seconds', None)
        usage = observed_tool_usage(parsed, handle.reason)
        summary = runtime_failure_message(code) if code else (
            '编码结果已收集；代码审查与测试结果由后续阶段确认。' if completed else
            '编码任务已取消。' if handle.state == 'cancelled' else '编码执行尚未结束，结果暂不可确认。')
        return {
            "execution_status": "completed" if completed else "failed" if handle.state == "completed" else handle.state,
            "quality_result": "unknown", "result": result if completed else None, "errors": errors,
            "artifacts": [str(final_path)] if completed else [], "runtime_failure_code": code,
            "logs": [handle.stdout_path, handle.stderr_path],
            "summary": summary,
            "active_seconds": active_seconds, **usage,
            **({'result_normalization': normalization} if normalization else {}),
        }

    async def collect_usage(self, attempt_id: str) -> dict:
        handle = await self.supervisor.inspect(attempt_id)
        result = self.events.read(Path(handle.stdout_path))
        return {"usage": result["usage"], "status": "reported" if result["usage"] else "unknown",
                "billing_authority": "model_proxy_ledger"}

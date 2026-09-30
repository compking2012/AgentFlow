"""Bounded process execution. Credentials never enter the project environment."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

import psutil
from pydantic import Field

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.execution.models import JobLimits, WireModel

SAFE_ENV = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL",
            "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE",
            "DBUS_SESSION_BUS_ADDRESS", "ANDROID_HOME", "ANDROID_SDK_ROOT", "JAVA_HOME",
            "DEVELOPER_DIR", "SDKROOT", "PLAYWRIGHT_BROWSERS_PATH", "AGENTFLOW_BROWSER_EXECUTABLE"}
PROHIBITED_ENV_PARTS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PRIVATE_KEY",
                        "CODEX", "OPENAI", "DEEPSEEK", "ANTHROPIC", "AGENTFLOW_OWNER", "SSLKEYLOG")


def child_environment(home: Path, additions: dict[str, str] | None = None) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    temp = home / "tmp"
    temp.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k in SAFE_ENV}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "TMPDIR": str(temp),
                "TMP": str(temp), "TEMP": str(temp), "CI": "1", "PYTHONNOUSERSITE": "1"})
    for key, value in (additions or {}).items():
        if any(part in key.upper() for part in PROHIBITED_ENV_PARTS):
            raise DomainError("forbidden_child_credential", f"Credential environment {key} is forbidden", 403)
        if key in {"LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "NODE_OPTIONS", "BASH_ENV", "ENV"}:
            raise DomainError("unsafe_child_environment", f"Loader override {key} is forbidden", 403)
        env[key] = value
    return env


class CommandSpec(WireModel):
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: Path
    label: str
    environment: dict[str, str] = Field(default_factory=dict)


class ProcessResult(WireModel):
    execution_status: Literal["completed", "error", "cancelled", "execution_unknown"]
    exit_code: int | None
    pid: int | None
    process_created: float | None = None
    process_fingerprint: str | None
    started_at: str
    finished_at: str
    stdout_path: str
    stderr_path: str
    output_truncated: bool = False
    reason: str | None = None
    cleanup_verified: bool = False


class ProcessExecutor:
    """Runs only adapter-generated argv, never a shell string.

    This supervisor enforces timeout/output/process limits by monitoring. It is
    not an OS security sandbox; node configuration must separately authorize a
    trusted-project mode or provide verified external account/container isolation.
    """

    async def execute(self, command: CommandSpec, output_dir: Path, limits: JobLimits,
                      cancel: asyncio.Event | None = None,
                      on_start: Callable[[int, str], Awaitable[None]] | None = None) -> ProcessResult:
        if not command.cwd.is_dir() or command.cwd.is_symlink():
            raise DomainError("invalid_working_directory", "A real isolated workspace is required", 422)
        output_dir.mkdir(parents=True, exist_ok=True)
        stdout_path, stderr_path = output_dir / "stdout.log", output_dir / "stderr.log"
        started_at = utc_now()
        env = child_environment(output_dir / "home", command.environment)
        kwargs = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000200}
        argv = list(command.argv)
        if on_start:
            argv = [sys.executable, "-I", str(Path(__file__).with_name("launcher.py")), "--", *argv]
        try:
            process = await asyncio.create_subprocess_exec(*argv, cwd=command.cwd, env=env,
                                                           stdin=asyncio.subprocess.PIPE if on_start else asyncio.subprocess.DEVNULL,
                                                           stdout=asyncio.subprocess.PIPE,
                                                           stderr=asyncio.subprocess.PIPE, **kwargs)
        except (OSError, ValueError) as exc:
            stdout_path.write_bytes(b"")
            stderr_path.write_text(str(exc))
            return ProcessResult(execution_status="error", exit_code=None, pid=None,
                                 process_fingerprint=None, started_at=started_at, finished_at=utc_now(),
                                 stdout_path=str(stdout_path), stderr_path=str(stderr_path), reason="spawn_failed",
                                 cleanup_verified=True)
        parent = psutil.Process(process.pid)
        fingerprint = canonical_digest({"pid": process.pid, "created": parent.create_time(),
                                         "argv": list(command.argv)})
        if on_start:
            try:
                await on_start(process.pid, fingerprint)
                process.stdin.write(b"RUN\n")
                await process.stdin.drain()
                process.stdin.close()
            except BaseException:
                await self._terminate(process)
                raise
        count = 0
        limit_hit = asyncio.Event()
        descendants: dict[int, float] = {}

        async def copy(stream: asyncio.StreamReader, path: Path) -> None:
            nonlocal count
            with path.open("wb") as target:
                while block := await stream.read(32768):
                    remaining = max(0, limits.maximum_output_bytes - count)
                    target.write(block[:remaining])
                    count += len(block)
                    if count > limits.maximum_output_bytes:
                        limit_hit.set()

        readers = [asyncio.create_task(copy(process.stdout, stdout_path)),
                   asyncio.create_task(copy(process.stderr, stderr_path))]
        reason = None
        begin = time.monotonic()
        try:
            while process.returncode is None:
                if cancel and cancel.is_set():
                    reason = "cancelled"
                elif limit_hit.is_set():
                    reason = "output_limit"
                elif time.monotonic() - begin >= limits.maximum_active_seconds:
                    reason = "timeout"
                try:
                    children = parent.children(recursive=True)
                    for child in children:
                        descendants[child.pid] = child.create_time()
                    if len(children) + 1 > limits.maximum_processes:
                        reason = "process_limit"
                    rss = sum(p.memory_info().rss for p in [parent, *children] if p.is_running())
                    if rss > limits.maximum_memory_bytes:
                        reason = "memory_limit"
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                if reason:
                    break
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            reason = "cancelled"
        finally:
            if reason and process.returncode is None:
                await self._terminate(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                reason = "cleanup_unconfirmed"
            # Child processes can outlive a successful parent, or keep pipes open.
            cleanup = await self._cleanup_descendants(descendants)
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    cleanup = False
            try:
                await asyncio.wait_for(asyncio.gather(*readers), timeout=3)
            except TimeoutError:
                cleanup = False
                for reader in readers:
                    reader.cancel()
            if not cleanup:
                reason = "cleanup_unconfirmed"
        status = "cancelled" if reason == "cancelled" else "execution_unknown" if not cleanup else (
            "completed" if process.returncode == 0 and reason is None else "error")
        return ProcessResult(execution_status=status, exit_code=process.returncode, pid=process.pid,
                             process_created=parent.create_time(),
                             process_fingerprint=fingerprint, started_at=started_at, finished_at=utc_now(),
                             stdout_path=str(stdout_path), stderr_path=str(stderr_path), reason=reason,
                             output_truncated=count > limits.maximum_output_bytes, cleanup_verified=cleanup)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "nt":
                for child in psutil.Process(process.pid).children(recursive=True):
                    child.terminate()
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=2)
        except (ProcessLookupError, psutil.NoSuchProcess):
            return
        except TimeoutError:
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    @staticmethod
    async def _cleanup_descendants(identities: dict[int, float]) -> bool:
        living = []
        for pid, created in identities.items():
            try:
                child = psutil.Process(pid)
                if child.create_time() == created and child.status() != psutil.STATUS_ZOMBIE:
                    child.kill()
                    living.append(child)
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                return False
        _, alive = await asyncio.to_thread(psutil.wait_procs, living, 2)
        return not alive

"""Small, durable child supervisor. Reads a private launch descriptor, never an LLM instruction."""
from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil

from agentflow.common import utc_now

from .process_birth import process_birth_identity
from .process_identity import current_boot_identity

STARTUP_CLOCK_FIELDS = (
    'startup_clock_version', 'startup_started_at', 'startup_started_monotonic',
    'startup_deadline_at', 'startup_deadline_monotonic', 'startup_window_seconds',
    'startup_boot_identity_source', 'startup_boot_fingerprint',
)


def startup_deadline(config: dict, boot_source: str, boot_fingerprint: str) -> float:
    """Versioned descriptors use the original same-boot deadline, never a fresh allowance."""
    if 'startup_clock_version' not in config:
        if any(key in config for key in STARTUP_CLOCK_FIELDS):
            raise ValueError('Incomplete startup clock')
        # Read-only compatibility for descriptors emitted before the startup clock.
        return time.monotonic() + config.get('handshake_timeout', 60)
    if type(config['startup_clock_version']) is not int or config['startup_clock_version'] != 1:
        raise ValueError('Invalid startup clock version')
    started, deadline, window = (config.get(key) for key in
        ('startup_started_monotonic', 'startup_deadline_monotonic', 'startup_window_seconds'))
    if (any(type(value) not in {int, float} or not math.isfinite(value) for value in
            (started, deadline, window)) or not 0 < window <= 60 or started < 0
            or not math.isclose(deadline - started, window, rel_tol=0, abs_tol=.000001)
            or config.get('startup_boot_identity_source') != boot_source
            or config.get('startup_boot_fingerprint') != boot_fingerprint):
        raise ValueError('Invalid startup clock')
    try:
        started_at = datetime.fromisoformat(config['startup_started_at'])
        deadline_at = datetime.fromisoformat(config['startup_deadline_at'])
        if (started_at.tzinfo is None or deadline_at.tzinfo is None
                or not math.isclose((deadline_at - started_at).total_seconds(), window,
                                    rel_tol=0, abs_tol=.000001)):
            raise ValueError('Invalid startup clock timestamps')
    except (KeyError, TypeError) as exc:
        raise ValueError('Invalid startup clock timestamps') from exc
    return deadline


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        os.chmod(temporary, 0o600)
        json.dump(value, file, ensure_ascii=False, allow_nan=False)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class RedactingSink:
    def __init__(self, path: Path, secrets: list[bytes], maximum: int):
        self.file = path.open("wb")
        os.chmod(path, 0o600)
        self.secrets = [value for value in secrets if value]
        self.tail = b""
        self.keep = max([len(value) for value in self.secrets] + [1])
        self.written = 0
        self.maximum = maximum
        self.truncated = False

    def write(self, data: bytes) -> None:
        data = self.tail + data
        for value in self.secrets:
            data = data.replace(value, b"[REDACTED]")
        split = max(0, len(data) - self.keep)
        self.tail = data[split:]
        self._write(data[:split])

    def _write(self, data: bytes) -> None:
        remaining = max(0, self.maximum - self.written)
        self.file.write(data[:remaining])
        self.written += min(remaining, len(data))
        self.truncated |= len(data) > remaining
        self.file.flush()

    def close(self) -> None:
        self._write(self.tail)
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()


def main() -> int:
    config_path = Path(sys.argv[1]).resolve(strict=True)
    directory = config_path.parent
    config = json.loads(config_path.read_text())
    # Do not retain scoped credentials or prompt text after the launcher has them.
    config_path.unlink()
    boot_source, boot_fingerprint = current_boot_identity()
    identity = {
        **{key: config[key] for key in STARTUP_CLOCK_FIELDS if key in config},
        **{key: config[key] for key in ('launcher_spawn_requested_at',
                                       'launcher_spawn_requested_monotonic') if key in config},
        'startup_phase': 'ready', 'launcher_ready_at': utc_now(),
        'launcher_ready_monotonic': time.monotonic(),
        "attempt_id": config["attempt_id"], "operation_id": config["operation_id"],
        "nonce": config["nonce"], "pid": os.getpid(),
        "process_started_at": psutil.Process().create_time(),
        **process_birth_identity(os.getpid()),
        "boot_fingerprint": boot_fingerprint, "boot_identity_source": boot_source,
        "fencing_token": config["fencing_token"], "ready_at": utc_now(),
    }
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    result = {**identity, "execution_status": "execution_unknown", "exit_code": None}
    def finish_unpermitted(reason='launch_not_authorized', status='cancelled'):
        result.update(execution_status=status, reason=reason, finished_at=utc_now(), active_seconds=0.0,
                      startup_phase='finished', launcher_finished_at=utc_now(),
                      launcher_finished_monotonic=time.monotonic(), child_started=False,
                      startup_stop_reason=('startup_deadline_expired' if
                          time.monotonic() >= config.get('startup_deadline_monotonic', float('inf'))
                          else 'authorization_revoked'))
        atomic_json(directory / 'result.json', result)
        return 0 if status == 'cancelled' else 1

    try:
        deadline = startup_deadline(config, boot_source, boot_fingerprint)
    except ValueError:
        atomic_json(directory / 'identity.json', identity)
        return finish_unpermitted('invalid_startup_deadline', 'failed')
    if 'startup_clock_version' in config:
        # Parent writes actual OS-spawn timing before waiting for our ready acknowledgement.
        while not (directory / 'spawn.json').exists() and time.monotonic() < deadline:
            if stop.is_set() or (directory / 'cancel.json').exists():
                break
            time.sleep(.005)
        if (directory / 'spawn.json').exists():
            spawn = json.loads((directory / 'spawn.json').read_text())
            if spawn.get('pid') == identity['pid'] and spawn.get('nonce') == identity['nonce']:
                identity.update({key: spawn[key] for key in
                                 ('launcher_spawned_at', 'launcher_spawned_monotonic')})
    identity.update(launcher_ready_at=utc_now(), launcher_ready_monotonic=time.monotonic())
    result.update(identity)
    atomic_json(directory / 'identity.json', identity)
    while True:
        if stop.is_set() or (directory / "cancel.json").exists() or time.monotonic() >= deadline:
            return finish_unpermitted()
        if (directory / 'go.json').exists():
            break
        time.sleep(0.02)
    permit = json.loads((directory / "go.json").read_text())
    if permit.get("nonce") != identity["nonce"] or permit.get("fencing_token") != identity["fencing_token"]:
        return finish_unpermitted('invalid_launch_permit', 'failed')
    if 'startup_clock_version' in config and (
            any(type(permit.get(key)) is not type(config[key]) or permit.get(key) != config[key]
                for key in STARTUP_CLOCK_FIELDS)
            or type(permit.get('launcher_go_monotonic')) not in {int, float}
            or not config['startup_started_monotonic'] <= permit['launcher_go_monotonic'] < deadline):
        return finish_unpermitted('invalid_launch_permit', 'failed')
    if stop.is_set() or (directory / "cancel.json").exists() or time.monotonic() >= deadline:
        return finish_unpermitted()
    identity.update(startup_phase='permitted', **{key: permit[key] for key in
                    ('launcher_go_at', 'launcher_go_monotonic', 'launcher_acknowledged_at',
                     'launcher_acknowledged_monotonic') if key in permit})
    result.update(identity)
    sinks = [
        RedactingSink(directory / name, [value.encode() for value in config.get("redact_values", [])], config["max_log_bytes"])
        for name in ["stdout.jsonl", "stderr.log"]
    ]
    child = None
    execution_started = None
    try:
        # Recheck after opening sinks too: no scheduler or filesystem delay renews startup authority.
        if stop.is_set() or (directory / 'cancel.json').exists() or time.monotonic() >= deadline:
            return finish_unpermitted()
        execution_started = time.monotonic()
        execution_started_at = datetime.now(UTC)
        execution_deadline = execution_started + config['timeout_seconds']
        child = subprocess.Popen(
            config["argv"], cwd=config["cwd"], env=config["environment"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True,
        )
        result['child_started'] = True
        child_identity = {"pid": child.pid, "process_started_at": psutil.Process(child.pid).create_time(),
                          **process_birth_identity(child.pid)}
        atomic_json(directory / "child.json", {**identity, "startup_phase": "child_started",
            "child": child_identity, "started_at": utc_now(),
            'execution_clock_version': 1, 'execution_started_at': execution_started_at.isoformat(),
            'execution_started_monotonic': execution_started, 'execution_deadline_monotonic': execution_deadline,
            'execution_deadline_at': (execution_started_at + timedelta(seconds=config['timeout_seconds'])).isoformat(),
            'execution_window_seconds': config['timeout_seconds']})

        def pump(stream, sink):
            try:
                while chunk := stream.read1(16384):
                    sink.write(chunk)
            finally:
                stream.close()

        threads = [threading.Thread(target=pump, args=(stream, sink), daemon=True)
                   for stream, sink in zip([child.stdout, child.stderr], sinks, strict=True)]
        for thread in threads:
            thread.start()
        def input_pump():
            try:
                child.stdin.write(config.get("stdin_text", "").encode())
            except (BrokenPipeError, OSError):
                pass
            finally:
                child.stdin.close()
        input_thread = threading.Thread(target=input_pump, daemon=True)
        input_thread.start()
        threads.append(input_thread)
        timeout = execution_deadline
        reason = None
        while child.poll() is None:
            if stop.is_set() or (directory / "cancel.json").exists():
                reason = "cancelled"
                break
            if time.monotonic() >= timeout:
                reason = "timeout"
                break
            if any(sink.truncated for sink in sinks):
                reason = "log_limit"
                break
            time.sleep(0.04)
        if reason:
            # Descendant cleanup is based on current process objects/identity, not a stale PID.
            descendants = psutil.Process(child.pid).children(recursive=True) if child.poll() is None else []
            try:
                child.send_signal(signal.SIGINT)
                child.wait(timeout=config["stop_grace_seconds"])
            except (subprocess.TimeoutExpired, ProcessLookupError):
                child.terminate()
            try:
                child.wait(timeout=max(0.1, config["stop_grace_seconds"]))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
            for process in descendants:
                try:
                    process.kill()
                except psutil.Error:
                    pass
            _, alive = psutil.wait_procs(descendants, timeout=2)
            if alive:
                result.update(execution_status="execution_unknown", reason="descendants_not_confirmed_stopped")
            else:
                result.update(execution_status="cancelled" if reason == "cancelled" else "failed", reason=reason)
        else:
            result.update(execution_status="completed" if child.returncode == 0 else "failed", reason=None)
        for thread in threads:
            thread.join(timeout=2)
        if any(thread.is_alive() for thread in threads):
            result.update(execution_status="execution_unknown", reason="inherited_output_pipes_still_open")
        result["exit_code"] = child.returncode
    except BaseException as exc:
        if child and child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        result.update(execution_status="failed", reason=type(exc).__name__)
    finally:
        for sink in sinks:
            sink.close()
        result.update(startup_phase='finished', launcher_finished_at=utc_now(),
                      launcher_finished_monotonic=time.monotonic())
        result["finished_at"] = utc_now()
        result['active_seconds'] = max(0.0, time.monotonic() - execution_started) if execution_started is not None else 0.0
        result["logs_truncated"] = any(sink.truncated for sink in sinks)
        atomic_json(directory / "result.json", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

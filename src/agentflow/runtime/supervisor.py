from __future__ import annotations

import asyncio
import json
import math
import os
import secrets
import signal
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psutil

from agentflow.common import DomainError, canonical_digest, utc_now

from .contracts import BackendHandle, LaunchSpec, Store, require_record
from .launcher import STARTUP_CLOCK_FIELDS, atomic_json, startup_deadline
from .process_birth import BIRTH_FIELDS, process_birth_identity
from .process_identity import (
    STABLE_BOOT_SOURCES,
    current_boot_identity,
    observe_process,
    same_launcher_identity,
)


class Supervisor:
    """Durable launch intent -> live identity -> permit handshake; never blind respawn."""

    def __init__(self, store: Store, data_dir: Path, *, handshake_timeout: float = 60):
        if not 0 < handshake_timeout <= 60:
            raise ValueError("Launcher handshake timeout must be positive and at most 60 seconds")
        self.handshake_timeout = handshake_timeout
        self.store = store
        self.root = Path(data_dir).resolve() / "supervisor"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._children: dict[str, asyncio.subprocess.Process] = {}
        self._child_identities: dict[str, dict] = {}
        self._watchers: set[asyncio.Task] = set()
        self._launch_locks: dict[str, asyncio.Lock] = {}

    def _dir(self, attempt_id: str) -> Path:
        # IDs supplied by the controller need not be UUIDs, but cannot name paths.
        return self.root / canonical_digest({"attempt_id": attempt_id}).split(":")[1]

    async def start(self, spec: LaunchSpec) -> BackendHandle:
        async with self._launch_locks.setdefault(spec.attempt_id, asyncio.Lock()):
            return await self._start(spec)

    async def _start(self, spec: LaunchSpec) -> BackendHandle:
        spec = LaunchSpec.model_validate(spec)
        if os.name != "posix":
            raise DomainError("unsupported_supervisor", "Controller POSIX process supervisor is required")
        if not spec.cwd.resolve().is_dir():
            raise DomainError("invalid_workspace", "Working directory does not exist", 422)
        directory = self._dir(spec.attempt_id)
        launch_fingerprint = canonical_digest({
            **spec.model_dump(mode="json", exclude={"environment", "stdin_text"}),
            "environment_fingerprint": canonical_digest(spec.environment),
            "stdin_fingerprint": canonical_digest(spec.stdin_text),
        })

        def intent(tx):
            prelaunch = tx.get('prelaunch_failure', spec.attempt_id)
            if prelaunch and prelaunch.get('outcome') == 'not_started':
                raise DomainError('prelaunch_failure_sealed', 'This attempt failed before launch; create a new attempt to retry')
            existing = tx.get("supervised_attempt", spec.attempt_id)
            if existing:
                if existing["launch_fingerprint"] != launch_fingerprint:
                    raise DomainError("idempotency_conflict", "Attempt already has a different launch")
                return existing
            started_at, started = datetime.now(UTC), time.monotonic()
            boot_source, boot_fingerprint = current_boot_identity()
            body = {
                'startup_clock_version': 1, 'startup_phase': 'intent',
                'startup_started_at': started_at.isoformat(), 'startup_started_monotonic': started,
                'startup_deadline_at': (started_at + timedelta(seconds=self.handshake_timeout)).isoformat(),
                'startup_deadline_monotonic': started + self.handshake_timeout,
                'startup_window_seconds': self.handshake_timeout,
                'startup_boot_identity_source': boot_source, 'startup_boot_fingerprint': boot_fingerprint,
                "attempt_id": spec.attempt_id, "operation_id": spec.operation_id, "run_id": spec.run_id,
                "input_fingerprint": spec.input_fingerprint, "fencing_token": spec.fencing_token,
                "backend": spec.backend, "backend_version": spec.backend_version,
                "launch_fingerprint": launch_fingerprint, "nonce": secrets.token_hex(32),
                "state": "launch_intent", "pid": None, "process_started_at": None,
                "boot_fingerprint": None, "created_at": utc_now(), "exit_code": None,
                "reason": None, "directory": str(directory),
            }
            created = tx.put("supervised_attempt", spec.attempt_id, body)
            tx.event("attempt_launch_intent", {"attempt_id": spec.attempt_id}, run_id=spec.run_id)
            return created

        await self.store.command("launch_intent", spec.operation_id, {"fingerprint": launch_fingerprint}, intent)
        record = await self.store.read("supervised_attempt", spec.attempt_id)
        if record["state"] != "launch_intent":
            return await self.inspect(spec.attempt_id)
        # A private O_EXCL marker is a second defence against concurrent launch consumers.
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            marker = os.open(directory / "launch.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(marker)
        except FileExistsError:
            return await self.recover(spec.attempt_id)
        # Only fresh intents can be launched. Legacy intents are reconciled, never respawned.
        if 'startup_clock_version' not in record:
            return await self.recover(spec.attempt_id)
        clock = {key: record[key] for key in STARTUP_CLOCK_FIELDS}
        spawn_requested = {'launcher_spawn_requested_at': utc_now(),
                           'launcher_spawn_requested_monotonic': time.monotonic()}
        config = {
            **clock, **spawn_requested, 'startup_phase': 'intent',
            "attempt_id": spec.attempt_id, "operation_id": spec.operation_id, "nonce": record["nonce"],
            "fencing_token": spec.fencing_token, "argv": spec.argv, "cwd": str(spec.cwd.resolve()),
            "environment": spec.environment, "stdin_text": spec.stdin_text,
            "max_log_bytes": spec.max_log_bytes, "timeout_seconds": spec.timeout_seconds,
            "stop_grace_seconds": spec.stop_grace_seconds,
            "redact_values": [value for name, value in spec.environment.items() if any(
                marker in name.upper() for marker in ["TOKEN", "API_KEY", "SECRET", "PASSWORD"]
            )],
        }
        atomic_json(directory / "launch.json", config)
        launcher_environment = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "PYTHONUNBUFFERED": "1",
        }
        async def spawn_and_record():
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "agentflow.runtime.launcher", str(directory / "launch.json"),
                    cwd=directory, env=launcher_environment, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
                )
            except Exception:
                (directory / 'launch.json').unlink(missing_ok=True)
                await self._state(spec.attempt_id, 'failed', 'launcher_spawn_failed')
                raise
            spawn_timing = {'launcher_spawned_at': utc_now(), 'launcher_spawned_monotonic': time.monotonic()}
            self._children[spec.attempt_id] = process
            persisted = asyncio.Event()
            watcher = asyncio.create_task(self._watch(spec.attempt_id, process, persisted),
                                          name=f"supervise:{spec.attempt_id}")
            self._watchers.add(watcher)
            watcher.add_done_callback(self._watchers.discard)
            try:
                boot_source, boot_fingerprint = current_boot_identity()
                identity = {**record, 'pid': process.pid,
                            'process_started_at': psutil.Process(process.pid).create_time(),
                            'boot_identity_source': boot_source, 'boot_fingerprint': boot_fingerprint,
                            **process_birth_identity(process.pid)}
                self._child_identities[spec.attempt_id] = identity
                atomic_json(directory / 'spawn.json', {**identity, **spawn_requested, **spawn_timing,
                                                       'startup_phase': 'spawned'})

                def persist_spawn(tx):
                    current = require_record(tx, 'supervised_attempt', spec.attempt_id)
                    return tx.put('supervised_attempt', spec.attempt_id, {
                        **current, **spawn_requested, **spawn_timing, 'startup_phase': 'spawned',
                        'launcher_spawn_observed_process_started_at': identity['process_started_at'],
                        **{key: identity[key] for key in ('pid', 'process_started_at', 'boot_identity_source',
                                                         'boot_fingerprint', *BIRTH_FIELDS)},
                    }, current['revision'])

                await self.store.command('launcher_spawn', spec.operation_id, {'nonce': record['nonce']}, persist_spawn)
                return process, identity
            finally:
                persisted.set()

        # Shield the OS spawn/persistence unit: cancellation cannot lose a just-created process.
        spawn_task = asyncio.create_task(spawn_and_record())
        permitted = False
        try:
            process, spawned_identity = await asyncio.shield(spawn_task)
            deadline = startup_deadline(record, *current_boot_identity())
            while time.monotonic() < deadline:
                if (directory / 'cancel.json').exists():
                    raise DomainError('launch_cancelled', 'Launch authorization is no longer current')
                if (directory / "identity.json").exists():
                    identity = json.loads((directory / "identity.json").read_text())
                    observation = self._identity_observation(identity)
                    # Native birth tokens remain stable when wall-clock-derived timestamps differ.
                    spawn_matches = same_launcher_identity(identity, {
                        **spawned_identity, 'process_started_at': identity.get('process_started_at')})
                    observation.update(nonce_matches=identity.get("nonce") == record["nonce"],
                                       expected_pid=process.pid, pid_matches=identity.get("pid") == process.pid,
                                       spawn_matches=spawn_matches)
                    observations = [dict(observation)]
                    for _ in range(2):
                        if (observation["verified"] or not observation["nonce_matches"]
                                or not observation["pid_matches"] or not spawn_matches
                                or process.returncode is not None or time.monotonic() >= deadline):
                            break
                        await asyncio.sleep(min(.05, max(0, deadline - time.monotonic())))
                        observation = {**observation, **self._identity_observation(identity)}
                        observations.append(dict(observation))
                    observation["observations"] = observations
                    atomic_json(directory / "identity-verification.json", observation)
                    if time.monotonic() >= deadline:
                        await self._revoke(record, 'launcher_handshake_timeout')
                        raise DomainError('execution_uncertain', 'Launch handshake was not observed')
                    if (not observation["nonce_matches"] or not observation["pid_matches"]
                            or not spawn_matches or not observation["verified"]
                            or any(type(identity.get(key)) is not type(record[key]) or identity.get(key) != record[key]
                                   for key in STARTUP_CLOCK_FIELDS)):
                        await self._revoke(record, 'identity_handshake_mismatch')
                        raise DomainError("unsafe_process_identity", "Launcher identity could not be verified",
                                          details=observation)
                    break
                if process.returncode is not None:
                    await self._state(spec.attempt_id, "failed", "launcher_exited_before_handshake")
                    raise DomainError("launcher_failed", "Launcher exited before identity handshake")
                await asyncio.sleep(min(.02, max(0, deadline - time.monotonic())))
            else:
                await self._revoke(record, 'launcher_handshake_timeout')
                raise DomainError("execution_uncertain", "Launch handshake was not observed")

            def acknowledge(tx):
                current = require_record(tx, "supervised_attempt", spec.attempt_id)
                if time.monotonic() >= deadline:
                    raise DomainError('execution_uncertain', 'Launch handshake was not observed')
                if (current["state"] != "launch_intent" or current["nonce"] != identity["nonce"]
                        or (directory / 'cancel.json').exists()):
                    raise DomainError("launch_cancelled", "Launch authorization is no longer current")
                return tx.put("supervised_attempt", spec.attempt_id, {
                    **current, "state": "running", 'startup_phase': 'acknowledged',
                    'launcher_acknowledged_at': utc_now(), 'launcher_acknowledged_monotonic': time.monotonic(),
                    **{key: identity[key] for key in ('pid', 'process_started_at', 'boot_identity_source',
                        'boot_fingerprint', 'launcher_ready_at', 'launcher_ready_monotonic', *BIRTH_FIELDS)},
                }, current["revision"])

            acknowledged = await self.store.command("launch_ack", spec.operation_id,
                                                    {"nonce": record["nonce"]}, acknowledge)
            if time.monotonic() >= deadline:
                raise DomainError('execution_uncertain', 'Launch handshake was not observed')
            if (directory / 'cancel.json').exists():
                raise DomainError('launch_cancelled', 'Launch authorization is no longer current')
            self._child_identities[spec.attempt_id] = dict(identity)
            permit = {**clock, 'nonce': record['nonce'], 'fencing_token': spec.fencing_token,
                      'startup_phase': 'permitted', 'launcher_go_at': utc_now(),
                      'launcher_go_monotonic': time.monotonic(),
                      **{key: acknowledged[key] for key in
                         ('launcher_acknowledged_at', 'launcher_acknowledged_monotonic')}}
            atomic_json(directory / "go.json", permit)
            permitted = True

            def record_permit(tx):
                current = require_record(tx, 'supervised_attempt', spec.attempt_id)
                return tx.put('supervised_attempt', spec.attempt_id, {
                    **current, 'startup_phase': ('finished' if current['startup_phase'] == 'finished'
                                                else 'permitted'),
                    'launcher_go_at': permit['launcher_go_at'],
                    'launcher_go_monotonic': permit['launcher_go_monotonic'],
                }, current['revision'])

            await self.store.command('launcher_permit', spec.operation_id, {'nonce': record['nonce']}, record_permit)
            return await self.inspect(spec.attempt_id)
        except BaseException:
            # No permit exists: acquire the spawn identity before revocation can make the
            # launcher exit, including cancellation inside create_subprocess_exec.
            try:
                await asyncio.shield(spawn_task)
            except Exception:
                pass
            if not permitted:
                reason = ('launcher_handshake_timeout'
                          if time.monotonic() >= record['startup_deadline_monotonic'] else 'launch_cancelled')
                await asyncio.shield(self._revoke(record, reason))
            raise

    async def _revoke(self, record: dict, reason: str):
        directory = Path(record['directory'])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        revoked_at = utc_now()
        cancel_path = directory / 'cancel.json'
        if not cancel_path.exists():
            atomic_json(cancel_path, {'nonce': record['nonce'], 'reason': reason, 'revoked_at': revoked_at})

        def revoke(tx):
            current = require_record(tx, 'supervised_attempt', record['attempt_id'])
            terminal = current['state'] in {'completed', 'failed', 'cancelled'}
            return tx.put('supervised_attempt', record['attempt_id'], {
                **current, 'state': current['state'] if terminal else 'execution_unknown',
                'reason': current.get('reason') if terminal else reason,
                'launch_authorization_revoked_reason': current.get('launch_authorization_revoked_reason', reason),
                'launch_authorization_revoked_at': current.get('launch_authorization_revoked_at', revoked_at),
            }, current['revision'])

        return await self.store.command('launch_revoke', str(uuid4()), {'attempt_id': record['attempt_id']}, revoke)

    @staticmethod
    def _identity_observation(identity: dict) -> dict:
        return observe_process(identity)

    @staticmethod
    def _alive(identity: dict) -> bool:
        return Supervisor._identity_observation(identity)["verified"]

    def _owned_running(self, record: dict) -> bool:
        process = self._children.get(record['attempt_id'])
        identity = self._child_identities.get(record['attempt_id'])
        return (process is not None and process.returncode is None and process.pid == record.get('pid')
                and identity is not None and same_launcher_identity(identity, record))

    async def _watch(self, attempt_id, process, persisted):
        await process.wait()
        await persisted.wait()
        self._children.pop(attempt_id, None)
        self._child_identities.pop(attempt_id, None)
        await self.inspect(attempt_id)

    async def _state(self, attempt_id, state, reason=None, exit_code=None, *, expected_revision=None,
                     timing=None):
        def apply(tx):
            current = require_record(tx, "supervised_attempt", attempt_id)
            if expected_revision is not None and current['revision'] != expected_revision:
                return current
            if current["state"] in {"completed", "failed", "cancelled"} and state != "execution_unknown":
                return current
            result = tx.put("supervised_attempt", attempt_id, {
                **current, **(timing or {}), "state": state, "reason": reason, "exit_code": exit_code,
                "updated_at": utc_now(),
            }, current["revision"])
            tx.event("attempt_" + state, {"attempt_id": attempt_id, "reason": reason}, run_id=current["run_id"])
            return result
        return await self.store.command("attempt_state", str(uuid4()), {"attempt_id": attempt_id, "state": state, "reason": reason}, apply)

    async def inspect(self, attempt_id: str) -> BackendHandle:
        record = await self.store.read("supervised_attempt", attempt_id)
        if record is None:
            raise DomainError("not_found", "Attempt is not supervised", 404)
        directory = Path(record["directory"])
        result_path = directory / "result.json"
        active_seconds = None
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text())
                if not isinstance(result, dict) or result.get("execution_status") not in {"completed", "failed", "cancelled", "execution_unknown"}:
                    raise ValueError("Malformed execution receipt")
            except (OSError, ValueError, TypeError):
                result = {}
            # The receipt can arrive while an initial read still contains a pre-spawn snapshot.
            # Refresh before deciding mismatch; never adopt identity from the receipt itself.
            record = await self.store.read('supervised_attempt', attempt_id)
            clock_matches = all(type(result.get(key)) is type(record.get(key))
                                and result.get(key) == record.get(key) for key in STARTUP_CLOCK_FIELDS)
            canonical_time = {}
            if (clock_matches and type(record.get('startup_clock_version')) is int
                    and record['startup_clock_version'] == 1
                    and not record.get('launcher_acknowledged_at')
                    and result.get('execution_status') == 'cancelled'
                    and result.get('reason') == 'launch_not_authorized' and result.get('child_started') is False
                    and record.get('boot_identity_source') in STABLE_BOOT_SOURCES
                    and all(record.get(key) for key in BIRTH_FIELDS)
                    and result.get('process_started_at') != record.get('process_started_at')
                    and same_launcher_identity(result, {**record,
                        'process_started_at': result.get('process_started_at')})):
                try:
                    ready_identity = json.loads((directory / 'identity.json').read_text())
                except (OSError, ValueError):
                    ready_identity = {}
                if (same_launcher_identity(result, ready_identity)
                        and all(type(ready_identity.get(key)) is type(record[key])
                                and ready_identity.get(key) == record[key] for key in STARTUP_CLOCK_FIELDS)):
                    # The exact persisted kernel birth, stable boot and launch bindings verify this
                    # stopped launcher; require its independent ready receipt too. Never use this
                    # timestamp normalization for legacy identities or a child that ran.
                    canonical_time = {
                        'launcher_spawn_observed_process_started_at': record.get(
                            'launcher_spawn_observed_process_started_at', record['process_started_at']),
                        'process_started_at': result['process_started_at'],
                        'launcher_identity_timestamp_canonicalized_at': utc_now(),
                    }
                    record = {**record, **canonical_time}
            if same_launcher_identity(result, record) and clock_matches:
                timing = {key: result[key] for key in ('startup_phase', 'launcher_finished_at',
                          'launcher_finished_monotonic', 'launcher_go_at', 'launcher_go_monotonic') if key in result}
                timing.update(canonical_time)
                record = await self._state(attempt_id, result["execution_status"], result.get("reason"),
                                           result.get("exit_code"), expected_revision=record['revision'], timing=timing)
                if (result.get('attempt_id') == attempt_id and result.get('operation_id') == record['operation_id']
                        and result.get('process_started_at') == record.get('process_started_at')):
                    elapsed = result.get('active_seconds')
                    if elapsed is None:
                        try:
                            from datetime import datetime
                            finished = datetime.fromisoformat(result['finished_at'])
                            elapsed = finished.timestamp() - record['process_started_at'] if finished.tzinfo else None
                        except (KeyError, TypeError, ValueError):
                            elapsed = None
                    if type(elapsed) in {int, float} and math.isfinite(elapsed) and elapsed >= 0:
                        active_seconds = float(elapsed)
            else:
                record = await self._state(attempt_id, "execution_unknown", "completion_identity_mismatch",
                                           expected_revision=record['revision'])
        elif record["state"] in {"running", "cancelling"} or (record["state"] == "launch_intent" and record.get("pid")):
            observation = self._identity_observation(record)
            # The child watcher owns the exact launched process, so a temporary
            # OS query failure is not evidence of exit. Never mask a birth/boot mismatch.
            owned = observation.get('inspection_error', False) and self._owned_running(record)
            if not observation['alive'] and not owned:
                record = await self._state(attempt_id, "execution_unknown", "process_disappeared_without_receipt",
                                           expected_revision=record['revision'])
        return BackendHandle(
            attempt_id=attempt_id, operation_id=record["operation_id"], backend=record["backend"],
            backend_version=record["backend_version"], input_fingerprint=record["input_fingerprint"],
            fencing_token=record["fencing_token"], state=record["state"], pid=record.get("pid"),
            process_started_at=record.get("process_started_at"), boot_fingerprint=record.get("boot_fingerprint"),
            launcher_nonce=record["nonce"], stdout_path=str(directory / "stdout.jsonl"),
            stderr_path=str(directory / "stderr.log"), exit_code=record.get("exit_code"), reason=record.get("reason"),
            active_seconds=active_seconds,
        )

    async def cancel(self, attempt_id: str, grace_seconds: float = 3) -> BackendHandle:
        handle = await self.inspect(attempt_id)
        if handle.state in {"completed", "failed", "cancelled"}:
            return handle
        record = await self.store.read("supervised_attempt", attempt_id)
        directory = Path(record["directory"])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json(directory / "cancel.json", {"nonce": record["nonce"], "requested_at": utc_now()})
        if not record.get("pid") or not self._alive(record):
            await self._state(attempt_id, "execution_unknown", "cancel_cannot_confirm_process_identity")
            return await self.inspect(attempt_id)
        await self._state(attempt_id, "cancelling", "owner_cancel_requested")
        try:
            # Before readiness the Python launcher may not have installed signal handlers yet.
            if (directory / 'go.json').exists():
                os.kill(record["pid"], signal.SIGINT)
        except ProcessLookupError:
            pass
        deadline = asyncio.get_running_loop().time() + grace_seconds
        while asyncio.get_running_loop().time() < deadline:
            handle = await self.inspect(attempt_id)
            if handle.state in {"completed", "failed", "cancelled"}:
                return handle
            await asyncio.sleep(0.05)
        # Never signal an identity that was replaced by a reused PID.
        if self._alive(record):
            try:
                if os.getpgid(record["pid"]) != record["pid"]:
                    raise DomainError("unsafe_process_identity", "Unexpected process group")
                os.killpg(record["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        await self._state(attempt_id, "execution_unknown", "forced_stop_requires_cleanup_verification")
        return await self.inspect(attempt_id)

    async def recover(self, attempt_id: str) -> BackendHandle:
        record = await self.store.read("supervised_attempt", attempt_id)
        if record is None:
            raise DomainError("not_found", "Attempt not found", 404)
        if record["state"] == "launch_intent" or (
                record.get('startup_phase') == 'acknowledged'
                and not (Path(record['directory']) / 'go.json').exists()):
            # A restart revokes immediately even when the delayed launcher has no identity file yet.
            await self._revoke(record, 'unacknowledged_launch_requires_reconciliation')
        return await self.inspect(attempt_id)

    async def recover_all(self) -> list[BackendHandle]:
        return [await self.recover(record["attempt_id"]) for record in await self.store.list("supervised_attempt")]

    async def wait(self, attempt_id: str) -> BackendHandle:
        while True:
            handle = await self.inspect(attempt_id)
            if handle.state in {"completed", "failed", "cancelled", "execution_unknown"}:
                return handle
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        for record in await self.store.list("supervised_attempt"):
            if record["state"] in {"running", "launch_intent", "cancelling"}:
                await self.cancel(record["attempt_id"])
        if self._watchers:
            await asyncio.gather(*list(self._watchers), return_exceptions=True)

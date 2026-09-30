"""Runtime identity regressions use only temporary stores and small local subprocesses."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psutil
import pytest

from agentflow.common import DomainError, canonical_digest
from agentflow.control.recovery import RunRecoveryService
from agentflow.runtime.contracts import LaunchSpec
from agentflow.runtime.maintenance import _process_stopped, _Skip
from agentflow.runtime.process_birth import BIRTH_FIELDS, process_birth_identity
from agentflow.runtime.process_identity import (
    boot_relation,
    current_boot_identity,
    observe_process,
    process_is_stopped,
    same_launcher_identity,
)
from agentflow.runtime.supervisor import Supervisor


def launch_spec(tmp_path, script="import time; time.sleep(30)"):
    return LaunchSpec(
        attempt_id="identity-attempt", operation_id="identity-operation", run_id="identity-run",
        input_fingerprint="sha256:" + "a" * 64, fencing_token=1,
        argv=[sys.executable, "-c", script], cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin"}, timeout_seconds=40, stop_grace_seconds=.1,
    )


async def update_record(store, attempt_id, **changes):
    def write(tx):
        record = tx.get("supervised_attempt", attempt_id)
        return tx.put("supervised_attempt", attempt_id, {**record, **changes}, record["revision"])
    return await store.command("identity-test-update", str(uuid4()), {}, write)


async def test_boot_wall_clock_drift_does_not_break_new_launcher_or_recovered_inspection(store, tmp_path, monkeypatch):
    # The launcher runs in a separate interpreter and observes the original wall clock.
    original_boot_time = psutil.boot_time()
    monkeypatch.setattr(psutil, "boot_time", lambda: original_boot_time + 1)
    supervisor = Supervisor(store, tmp_path)
    spec = launch_spec(tmp_path)
    try:
        started = await supervisor.start(spec)
        recovered = Supervisor(store, tmp_path)
        assert started.state == "running"
        assert (await recovered.inspect(spec.attempt_id)).state == "running"
        identity = json.loads((supervisor._dir(spec.attempt_id) / "identity.json").read_text())
        assert identity["boot_identity_source"] in {"macos_bootsessionuuid", "linux_boot_id"}
    finally:
        monkeypatch.undo()
        await supervisor.close()


@pytest.mark.parametrize('error', [psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error, PermissionError])
async def test_owned_child_remains_running_during_temporary_psutil_failure(store, tmp_path, monkeypatch, error):
    supervisor = Supervisor(store, tmp_path)
    spec = launch_spec(tmp_path)
    try:
        started = await supervisor.start(spec)
        original_process = psutil.Process
        def unavailable(pid=None):
            if pid == started.pid:
                raise error(pid)
            return original_process(pid)
        with monkeypatch.context() as patch:
            patch.setattr(psutil, "Process", unavailable)
            assert (await supervisor.inspect(spec.attempt_id)).state == "running"
        assert (await supervisor.cancel(spec.attempt_id)).state == "cancelled"
    finally:
        await supervisor.cancel(spec.attempt_id)
        await supervisor.close()


async def test_legacy_boot_drift_keeps_same_birth_pid_running_after_recovery(store, tmp_path, monkeypatch):
    supervisor = Supervisor(store, tmp_path)
    spec = launch_spec(tmp_path)
    try:
        await supervisor.start(spec)
        record = await store.read("supervised_attempt", spec.attempt_id)
        original_boot_time = psutil.boot_time()
        await update_record(store, spec.attempt_id, boot_identity_source=None,
                            boot_fingerprint=canonical_digest({"boot_time": original_boot_time}))
        with monkeypatch.context() as patch:
            patch.setattr(psutil, "boot_time", lambda: original_boot_time + 1)
            assert (await Supervisor(store, tmp_path).inspect(spec.attempt_id)).state == "running"
        await update_record(store, spec.attempt_id, boot_fingerprint=record["boot_fingerprint"],
                            boot_identity_source=record.get("boot_identity_source"))
    finally:
        await supervisor.close()


@pytest.mark.parametrize("verifier,error", [
    (RunRecoveryService._stopped_process, DomainError), (_process_stopped, _Skip),
])
async def test_legacy_boot_drift_cannot_prove_live_process_stopped(monkeypatch, verifier, error):
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)")
    try:
        boot_time = psutil.boot_time()
        identity = {"pid": child.pid, "process_started_at": psutil.Process(child.pid).create_time(),
                    "boot_fingerprint": canonical_digest({"boot_time": boot_time})}
        monkeypatch.setattr(psutil, "boot_time", lambda: boot_time + 1)
        with pytest.raises(error):
            verifier(identity)
    finally:
        child.terminate()
        await child.wait()


async def test_exited_owned_launcher_without_receipt_stays_unknown(store, tmp_path):
    supervisor = Supervisor(store, tmp_path)
    spec = launch_spec(tmp_path)
    try:
        started = await supervisor.start(spec)
        child = supervisor._children[spec.attempt_id]
        os.killpg(started.pid, 9)
        await child.wait()
        result = await supervisor.inspect(spec.attempt_id)
        assert result.state == "execution_unknown"
        assert result.reason == "process_disappeared_without_receipt"
    finally:
        await supervisor.close()


@pytest.mark.parametrize("field,wrong", [
    ("attempt_id", "other-attempt"), ("operation_id", "other-operation"),
    ("nonce", "other-nonce"), ("fencing_token", 2), ("pid", 1073741800),
    ("process_started_at", 1.0), ("boot_fingerprint", "sha256:" + "f" * 64),
    ("boot_identity_source", None), ("boot_identity_source", ["linux_boot_id"]),
    ("fencing_token", True), ("pid", True), ("execution_status", []),
])
async def test_receipt_requires_valid_status_and_every_launcher_identity_field(store, tmp_path, field, wrong):
    supervisor = Supervisor(store, tmp_path)
    spec = launch_spec(tmp_path, "pass")
    try:
        await supervisor.start(spec)
        done = await supervisor.wait(spec.attempt_id)
        assert done.state == "completed"
        path = Path(done.stdout_path).parent / "result.json"
        receipt = json.loads(path.read_text())
        receipt[field] = wrong
        path.write_text(json.dumps(receipt))
        inspected = await supervisor.inspect(spec.attempt_id)
        assert inspected.state == "execution_unknown"
        assert inspected.reason == "completion_identity_mismatch"
        assert inspected.active_seconds is None
    finally:
        await supervisor.close()


@pytest.mark.parametrize('platform', ['darwin', 'linux'])
def test_kernel_boot_session_identity_is_cached_despite_clock_or_query_changes(monkeypatch, platform):
    current_boot_identity.cache_clear()
    boot_id = ['edca9b50-3621-42b6-af30-6c5c69db917f']
    if platform == 'darwin':
        monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=boot_id[0] + '\n'))
    else:
        read_text = Path.read_text
        monkeypatch.setattr(Path, 'read_text', lambda path, *args, **kwargs:
                            boot_id[0] if str(path) == '/proc/sys/kernel/random/boot_id'
                            else read_text(path, *args, **kwargs))
    monkeypatch.setattr(sys, 'platform', platform)
    try:
        source, fingerprint = current_boot_identity()
        record = {'boot_identity_source': source, 'boot_fingerprint': fingerprint}
        boot_id[0] = 'a9171a39-bd17-4fb6-a3d5-3e1b7df52961'
        monkeypatch.setattr(psutil, 'boot_time', lambda: 1.0)
        assert boot_relation(record) == 'same'
        assert source in {'macos_bootsessionuuid', 'linux_boot_id'}
    finally:
        current_boot_identity.cache_clear()


def test_changed_stable_boot_session_is_stop_evidence_even_for_matching_numeric_pid():
    source, _ = current_boot_identity()
    identity = {'pid': os.getpid(), 'process_started_at': psutil.Process().create_time(),
                'boot_identity_source': source, 'boot_fingerprint': 'sha256:' + 'f' * 64}
    observation = observe_process(identity)
    assert observation['stopped'] is True
    assert observation['alive'] is False
    assert observation['verified'] is False


def test_legacy_boot_mismatch_with_matching_birth_is_live_but_not_signal_authority(monkeypatch):
    boot_time = psutil.boot_time()
    identity = {'pid': os.getpid(), 'process_started_at': psutil.Process().create_time(),
                'boot_fingerprint': canonical_digest({'boot_time': boot_time})}
    monkeypatch.setattr(psutil, 'boot_time', lambda: boot_time + 1)
    observation = observe_process(identity)
    assert observation['alive'] is True and observation['stopped'] is False
    assert Supervisor._alive(identity) is False


async def test_reused_pid_is_never_signalled_even_with_live_owned_launcher(store, tmp_path):
    unrelated = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)")
    supervisor = Supervisor(store, tmp_path)
    spec = launch_spec(tmp_path)
    try:
        await supervisor.start(spec)
        record = await store.read("supervised_attempt", spec.attempt_id)
        await update_record(store, spec.attempt_id, pid=unrelated.pid,
                            process_started_at=psutil.Process(unrelated.pid).create_time() - 999)
        assert (await supervisor.cancel(spec.attempt_id, grace_seconds=.1)).state == "execution_unknown"
        await asyncio.sleep(.1)
        assert unrelated.returncode is None
        await update_record(store, spec.attempt_id, pid=record["pid"], process_started_at=record["process_started_at"])
    finally:
        unrelated.terminate()
        await unrelated.wait()
        await supervisor.close()


@pytest.mark.parametrize('phase', ['start', 'inspect', 'recover'])
async def test_process_wall_clock_adjustment_preserves_running_identity(store, tmp_path, monkeypatch, phase):
    supervisor = Supervisor(store, tmp_path)
    spec = launch_spec(tmp_path)
    original_create_time = psutil.Process.create_time
    try:
        if phase != 'start':
            await supervisor.start(spec)
        with monkeypatch.context() as patch:
            # Only this controller interpreter sees the clock correction; the
            # real launcher persists its original timestamp in another process.
            patch.setattr(psutil.Process, 'create_time', lambda process: original_create_time(process) + 10)
            if phase == 'start':
                handle = await supervisor.start(spec)
            elif phase == 'recover':
                handle = await Supervisor(store, tmp_path).recover(spec.attempt_id)
            else:
                handle = await supervisor.inspect(spec.attempt_id)
            assert handle.state == 'running'
            assert psutil.pid_exists(handle.pid)
    finally:
        await supervisor.cancel(spec.attempt_id)
        await supervisor.close()
        for process in list(supervisor._children.values()):
            await asyncio.wait_for(process.wait(), 5)


def test_legacy_birth_time_mismatch_cannot_prove_a_live_process_stopped(monkeypatch):
    source, fingerprint = current_boot_identity()
    identity = {'pid': os.getpid(), 'process_started_at': psutil.Process().create_time(),
                'boot_identity_source': source, 'boot_fingerprint': fingerprint}
    original = psutil.Process.create_time
    monkeypatch.setattr(psutil.Process, 'create_time', lambda process: original(process) + 10)
    observation = observe_process(identity)
    assert observation['verified'] is False and observation['stopped'] is None
    with pytest.raises(ValueError):
        process_is_stopped(identity)


async def test_legacy_completed_receipts_remain_recoverable_without_birth_fields(store, tmp_path):
    supervisor = Supervisor(store, tmp_path)
    spec = launch_spec(tmp_path, 'pass')
    try:
        await supervisor.start(spec)
        launcher = supervisor._children.get(spec.attempt_id)
        done = await supervisor.wait(spec.attempt_id)
        assert done.state == 'completed'
        # A result receipt is written before launcher exit. Reap the actual
        # child and finish its watcher before replacing the persisted fixture.
        if launcher is not None:
            await asyncio.wait_for(launcher.wait(), 5)
        await asyncio.gather(*tuple(supervisor._watchers))
        record = await store.read('supervised_attempt', spec.attempt_id)
        path = Path(record['directory']) / 'result.json'
        receipt = {key: value for key, value in json.loads(path.read_text()).items() if key not in BIRTH_FIELDS}
        legacy_record = {key: value for key, value in record.items() if key not in BIRTH_FIELDS}
        path.write_text(json.dumps(receipt))
        def replace(tx):
            return tx.put('supervised_attempt', spec.attempt_id, legacy_record, record['revision'])
        await store.command('legacy-fixture', str(uuid4()), {}, replace)
        assert same_launcher_identity(receipt, legacy_record)
        assert (await Supervisor(store, tmp_path).recover(spec.attempt_id)).state == 'completed'
        assert process_is_stopped(receipt)
    finally:
        await supervisor.close()


@pytest.mark.parametrize('damage', ['missing', 'invalid', 'different'])
async def test_new_receipt_birth_binding_rejects_downgrade_or_change(store, tmp_path, damage):
    supervisor = Supervisor(store, tmp_path)
    spec = launch_spec(tmp_path, 'pass')
    try:
        await supervisor.start(spec)
        done = await supervisor.wait(spec.attempt_id)
        path = Path(done.stdout_path).parent / 'result.json'
        receipt = json.loads(path.read_text())
        assert all(receipt.get(field) for field in BIRTH_FIELDS)
        if damage == 'missing':
            for field in BIRTH_FIELDS:
                receipt.pop(field)
        else:
            receipt['process_birth_fingerprint'] = [] if damage == 'invalid' else 'sha256:' + 'f' * 64
        path.write_text(json.dumps(receipt))
        assert (await supervisor.inspect(spec.attempt_id)).state == 'execution_unknown'
    finally:
        await supervisor.close()


def test_native_birth_mismatch_and_boot_change_remain_stop_evidence(monkeypatch):
    source, fingerprint = current_boot_identity()
    identity = {'pid': os.getpid(), 'process_started_at': psutil.Process().create_time(),
                'boot_identity_source': source, 'boot_fingerprint': fingerprint,
                **process_birth_identity(os.getpid())}
    original = psutil.Process.create_time
    monkeypatch.setattr(psutil.Process, 'create_time', lambda process: original(process) + 10)
    assert observe_process(identity)['verified'] is True
    changed_birth = {**identity, 'process_birth_fingerprint': 'sha256:' + 'f' * 64}
    assert observe_process(changed_birth)['stopped'] is True
    assert not Supervisor._alive(changed_birth)
    changed_boot = {**identity, 'boot_fingerprint': 'sha256:' + 'f' * 64}
    assert observe_process(changed_boot)['stopped'] is True
    assert not Supervisor._alive(changed_boot)


def test_unavailable_native_birth_source_does_not_prove_process_stopped(monkeypatch):
    source, fingerprint = current_boot_identity()
    identity = {'pid': os.getpid(), 'process_started_at': psutil.Process().create_time(),
                'boot_identity_source': source, 'boot_fingerprint': fingerprint,
                **process_birth_identity(os.getpid())}
    other_source = 'linux_proc_start_ticks' if sys.platform == 'darwin' else 'macos_proc_bsdinfo'
    identity['process_birth_source'] = other_source
    observation = observe_process(identity)
    assert observation['stopped'] is None
    assert observation['verified'] is False


def test_linux_birth_ticks_parse_parentheses_and_survive_wall_clock_changes(monkeypatch):
    ticks = [123456]
    def read_stat(path):
        assert str(path) == '/proc/321/stat'
        tail = ['S', *(['0'] * 18), str(ticks[0]), *(['0'] * 10)]
        return ('321 (fixture with ) parentheses) ' + ' '.join(tail)).encode()
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(Path, 'read_bytes', read_stat)
    monkeypatch.setattr(psutil, 'boot_time', lambda: 10)
    first = process_birth_identity(321)
    assert first == {'process_birth_source': 'linux_proc_start_ticks',
        'process_birth_fingerprint': canonical_digest({'source': 'linux_proc_start_ticks',
                                                     'pid': 321, 'birth': 123456})}
    monkeypatch.setattr(psutil, 'boot_time', lambda: 999)
    assert process_birth_identity(321) == first
    ticks[0] += 1
    assert process_birth_identity(321) != first


def test_macos_incomplete_native_birth_response_fails_closed(monkeypatch):
    from agentflow.runtime import process_birth

    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(process_birth, '_mac_proc_pidinfo', lambda: lambda *args: 1)
    with pytest.raises(OSError):
        process_birth_identity(os.getpid())


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS psutil clock-adjustment regression')
def test_actual_psutil_macos_clock_adjustment_cannot_change_kernel_birth(monkeypatch):
    from psutil import _psosx

    monkeypatch.setattr(_psosx, 'boot_time', lambda: _psosx.INIT_BOOT_TIME)
    source, fingerprint = current_boot_identity()
    identity = {'pid': os.getpid(), 'process_started_at': psutil.Process().create_time(),
                'boot_identity_source': source, 'boot_fingerprint': fingerprint,
                **process_birth_identity(os.getpid())}
    monkeypatch.setattr(_psosx, 'boot_time', lambda: _psosx.INIT_BOOT_TIME + 10)
    assert psutil.Process().create_time() - identity['process_started_at'] == 10
    observation = observe_process(identity)
    assert observation['verified'] and observation['alive'] and observation['stopped'] is False

"""Startup lifecycle regressions; all children and evidence stay in temporary directories."""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentflow.common import DomainError
from agentflow.runtime.contracts import LaunchSpec
from agentflow.runtime.supervisor import Supervisor


def launch(tmp_path, index=0):
    return LaunchSpec(
        attempt_id=f'startup-{index}', operation_id=f'startup-operation-{index}', run_id='startup-run',
        input_fingerprint='sha256:' + 'a' * 64, fencing_token=1,
        argv=[sys.executable, '-c', f"from pathlib import Path;Path('started-{index}').open('a').write('1')"],
        cwd=tmp_path, timeout_seconds=5, stop_grace_seconds=.1,
    )


def delay_launcher(monkeypatch, seconds):
    original = asyncio.create_subprocess_exec

    async def delayed(*argv, **kwargs):
        if argv[1:3] == ('-m', 'agentflow.runtime.launcher'):
            wrapper = (f'import runpy,sys,time;time.sleep({seconds!r});'
                       'sys.argv=["agentflow.runtime.launcher",sys.argv[1]];'
                       'runpy.run_module("agentflow.runtime.launcher",run_name="__main__")')
            return await original(argv[0], '-c', wrapper, argv[3], **kwargs)
        return await original(*argv, **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed)


async def spawned(supervisor, attempt_id):
    for _ in range(200):
        process = supervisor._children.get(attempt_id)
        record = await supervisor.store.read('supervised_attempt', attempt_id)
        if process is not None and record and record.get('pid'):
            return process, record
        await asyncio.sleep(.005)
    raise AssertionError('OS spawn identity was not durably recorded before readiness')


def test_launcher_import_avoids_contracts_and_pydantic():
    script = ('import agentflow.runtime.launcher,sys;'
              'assert "agentflow.runtime.contracts" not in sys.modules;'
              'assert "pydantic" not in sys.modules;'
              'from agentflow.runtime import LaunchSpec,BackendHandle,Capability,TaskEnvelope;'
              'assert LaunchSpec.__module__ == "agentflow.runtime.contracts"')
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


async def test_three_delayed_launchers_persist_spawn_before_readiness_and_share_deadline(store, tmp_path, monkeypatch):
    delay_launcher(monkeypatch, .6)
    supervisor = Supervisor(store, tmp_path, handshake_timeout=3)
    specs = [launch(tmp_path, index) for index in range(3)]
    starts = [asyncio.create_task(supervisor.start(value)) for value in specs]
    try:
        records = [await spawned(supervisor, value.attempt_id) for value in specs]
        for process, record in records:
            assert record['state'] == 'launch_intent' and record['startup_phase'] == 'spawned'
            assert record['pid'] == process.pid and record['process_birth_fingerprint']
            assert not (Path(record['directory']) / 'identity.json').exists()
        assert len(supervisor._watchers) == 3
        await asyncio.gather(*starts)
        for value, (_, initial) in zip(specs, records, strict=True):
            assert (await supervisor.wait(value.attempt_id)).state == 'completed'
            current = await store.read('supervised_attempt', value.attempt_id)
            directory = Path(current['directory'])
            evidence = [json.loads((directory / name).read_text()) for name in
                        ['identity.json', 'child.json', 'result.json']]
            for item in [current, *evidence]:
                assert item['startup_clock_version'] == 1
                assert item['startup_deadline_monotonic'] == initial['startup_deadline_monotonic']
                assert item['startup_window_seconds'] == 3
            result = evidence[-1]
            assert result['launcher_spawned_monotonic'] <= result['launcher_ready_monotonic']
            assert result['launcher_ready_monotonic'] <= result['launcher_go_monotonic']
            assert result['launcher_go_monotonic'] < result['startup_deadline_monotonic']
            assert result['launcher_go_monotonic'] <= result['launcher_finished_monotonic']
            assert (tmp_path / f'started-{specs.index(value)}').read_text() == '1'
    finally:
        await asyncio.gather(*starts, return_exceptions=True)
        await supervisor.close()


async def test_timeout_watcher_persists_late_cancelled_receipt_without_explicit_inspect(store, tmp_path, monkeypatch):
    delay_launcher(monkeypatch, .4)
    supervisor = Supervisor(store, tmp_path, handshake_timeout=.1)
    value = launch(tmp_path)
    try:
        with pytest.raises(DomainError, match='handshake was not observed'):
            await supervisor.start(value)
        record = await store.read('supervised_attempt', value.attempt_id)
        assert record['pid'] and record['process_birth_fingerprint']
        assert record['launch_authorization_revoked_reason'] == 'launcher_handshake_timeout'
        await asyncio.gather(*tuple(supervisor._watchers))
        record = await store.read('supervised_attempt', value.attempt_id)
        assert record['state'] == 'cancelled' and record['reason'] == 'launch_not_authorized'
        assert record['launch_authorization_revoked_reason'] == 'launcher_handshake_timeout'
        assert not (tmp_path / 'started-0').exists()
        assert not (Path(record['directory']) / 'go.json').exists()
    finally:
        await supervisor.close()


@pytest.mark.parametrize('stop', ['cancel_task', 'recover', 'cancel'])
async def test_stop_during_readiness_never_grants_or_duplicates_child(store, tmp_path, monkeypatch, stop):
    delay_launcher(monkeypatch, .5)
    supervisor = Supervisor(store, tmp_path, handshake_timeout=2)
    value = launch(tmp_path)
    start = asyncio.create_task(supervisor.start(value))
    try:
        process, record = await spawned(supervisor, value.attempt_id)
        if stop == 'cancel_task':
            start.cancel()
        elif stop == 'recover':
            await Supervisor(store, tmp_path).recover(value.attempt_id)
        else:
            await supervisor.cancel(value.attempt_id, grace_seconds=1)
        await asyncio.gather(start, return_exceptions=True)
        await asyncio.wait_for(process.wait(), 5)
        await asyncio.gather(*tuple(supervisor._watchers))
        assert not (tmp_path / 'started-0').exists()
        assert not (Path(record['directory']) / 'go.json').exists()
        assert (await supervisor.start(value)).pid == process.pid
        assert not (tmp_path / 'started-0').exists()
    finally:
        if not start.done():
            start.cancel()
        await asyncio.gather(start, return_exceptions=True)
        await supervisor.close()


async def test_deadline_expiring_during_ack_does_not_write_permit(store, tmp_path, monkeypatch):
    supervisor = Supervisor(store, tmp_path, handshake_timeout=.6)
    original = store.command

    async def slow_ack(scope, *args, **kwargs):
        if scope == 'launch_ack':
            await asyncio.sleep(.7)
        return await original(scope, *args, **kwargs)

    monkeypatch.setattr(store, 'command', slow_ack)
    value = launch(tmp_path)
    try:
        with pytest.raises(DomainError):
            await supervisor.start(value)
        await asyncio.gather(*tuple(supervisor._watchers))
        assert not (tmp_path / 'started-0').exists()
        assert not (supervisor._dir(value.attempt_id) / 'go.json').exists()
    finally:
        await supervisor.close()


async def test_inspect_refreshes_stale_pre_spawn_snapshot_before_marking_receipt_mismatch(store, tmp_path, monkeypatch):
    supervisor = Supervisor(store, tmp_path)
    value = launch(tmp_path)
    try:
        await supervisor.start(value)
        assert (await supervisor.wait(value.attempt_id)).state == 'completed'
        await asyncio.gather(*tuple(supervisor._watchers))
        current = await store.read('supervised_attempt', value.attempt_id)
        stale = {**current, 'state': 'launch_intent', 'startup_phase': 'intent', 'pid': None,
                 'process_started_at': None, 'boot_fingerprint': None, 'revision': current['revision'] - 1}
        original = store.read
        snapshots = [stale]

        async def stale_read(kind, identifier):
            if kind == 'supervised_attempt' and snapshots:
                return snapshots.pop()
            return await original(kind, identifier)

        monkeypatch.setattr(store, 'read', stale_read)
        assert (await supervisor.inspect(value.attempt_id)).state == 'completed'
        assert (await store.read('supervised_attempt', value.attempt_id))['reason'] is None
    finally:
        await supervisor.close()


def test_launcher_rejects_predeadline_permit_observed_after_deadline(tmp_path):
    from datetime import UTC, datetime, timedelta

    from agentflow.runtime.process_identity import current_boot_identity

    source, fingerprint = current_boot_identity()
    started = time.monotonic() - 2
    started_at = datetime.now(UTC) - timedelta(seconds=2)
    clock = {
        'startup_clock_version': 1, 'startup_started_monotonic': started,
        'startup_deadline_monotonic': started + 1, 'startup_window_seconds': 1,
        'startup_started_at': started_at.isoformat(),
        'startup_deadline_at': (started_at + timedelta(seconds=1)).isoformat(),
        'startup_boot_identity_source': source, 'startup_boot_fingerprint': fingerprint,
    }
    config = {**clock, 'attempt_id': 'late-permit', 'operation_id': 'late-permit-operation',
              'nonce': 'fixture-nonce', 'fencing_token': 1,
              'argv': [sys.executable, '-c', "from pathlib import Path;Path('forbidden').touch()"],
              'cwd': str(tmp_path), 'environment': {}, 'timeout_seconds': 5,
              'max_log_bytes': 1024, 'stop_grace_seconds': .1}
    config_path = tmp_path / 'launch.json'
    config_path.write_text(json.dumps(config))
    (tmp_path / 'go.json').write_text(json.dumps({**clock, 'nonce': 'fixture-nonce', 'fencing_token': 1,
        'launcher_go_at': started_at.isoformat(), 'launcher_go_monotonic': started + .1}))
    completed = subprocess.run([sys.executable, '-m', 'agentflow.runtime.launcher', str(config_path)],
                               capture_output=True, text=True, timeout=5)
    assert completed.returncode == 0, completed.stderr
    assert not (tmp_path / 'forbidden').exists() and not (tmp_path / 'child.json').exists()
    result = json.loads((tmp_path / 'result.json').read_text())
    assert result['execution_status'] == 'cancelled' and result['reason'] == 'launch_not_authorized'
    assert result['child_started'] is False
    assert result['startup_stop_reason'] == 'startup_deadline_expired'
    assert result['launcher_finished_monotonic'] >= clock['startup_deadline_monotonic']


async def test_cancellation_during_subprocess_creation_retains_spawn_identity_and_watcher(store, tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec
    os_spawned = asyncio.Event()
    created = []

    async def delayed_return(*argv, **kwargs):
        process = await original(*argv, **kwargs)
        created.append(process)
        os_spawned.set()
        await asyncio.sleep(.2)
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed_return)
    supervisor = Supervisor(store, tmp_path, handshake_timeout=2)
    value = launch(tmp_path)
    start = asyncio.create_task(supervisor.start(value))
    try:
        await os_spawned.wait()
        start.cancel()
        await asyncio.gather(start, return_exceptions=True)
        await asyncio.gather(*tuple(supervisor._watchers))
        record = await store.read('supervised_attempt', value.attempt_id)
        assert record['pid'] == created[0].pid
        assert record['process_birth_fingerprint']
        assert not (tmp_path / 'started-0').exists()
    finally:
        await asyncio.gather(start, return_exceptions=True)
        await supervisor.close()


@pytest.mark.parametrize('field,wrong', [('startup_clock_version', 2), ('startup_clock_version', True),
                                        ('startup_window_seconds', 30),
                                        ('startup_deadline_monotonic', 1)])
async def test_receipt_cannot_change_frozen_startup_clock(store, tmp_path, field, wrong):
    supervisor = Supervisor(store, tmp_path)
    value = launch(tmp_path)
    try:
        await supervisor.start(value)
        assert (await supervisor.wait(value.attempt_id)).state == 'completed'
        await asyncio.gather(*tuple(supervisor._watchers))
        result_path = supervisor._dir(value.attempt_id) / 'result.json'
        receipt = json.loads(result_path.read_text())
        receipt[field] = wrong
        result_path.write_text(json.dumps(receipt))
        assert (await supervisor.inspect(value.attempt_id)).state == 'execution_unknown'
    finally:
        await supervisor.close()


async def test_os_spawn_failure_is_known_failure_without_process_or_watcher(store, tmp_path, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise OSError('fixture fork failure')

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', unavailable)
    supervisor = Supervisor(store, tmp_path)
    value = launch(tmp_path)
    try:
        with pytest.raises(OSError, match='fixture fork failure'):
            await supervisor.start(value)
        record = await store.read('supervised_attempt', value.attempt_id)
        assert record['state'] == 'failed' and record['reason'] == 'launcher_spawn_failed'
        assert record['pid'] is None and not supervisor._watchers
    finally:
        await supervisor.close()


def test_versioned_clock_rejects_partial_or_inconsistent_fields():
    from agentflow.runtime.launcher import startup_deadline

    clock = {'startup_clock_version': 1, 'startup_started_monotonic': 100,
             'startup_deadline_monotonic': 101, 'startup_window_seconds': 1,
             'startup_started_at': '2026-09-28T00:00:00+00:00',
             'startup_deadline_at': '2026-09-28T00:00:01+00:00',
             'startup_boot_identity_source': 'linux_boot_id', 'startup_boot_fingerprint': 'boot-one'}
    assert startup_deadline(clock, 'linux_boot_id', 'boot-one') == 101
    invalid = [
        {key: value for key, value in clock.items() if key != 'startup_clock_version'},
        {**clock, 'startup_deadline_at': '2026-09-28T00:00:30+00:00'},
        {**clock, 'startup_started_at': 'invalid'},
        {**clock, 'startup_clock_version': True},
        {**clock, 'startup_deadline_monotonic': float('nan')},
    ]
    for value in invalid:
        with pytest.raises(ValueError):
            startup_deadline(value, 'linux_boot_id', 'boot-one')


async def test_exit_watcher_phase_is_not_overwritten_by_slow_permit_persistence(store, tmp_path, monkeypatch):
    supervisor = Supervisor(store, tmp_path)
    original = store.command

    async def slow_permit(scope, *args, **kwargs):
        if scope == 'launcher_permit':
            await asyncio.sleep(.3)
        return await original(scope, *args, **kwargs)

    monkeypatch.setattr(store, 'command', slow_permit)
    value = launch(tmp_path)
    try:
        await supervisor.start(value)
        await asyncio.gather(*tuple(supervisor._watchers))
        record = await store.read('supervised_attempt', value.attempt_id)
        assert record['state'] == 'completed' and record['startup_phase'] == 'finished'
    finally:
        await supervisor.close()


async def test_restart_after_ack_but_before_go_revokes_authority(store, tmp_path, monkeypatch):
    supervisor = Supervisor(store, tmp_path)
    original = store.command
    acknowledged, resume = asyncio.Event(), asyncio.Event()

    async def pause_after_ack(scope, *args, **kwargs):
        result = await original(scope, *args, **kwargs)
        if scope == 'launch_ack':
            acknowledged.set()
            await resume.wait()
        return result

    monkeypatch.setattr(store, 'command', pause_after_ack)
    value = launch(tmp_path)
    start = asyncio.create_task(supervisor.start(value))
    try:
        await asyncio.wait_for(acknowledged.wait(), 3)
        await Supervisor(store, tmp_path).recover(value.attempt_id)
        resume.set()
        await asyncio.gather(start, return_exceptions=True)
        await asyncio.gather(*tuple(supervisor._watchers))
        assert not (tmp_path / 'started-0').exists()
        assert not (supervisor._dir(value.attempt_id) / 'go.json').exists()
    finally:
        resume.set()
        await asyncio.gather(start, return_exceptions=True)
        await supervisor.close()


async def test_identity_observation_crossing_deadline_is_startup_timeout(store, tmp_path, monkeypatch):
    supervisor = Supervisor(store, tmp_path, handshake_timeout=.5)
    original = Supervisor._identity_observation

    def slow_observation(identity):
        time.sleep(.6)
        return original(identity)

    monkeypatch.setattr(Supervisor, '_identity_observation', staticmethod(slow_observation))
    value = launch(tmp_path)
    try:
        with pytest.raises(DomainError, match='handshake was not observed'):
            await supervisor.start(value)
        await asyncio.gather(*tuple(supervisor._watchers))
        record = await store.read('supervised_attempt', value.attempt_id)
        assert record['launch_authorization_revoked_reason'] == 'launcher_handshake_timeout'
        assert not (tmp_path / 'started-0').exists()
    finally:
        await supervisor.close()


async def test_timeout_receipt_normalizes_wall_time_only_with_matching_native_birth(store, tmp_path, monkeypatch):
    import psutil

    delay_launcher(monkeypatch, .4)
    original_create_time = psutil.Process.create_time
    monkeypatch.setattr(psutil.Process, 'create_time', lambda process: original_create_time(process) + 10)
    supervisor = Supervisor(store, tmp_path, handshake_timeout=.1)
    value = launch(tmp_path)
    try:
        with pytest.raises(DomainError, match='handshake was not observed'):
            await supervisor.start(value)
        before = await store.read('supervised_attempt', value.attempt_id)
        await asyncio.gather(*tuple(supervisor._watchers))
        record = await store.read('supervised_attempt', value.attempt_id)
        identity = json.loads((supervisor._dir(value.attempt_id) / 'identity.json').read_text())
        assert record['state'] == 'cancelled' and record['reason'] == 'launch_not_authorized'
        assert record['process_started_at'] == identity['process_started_at']
        assert record['launcher_spawn_observed_process_started_at'] == before['process_started_at']
        assert record['process_started_at'] == before['process_started_at'] - 10
        assert not (tmp_path / 'started-0').exists()
    finally:
        await supervisor.close()

import asyncio
import json
import os
import sys
from pathlib import Path

import psutil
import pytest

from agentflow.common import DomainError
from agentflow.runtime.contracts import LaunchSpec
from agentflow.runtime.launcher import RedactingSink
from agentflow.runtime.supervisor import Supervisor


def spec(tmp_path, script, **overrides):
    return LaunchSpec(
        attempt_id="attempt-one", operation_id="operation-one", run_id="run-one",
        input_fingerprint="sha256:" + "a" * 64, fencing_token=1,
        argv=[sys.executable, "-c", script], cwd=tmp_path,
        environment={"PATH": "/usr/bin:/bin", "TASK_TOKEN": "secret-task-token"},
        timeout_seconds=overrides.pop("timeout_seconds", 10), stop_grace_seconds=0.2, **overrides,
    )


async def test_real_launch_receipt_logs_and_exactly_one_start(store, tmp_path):
    supervisor = Supervisor(store, tmp_path)
    script = "import os;from pathlib import Path;Path('count').open('a').write('1');print(os.environ['TASK_TOKEN'])"
    launch = spec(tmp_path, script)
    try:
        handles = await asyncio.gather(*[supervisor.start(launch) for _ in range(4)])
        done = await supervisor.wait(launch.attempt_id)
        assert done.state == "completed" and done.exit_code == 0
        assert (tmp_path / "count").read_text() == "1"
        assert all(handle.pid == done.pid for handle in handles)
        stdout = Path(done.stdout_path).read_text()
        assert "secret-task-token" not in stdout and "[REDACTED]" in stdout
        directory = Path(done.stdout_path).parent
        assert not (directory / "launch.json").exists()
        assert json.loads((directory / "result.json").read_text())["nonce"] == done.launcher_nonce
        assert "secret-task-token" not in json.dumps(await store.read("supervised_attempt", launch.attempt_id))
        with pytest.raises(DomainError):
            await supervisor.start(launch.model_copy(update={"stdin_text": "different"}))
    finally:
        await supervisor.close()


async def test_recovery_uses_existing_process_and_never_restarts(store, tmp_path):
    original = Supervisor(store, tmp_path)
    launch = spec(tmp_path, "import time;from pathlib import Path;Path('count').open('a').write('1');time.sleep(.5)")
    try:
        started = await original.start(launch)
        recovered = Supervisor(store, tmp_path)
        observed = await recovered.recover(launch.attempt_id)
        assert observed.pid == started.pid and observed.state == "running"
        done = await recovered.wait(launch.attempt_id)
        assert done.state == "completed"
        assert (await recovered.start(launch)).pid == started.pid
        assert (tmp_path / "count").read_text() == "1"
        await recovered.close()
    finally:
        await original.close()


async def test_deadline_enforced_even_when_child_does_not_read_stdin(store, tmp_path):
    supervisor = Supervisor(store, tmp_path)
    try:
        launch = spec(tmp_path, "import time;time.sleep(60)", timeout_seconds=.2, stdin_text="x" * 2_000_000)
        await supervisor.start(launch)
        done = await asyncio.wait_for(supervisor.wait(launch.attempt_id), 8)
        assert done.state == "failed" and done.reason == "timeout"
        child = json.loads((Path(done.stdout_path).parent / "child.json").read_text())["child"]
        assert not psutil.pid_exists(child["pid"])
    finally:
        await supervisor.close()


async def test_cancel_stops_observed_descendant(store, tmp_path):
    supervisor = Supervisor(store, tmp_path)
    script = ("import subprocess,sys,time;from pathlib import Path;"
              "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
              "Path('descendant.pid').write_text(str(p.pid));time.sleep(60)")
    try:
        await supervisor.start(spec(tmp_path, script))
        for _ in range(100):
            if (tmp_path / "descendant.pid").exists():
                break
            await asyncio.sleep(.02)
        child_pid = int((tmp_path / "descendant.pid").read_text())
        done = await supervisor.cancel("attempt-one", grace_seconds=5)
        assert done.state == "cancelled"
        assert not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
    finally:
        await supervisor.close()


async def test_missing_receipt_or_wrong_identity_stays_unknown(store, tmp_path):
    supervisor = Supervisor(store, tmp_path)
    await supervisor.start(spec(tmp_path, "pass"))
    done = await supervisor.wait("attempt-one")
    record = await store.read("supervised_attempt", "attempt-one")
    directory = Path(record["directory"])
    (directory / "result.json").unlink()

    def change(tx):
        current = tx.get("supervised_attempt", "attempt-one")
        return tx.put("supervised_attempt", "attempt-one", {**current, "state": "running",
            "process_started_at": current["process_started_at"] - 999}, current["revision"])

    await store.command("test-state", "one", {}, change)
    assert (await supervisor.recover("attempt-one")).state == "execution_unknown"
    assert (await supervisor.cancel("attempt-one")).state == "execution_unknown"
    assert done.pid != os.getpid()
    await supervisor.close()


async def test_corrupted_finished_receipt_cannot_remain_successful(store, tmp_path):
    supervisor = Supervisor(store, tmp_path)
    try:
        await supervisor.start(spec(tmp_path, "pass"))
        done = await supervisor.wait("attempt-one")
        assert done.state == "completed"
        path = Path(done.stdout_path).parent / "result.json"
        path.write_text("not valid JSON")
        assert (await supervisor.recover("attempt-one")).state == "execution_unknown"
    finally:
        await supervisor.close()


async def test_runtime_resume_missing_profile_retains_evidence_without_start(store, tmp_path):
    from types import SimpleNamespace

    from agentflow.runtime.service import RuntimeService

    class UnavailableProfiles:
        async def get(self, profile_id):
            raise DomainError("not_found", "Profile was removed", 404)

    runtime = RuntimeService(store, tmp_path, SimpleNamespace(registry=UnavailableProfiles()))
    try:
        await runtime.supervisor.start(spec(tmp_path, "from pathlib import Path;Path('count').open('a').write('1')"))
        await runtime.supervisor.wait("attempt-one")
        result = await runtime.resume_task({"attempt_id": "attempt-one", "role": "review", "profile_id": "gone"})
        assert result["execution_status"] == "execution_unknown"
        assert Path(result["stdout_path"]).is_file()
        assert (tmp_path / "count").read_text() == "1"
    finally:
        await runtime.close()


def test_redaction_across_chunks_and_log_cap(tmp_path):
    path = tmp_path / "log"
    sink = RedactingSink(path, [b"very-long-secret-value"], 100)
    for chunk in [b"before very-long", b"-sec", b"ret-value after", b"x" * 200]:
        sink.write(chunk)
    sink.close()
    assert b"very-long-secret-value" not in path.read_bytes()
    assert b"[REDACTED]" in path.read_bytes()
    assert len(path.read_bytes()) == 100 and sink.truncated


async def test_delayed_handshake_revokes_permission_without_starting_tool(store, tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec
    async def delayed(*argv, **kwargs):
        if len(argv) > 2 and argv[1:3] == ('-m', 'agentflow.runtime.launcher'):
            wrapper = ('import runpy,sys,time; time.sleep(.25); '
                       'sys.argv=["agentflow.runtime.launcher",sys.argv[1]]; '
                       'runpy.run_module("agentflow.runtime.launcher",run_name="__main__")')
            return await original(argv[0], '-c', wrapper, argv[3], **kwargs)
        return await original(*argv, **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed)
    supervisor = Supervisor(store, tmp_path, handshake_timeout=.05)
    launch = spec(tmp_path, "from pathlib import Path;Path('must-not-start').write_text('executed')")
    try:
        with pytest.raises(DomainError, match='handshake was not observed'):
            await supervisor.start(launch)
        assert (await store.read('supervised_attempt', launch.attempt_id))['state'] == 'execution_unknown'
        await asyncio.wait_for(supervisor._children[launch.attempt_id].wait(), 5)
        assert not (tmp_path / 'must-not-start').exists()
        result = json.loads((supervisor._dir(launch.attempt_id) / 'result.json').read_text())
        assert result['reason'] == 'launch_not_authorized' and result['execution_status'] == 'cancelled'
    finally:
        await supervisor.close()


async def test_transient_identity_observation_rechecks_same_process_before_permit(store, tmp_path, monkeypatch):
    supervisor = Supervisor(store, tmp_path)
    original = Supervisor._identity_observation
    observed = []
    def temporarily_unavailable(identity):
        value = original(identity)
        observed.append(identity['pid'])
        if len(observed) == 1:
            return {**value, 'verified': False, 'reason': 'transient_os_inspection'}
        return value
    monkeypatch.setattr(Supervisor, '_identity_observation', staticmethod(temporarily_unavailable))
    launch = spec(tmp_path, "from pathlib import Path;Path('one-start').open('a').write('started')")
    try:
        await supervisor.start(launch)
        result = await supervisor.wait(launch.attempt_id)
        assert result.state == 'completed'
        assert (tmp_path / 'one-start').read_text() == 'started'
        report = json.loads((supervisor._dir(launch.attempt_id) / 'identity-verification.json').read_text())
        assert report['verified'] and len(report['observations']) == 2
        assert not report['observations'][0]['verified']
        assert report['observations'][0]['pid'] == report['observations'][1]['pid']
    finally:
        await supervisor.close()

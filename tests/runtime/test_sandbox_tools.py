import asyncio
import json
import platform
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psutil
import pytest

from agentflow.adapters.openhands import OpenHandsRoleAdapter
from agentflow.adapters.openhands.tools import ToolBroker
from agentflow.common import DomainError, canonical_digest
from agentflow.runtime.events import CodexEventNormalizer
from agentflow.runtime.sandbox import MacSeatbeltSandbox


async def test_real_mac_sandbox_probes_workspace_protected_state_and_network(task, tmp_path):
    sandbox = MacSeatbeltSandbox(tmp_path / "isolator")
    if platform.system() != "Darwin":
        with pytest.raises(DomainError, match="unavailable"):
            await sandbox.prepare(task, Path(sys.executable).resolve(), tmp_path / "private")
        return
    prefix, evidence = await sandbox.prepare(task, Path(sys.executable).resolve(), tmp_path / "private")
    assert evidence["verified"] and evidence["filesystem"] == "hard"
    assert evidence["process_tree"] == "observed"
    assert evidence['probe_version'] == 2 and evidence['phase'] == 'before_agent_launch'
    assert evidence['probe_timeout_seconds'] == 30 and evidence['failure_code'] is None
    for field in ('attempt_id', 'run_id', 'iteration_id', 'work_item_id', 'fencing_token', 'input_fingerprint'):
        assert evidence[field] == getattr(task, field)
    binding = {field: evidence[field] for field in ('probe_version', 'phase', 'attempt_id', 'run_id',
        'iteration_id', 'work_item_id', 'fencing_token', 'input_fingerprint', 'policy_fingerprint')}
    assert evidence['probe_binding_fingerprint'] == canonical_digest(binding)
    assert evidence['workspace_read_probe'] == 0
    assert evidence['readonly_workspace_write_probe'] == 77
    assert evidence['protected_read_probe'] == evidence['protected_write_probe'] == evidence['denied_port_probe'] == 77
    assert all(result['passed'] and not result['timed_out'] for result in evidence['probe_results'].values())
    assert prefix[0] == "/usr/bin/sandbox-exec"
    assert await sandbox._run_probe(Path(prefix[-1]),
        f"from pathlib import Path;Path({str(task.workspace / 'original.py')!r}).write_text('changed')") != 0
    assert (task.workspace / "original.py").read_text() == "ORIGINAL = True\n"


@pytest.mark.parametrize('timeout', [0, -1, True, float('nan'), float('inf'), 121])
def test_probe_timeout_is_explicit_and_bounded(tmp_path, timeout):
    with pytest.raises(ValueError, match='probe_timeout_seconds'):
        MacSeatbeltSandbox(tmp_path / 'invalid', probe_timeout_seconds=timeout)


@pytest.mark.parametrize('code', [-1, 0, 1, 2, 126, 127])
async def test_negative_probe_only_accepts_the_permission_denied_exit(task, tmp_path, monkeypatch, code):
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator')
    monkeypatch.setattr(platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr('agentflow.runtime.sandbox.shutil.which', lambda _: '/usr/bin/sandbox-exec')
    async def execute(profile, script):
        if '.forbidden' in script and 'os.O_RDONLY' in script:
            return code
        return 77 if 'except PermissionError:' in script else 0
    monkeypatch.setattr(sandbox, '_run_probe', execute)
    with pytest.raises(DomainError) as error:
        await sandbox.prepare(task, Path(sys.executable).resolve(), tmp_path / 'private')
    assert error.value.code == 'isolation_unverified'
    evidence = error.value.details
    assert evidence['failure_code'] == 'isolation_unverified' and not evidence['verified']
    observed = evidence['probe_results']['protected_read_probe']
    assert observed['exit_code'] == code and not observed['passed'] and not observed['timed_out']
    assert observed['outcome'] == ('completed' if code == 0 else 'exit_error')
    saved = list(sandbox.state_dir.glob('*.probe.json'))
    assert len(saved) == 1 and json.loads(saved[0].read_text()) == evidence
    assert not list(task.workspace.glob('.agentflow-readonly-probe-*'))
    assert (task.workspace / 'original.py').read_text() == 'ORIGINAL = True\n'


async def test_timeout_evidence_is_distinct_from_signal_exit_and_blocks_agent_launch(task, tmp_path, monkeypatch):
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator', probe_timeout_seconds=0.1)
    monkeypatch.setattr(platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr('agentflow.runtime.sandbox.shutil.which', lambda _: '/usr/bin/sandbox-exec')
    async def execute(profile, script):
        if '.forbidden' in script and 'os.O_RDONLY' in script:
            raise DomainError('isolation_probe_timeout', 'fixture timeout')
        return 77 if 'except PermissionError:' in script else 0
    monkeypatch.setattr(sandbox, '_run_probe', execute)
    supervisor = SimpleNamespace(root=tmp_path / 'supervisor', start=AsyncMock())
    adapter = OpenHandsRoleAdapter(supervisor, sandbox)
    monkeypatch.setattr(adapter, 'probe', AsyncMock(return_value={'available': True}))
    with pytest.raises(DomainError) as error:
        await adapter.start(task)
    assert error.value.code == 'isolation_probe_timeout'
    evidence = error.value.details
    assert evidence['failure_code'] == 'isolation_probe_timeout' and evidence['phase'] == 'before_agent_launch'
    assert evidence['probe_results']['protected_read_probe']['timed_out'] is True
    assert evidence['probe_results']['protected_read_probe']['outcome'] == 'timeout'
    assert evidence['protected_read_probe'] == -1
    assert not evidence['verified']
    supervisor.start.assert_not_awaited()


async def test_probe_start_failure_is_not_permission_denial(tmp_path, monkeypatch):
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator')
    async def missing(*args, **kwargs):
        raise FileNotFoundError('fixture executable absent')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', missing)
    with pytest.raises(DomainError) as error:
        await sandbox._run_probe(tmp_path / 'profile.sb', 'pass')
    assert error.value.code == 'isolation_unverified'
    assert error.value.details['outcome'] == 'start_error'


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Real macOS probe process')
async def test_real_slow_probe_uses_longer_default_without_weakening_denial(tmp_path):
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator')
    profile = sandbox.state_dir / 'slow.sb'
    profile.write_text(sandbox.policy([Path(sys.prefix), Path(sys.base_prefix)], [], []))
    assert await sandbox._run_probe(profile, 'import time;time.sleep(5.2)') == 0
    assert await sandbox._run_probe(profile, sandbox._denial_probe("raise PermissionError('fixture')")) == 77
    assert await sandbox._run_probe(profile, sandbox._denial_probe("raise FileNotFoundError('fixture')")) == 1


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Real macOS probe process')
async def test_real_timed_out_probe_is_killed_and_reaped(tmp_path, monkeypatch):
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator', probe_timeout_seconds=0.1)
    profile = sandbox.state_dir / 'timeout.sb'
    profile.write_text(sandbox.policy([Path(sys.prefix), Path(sys.base_prefix)], [], []))
    original = asyncio.create_subprocess_exec
    processes = []
    async def capture(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', capture)
    with pytest.raises(DomainError) as error:
        await sandbox._run_probe(profile, 'import time;time.sleep(30)')
    assert error.value.code == 'isolation_probe_timeout'
    assert error.value.details['timed_out'] is True
    assert processes[0].returncode is not None
    assert not psutil.pid_exists(processes[0].pid)


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Real macOS readonly source permission')
async def test_readonly_task_with_accidentally_writable_source_fails_without_modifying_code(task, tmp_path):
    source = task.workspace / 'original.py'
    before = source.read_bytes()
    inconsistent = task.model_copy(update={'allowed_write_roots': [source], 'allow_code_write': False})
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator')
    with pytest.raises(DomainError) as error:
        await sandbox.prepare(inconsistent, Path(sys.executable).resolve(), tmp_path / 'private')
    assert error.value.code == 'isolation_unverified'
    assert error.value.details['readonly_workspace_write_probe'] == 0
    assert not error.value.details['verified']
    assert source.read_bytes() == before


async def test_unverified_platform_fails_closed_and_research_keeps_no_direct_egress(task, tmp_path, monkeypatch):
    sandbox = MacSeatbeltSandbox(tmp_path / "isolator")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    with pytest.raises(DomainError, match="unavailable"):
        await sandbox.prepare(task, Path(sys.executable), tmp_path / "private")
    assert await sandbox.resolve_web_hosts(task.model_copy(update={"allow_public_web": True})) == {}
    assert await sandbox.resolve_web_hosts(task.model_copy(update={"allowed_web_hosts": ["127.0.0.1"]})) == {}


async def test_protected_state_cannot_be_added_as_a_readable_subdirectory(task, tmp_path):
    if platform.system() != "Darwin":
        pytest.skip("macOS policy preparation")
    sandbox = MacSeatbeltSandbox(tmp_path / "isolator")
    protected = tmp_path / "controller_state"
    (protected / "nested").mkdir(parents=True)
    task = task.model_copy(update={"protected_roots": [protected], "allowed_read_roots": [protected / "nested"]})
    with pytest.raises(DomainError, match="protected"):
        await sandbox.prepare(task, Path(sys.executable).resolve(), tmp_path / "private")


def test_tools_only_read_code_and_write_staged_documents(task, tmp_path):
    broker = ToolBroker(task)
    assert "ORIGINAL" in broker.read_code("original.py")["text"]
    broker.write_document("review.md", "review result")
    assert (task.artifact_dir / "review.md").read_text() == "review result"
    for path in ["change.py", "../source/original.py", ".env", "auth.json"]:
        with pytest.raises(DomainError):
            broker.write_document(path, "forbidden")
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (task.workspace / "linked").symlink_to(outside)
    for path in ["linked", "../outside.txt", str(outside)]:
        with pytest.raises(DomainError):
            broker.read_code(path)
    with pytest.raises(DomainError):
        broker.execute("terminal", command="echo forbidden")
    proposal = broker.propose_work({"role": "development", "goal": "Implement separately"})
    assert proposal["state"] == "proposed_not_dispatched"
    assert json.loads((task.artifact_dir / proposal["proposal_path"]).read_text())["dispatch_authorized"] is False
    assert (task.workspace / "original.py").read_text() == "ORIGINAL = True\n"


def test_tool_count_includes_finish_and_blocks_before_side_effect(task):
    broker = ToolBroker(task.model_copy(update={"max_tool_calls": 1}))
    broker.read_code("original.py")
    with pytest.raises(DomainError, match="quota"):
        broker.finish({"review": "done"})
    assert not (task.artifact_dir / "openhands_final.json").exists()
    assert broker.calls == 1


def test_frozen_context_reader_pages_without_exposing_arbitrary_files(task, tmp_path):
    import hashlib

    context = tmp_path / 'context'
    context.mkdir()
    data = json.dumps({'content': '产品目标与市场信息' * 4000}, ensure_ascii=False).encode()
    name = hashlib.sha256(data).hexdigest() + '.json'
    path = context / name
    path.write_bytes(data)
    broker = ToolBroker(task.model_copy(update={'allowed_read_roots': [context], 'context_directory': context}))
    first = broker.execute('read_context', path=name, limit=12000)
    second = broker.execute('read_context', path=name, offset=first['next_offset'], limit=12000)
    assert first['has_more'] and first['text'] + second['text'] == data.decode()[:24000]
    for invalid in ['../auth.json', str(path), '.env', 'not-indexed.json']:
        with pytest.raises(DomainError):
            broker.read_context(invalid)
    with pytest.raises(DomainError):
        broker.read_context(name, limit=100000)
    path.write_text('modified')
    with pytest.raises(DomainError, match='digest'):
        broker.read_context(name)


def test_coding_write_scope_is_explicit_and_cannot_escape_workspace(task, tmp_path):
    coding = task.model_copy(update={"allow_code_write": True})
    with pytest.raises(DomainError, match="explicit"):
        coding.assert_paths()
    with pytest.raises(DomainError, match="owned workspace"):
        coding.model_copy(update={"allowed_write_roots": [tmp_path / "outside"]}).assert_paths()


def test_unknown_and_malformed_codex_events_are_not_success(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n')
    assert CodexEventNormalizer().read(path)["errors"] == ["terminal_event_missing"]
    path.write_text('{"type":"unknown"}\nnot json\n{"type":"turn.completed","usage":{"input_tokens":1}}\n')
    result = CodexEventNormalizer().read(path)
    assert not result["valid"] and len(result["errors"]) == 2

import asyncio
import sys

import pytest

from agentflow.common import DomainError
from agentflow.execution.models import JobLimits
from agentflow.execution.process import CommandSpec, ProcessExecutor
from node_agent.journal import NodeJournal


async def test_process_real_output_and_credential_scrubbing(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-inherit")
    monkeypatch.setenv("AGENTFLOW_OWNER_TOKEN", "must-not-inherit")
    script = tmp_path / "inspect.py"
    script.write_text("import os; print(os.getenv('DEEPSEEK_API_KEY')); print(os.getenv('AGENTFLOW_OWNER_TOKEN'))")
    result = await ProcessExecutor().execute(CommandSpec(argv=(sys.executable, str(script)), cwd=tmp_path,
                                                        label="test"), tmp_path / "logs", JobLimits())
    assert result.execution_status == "completed"
    assert (tmp_path / "logs/stdout.log").read_text().splitlines() == ["None", "None"]
    assert result.cleanup_verified


async def test_process_start_barrier_is_recorded_before_project_code(tmp_path):
    journal = NodeJournal(tmp_path / "private/journal.sqlite")
    assignment = {"job_id": "one", "attempt_id": "a", "fencing_token": 1, "input_fingerprint": "fp"}
    assert journal.record_assignment(assignment) == "start_new"
    assert journal.record_assignment(assignment) == "observe_existing"
    journal.mark_starting("one")
    output = tmp_path / "executed"
    script = tmp_path / "work.py"
    script.write_text(f"from pathlib import Path; Path({str(output)!r}).write_text('done')")

    async def identified(pid, fp):
        assert not output.exists()
        journal.record_process("one", pid, fp)
        assert journal.get("one")["state"] == "running"
    result = await ProcessExecutor().execute(CommandSpec(argv=(sys.executable, str(script)), cwd=tmp_path,
        label="barrier"), tmp_path / "logs", JobLimits(), on_start=identified)
    assert result.execution_status == "completed" and output.read_text() == "done"
    journal.record_result("one", result.model_dump(mode="json"))
    journal.close()
    reopened = NodeJournal(tmp_path / "private/journal.sqlite")
    assert reopened.record_assignment(assignment) == "terminal"
    with pytest.raises(DomainError):
        reopened.record_assignment({**assignment, "fencing_token": 2})
    reopened.close()


async def test_cancel_and_output_limits_really_stop_child(tmp_path):
    script = tmp_path / "loop.py"
    script.write_text("import time\nwhile True:\n print('x'*5000, flush=True)\n time.sleep(.01)\n")
    result = await ProcessExecutor().execute(CommandSpec(argv=(sys.executable, str(script)), cwd=tmp_path,
        label="output"), tmp_path / "limited", JobLimits(maximum_output_bytes=1024))
    assert result.execution_status == "error" and result.output_truncated and result.cleanup_verified
    cancel = asyncio.Event()
    task = asyncio.create_task(ProcessExecutor().execute(CommandSpec(argv=(sys.executable, "-c", "import time; time.sleep(60)"),
        cwd=tmp_path, label="cancel"), tmp_path / "cancel", JobLimits(), cancel))
    await asyncio.sleep(.15)
    cancel.set()
    result = await task
    assert result.execution_status == "cancelled" and result.cleanup_verified

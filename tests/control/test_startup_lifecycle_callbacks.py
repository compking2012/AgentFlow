import pytest
from test_recovery import env as env
from test_recovery import patch
from test_startup_reconciliation import expired_permit

from agentflow.control.execution_reconciliation import ExecutionReconciliation


async def test_real_collector_cancelled_status_is_reconciled(env):
    await expired_permit(env)
    await patch(env, "work_item", "bad", status="running", runtime_failure_code=None)
    await patch(env, "attempt", "bad-attempt", status="running", runtime_failure_code=None)
    await env.workflow.finish_attempt(
        "bad-attempt",
        {
            "execution_status": "cancelled",
            "quality_result": "unknown",
            "fencing_token": 1,
            "input_fingerprint": "bad-input",
            "runtime_failure_code": "worker_exited",
        },
        "normal-collector-result",
        verified_artifacts=[],
    )
    assert (await env.store.read("work_item", "bad"))["status"] == "cancelled"
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    work = await env.store.read("work_item", "bad")
    assert work["status"] == "failed" and work["runtime_failure_code"] == "launcher_startup_timeout", work


async def test_late_callback_cannot_undo_reconciliation(env):
    await expired_permit(env)
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    work = await env.store.read("work_item", "bad")
    assert work["status"] == "failed"
    await env.workflow.block_attempt(
        "bad-attempt", "Result cannot change a finished attempt", "late-callback"
    )
    work = await env.store.read("work_item", "bad")
    assert work["status"] == "failed", work


@pytest.mark.parametrize("coding", [False, True])
@pytest.mark.parametrize("resuming", [False, True])
async def test_runtime_and_scheduler_expired_permit_is_retryable(env, monkeypatch, coding, resuming):
    import asyncio
    import os
    import signal
    import sys
    import time

    from pydantic import SecretStr

    import agentflow.runtime.supervisor as supervisor_module
    from agentflow.control.scheduler import Scheduler
    from agentflow.runtime.contracts import LaunchSpec, TaskEnvelope
    from agentflow.runtime.service import RuntimeService

    await patch(env, "run", "run", execution_state="running")
    from agentflow.control.coding_steps import CodingSteps

    if coding:
        await patch(env, "work_item", "bad", step="implementation", role="development", write_paths=["src"])
    work = await patch(
        env,
        "work_item",
        "bad",
        status="running",
        runtime_failure_code=None,
        input_fingerprint="sha256:" + "a" * 64,
    )
    await patch(
        env,
        "attempt",
        "bad-attempt",
        status="running",
        runtime_failure_code=None,
        input_fingerprint="sha256:" + "a" * 64,
    )
    task = {
        "attempt_id": "bad-attempt",
        "work_item_id": "bad",
        "run_id": "run",
        "iteration_id": "iteration",
        "step": "research",
        "role": "research",
        "fencing_token": 1,
        "input_fingerprint": "sha256:" + "a" * 64,
    }
    if coding:
        work = await env.store.read("work_item", "bad")
        control = await CodingSteps(env.store, env.settings, env.service.repository).prepare(
            await env.store.read("run", "run"),
            work,
            await env.store.read("attempt", "bad-attempt"),
            env.project["base_commit"],
            512,
        )
        task.update(step="implementation", role="development", coding_step=control)
    before_budget = await env.store.list("coding_work_budget")
    await patch(env, "dispatch_context", "bad-attempt", task=task)
    artifact_dir = env.settings.data_dir / "attempt_artifacts" / "fixture"
    artifact_dir.mkdir(parents=True)
    envelope = TaskEnvelope(
        attempt_id="bad-attempt",
        operation_id="bad-attempt",
        work_item_id="bad",
        run_id="run",
        iteration_id="iteration",
        role="research",
        goal="fixture",
        input_fingerprint="sha256:" + "a" * 64,
        fencing_token=1,
        workspace=env.tmp_path,
        artifact_dir=artifact_dir,
        model_profile_id="fixture",
        model="fixture",
        proxy_base_url="http://127.0.0.1:1/v1",
        proxy_token=SecretStr("fixture"),
        output_schema={"type": "object"},
    )
    runtime = RuntimeService(
        env.store,
        env.settings.data_dir,
        None,
        sandbox=object(),
        settings=env.settings.model_copy(update={"agent_startup_timeout_seconds": 0.6}),
    )

    async def start(value):
        return await runtime.supervisor.start(
            LaunchSpec(
                attempt_id=value.attempt_id,
                operation_id=value.operation_id,
                run_id=value.run_id,
                input_fingerprint=value.input_fingerprint,
                fencing_token=value.fencing_token,
                argv=[sys.executable, "-c", 'raise AssertionError("child forbidden")'],
                cwd=env.tmp_path,
                timeout_seconds=3,
                stop_grace_seconds=0.1,
            )
        )

    adapter = runtime.codex if coding else runtime.openhands
    adapter.start = start

    async def envelope_for(task, **kwargs):
        return envelope, adapter

    runtime._envelope = envelope_for
    original = supervisor_module.atomic_json
    continuations = []

    def stop_before_go(path, value):
        if path.name == "go.json":
            process = runtime.supervisor._children["bad-attempt"]
            os.kill(process.pid, signal.SIGSTOP)

            async def resume():
                await asyncio.sleep(max(0, value["startup_deadline_monotonic"] - time.monotonic()) + 0.03)
                os.kill(process.pid, signal.SIGCONT)

            continuations.append(asyncio.create_task(resume()))
        return original(path, value)

    monkeypatch.setattr(supervisor_module, "atomic_json", stop_before_go)
    scheduler = Scheduler(env.workflow, env.store, runtime, None, env.settings)
    try:
        if resuming:
            await start(envelope)
        await scheduler._execute_existing(task, resume=resuming)
        work = await env.store.read("work_item", "bad")
        assert work["status"] == "blocked" and work["runtime_failure_code"] == "execution_unconfirmed", work
        await scheduler.execution_reconciliation.reconcile()
        work = await env.store.read("work_item", "bad")
        assert work["status"] == "failed" and work["runtime_failure_code"] == "launcher_startup_timeout", work
        assert before_budget == await env.store.list("coding_work_budget")
        assert not await env.store.list("coding_step_usage")
        if coding:
            from agentflow.control.recovery import coding_usage_blockers, coding_usage_snapshot

            state = await coding_usage_snapshot(env.store, "run", "bad")
            assert await coding_usage_blockers(state, env.settings.data_dir, "bad") == []
    finally:
        await asyncio.gather(*continuations)
        await runtime.supervisor.close()


@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
async def test_late_block_callback_preserves_terminal_outcomes(env, status):
    work = await patch(env, 'work_item', 'bad', status=status)
    attempt = await patch(env, 'attempt', 'bad-attempt', status=status)
    await env.workflow.block_attempt('bad-attempt', 'Late callback', 'late-terminal-callback',
                                    failure_code='execution_unconfirmed')
    assert await env.store.read('work_item', 'bad') == work
    assert await env.store.read('attempt', 'bad-attempt') == attempt


async def test_owner_cancelled_run_is_not_reopened_by_startup_evidence_or_late_callback(env):
    await expired_permit(env)
    run = await env.store.read('run', 'run')
    await env.workflow.control_run('run', {'expected_revision': run['revision'], 'action': 'cancel',
        'reason': 'Owner stops this run'}, 'owner-stops-startup')
    work = await env.store.read('work_item', 'bad')
    await env.workflow.block_attempt('bad-attempt', 'Late startup callback', 'late-cancel-callback',
                                    failure_code='execution_unconfirmed')
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert await env.store.read('work_item', 'bad') == work
    assert (await env.store.read('run', 'run'))['execution_state'] == 'cancelled'
    assert not await env.store.list('execution_reconciliation')

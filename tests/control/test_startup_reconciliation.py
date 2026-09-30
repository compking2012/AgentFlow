"""Real unpermitted launch receipts reconcile only proven startup timeouts."""
import asyncio
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime, timedelta

import psutil
import pytest
from test_recovery import env as env
from test_recovery import patch

from agentflow.common import canonical_digest, utc_now
from agentflow.control.execution_reconciliation import ExecutionReconciliation
from agentflow.control.failure_remediation import FailureRemediation
from agentflow.runtime.launcher import atomic_json
from agentflow.runtime.process_birth import process_birth_identity
from agentflow.runtime.process_identity import current_boot_identity


async def startup_timeout(env, *, work_id='bad', legacy=True):
    await patch(env, 'run', 'run', execution_state='running')
    original = await env.store.read('work_item', 'bad')
    attempt_id = work_id + '-attempt'
    work = await patch(env, 'work_item', work_id,
        **{k: v for k, v in original.items() if k not in {'id', 'revision', 'attempt_id', 'key', 'status', 'runtime_failure_code', 'step', 'role'}},
        key=work_id, attempt_id=attempt_id, step='code_review', role='review', status='blocked', runtime_failure_code='execution_unconfirmed')
    await patch(env, 'attempt', attempt_id, run_id='run', iteration_id='iteration', work_item_id=work_id,
        generation=1, fencing_token=1, input_fingerprint=work['input_fingerprint'], status='blocked',
        execution_status=None, runtime_failure_code='execution_unconfirmed')
    await patch(env, 'dispatch_context', attempt_id, task={
        'attempt_id': attempt_id, 'work_item_id': work_id, 'run_id': 'run', 'iteration_id': 'iteration',
        'step': work['step'], 'role': work['role'], 'fencing_token': 1, 'input_fingerprint': work['input_fingerprint']})
    directory = env.settings.data_dir / 'supervisor' / canonical_digest({'attempt_id': attempt_id})[7:]
    directory.mkdir(parents=True, mode=0o700)
    directory.parent.chmod(0o700)
    nonce = 'a' * 64
    created_at = utc_now()
    atomic_json(directory / 'cancel.json', {'nonce': nonce})
    atomic_json(directory / 'launch.json', {'attempt_id': attempt_id, 'operation_id': work_id + '-operation',
        'nonce': nonce, 'fencing_token': 1, 'argv': [sys.executable, '-c', 'raise AssertionError("must not start")'],
        'cwd': str(env.tmp_path), 'environment': {}, 'max_log_bytes': 1000, 'timeout_seconds': 3,
        'stop_grace_seconds': .1})
    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'agentflow.runtime.launcher',
        str(directory / 'launch.json'), start_new_session=True, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE)
    _, error = await process.communicate()
    assert process.returncode == 0, error
    identity = json.loads((directory / 'identity.json').read_text())
    receipt = json.loads((directory / 'result.json').read_text())
    assert receipt['reason'] == 'launch_not_authorized' and not (directory / 'go.json').exists()
    row = {**identity, 'run_id': 'run', 'input_fingerprint': work['input_fingerprint'],
        'directory': str(directory), 'state': 'execution_unknown', 'reason': 'launcher_handshake_timeout',
        'exit_code': None, 'created_at': created_at}
    if legacy:
        row.update(pid=None, process_started_at=None, boot_fingerprint=None)
        for field in ('boot_identity_source', 'process_birth_source', 'process_birth_fingerprint'):
            row.pop(field, None)
    await patch(env, 'supervised_attempt', attempt_id, **row)
    return directory


async def test_historical_blocked_timeout_becomes_failed_without_changing_raw_evidence(env):
    directory = await startup_timeout(env)
    originals = {kind: await env.store.read(kind, 'bad' if kind == 'work_item' else 'bad-attempt')
        for kind in ('work_item', 'attempt', 'supervised_attempt')}
    files = {path.name: path.read_bytes() for path in directory.iterdir()}
    budgets = await env.store.list('budget_account')
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    work = await env.store.read('work_item', 'bad')
    assert work['status'] == 'failed' and work['runtime_failure_code'] == 'launcher_startup_timeout'
    assert work['attempt_id'] == 'bad-attempt' and work['generation'] == 1
    attempt = await env.store.read('attempt', 'bad-attempt')
    assert attempt['status'] == 'failed' and attempt['execution_status'] == 'failed'
    supervised = await env.store.read('supervised_attempt', 'bad-attempt')
    assert supervised['state'] == 'cancelled' and supervised['reason'] == 'launch_not_authorized'
    assert supervised['pid'] == json.loads(files['identity.json'])['pid']
    audits = await env.store.list('execution_reconciliation')
    assert len(audits) == 1 and audits[0]['outcome'] == 'not_started'
    assert audits[0]['reason'] == 'launcher_handshake_timeout'
    assert audits[0]['original_work'] == originals['work_item']
    assert audits[0]['original_attempt'] == originals['attempt']
    assert audits[0]['original_process'] == originals['supervised_attempt']
    assert audits[0]['finished_at'] == json.loads(files['result.json'])['finished_at']
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == files
    assert await env.store.list('budget_account') == budgets
    assert not await env.store.list('model_invocation')
    assert not await env.store.list('run_recovery')


async def test_three_startup_failures_enter_existing_bounded_recovery_once(env):
    for identity in ('bad', 'bad-two', 'bad-three'):
        await startup_timeout(env, work_id=identity)
    env.workflow.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0})
    budgets = await env.store.list('budget_account')
    upstream = await env.store.read('work_item', 'upstream')
    await asyncio.gather(*(ExecutionReconciliation(env.store, env.workflow).reconcile() for _ in range(3)))
    assert len(await env.store.list('execution_reconciliation')) == 3
    for identity in ('bad', 'bad-two', 'bad-three'):
        result = await FailureRemediation(env.store, env.workflow).repair(identity)
        assert result['status'] == 'repair_scheduled', result
        work = await env.store.read('work_item', identity)
        assert work['generation'] == 2 and work['status'] == 'pending' and work['attempt_id'] is None
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert len(await env.store.list('execution_reconciliation')) == 3
    assert len(await env.store.list('run_recovery')) == 3
    assert await env.store.read('work_item', 'upstream') == upstream
    assert await env.store.list('budget_account') == budgets
    assert not await env.store.list('model_invocation')
    assert not await env.store.list('code_snapshot')


@pytest.mark.parametrize('damage', ['permit', 'child', 'stdout', 'stderr', 'cancel_missing', 'wrong_nonce',
    'wrong_fence', 'wrong_operation', 'wrong_identity', 'wrong_generation', 'stale_context', 'owner_cancel',
    'user_cancelled_run', 'waiting_approval', 'wrong_root', 'symlink', 'invocation', 'request_count',
    'uncertainty', 'restored_context'])
async def test_ambiguous_or_authorized_launch_is_never_reclassified(env, damage):
    directory = await startup_timeout(env)
    if damage in {'permit', 'child', 'stdout', 'stderr'}:
        name = {'permit': 'go.json', 'child': 'child.json', 'stdout': 'stdout.jsonl', 'stderr': 'stderr.log'}[damage]
        atomic_json(directory / name, {})
    elif damage == 'cancel_missing':
        (directory / 'cancel.json').unlink()
    elif damage in {'wrong_nonce', 'wrong_fence', 'wrong_operation', 'wrong_identity'}:
        path = directory / ('identity.json' if damage == 'wrong_identity' else 'result.json')
        value = json.loads(path.read_text())
        value[{'wrong_nonce': 'nonce', 'wrong_fence': 'fencing_token', 'wrong_operation': 'operation_id',
            'wrong_identity': 'nonce'}[damage]] = 'wrong'
        atomic_json(path, value)
    elif damage == 'wrong_generation':
        await patch(env, 'work_item', 'bad', generation=2)
    elif damage == 'stale_context':
        context = await env.store.read('dispatch_context', 'bad-attempt')
        await patch(env, 'dispatch_context', 'bad-attempt', task={**context['task'], 'fencing_token': 9})
    elif damage == 'owner_cancel':
        atomic_json(directory / 'cancel.json', {'nonce': 'a' * 64, 'reason': 'owner_cancelled'})
    elif damage == 'user_cancelled_run':
        await patch(env, 'run', 'run', execution_state='cancelled')
    elif damage == 'waiting_approval':
        await patch(env, 'work_item', 'bad', status='waiting_approval')
    elif damage == 'wrong_root':
        await patch(env, 'supervised_attempt', 'bad-attempt', directory=str(env.tmp_path))
    elif damage == 'symlink':
        target = directory / 'real-result.json'
        (directory / 'result.json').rename(target)
        (directory / 'result.json').symlink_to(target)
    elif damage == 'invocation':
        await patch(env, 'model_invocation', 'call', attempt_id='bad-attempt', run_id='run', state='completed')
    elif damage in {'request_count', 'uncertainty'}:
        await patch(env, 'model_attempt_budget', 'bad-attempt', attempt_id='bad-attempt',
            request_count=int(damage == 'request_count'), uncertain_invocations=int(damage == 'uncertainty'))
    else:
        await patch(env, 'dispatch_context', 'bad-attempt', restore_uncertain=True)
    before = await env.store.read('work_item', 'bad')
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert await env.store.read('work_item', 'bad') == before
    assert not await env.store.list('execution_reconciliation')


async def test_paused_run_reconciles_without_automatic_start(env):
    await startup_timeout(env)
    await patch(env, 'run', 'run', execution_state='paused')
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert (await env.store.read('work_item', 'bad'))['runtime_failure_code'] == 'launcher_startup_timeout'
    await FailureRemediation(env.store, env.workflow).reconcile()
    assert (await env.store.read('run', 'run'))['execution_state'] == 'paused'
    assert not await env.store.list('run_recovery')


async def test_unrelated_parallel_reservation_does_not_prevent_startup_classification(env):
    await startup_timeout(env)
    account = next(row for row in await env.store.list('budget_account') if row['owner_kind'] == 'run')
    await patch(env, 'budget_account', account['id'], reserved_micros=1)
    await patch(env, 'model_invocation', 'other-call', attempt_id='other-attempt', run_id='run', state='reserved')
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert (await env.store.read('work_item', 'bad'))['runtime_failure_code'] == 'launcher_startup_timeout'
    assert (await env.store.read('model_invocation', 'other-call'))['state'] == 'reserved'


@pytest.mark.parametrize('race', ['receipt', 'model_invocation', 'model_budget', 'owner_cancel'])
async def test_proof_and_usage_are_rechecked_inside_atomic_reconciliation(env, monkeypatch, race):
    directory = await startup_timeout(env)
    command = env.store.command
    changed = False
    async def race_before_commit(scope, key, payload, handler):
        nonlocal changed
        if scope == 'execution.reconcile' and not changed:
            changed = True
            if race == 'receipt':
                path = directory / 'result.json'
                path.write_bytes(path.read_bytes() + b'\n')
            elif race == 'model_invocation':
                await patch(env, 'model_invocation', 'late-call', attempt_id='bad-attempt', state='reserved')
            elif race == 'model_budget':
                await patch(env, 'model_attempt_budget', 'bad-attempt', attempt_id='bad-attempt',
                    request_count=1, uncertain_invocations=0)
            else:
                await patch(env, 'run', 'run', execution_state='cancelled')
        return await command(scope, key, payload, handler)
    monkeypatch.setattr(env.store, 'command', race_before_commit)
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert (await env.store.read('work_item', 'bad'))['status'] == 'blocked'
    assert not await env.store.list('execution_reconciliation')
    if race == 'receipt':
        await ExecutionReconciliation(env.store, env.workflow).reconcile()
        assert (await env.store.read('work_item', 'bad'))['status'] == 'failed'


async def test_terminal_cancelled_supervisor_retains_timeout_classification(env):
    directory = await startup_timeout(env, legacy=False)
    process = await env.store.read('supervised_attempt', 'bad-attempt')
    revoked_at = process['created_at']
    atomic_json(directory / 'cancel.json', {'nonce': process['nonce'], 'reason': 'launcher_handshake_timeout',
        'revoked_at': revoked_at})
    await patch(env, 'supervised_attempt', 'bad-attempt', state='cancelled', reason='launch_not_authorized',
        launch_authorization_revoked_reason='launcher_handshake_timeout', launch_authorization_revoked_at=revoked_at)
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert (await env.store.read('work_item', 'bad'))['runtime_failure_code'] == 'launcher_startup_timeout'


async def test_reconciled_startup_diagnostic_is_visible_without_private_receipt_fields(env):
    await startup_timeout(env)
    from agentflow.runtime.trace import ExecutionTrace
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    page = await ExecutionTrace(env.store).page('bad-attempt')
    text = json.dumps(page, ensure_ascii=False)
    assert 'Agent 尚未执行' in text
    assert 'a' * 64 not in text and 'nonce' not in text and 'process_birth' not in text


async def test_coding_startup_reconciliation_does_not_invent_usage_or_reset_totals(env):
    directory = await startup_timeout(env)
    from agentflow.control.coding_steps import CodingSteps
    from agentflow.control.recovery import coding_usage_blockers, coding_usage_snapshot
    run = await env.store.read('run', 'run')
    work = await patch(env, 'work_item', 'bad', step='implementation', role='development', write_paths=['src'], status='running')
    attempt = await env.store.read('attempt', 'bad-attempt')
    control = await CodingSteps(env.store, env.settings, env.service.repository).prepare(
        run, work, attempt, env.project['base_commit'], 512)
    await patch(env, 'work_item', 'bad', status='blocked')
    context = await env.store.read('dispatch_context', 'bad-attempt')
    await patch(env, 'dispatch_context', 'bad-attempt', task={**context['task'], 'step': 'implementation',
        'role': 'development', 'coding_step': control})
    budget = await env.store.list('coding_work_budget')
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert (await env.store.read('work_item', 'bad'))['status'] == 'failed'
    state = await coding_usage_snapshot(env.store, 'run', 'bad')
    assert await coding_usage_blockers(state, env.settings.data_dir, 'bad') == []
    assert await env.store.list('coding_work_budget') == budget
    assert not await env.store.list('coding_step_usage')
    # Persisted audit never replaces re-reading live receipt/permit evidence.
    atomic_json(directory / 'go.json', {})
    state = await coding_usage_snapshot(env.store, 'run', 'bad')
    assert await coding_usage_blockers(state, env.settings.data_dir, 'bad')


async def expired_permit(env, *, permit=True):
    directory = await startup_timeout(env)
    original = await env.store.read('supervised_attempt', 'bad-attempt')
    for name in ('cancel.json', 'identity.json', 'result.json'):
        (directory / name).unlink()
    source, boot = current_boot_identity()
    started_at, started = datetime.now(UTC), time.monotonic()
    clock = {'startup_clock_version': 1, 'startup_started_at': started_at.isoformat(),
        'startup_started_monotonic': started, 'startup_deadline_at': (started_at + timedelta(seconds=.5)).isoformat(),
        'startup_deadline_monotonic': started + .5, 'startup_window_seconds': .5,
        'startup_boot_identity_source': source, 'startup_boot_fingerprint': boot}
    atomic_json(directory / 'launch.json', {**clock, 'attempt_id': 'bad-attempt', 'operation_id': 'bad-operation',
        'nonce': original['nonce'], 'fencing_token': 1, 'argv': [sys.executable, '-c', 'raise AssertionError("must not execute")'],
        'cwd': str(env.tmp_path), 'environment': {}, 'max_log_bytes': 1000, 'timeout_seconds': 3,
        'stop_grace_seconds': .1})
    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'agentflow.runtime.launcher',
        str(directory / 'launch.json'), start_new_session=True, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE)
    atomic_json(directory / 'spawn.json', {'pid': process.pid, 'nonce': original['nonce'],
        'launcher_spawned_at': utc_now(), 'launcher_spawned_monotonic': time.monotonic()})
    try:
        while not (directory / 'identity.json').exists():
            assert time.monotonic() < started + 2
            await asyncio.sleep(.002)
        os.kill(process.pid, signal.SIGSTOP)
        go = {**clock, 'nonce': original['nonce'], 'fencing_token': 1,
            'launcher_go_monotonic': time.monotonic(), 'launcher_go_at': utc_now()}
        assert go['launcher_go_monotonic'] < clock['startup_deadline_monotonic']
        if permit:
            atomic_json(directory / 'go.json', go)
        await asyncio.sleep(max(0, clock['startup_deadline_monotonic'] - time.monotonic()) + .02)
        os.kill(process.pid, signal.SIGCONT)
        _, error = await asyncio.wait_for(process.communicate(), 3)
        assert process.returncode == 0, error
    finally:
        if process.returncode is None:
            os.kill(process.pid, signal.SIGCONT)
            process.kill()
            await process.wait()
    identity = json.loads((directory / 'identity.json').read_text())
    receipt = json.loads((directory / 'result.json').read_text())
    assert receipt['child_started'] is False and receipt['startup_stop_reason'] == 'startup_deadline_expired'
    await patch(env, 'supervised_attempt', 'bad-attempt', **identity,
        **({key: go[key] for key in ('launcher_go_at', 'launcher_go_monotonic')} if permit else {}),
        state='cancelled', reason='launch_not_authorized', created_at=clock['startup_started_at'])
    return directory


@pytest.mark.parametrize('permit', [True, False])
async def test_versioned_deadline_expiration_before_child_is_proven_without_controller_cancel(env, permit):
    await expired_permit(env, permit=permit)
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert (await env.store.read('work_item', 'bad'))['runtime_failure_code'] == 'launcher_startup_timeout'
    assert not await env.store.list('model_invocation')


@pytest.mark.parametrize('damage', ['wrong_clock', 'wrong_boot', 'go_after_deadline', 'child_started',
    'missing_stop_reason', 'missing_version', 'owner_cancel', 'child_present', 'nonempty_log'])
async def test_versioned_deadline_expiration_rejects_partial_or_conflicting_proof(env, damage):
    directory = await expired_permit(env)
    path = directory / 'result.json'
    value = json.loads(path.read_text())
    if damage == 'wrong_clock':
        value['startup_deadline_monotonic'] += 1
    elif damage == 'wrong_boot':
        value['startup_boot_fingerprint'] = 'sha256:' + '0' * 64
    elif damage == 'go_after_deadline':
        path = directory / 'go.json'
        value = json.loads(path.read_text())
        value['launcher_go_monotonic'] = value['startup_deadline_monotonic'] + 1
    elif damage == 'child_started':
        value['child_started'] = True
    elif damage == 'missing_stop_reason':
        value.pop('startup_stop_reason')
    elif damage == 'missing_version':
        value.pop('startup_clock_version')
    elif damage == 'owner_cancel':
        atomic_json(directory / 'cancel.json', {'nonce': value['nonce'], 'reason': 'owner_cancel'})
    elif damage == 'child_present':
        atomic_json(directory / 'child.json', {})
    else:
        atomic_json(directory / 'stdout.jsonl', {'type': 'thread.started'})
    atomic_json(path, value)
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert (await env.store.read('work_item', 'bad'))['status'] == 'blocked'
    assert not await env.store.list('execution_reconciliation')


async def test_live_launcher_is_not_reclassified_and_never_signalled(env):
    directory = await startup_timeout(env)
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(30)', start_new_session=True)
    try:
        source, boot = current_boot_identity()
        identity = json.loads((directory / 'identity.json').read_text())
        identity.update(pid=process.pid, process_started_at=psutil.Process(process.pid).create_time(),
            boot_identity_source=source, boot_fingerprint=boot, **process_birth_identity(process.pid))
        atomic_json(directory / 'identity.json', identity)
        receipt = json.loads((directory / 'result.json').read_text())
        atomic_json(directory / 'result.json', {**receipt, **identity})
        await ExecutionReconciliation(env.store, env.workflow).reconcile()
        assert (await env.store.read('work_item', 'bad'))['status'] == 'blocked'
        assert process.returncode is None and not await env.store.list('execution_reconciliation')
    finally:
        process.terminate()
        await process.wait()


@pytest.mark.parametrize('late_cancel', [False, True])
async def test_versioned_self_expiry_accepts_empty_logs_and_later_timeout_revocation(env, late_cancel):
    directory = await expired_permit(env)
    for name in ('stdout.jsonl', 'stderr.log'):
        path = directory / name
        path.write_bytes(b'')
        path.chmod(0o600)
    if late_cancel:
        process = await env.store.read('supervised_attempt', 'bad-attempt')
        revoked_at = utc_now()
        atomic_json(directory / 'cancel.json', {'nonce': process['nonce'], 'reason': 'launcher_handshake_timeout',
            'revoked_at': revoked_at})
        await patch(env, 'supervised_attempt', 'bad-attempt', launch_authorization_revoked_reason='launcher_handshake_timeout',
            launch_authorization_revoked_at=revoked_at)
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert (await env.store.read('work_item', 'bad'))['runtime_failure_code'] == 'launcher_startup_timeout'


async def test_stopped_launcher_with_live_group_member_remains_blocked(env):
    directory = await startup_timeout(env)
    process = await asyncio.create_subprocess_exec(sys.executable, '-c',
        'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
        'print(p.pid,flush=True); time.sleep(30)', start_new_session=True, stdout=asyncio.subprocess.PIPE)
    child_pid = int(await process.stdout.readline())
    try:
        source, boot = current_boot_identity()
        identity = json.loads((directory / 'identity.json').read_text())
        identity.update(pid=process.pid, process_started_at=psutil.Process(process.pid).create_time(),
            boot_identity_source=source, boot_fingerprint=boot, **process_birth_identity(process.pid))
        atomic_json(directory / 'identity.json', identity)
        receipt = json.loads((directory / 'result.json').read_text())
        atomic_json(directory / 'result.json', {**receipt, **identity})
        process.terminate()
        # The inherited output pipe can stay open after its leader stops.
        for _ in range(100):
            if process.returncode is not None:
                break
            await asyncio.sleep(.01)
        assert process.returncode is not None
        await ExecutionReconciliation(env.store, env.workflow).reconcile()
        assert (await env.store.read('work_item', 'bad'))['status'] == 'blocked'
        assert psutil.Process(child_pid).is_running()
    finally:
        try:
            psutil.Process(child_pid).terminate()
        except psutil.NoSuchProcess:
            pass
        if process.returncode is None:
            process.terminate()
        await process.wait()


async def test_reused_native_birth_does_not_block_or_signal_unrelated_group(env):
    directory = await startup_timeout(env)
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(30)', start_new_session=True)
    try:
        source, boot = current_boot_identity()
        identity = json.loads((directory / 'identity.json').read_text())
        identity.update(pid=process.pid, process_started_at=psutil.Process(process.pid).create_time(),
            boot_identity_source=source, boot_fingerprint=boot, **process_birth_identity(process.pid))
        identity['process_birth_fingerprint'] = 'sha256:' + '0' * 64
        atomic_json(directory / 'identity.json', identity)
        receipt = json.loads((directory / 'result.json').read_text())
        atomic_json(directory / 'result.json', {**receipt, **identity})
        await ExecutionReconciliation(env.store, env.workflow).reconcile()
        assert (await env.store.read('work_item', 'bad'))['runtime_failure_code'] == 'launcher_startup_timeout'
        assert process.returncode is None
    finally:
        process.terminate()
        await process.wait()


async def test_versioned_proof_uses_monotonic_order_after_wall_clock_moves_backward(env):
    directory = await expired_permit(env)
    original = json.loads((directory / 'identity.json').read_text())
    earlier = (datetime.fromisoformat(original['startup_started_at']) - timedelta(minutes=2)).isoformat()
    identity = {**original, 'ready_at': earlier, 'launcher_ready_at': earlier}
    receipt = json.loads((directory / 'result.json').read_text())
    atomic_json(directory / 'identity.json', identity)
    atomic_json(directory / 'result.json', {**receipt, 'ready_at': earlier, 'launcher_ready_at': earlier,
        'finished_at': earlier, 'launcher_finished_at': earlier})
    await ExecutionReconciliation(env.store, env.workflow).reconcile()
    assert (await env.store.read('work_item', 'bad'))['runtime_failure_code'] == 'launcher_startup_timeout'

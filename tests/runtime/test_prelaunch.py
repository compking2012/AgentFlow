"""Bounded non-execution attestations; fixtures never start an Agent or model."""
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from agentflow.common import DomainError, canonical_digest
from agentflow.runtime.contracts import LaunchSpec
from agentflow.runtime.launcher import atomic_json
from agentflow.runtime.prelaunch import (
    prelaunch_failure_code,
    read_prelaunch_failure,
    record_prelaunch_failure,
)
from agentflow.runtime.sandbox import MacSeatbeltSandbox
from agentflow.runtime.supervisor import Supervisor


async def change(store, kind, identity, **fields):
    def write(tx):
        prior = tx.get(kind, identity)
        return tx.put(kind, identity, {**(prior or {}), **fields}, prior['revision'] if prior else None)
    return await store.command('fixture.change', str(uuid4()), {}, write)


async def seed(store, task, *, status='blocked', legacy=False):
    identity = {'run_id': task.run_id, 'iteration_id': task.iteration_id, 'work_item_id': task.work_item_id,
                'generation': 1, 'fencing_token': task.fencing_token, 'input_fingerprint': task.input_fingerprint}
    await change(store, 'run', task.run_id, execution_state='paused')
    await change(store, 'work_item', task.work_item_id, **{k: v for k, v in identity.items() if k != 'work_item_id'},
                 status=status, step='code_review', attempt_id=task.attempt_id)
    await change(store, 'attempt', task.attempt_id, **identity, status=status,
        started_at=(datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
        summary='Filesystem/network isolation probe failed; execution blocked' if legacy else 'Fixture preflight failure')
    payload = {**{k: v for k, v in identity.items() if k != 'generation'}, 'attempt_id': task.attempt_id,
        'step': 'code_review', 'role': 'review', 'workspace': str(task.workspace.resolve()),
        'output_schema': {'type': 'object'}, 'profile_id': 'fixture', 'goal': 'PRIVATE_PROMPT',
        'source_commit': 'a' * 40}
    await change(store, 'dispatch_context', task.attempt_id, task=payload)
    return {**payload, 'task_token': 'PRIVATE_TASK_TOKEN'}


async def legacy_probe(store, payload, *, suffix='', executable=None, **changes):
    root = store.data_dir / 'sandbox_profiles'
    sandbox = MacSeatbeltSandbox(root)
    name = canonical_digest(payload['attempt_id']).split(':')[1]
    home, artifact = (store.data_dir / folder / name for folder in ('openhands_homes', 'attempt_artifacts'))
    home.mkdir(parents=True, exist_ok=True)
    artifact.mkdir(parents=True, exist_ok=True)
    policy = sandbox.policy([Path(payload['workspace']), home, artifact], [home, artifact], [8787]) + suffix
    digest = canonical_digest({'policy': policy, 'executable': executable or str(Path(sys.executable).resolve())})
    path = root / (digest.split(':')[1] + '.sb')
    path.write_text(policy)
    path.chmod(0o600)
    attempt = await store.read('attempt', payload['attempt_id'])
    probe = {'verified': False, 'filesystem': 'hard', 'network': 'hard', 'process_tree': 'observed',
        'allowed_workspace_probe': -1, 'protected_read_probe': -1, 'protected_write_probe': 1,
        'allowed_port_probe': 0, 'denied_port_probe': 1, 'policy_fingerprint': digest,
        'checked_at': (datetime.fromisoformat(attempt['started_at']) + timedelta(seconds=2)).isoformat(), **changes}
    atomic_json(path.with_suffix('.probe.json'), probe)
    return path, probe


@pytest.mark.parametrize('phase,code,ui', [('sandbox_validation', 'isolation_unverified', 'isolation_unverified'),
    ('sandbox_validation', 'isolation_probe_timeout', 'isolation_probe_timeout'),
    ('sandbox_validation', 'unsafe_sandbox_roots', 'isolation_unverified'),
    ('sandbox_validation', 'coding_workspace_unwritable', 'coding_workspace_unwritable'),
    ('adapter_preflight', 'sdk_version_unverified', 'worker_environment_unavailable'),
    ('adapter_preflight', 'capability_unverified', 'worker_environment_unavailable')])
async def test_explicit_prelaunch_receipt_is_bound_private_and_does_not_mark_success(store, task, phase, code, ui):
    payload = await seed(store, task)
    before = {kind: await store.list(kind) for kind in ('attempt', 'work_item', 'model_invocation', 'budget_account')}
    receipt = await record_prelaunch_failure(store, store.data_dir, payload, phase=phase, failure_code=code)
    assert receipt['outcome'] == 'not_started' and receipt['actor'] == 'controller'
    assert receipt['fencing_token'] == task.fencing_token and receipt['input_fingerprint'] == task.input_fingerprint
    assert await record_prelaunch_failure(store, store.data_dir, payload, phase=phase, failure_code=code) == receipt
    assert len(await store.list('prelaunch_failure')) == 1
    proof = await read_prelaunch_failure(store, store.data_dir, task.attempt_id)
    assert proof['source'] == 'controller_preflight' and proof['failure_code'] == code
    assert await prelaunch_failure_code(store, store.data_dir, task.attempt_id) == ui
    assert 'PRIVATE_PROMPT' not in json.dumps(receipt) and 'PRIVATE_TASK_TOKEN' not in json.dumps(receipt)
    assert 'PRIVATE_PROMPT' not in json.dumps(proof)
    for kind, records in before.items():
        assert await store.list(kind) == records


async def test_runtime_can_seal_running_preflight_but_recovery_requires_blocked_status(store, task):
    payload = await seed(store, task, status='running')
    assert await record_prelaunch_failure(store, store.data_dir, payload, failure_code='isolation_unverified')
    assert await read_prelaunch_failure(store, store.data_dir, task.attempt_id) is None
    await change(store, 'attempt', task.attempt_id, status='blocked')
    assert await read_prelaunch_failure(store, store.data_dir, task.attempt_id)


@pytest.mark.parametrize('case', ['missing_receipt', 'unknown_attempt', 'wrong_context', 'wrong_fence',
    'wrong_work', 'restore_run', 'launch_intent', 'supervisor_directory', 'dangling_supervisor',
    'permit_without_record', 'settled_call', 'reserved_call', 'uncertain_call', 'orphan_call_count'])
async def test_absence_of_supervisor_alone_never_proves_no_execution(store, task, case):
    payload = await seed(store, task)
    if case != 'missing_receipt':
        assert await record_prelaunch_failure(store, store.data_dir, payload, failure_code='isolation_unverified')
    if case == 'unknown_attempt':
        await change(store, 'attempt', task.attempt_id, status='execution_unknown')
    elif case == 'wrong_context':
        await change(store, 'dispatch_context', task.attempt_id, task={**payload, 'work_item_id': 'other'})
    elif case in {'wrong_fence', 'wrong_work'}:
        await change(store, 'prelaunch_failure', task.attempt_id,
                     **({'fencing_token': 99} if case == 'wrong_fence' else {'work_item_id': 'other'}))
    elif case == 'restore_run':
        await change(store, 'run', task.run_id, restore_reconciliation_required=True)
    elif case == 'launch_intent':
        await change(store, 'supervised_attempt', task.attempt_id, state='launch_intent')
    elif case in {'supervisor_directory', 'dangling_supervisor', 'permit_without_record'}:
        folder = store.data_dir / 'supervisor' / canonical_digest({'attempt_id': task.attempt_id}).split(':')[1]
        folder.parent.mkdir(exist_ok=True)
        if case == 'dangling_supervisor':
            folder.symlink_to(store.data_dir / 'missing-target', target_is_directory=True)
        else:
            folder.mkdir()
            if case == 'permit_without_record':
                (folder / 'go.json').write_text('{}')
    elif case in {'settled_call', 'reserved_call', 'uncertain_call'}:
        await change(store, 'model_invocation', 'call', attempt_id=task.attempt_id, state=case.removesuffix('_call'))
    elif case == 'orphan_call_count':
        await change(store, 'model_attempt_budget', task.attempt_id, request_count=1, uncertain_invocations=0)
    assert await read_prelaunch_failure(store, store.data_dir, task.attempt_id) is None


@pytest.mark.parametrize('case', ['supervisor', 'directory', 'call', 'changed_owner', 'restored', 'unlisted_failure'])
async def test_controller_cannot_seal_uncertain_or_already_started_attempts(store, task, case):
    payload = await seed(store, task)
    code = 'isolation_unverified'
    if case == 'supervisor':
        await change(store, 'supervised_attempt', task.attempt_id, state='launch_intent')
    elif case == 'directory':
        (store.data_dir / 'supervisor' / canonical_digest({'attempt_id': task.attempt_id}).split(':')[1]).mkdir(parents=True)
    elif case == 'call':
        await change(store, 'model_invocation', 'call', attempt_id=task.attempt_id, state='released')
    elif case == 'changed_owner':
        await change(store, 'work_item', task.work_item_id, attempt_id='new-attempt')
    elif case == 'restored':
        await change(store, 'attempt', task.attempt_id, restore_reconciliation_required=True)
    else:
        code = 'some_unclassified_exception'
    assert await record_prelaunch_failure(store, store.data_dir, payload, failure_code=code) is None
    assert not await store.list('prelaunch_failure')


@pytest.mark.parametrize('version', [None, 1])
async def test_legacy_minus_one_does_not_distinguish_timeout_from_sighup_or_change_no_launch_proof(store, task, version):
    payload = await seed(store, task, legacy=True)
    path, _ = await legacy_probe(store, payload, **({'probe_version': version} if version is not None else {}))
    raw_probe = path.with_suffix('.probe.json').read_bytes()
    records = await store.list('attempt')
    proof = await read_prelaunch_failure(store, store.data_dir, task.attempt_id)
    assert proof['source'] == 'legacy_sandbox_probe' and proof['failure_code'] == 'isolation_unverified'
    assert proof['outcome'] == 'not_started' and proof['phase'] == 'sandbox_validation'
    assert await prelaunch_failure_code(store, store.data_dir, task.attempt_id) == 'isolation_unverified'
    assert not await store.list('prelaunch_failure') and await store.list('attempt') == records
    assert path.with_suffix('.probe.json').read_bytes() == raw_probe
    assert 'PRIVATE_PROMPT' not in json.dumps(proof)


async def test_v2_explicit_timeout_stays_specific_and_is_not_interpreted_as_legacy_exit_codes(store, task):
    payload = await seed(store, task, legacy=True)
    await legacy_probe(store, payload, probe_version=2, failure_code='isolation_probe_timeout',
                       probe_results={'protected_read_probe': {'exit_code': -1, 'outcome': 'timeout', 'timed_out': True}})
    assert await read_prelaunch_failure(store, store.data_dir, task.attempt_id) is None
    receipt = await record_prelaunch_failure(store, store.data_dir, payload,
        phase='sandbox_validation', failure_code='isolation_probe_timeout')
    assert receipt['outcome'] == 'not_started'
    proof = await read_prelaunch_failure(store, store.data_dir, task.attempt_id)
    assert proof['source'] == 'controller_preflight' and proof['failure_code'] == 'isolation_probe_timeout'
    assert await prelaunch_failure_code(store, store.data_dir, task.attempt_id) == 'isolation_probe_timeout'


@pytest.mark.parametrize('case', ['message_only', 'policy_changed', 'wrong_executable', 'verified_probe',
    'successful_codes', 'wrong_fingerprint', 'wrong_time', 'future_time', 'boolean_exit', 'unknown_version',
    'ambiguous_probes', 'wrong_workspace', 'symlink_probe', 'explicit_receipt_corrupt'])
async def test_legacy_fallback_requires_complete_exact_probe_binding(store, task, case):
    payload = await seed(store, task, legacy=True)
    if case != 'message_only':
        path, probe = await legacy_probe(store, payload,
            **({'executable': '/different/controller/python'} if case == 'wrong_executable' else {}))
        if case == 'policy_changed':
            path.write_text(path.read_text() + '\n; changed')
        elif case == 'verified_probe':
            probe['verified'] = True
        elif case == 'successful_codes':
            probe.update(allowed_workspace_probe=0, protected_read_probe=1, protected_write_probe=1)
        elif case == 'wrong_fingerprint':
            probe['policy_fingerprint'] = 'sha256:' + '0' * 64
        elif case == 'wrong_time':
            probe['checked_at'] = '2000-01-01T00:00:00Z'
        elif case == 'future_time':
            probe['checked_at'] = (datetime.now(UTC) + timedelta(seconds=20)).isoformat()
        elif case == 'boolean_exit':
            probe['protected_write_probe'] = True
        elif case == 'unknown_version':
            probe['probe_version'] = 99
        elif case == 'ambiguous_probes':
            await legacy_probe(store, payload, suffix='\n; second policy')
        elif case == 'wrong_workspace':
            other = task.workspace.parent / 'other-workspace'
            other.mkdir()
            await change(store, 'dispatch_context', task.attempt_id, task={**payload, 'workspace': str(other)})
        elif case == 'explicit_receipt_corrupt':
            await change(store, 'prelaunch_failure', task.attempt_id, outcome='not_started')
        if case == 'symlink_probe':
            probe_path = path.with_suffix('.probe.json')
            outside = store.data_dir / 'other-probe.json'
            probe_path.rename(outside)
            probe_path.symlink_to(outside)
        else:
            atomic_json(path.with_suffix('.probe.json'), probe)
    assert await read_prelaunch_failure(store, store.data_dir, task.attempt_id) is None


async def test_sealed_prelaunch_failure_prevents_late_supervisor_start(store, task, monkeypatch):
    payload = await seed(store, task)
    assert await record_prelaunch_failure(store, store.data_dir, payload, failure_code='isolation_unverified')
    async def forbidden(*args, **kwargs):
        raise AssertionError('No process can be created after not_started is sealed')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    supervisor = Supervisor(store, store.data_dir)
    launch = LaunchSpec(attempt_id=task.attempt_id, operation_id=task.attempt_id, run_id=task.run_id,
        input_fingerprint=task.input_fingerprint, fencing_token=task.fencing_token,
        argv=[sys.executable, '-c', 'pass'], cwd=task.workspace)
    with pytest.raises(DomainError) as error:
        await supervisor.start(launch)
    assert error.value.code == 'prelaunch_failure_sealed'
    assert await store.read('supervised_attempt', task.attempt_id) is None
    assert not supervisor._dir(task.attempt_id).exists()


async def test_seal_and_launch_intent_cannot_both_win_the_writer_transaction(store, task, monkeypatch):
    payload = await seed(store, task, status='running')
    async def no_process(*args, **kwargs):
        raise OSError('Fixture deliberately never starts a process')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', no_process)
    supervisor = Supervisor(store, store.data_dir)
    launch = LaunchSpec(attempt_id=task.attempt_id, operation_id=task.attempt_id, run_id=task.run_id,
        input_fingerprint=task.input_fingerprint, fencing_token=task.fencing_token,
        argv=[sys.executable, '-c', 'pass'], cwd=task.workspace)
    await asyncio.gather(record_prelaunch_failure(store, store.data_dir, payload, failure_code='isolation_unverified'),
                         supervisor.start(launch), return_exceptions=True)
    sealed = await store.read('prelaunch_failure', task.attempt_id)
    launched = await store.read('supervised_attempt', task.attempt_id)
    assert bool(sealed) != bool(launched)

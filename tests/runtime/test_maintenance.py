"""Scratch cleanup against temporary Stores and synthetic stopped-process receipts."""
import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import psutil
import pytest

from agentflow.common import canonical_digest
from agentflow.runtime.launcher import atomic_json
from agentflow.runtime.maintenance import TRASH, RuntimeMaintenance


async def patch(store, record_kind, identity, **fields):
    def write(tx):
        current = tx.get(record_kind, identity)
        return tx.put(record_kind, identity, {**(current or {}), **fields}, current['revision'] if current else None)
    return await store.command('fixture.patch', str(uuid4()), {}, write)


async def home_fixture(store, monkeypatch, folder='codex_homes', identity='attempt', status='completed'):
    monkeypatch.setattr(psutil, 'process_iter', lambda *args, **kwargs: [])
    home = store.data_dir / folder / canonical_digest(identity).split(':')[1]
    home.mkdir(parents=True, mode=0o700)
    (home / 'scratch.bin').write_bytes(b'x' * 8192)
    (home / 'config.toml').write_text('fixture execution configuration')
    (home / 'output_schema.json').write_text('{"type":"object"}')
    (home / 'conversation').mkdir()
    (home / 'conversation' / 'temporary.json').write_text('fixture scratch state')
    for name in ('workspaces', 'workspace_metadata', 'attempt_artifacts', 'artifacts', 'model_invocations', 'secrets'):
        protected = store.data_dir / name
        protected.mkdir(exist_ok=True)
        (protected / 'preserve.txt').write_text('retained result or code')
    outside = store.data_dir / 'workspaces/preserve.txt'
    (home / 'linked-code').symlink_to(outside)
    attempt = await patch(store, 'attempt', identity, run_id='run', iteration_id='iteration', work_item_id='work',
        status=status, generation=1, fencing_token=1, input_fingerprint='frozen-input')
    task = {k: attempt[k] for k in ('run_id', 'iteration_id', 'work_item_id', 'fencing_token', 'input_fingerprint')}
    await patch(store, 'dispatch_context', identity, task={**task, 'attempt_id': identity,
        'output_schema': {'type': 'object'}, 'workspace': str(store.data_dir / 'workspaces')})
    directory = store.data_dir / 'supervisor' / canonical_digest({'attempt_id': identity}).split(':')[1]
    directory.mkdir(parents=True, mode=0o700)
    process = {'attempt_id': identity, 'operation_id': identity, 'nonce': 'fixture-nonce', 'fencing_token': 1,
               'pid': 1073741000, 'process_started_at': 1.0, 'boot_fingerprint': canonical_digest({'boot_time': psutil.boot_time()})}
    receipt = {**process, 'execution_status': 'completed', 'exit_code': 0}
    child = {**process, 'child': {'pid': 1073741001, 'process_started_at': 1.0}}
    atomic_json(directory / 'result.json', receipt)
    atomic_json(directory / 'child.json', child)
    await patch(store, 'supervised_attempt', identity, **process, run_id='run', state='completed',
        backend='codex_exec' if folder == 'codex_homes' else 'openhands_role', directory=str(directory), input_fingerprint='frozen-input')
    await patch(store, 'model_invocation', 'call-' + identity, attempt_id=identity, run_id='run', iteration_id='iteration',
        fencing_token=1, input_fingerprint='frozen-input', state='completed_unpriced')
    await patch(store, 'model_attempt_budget', identity, request_count=8, uncertain_invocations=0)
    return SimpleNamespace(home=home, directory=directory, process=process, receipt=receipt, child=child, attempt=attempt)


@pytest.mark.parametrize('folder', ['codex_homes', 'openhands_homes'])
@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled', 'blocked'])
async def test_terminal_complete_home_cleanup_preserves_code_evidence_and_usage(store, monkeypatch, folder, status):
    env = await home_fixture(store, monkeypatch, folder=folder, status=status)
    before = {kind: await store.list(kind) for kind in ('attempt', 'supervised_attempt', 'dispatch_context',
        'model_invocation', 'model_attempt_budget', 'budget_account')}
    receipts = [p.read_bytes() for p in (env.directory / 'result.json', env.directory / 'child.json')]
    maintenance = RuntimeMaintenance(store, store.data_dir)
    result = await maintenance.sweep()
    assert result['cleaned'] == 1 and result['reclaimed_bytes'] >= 8192 and not env.home.exists()
    assert result['by_kind']['ephemeral_home']['cleaned'] == 1
    for name in ('workspaces', 'workspace_metadata', 'attempt_artifacts', 'artifacts', 'model_invocations', 'secrets'):
        assert (store.data_dir / name / 'preserve.txt').read_text() == 'retained result or code'
    assert [p.read_bytes() for p in (env.directory / 'result.json', env.directory / 'child.json')] == receipts
    for kind, rows in before.items():
        assert await store.list(kind) == rows
    assert (await store.list('runtime_maintenance'))[0]['state'] == 'completed'
    assert (await maintenance.sweep())['cleaned'] == 0


@pytest.mark.parametrize('kind,identity,fields', [
    ('attempt', 'attempt', {'status': 'running'}),
    ('attempt', 'attempt', {'status': 'execution_unknown'}),
    ('supervised_attempt', 'attempt', {'state': 'execution_unknown'}),
    ('supervised_attempt', 'attempt', {'state': 'running'}),
    ('supervised_attempt', 'attempt', {'fencing_token': 99}),
    ('supervised_attempt', 'attempt', {'backend': 'another_backend'}),
    ('model_invocation', 'call-attempt', {'state': 'reserved'}),
    ('model_invocation', 'call-attempt', {'state': 'dispatching'}),
    ('model_invocation', 'call-attempt', {'state': 'uncertain'}),
    ('model_invocation', 'call-attempt', {'fencing_token': 99}),
    ('model_attempt_budget', 'attempt', {'uncertain_invocations': 1}),
    ('dispatch_context', 'attempt', {'task': {}}),
])
async def test_home_requires_terminal_matching_identity_and_settled_calls(store, monkeypatch, kind, identity, fields):
    env = await home_fixture(store, monkeypatch)
    await patch(store, kind, identity, **fields)
    result = await RuntimeMaintenance(store, store.data_dir).sweep()
    assert result['cleaned'] == 0 and result['skipped'] and env.home.is_dir()
    assert not await store.list('runtime_maintenance')


@pytest.mark.parametrize('case', ['missing_result', 'missing_child', 'wrong_nonce', 'wrong_child', 'live_parent', 'live_child', 'live_group', 'symlink_receipt'])
async def test_process_and_child_receipts_must_confirm_stopped_processes(store, monkeypatch, case):
    env = await home_fixture(store, monkeypatch)
    if case == 'missing_result':
        (env.directory / 'result.json').unlink()
    elif case == 'missing_child':
        (env.directory / 'child.json').unlink()
    elif case == 'wrong_nonce':
        atomic_json(env.directory / 'result.json', {**env.receipt, 'nonce': 'wrong'})
    elif case == 'wrong_child':
        atomic_json(env.directory / 'child.json', {**env.child, 'fencing_token': 2})
    elif case == 'live_parent':
        identity = {'pid': os.getpid(), 'process_started_at': psutil.Process().create_time()}
        await patch(store, 'supervised_attempt', 'attempt', **identity)
        atomic_json(env.directory / 'result.json', {**env.receipt, **identity})
    elif case == 'live_child':
        atomic_json(env.directory / 'child.json', {**env.child,
            'child': {'pid': os.getpid(), 'process_started_at': psutil.Process().create_time()}})
    elif case == 'live_group':
        monkeypatch.setattr(psutil, 'process_iter', lambda *args, **kwargs: [SimpleNamespace(pid=1, info={'status': 'running'})])
        monkeypatch.setattr(os, 'getpgid', lambda _: env.process['pid'])
    else:
        outside = store.data_dir / 'elsewhere.json'
        (env.directory / 'result.json').rename(outside)
        (env.directory / 'result.json').symlink_to(outside)
    result = await RuntimeMaintenance(store, store.data_dir).sweep()
    assert not result['cleaned'] and env.home.exists()


async def test_source_symlinks_and_unmatched_homes_are_never_followed_or_removed(store, monkeypatch):
    env = await home_fixture(store, monkeypatch)
    outside = store.data_dir / 'valuable'
    env.home.rename(outside)
    env.home.symlink_to(outside, target_is_directory=True)
    unmatched = env.home.parent / ('f' * 64)
    unmatched.mkdir()
    (unmatched / 'keep').write_text('unknown owner')
    result = await RuntimeMaintenance(store, store.data_dir).sweep()
    assert not result['cleaned'] and env.home.is_symlink() and (outside / 'scratch.bin').exists()
    assert (unmatched / 'keep').read_text() == 'unknown owner'


async def test_state_change_after_prepare_leaves_source_in_place(store, monkeypatch):
    env = await home_fixture(store, monkeypatch)
    maintenance = RuntimeMaintenance(store, store.data_dir)
    original = maintenance._quarantine
    async def changed(record):
        await patch(store, 'model_invocation', 'call-attempt', state='dispatching')
        return await original(record)
    monkeypatch.setattr(maintenance, '_quarantine', changed)
    result = await maintenance.sweep()
    assert result['cleaned'] == 0 and result['pending'] == 1 and env.home.exists()


async def test_crash_after_rename_before_receipt_commit_can_resume_without_touching_new_home(store, monkeypatch):
    env = await home_fixture(store, monkeypatch)
    maintenance = RuntimeMaintenance(store, store.data_dir)
    original = os.fsync
    raised = False
    def fail_once(fd):
        nonlocal raised
        if not raised:
            raised = True
            raise OSError('fixture crash after rename')
        return original(fd)
    monkeypatch.setattr(os, 'fsync', fail_once)
    first = await maintenance.sweep()
    assert first['pending'] == 1 and not env.home.exists()
    record = (await store.list('runtime_maintenance'))[0]
    assert record['state'] == 'prepared' and (store.data_dir / TRASH / record['id']).is_dir()
    env.home.mkdir()
    (env.home / 'new-owner-data').write_text('preserve newly created directory')
    monkeypatch.setattr(os, 'fsync', original)
    result = await maintenance.sweep()
    assert result['cleaned'] == 1 and result['pending'] == 0
    assert (env.home / 'new-owner-data').read_text() == 'preserve newly created directory'


@pytest.mark.parametrize('failure', ['before_delete', 'after_delete'])
async def test_persisted_quarantine_resumes_after_crash_and_does_not_double_count(store, monkeypatch, failure):
    env = await home_fixture(store, monkeypatch)
    maintenance = RuntimeMaintenance(store, store.data_dir)
    if failure == 'before_delete':
        async def fail(_):
            raise RuntimeError('fixture crash')
        monkeypatch.setattr(maintenance, '_remove', fail)
    else:
        original = store.command
        async def fail(scope, *args, **kwargs):
            if scope == 'runtime.maintenance.complete':
                raise RuntimeError('fixture crash before completion receipt')
            return await original(scope, *args, **kwargs)
        monkeypatch.setattr(store, 'command', fail)
    with pytest.raises(RuntimeError):
        await maintenance.sweep()
    assert not env.home.exists()
    assert (await store.list('runtime_maintenance'))[0]['state'] == 'quarantined'
    monkeypatch.undo()
    next_service = RuntimeMaintenance(store, store.data_dir)
    result = await next_service.sweep()
    assert result['cleaned'] == 1 and result['pending'] == 0
    assert (await next_service.sweep())['cleaned'] == 0
    assert len([e for e in await store.events(0) if e['type'] == 'runtime.scratch_cleaned']) == 1


async def test_concurrent_services_share_one_sweep_and_only_count_cleanup_once(store, monkeypatch):
    await home_fixture(store, monkeypatch)
    services = [RuntimeMaintenance(store, store.data_dir) for _ in range(3)]
    results = await asyncio.gather(*(s.sweep() for s in services))
    assert sum(r['cleaned'] for r in results) == 1
    assert len(await store.list('runtime_maintenance')) == 1


def cache_fixture(store):
    cache = store.data_dir / 'package_cache' / 'npm'
    cache.mkdir(parents=True)
    (cache / 'package').write_bytes(b'x' * 8192)
    return cache


@pytest.mark.parametrize('maximum,removed', [(0, True), (1, True), (256 * 1024 * 1024, False)])
async def test_private_cache_threshold_and_zero_never_touch_user_cache_or_evidence(store, maximum, removed):
    cache = cache_fixture(store)
    unrelated = store.data_dir.parent / '.npm'
    unrelated.mkdir()
    (unrelated / 'keep').write_text('user cache')
    evidence = store.data_dir / 'nodes' / 'artifacts'
    evidence.mkdir(parents=True)
    (evidence / 'keep').write_text('retained execution evidence')
    result = await RuntimeMaintenance(store, store.data_dir, package_cache_max_bytes=maximum).sweep()
    assert (not cache.exists()) is removed and result['cleaned'] == int(removed)
    assert (unrelated / 'keep').read_text() == 'user cache'
    assert (evidence / 'keep').read_text() == 'retained execution evidence'


@pytest.mark.parametrize('kind,fields', [
    ('attempt', {'status': 'running'}), ('attempt', {'status': 'execution_unknown'}),
    ('supervised_attempt', {'state': 'launch_intent'}), ('supervised_attempt', {'state': 'execution_unknown'}),
    ('model_invocation', {'state': 'uncertain'}), ('node_job', {'state': 'queued'}),
    ('node_job', {'state': 'running'}), ('node_job', {'state': 'execution_unknown'}),
    ('node_resource', {'state': 'leased', 'current_lease_id': 'lease', 'owner_job_id': 'job'}),
    ('node_resource', {'state': 'available', 'current_lease_id': 'lease'}),
    ('node_resource', {'state': 'available', 'fencing_token': 1, 'last_cleanup_receipt_id': 'missing'}),
    ('product_launch', {'state': 'running'}), ('product_launch', {'state': 'execution_unknown'}),
    ('product_launch', {'state': 'stopped', 'attempt_id': 'missing-supervisor'}),
])
async def test_cache_requires_global_idle_released_resources_and_no_preview(store, kind, fields):
    cache = cache_fixture(store)
    await patch(store, kind, 'busy', **fields)
    result = await RuntimeMaintenance(store, store.data_dir, package_cache_max_bytes=0).sweep()
    assert result['cleaned'] == 0 and result['skipped'] and cache.exists()


async def test_released_node_resources_and_verified_cleanup_allow_idle_cache_eviction(store):
    cache = cache_fixture(store)
    await patch(store, 'node_resource', 'resource', state='available', current_lease_id=None, owner_job_id=None,
                fencing_token=1, last_cleanup_receipt_id='cleanup')
    await patch(store, 'node_cleanup_receipt', 'cleanup', resource_id='resource', fencing_token=1,
                alive_process_count=0, verified=True)
    result = await RuntimeMaintenance(store, store.data_dir, package_cache_max_bytes=0).sweep()
    assert result['cleaned'] == 1 and not cache.exists()


async def test_cache_directory_symlink_is_rejected_without_traversing_the_target(store):
    cache = cache_fixture(store)
    outside = store.data_dir.parent / 'outside-cache'
    cache.parent.rename(outside)
    cache.parent.symlink_to(outside, target_is_directory=True)
    result = await RuntimeMaintenance(store, store.data_dir, package_cache_max_bytes=0).sweep()
    assert result['cleaned'] == 0 and (outside / 'npm/package').exists()


@pytest.mark.parametrize('value', [-1, True, 1.5])
def test_invalid_cache_limit_cannot_change_cleanup_scope(store, value):
    with pytest.raises(ValueError):
        RuntimeMaintenance(store, store.data_dir, package_cache_max_bytes=value)


async def test_forged_trash_receipt_cannot_name_protected_directories(store):
    protected = store.data_dir / 'workspaces'
    protected.mkdir()
    (protected / 'keep').write_text('protected')
    await patch(store, 'runtime_maintenance', '../workspaces', kind='npm_cache', state='quarantined',
                source_relative='package_cache/npm', device=protected.stat().st_dev, inode=protected.stat().st_ino,
                allocated_bytes=10, files=1)
    result = await RuntimeMaintenance(store, store.data_dir, package_cache_max_bytes=0).sweep()
    assert not result['cleaned'] and (protected / 'keep').exists()

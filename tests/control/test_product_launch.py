"""Launch coordination fixtures; these tests never establish sandbox acceptance.

The fake sandbox and supervisor below isolate cancellation/order/unknown-state
behavior. Actual sandbox and executable acceptance use separate smoke tests.
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
import pytest_asyncio

from agentflow.common import DomainError, canonical_digest
from agentflow.control.product_launch import ProductLauncher
from agentflow.settings import Settings
from agentflow.storage import Store


@pytest_asyncio.fixture
async def launch_env(tmp_path):
    settings = Settings(data_dir=tmp_path / 'data')
    store = Store(settings.data_dir)
    await store.start()
    async def verify_product(_):
        pass
    sandbox = SimpleNamespace()
    async def prepare(*_, **__):
        return [], {'verified': True, 'fixture_scope': 'launch_coordination_only'}
    sandbox.prepare = prepare
    launcher = ProductLauncher(store, SimpleNamespace(verify_product=verify_product), settings, sandbox=sandbox)
    product = {'id': 'product', 'run_id': 'run', 'state': 'completed', 'output_directory': str(tmp_path / 'output'),
        'delivery': {'path': str(tmp_path / 'release'), 'manifest_digest': 'sha256:' + '1' * 64}}
    (tmp_path / 'release/product').mkdir(parents=True)
    try:
        yield SimpleNamespace(store=store, launcher=launcher, sandbox=sandbox, product=product)
    finally:
        await store.close()


async def test_stop_during_preparation_prevents_any_process_start(launch_env):
    env = launch_env
    entered, resume = asyncio.Event(), asyncio.Event()
    async def prepare(*_, **__):
        entered.set()
        await resume.wait()
        return [], {'verified': True, 'fixture_scope': 'coordination_only'}
    async def forbidden_start(_):
        pytest.fail('A stopped preparation must never start a process')
    env.sandbox.prepare = prepare
    env.launcher.supervisor.start = forbidden_start
    start = asyncio.create_task(env.launcher.launch(env.product))
    await asyncio.wait_for(entered.wait(), 3)
    stop = asyncio.create_task(env.launcher.stop('product'))
    await asyncio.sleep(0)
    assert 'product' in env.launcher._stop_requests
    resume.set()
    with pytest.raises(DomainError, match='启动已取消'):
        await start
    assert await stop == {'state': 'stopped', 'url': None}
    assert not await env.store.list('product_launch')
    assert not await env.store.list('supervised_attempt')


@pytest.mark.parametrize('cancel_state', ['cancelled', 'execution_unknown'])
async def test_stop_during_supervisor_start_keeps_actual_cleanup_state(launch_env, cancel_state):
    env = launch_env
    entered, resume = asyncio.Event(), asyncio.Event()
    calls = []
    async def start(spec):
        def intent(tx):
            return tx.put('supervised_attempt', spec.attempt_id, {'state': 'running'})
        await env.store.command('fixture.start', spec.attempt_id, {}, intent)
        entered.set()
        await resume.wait()
    async def cancel(identity):
        calls.append(identity)
        return SimpleNamespace(state=cancel_state)
    env.launcher.supervisor.start = start
    env.launcher.supervisor.cancel = cancel
    pending = asyncio.create_task(env.launcher.launch(env.product))
    await asyncio.wait_for(entered.wait(), 3)
    stopping = asyncio.create_task(env.launcher.stop('product'))
    await asyncio.sleep(0)
    resume.set()
    with pytest.raises(DomainError, match='启动已取消'):
        await pending
    result = await stopping
    assert result == {'state': 'execution_unknown' if cancel_state == 'execution_unknown' else 'stopped', 'url': None}
    assert calls and len(set(calls)) == 1
    assert (await env.store.read('product_launch', 'product'))['state'] == result['state']


async def test_missing_supervision_remains_unknown_and_cannot_be_restarted(launch_env):
    env = launch_env
    await env.store.command('fixture.intent', 'unknown', {}, lambda tx: tx.put('product_launch', 'product', {
        'product_id': 'product', 'attempt_id': 'missing', 'state': 'starting', 'url': None}))
    expected = {'state': 'execution_unknown', 'url': None}
    assert await env.launcher.status('product') == expected
    assert await env.launcher.stop('product') == expected
    with pytest.raises(DomainError, match='上一次产品启动结果未知'):
        await env.launcher.launch(env.product)
    assert not await env.store.list('supervised_attempt')


async def test_runtime_symlink_is_rejected_before_making_directories(launch_env, tmp_path):
    env = launch_env
    output = Path(env.product['output_directory'])
    output.mkdir()
    unrelated = tmp_path / 'user-data'
    unrelated.mkdir()
    (output / 'runtime').symlink_to(unrelated, target_is_directory=True)
    with pytest.raises(DomainError, match='符号链接'):
        await env.launcher.launch(env.product)
    assert list(unrelated.iterdir()) == []
    assert not await env.store.list('product_launch')


async def test_restored_launch_cannot_use_prebackup_receipt_to_claim_current_process_stopped(launch_env):
    env = launch_env
    def saved(tx):
        tx.put('supervised_attempt', 'old-attempt', {'state': 'completed'})
        return tx.put('product_launch', 'product', {'product_id': 'product', 'attempt_id': 'old-attempt',
            'state': 'execution_unknown', 'url': None, 'restore_reconciliation_required': True})
    await env.store.command('fixture.restore', 'launch', {}, saved)
    async def forbidden(_):
        pytest.fail('Old backup process evidence cannot establish the latest launcher identity')
    env.launcher.supervisor.inspect = forbidden
    env.launcher.supervisor.cancel = forbidden
    assert await env.launcher.status('product') == {'state': 'execution_unknown', 'url': None}
    assert await env.launcher.stop('product') == {'state': 'execution_unknown', 'url': None}
    with pytest.raises(DomainError, match='上一次产品启动结果未知'):
        await env.launcher.launch(env.product)


@pytest.mark.parametrize('condition', ['loopback', 'wildcard', 'wrong_port', 'reused_pid', 'wrong_nonce', 'child_wildcard'])
async def test_actual_process_listeners_must_match_private_identity_loopback_and_port(launch_env, condition):
    """Real kernel socket observations; this is not a sandbox acceptance claim."""
    env = launch_env
    child_script = 'import socket,time; s=socket.socket(); s.bind(("0.0.0.0",0)); s.listen(); print(s.getsockname()[1],flush=True); time.sleep(30)'
    script = ('import socket,time,json,sys,subprocess; s=socket.socket(); s.bind((sys.argv[1],0)); s.listen(); '
              'child=subprocess.Popen([sys.executable,"-u","-c",sys.argv[2]],stdout=subprocess.PIPE,text=True) if sys.argv[2] else None; '
              'child.stdout.readline() if child else None; print(json.dumps({"port":s.getsockname()[1],"child":child.pid if child else None}),flush=True); time.sleep(30)')
    process = subprocess.Popen([sys.executable, '-u', '-c', script, '0.0.0.0' if condition == 'wildcard' else '127.0.0.1',
                                child_script if condition == 'child_wildcard' else ''], stdout=subprocess.PIPE, text=True)
    spawned = None
    try:
        value = json.loads(await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 5))
        spawned = psutil.Process(value['child']) if value['child'] else None
        started = psutil.Process(process.pid).create_time()
        identity = {'pid': os.getpid(), 'process_started_at': psutil.Process().create_time(),
                    'nonce': 'private-protocol-fixture', 'fencing_token': 1,
                    'boot_fingerprint': canonical_digest({'boot_time': psutil.boot_time()}),
                    'child': {'pid': process.pid, 'process_started_at': started + (1 if condition == 'reused_pid' else 0)}}
        directory = env.launcher.supervisor._dir('listener-fixture')
        directory.mkdir()
        (directory / 'child.json').write_text(json.dumps(identity))
        record = {**identity, 'attempt_id': 'listener-fixture', 'state': 'running'}
        if condition == 'wrong_nonce':
            record['nonce'] = 'changed'
        await env.store.command('fixture.listener', condition, {}, lambda tx: tx.put('supervised_attempt', 'listener-fixture', record))
        port = value['port'] + (1 if condition == 'wrong_port' else 0)
        if condition == 'loopback':
            evidence = await env.launcher._verify_listeners('listener-fixture', port, process.pid)
            assert evidence['mode'] == 'observed'
            assert evidence['listeners'] == [{'pid': process.pid, 'host': '127.0.0.1', 'port': port}]
        else:
            with pytest.raises(DomainError, match='监听地址无法确认'):
                await env.launcher._verify_listeners('listener-fixture', port, process.pid)
    finally:
        if spawned:
            spawned.terminate()
        process.terminate()
        await asyncio.to_thread(process.wait, timeout=5)
        process.stdout.close()
        if spawned:
            for _ in range(100):
                if not spawned.is_running() or spawned.status() == psutil.STATUS_ZOMBIE:
                    break
                await asyncio.sleep(.01)
            assert not spawned.is_running() or spawned.status() == psutil.STATUS_ZOMBIE


@pytest.mark.parametrize('cleanup_state', ['cancelled', 'execution_unknown'])
async def test_periodic_listener_verification_revokes_url_and_stops_invalid_preview(launch_env, cleanup_state):
    env = launch_env
    def saved(tx):
        tx.put('supervised_attempt', 'attempt', {'attempt_id': 'attempt', 'state': 'running'})
        return tx.put('product_launch', 'product', {'product_id': 'product', 'attempt_id': 'attempt',
            'state': 'running', 'requested_port': 12345, 'url': 'http://127.0.0.1:12345'})
    await env.store.command('fixture.listener', 'running', {}, saved)
    cancelled = asyncio.Event()
    async def inspect(_):
        return SimpleNamespace(state='running')
    async def invalid(*_):
        raise DomainError('preview_listener_unverified', 'Injected observed external listener')
    async def cancel(_):
        cancelled.set()
        return SimpleNamespace(state=cleanup_state)
    env.launcher.supervisor.inspect = inspect
    env.launcher.supervisor.cancel = cancel
    env.launcher._verify_listeners = invalid
    await env.launcher.start()
    await asyncio.wait_for(cancelled.wait(), 3)
    for _ in range(100):
        row = await env.store.read('product_launch', 'product')
        if row['state'] == ('stopped' if cleanup_state == 'cancelled' else 'execution_unknown'):
            break
        await asyncio.sleep(.01)
    assert row['url'] is None and row['blocking_reason'] == 'preview_listener_unverified'
    assert row['state'] == ('stopped' if cleanup_state == 'cancelled' else 'execution_unknown')
    await env.launcher.close()

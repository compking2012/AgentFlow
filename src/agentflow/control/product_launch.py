"""Supervised, isolated loopback previews of verified product releases."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
from pathlib import Path
from uuid import uuid4

import httpx
import psutil

from agentflow.common import DomainError, utc_now
from agentflow.runtime.contracts import LaunchSpec
from agentflow.runtime.supervisor import Supervisor

logger = logging.getLogger(__name__)


class ProductLauncher:
    def __init__(self, store, exporter, settings, *, protected_ports=(), sandbox=None):
        self.store, self.exporter, self.settings = store, exporter, settings
        self.supervisor = Supervisor(store, settings.data_dir)
        self.protected_ports = tuple({settings.port, *protected_ports})
        self.sandbox = sandbox
        self._locks = {}
        self._stop_requests = set()
        self._observed_children = {}
        self._monitor_task = None

    async def start(self):
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor(), name='product-preview-listeners')

    async def _monitor(self):
        while True:
            try:
                for row in await self.store.list('product_launch'):
                    if row['state'] == 'running':
                        await self.status(row['product_id'])
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Product preview listener verification needs attention')
            await asyncio.sleep(2)

    async def _verify_listeners(self, attempt_id, port, expected_pid=None):
        record = await self.store.read('supervised_attempt', attempt_id)
        if not record or record.get('restore_reconciliation_required'):
            raise DomainError('preview_listener_unverified', '无法核验产品监听进程的持久身份')
        return await asyncio.to_thread(self._inspect_listeners, record, port, expected_pid)

    def _inspect_listeners(self, record, port, expected_pid=None):
        try:
            path = self.supervisor._dir(record['attempt_id']) / 'child.json'
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
                raise ValueError('Missing private child identity')
            identity = json.loads(path.read_text())
            if (identity.get('nonce') != record['nonce'] or identity.get('fencing_token') != record['fencing_token']
                    or identity.get('pid') != record['pid'] or not self.supervisor._alive(record)):
                raise ValueError('Launcher identity mismatch')
            child = identity['child']
            process = psutil.Process(child['pid'])
            if (type(port) is not int or port in self.protected_ports
                    or (expected_pid is not None and child['pid'] != expected_pid)
                    or abs(process.create_time() - child['process_started_at']) >= .01
                    or process.ppid() != record['pid']):
                raise ValueError('Product identity mismatch')
            known = self._observed_children.setdefault(record['attempt_id'], {})
            for item in [process, *process.children(recursive=True)]:
                try:
                    known[item.pid] = item.create_time()
                except psutil.NoSuchProcess:
                    continue
            listeners = []
            for pid, started in list(known.items()):
                try:
                    item = psutil.Process(pid)
                    if abs(item.create_time() - started) >= .01:
                        continue
                    for connection in item.net_connections(kind='inet'):
                        if connection.status != psutil.CONN_LISTEN:
                            continue
                        address = connection.laddr
                        if address.ip not in {'127.0.0.1', '::1'} or address.port != port:
                            raise ValueError('Product listens outside its declared loopback endpoint')
                        listeners.append({'pid': pid, 'host': address.ip, 'port': address.port})
                except psutil.NoSuchProcess:
                    continue
            if not listeners:
                raise ValueError('No matching process listener was observed')
            return {'mode': 'observed', 'checked_at': utc_now(), 'listeners': listeners}
        except (OSError, ValueError, KeyError, TypeError, psutil.Error) as exc:
            raise DomainError('preview_listener_unverified', '产品监听地址无法确认只对本机开放，预览不可用') from exc

    async def launch(self, product):
        async with self._locks.setdefault(product['id'], asyncio.Lock()):
            self._stop_requests.discard(product['id'])
            if product['state'] != 'completed' or not product.get('delivery'):
                raise DomainError('product_not_delivered', '产品尚未完成测试和交付，无法启动')
            current = await self.status(product['id'])
            if current['state'] == 'running':
                running = await self.store.read('product_launch', product['id'])
                if not running or running.get('delivery_fingerprint') != product['delivery'].get('manifest_digest'):
                    raise DomainError('preview_version_mismatch', '当前预览属于其他交付版本，请先停止后再启动当前版本。')
                return current
            if current['state'] == 'execution_unknown':
                raise DomainError('preview_unknown', '上一次产品启动结果未知，请先停止并确认原进程')
            await self.exporter.verify_product(product)
            executable = shutil.which('node')
            if not executable:
                raise DomainError('node_missing', '请安装 Node.js 22.13 或以上')
            runtime = Path(product['output_directory']) / 'runtime'
            if runtime.is_symlink() or runtime.resolve() != runtime:
                raise DomainError('preview_runtime_changed', '产品运行数据目录被替换为符号链接')
            for name in ('data', 'home', 'tmp'):
                directory = runtime / name
                if directory.is_symlink() or directory.resolve() != directory:
                    raise DomainError('preview_runtime_changed', '产品运行数据目录被替换为符号链接')
                directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            identity = str(uuid4())
            ready = runtime / ('ready-' + identity + '.json')
            release = Path(product['delivery']['path']) / 'product'
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                requested_port = listener.getsockname()[1]
            if self.sandbox is None:
                from node_agent.local_isolation import LocalExecutionSandbox
                self.sandbox = LocalExecutionSandbox(self.settings.data_dir, protected_ports=self.protected_ports)
            prefix, evidence = await self.sandbox.prepare(Path(executable).resolve(), read_roots=[release],
                write_roots=[runtime], evidence_dir=runtime / 'isolation', allowed_loopback_ports=[requested_port])
            if not evidence.get('verified'):
                raise DomainError('preview_isolation_unverified', '产品预览隔离验证未通过')
            if product['id'] in self._stop_requests:
                raise DomainError('product_start_cancelled', '产品启动已取消')
            def intent(tx):
                old = tx.get('product_launch', product['id'])
                return tx.put('product_launch', product['id'], {'product_id': product['id'], 'attempt_id': identity,
                    'state': 'starting', 'ready_path': str(ready), 'requested_port': requested_port, 'url': None,
                    'delivery_fingerprint': product['delivery']['manifest_digest']}, old['revision'] if old else None)
            await self.store.command('product.launch', identity, {'product_id': product['id']}, intent)
            spec = LaunchSpec(attempt_id=identity, operation_id=identity, run_id=product['run_id'],
                input_fingerprint=product['delivery']['manifest_digest'], fencing_token=1,
                argv=prefix + [str(Path(executable).resolve()), str(release / 'server.mjs')], cwd=release,
                environment={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(runtime / 'home'),
                    'TMPDIR': str(runtime / 'tmp'), 'HOST': '127.0.0.1', 'PORT': str(requested_port),
                    'AGENTFLOW_DATA_DIR': str(runtime / 'data'), 'AGENTFLOW_READY_FILE': str(ready)},
                timeout_seconds=86400, backend='product_preview', backend_version='node-web-api-v1')
            try:
                await self.supervisor.start(spec)
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=2) as client:
                    for _ in range(300):
                        if product['id'] in self._stop_requests:
                            raise DomainError('product_start_cancelled', '产品启动已取消')
                        handle = await self.supervisor.inspect(identity)
                        if handle.state in {'failed', 'cancelled', 'completed', 'execution_unknown'}:
                            raise DomainError('product_start_failed', '产品进程未成功启动，请查看运行日志')
                        if ready.is_file() and not ready.is_symlink() and ready.stat().st_size < 4096:
                            observed = json.loads(ready.read_text())
                            port = observed.get('port')
                            if (observed.get('host') != '127.0.0.1' or type(port) is not int
                                    or port != requested_port or port in self.protected_ports
                                    or observed.get('url') != f'http://127.0.0.1:{port}'):
                                raise DomainError('invalid_product_origin', '产品必须使用独立的本机端口')
                            child_path = self.supervisor._dir(identity) / 'child.json'
                            child = json.loads(child_path.read_text()) if child_path.exists() else {}
                            if observed.get('pid') != child.get('child', {}).get('pid'):
                                raise DomainError('invalid_product_identity', '产品就绪记录不属于本次受管进程')
                            listener_evidence = await self._verify_listeners(identity, port, observed['pid'])
                            response = await client.get(observed['url'] + '/health')
                            if response.status_code == 200:
                                listener_evidence = await self._verify_listeners(identity, port, observed['pid'])
                                return await self._state(product['id'], 'running', observed['url'], listener_evidence=listener_evidence)
                        await asyncio.sleep(.1)
                raise DomainError('product_start_timeout', '产品没有在规定时间内完成启动')
            except BaseException:
                supervised = await asyncio.shield(self.store.read('supervised_attempt', identity))
                cleanup = await asyncio.shield(self.supervisor.cancel(identity)) if supervised else None
                await asyncio.shield(self._state(product['id'],
                    'execution_unknown' if cleanup and cleanup.state == 'execution_unknown' else 'stopped', None))
                raise
            finally:
                self._stop_requests.discard(product['id'])

    async def _state(self, product_id, state, url, *, expected_attempt_id=None, **changes):
        def update(tx):
            row = tx.get('product_launch', product_id)
            if expected_attempt_id is not None and row['attempt_id'] != expected_attempt_id:
                return row
            result = tx.put('product_launch', product_id, {**row, **changes, 'state': state, 'url': url}, row['revision'])
            tx.event('product.preview_' + state, {'product_id': product_id})
            return result
        row = await self.store.command('product.preview', str(uuid4()), {'product_id': product_id, 'state': state}, update)
        return self._public_state(row['state'], row['url'], row)

    @staticmethod
    def _public_state(state, url, row):
        result = {'state': state, 'url': url}
        if row.get('blocking_reason') == 'preview_listener_unverified':
            result['detail'] = '检测到产品监听地址不符合本机预览要求，或无法核验其身份；预览地址已撤下。'
        return result

    async def status(self, product_id):
        row = await self.store.read('product_launch', product_id)
        if not row:
            return {'state': 'stopped', 'url': None}
        if row.get('restore_reconciliation_required'):
            return {'state': 'execution_unknown', 'url': None}
        if not await self.store.read('supervised_attempt', row['attempt_id']):
            return self._public_state('stopped' if row['state'] == 'stopped' else 'execution_unknown', None, row)
        handle = await self.supervisor.inspect(row['attempt_id'])
        if handle.state == 'running' and row['state'] == 'running':
            try:
                await self._verify_listeners(row['attempt_id'], row.get('requested_port'))
            except DomainError:
                await self._state(product_id, 'execution_unknown', None, expected_attempt_id=row['attempt_id'],
                                  blocking_reason='preview_listener_unverified')
                cleanup = await self.supervisor.cancel(row['attempt_id'])
                return await self._state(product_id, 'execution_unknown' if cleanup.state == 'execution_unknown' else 'stopped',
                                         None, expected_attempt_id=row['attempt_id'])
            return {'state': 'running', 'url': row['url']}
        if handle.state in {'launch_intent', 'execution_unknown', 'running'}:
            return self._public_state('execution_unknown', None, row)
        return self._public_state('stopped', None, row)

    async def stop(self, product_id):
        self._stop_requests.add(product_id)
        try:
            async with self._locks.setdefault(product_id, asyncio.Lock()):
                row = await self.store.read('product_launch', product_id)
                if row:
                    if row.get('restore_reconciliation_required'):
                        return await self._state(product_id, 'execution_unknown', None)
                    if not await self.store.read('supervised_attempt', row['attempt_id']):
                        return await self._state(product_id,
                            'stopped' if row['state'] == 'stopped' else 'execution_unknown', None)
                    result = await self.supervisor.cancel(row['attempt_id'])
                    if result.state == 'execution_unknown':
                        return await self._state(product_id, 'execution_unknown', None)
                    return await self._state(product_id, 'stopped', None)
                return {'state': 'stopped', 'url': None}
        finally:
            self._stop_requests.discard(product_id)

    async def close(self):
        if self._monitor_task:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None
        for row in await self.store.list('product_launch'):
            if row['state'] in {'starting', 'running'}:
                await self.stop(row['product_id'])
        await self.supervisor.close()

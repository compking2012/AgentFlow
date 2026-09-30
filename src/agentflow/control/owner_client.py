"""CLI uses the same authenticated owner commands as the browser."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx

from agentflow.common import DomainError
from agentflow.control.instance import active_data_dir
from agentflow.control.owner_ipc import launch_url


class OwnerClient:
    def __init__(self, origin, token):
        self.origin = origin
        self.client = httpx.AsyncClient(base_url=origin, trust_env=False, follow_redirects=False,
            headers={'Authorization': 'Bearer ' + token, 'Origin': origin}, timeout=60)

    @classmethod
    async def connect(cls, settings, *, start=True, discover_active=False):
        directory = (active_data_dir() if discover_active else None) or settings.data_dir
        try:
            url = await launch_url(directory)
        except DomainError as exc:
            if not start or exc.code != 'controller_unavailable':
                raise
            directory = settings.data_dir
            settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            log_path = settings.data_dir / 'controller.log'
            if log_path.is_symlink():
                raise DomainError('unsafe_log', 'Controller log cannot be a symlink')
            with log_path.open('ab') as log:
                log_path.chmod(0o600)
                args = [sys.executable, '-m', 'agentflow.server']
                process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=log, start_new_session=True, close_fds=True, env=dict(os.environ))
            timeout = settings.controller_startup_timeout_seconds
            deadline = monotonic() + timeout if timeout else None
            while True:
                remaining = deadline - monotonic() if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    raise DomainError('controller_start_timeout',
                        f'已达到配置的控制器启动等待时间；原进程可能仍在启动，请稍后查询状态或查看 {log_path}')
                try:
                    url = (await launch_url(settings.data_dir) if remaining is None else
                           await asyncio.wait_for(launch_url(settings.data_dir), remaining))
                    break
                except (DomainError, TimeoutError) as wait_error:
                    if isinstance(wait_error, DomainError) and wait_error.code != 'controller_unavailable':
                        raise
                    if process.poll() is not None:
                        raise DomainError('controller_start_failed', f'平台启动失败，请查看 {log_path}') from None
                    remaining = deadline - monotonic() if deadline is not None else None
                    if remaining is not None and remaining <= 0:
                        raise DomainError('controller_start_timeout',
                            f'已达到配置的控制器启动等待时间；原进程可能仍在启动，请稍后查询状态或查看 {log_path}') from None
                    await asyncio.sleep(min(.1, remaining) if remaining is not None else .1)
        parts = urlsplit(url)
        origin = f'{parts.scheme}://{parts.netloc}'
        code = parse_qs(parts.fragment)['bootstrap'][0]
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=15) as http:
            for _ in range(100):
                try:
                    response = await http.post(origin + '/api/v1/session', json={'bootstrap_token': code},
                        headers={'Origin': origin, 'Idempotency-Key': str(uuid4())})
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(.1)
                except httpx.RequestError:
                    raise DomainError('owner_session_interrupted', '本机会话连接中断，请重新连接工作台。', 503) from None
            else:
                raise DomainError('owner_listener_unavailable', 'The owner listener did not become ready')
            if response.status_code != 201:
                raise DomainError('owner_session_failed', 'Unable to open a local owner session')
            try:
                token = response.json()['owner_token']
                if not isinstance(token, str) or not token:
                    raise ValueError('Missing owner token')
            except (ValueError, KeyError, TypeError):
                raise DomainError('owner_session_failed', '本机服务未返回有效会话。', 502) from None
            client = cls(origin, token)
            client.data_dir = directory
            return client

    async def request(self, method, path, payload=None, *, key=None):
        method = method.upper()
        url = self.client.base_url.join(path)
        if (not path.startswith('/api/v1/') or '\\' in path or any(ord(char) < 32 for char in path)
                or url.scheme != self.client.base_url.scheme or url.host != self.client.base_url.host
                or url.port != self.client.base_url.port or not url.path.startswith('/api/v1/') or url.fragment):
            raise DomainError('invalid_owner_path', 'Owner requests must use a local API path')
        attempts = 3 if method == 'GET' else 1
        for index in range(attempts):
            try:
                response = await self.client.request(method, path, json=payload,
                    headers={'Idempotency-Key': key or str(uuid4())} if method != 'GET' else {},
                    timeout=httpx.Timeout(300 if path.endswith('/launch') else 60, connect=10))
                break
            except httpx.RequestError:
                if index + 1 < attempts:
                    await asyncio.sleep(.1 * (index + 1))
                    continue
                raise DomainError('owner_connection_interrupted',
                    '本机连接中断，命令可能已被接受。请先运行 agentflow status 查看状态；创建产品可再次运行同一命令核对原请求。', 503) from None
        if not response.is_success:
            try:
                value = response.json()['error']
            except (ValueError, KeyError, TypeError):
                raise DomainError('owner_request_failed', 'Local platform request failed', response.status_code) from None
            raise DomainError(value['code'], value['message'], response.status_code, value.get('details'))
        try:
            return response.json()
        except ValueError:
            raise DomainError('owner_response_invalid', '本机服务响应无法读取。请先查看状态再决定是否重试。', 502) from None

    async def close(self):
        await self.client.aclose()

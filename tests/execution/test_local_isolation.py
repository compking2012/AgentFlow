"""Real macOS boundary checks; no assumed sandbox or arbitrary HTTPS fallback."""
import asyncio
import json
import platform
import socket
import sys
from pathlib import Path

import pytest

from agentflow.common import DomainError
from agentflow.execution.models import JobLimits
from agentflow.execution.process import CommandSpec, child_environment
from node_agent.local_isolation import LocalExecutionSandbox, LocalIsolatedExecutor, _RegistryTunnel


def test_private_npm_cache_does_not_copy_the_owners_downloads(tmp_path, monkeypatch):
    user_home = tmp_path / 'home'
    user_cache = user_home / '.npm/_cacache'
    user_cache.mkdir(parents=True)
    (user_cache / 'unrelated-download').write_text('belongs to another project')
    monkeypatch.setenv('HOME', str(user_home))
    destination = tmp_path / 'control/package_cache/npm'
    executor = LocalIsolatedExecutor.__new__(LocalIsolatedExecutor)
    executor._seed_cache(destination)
    assert destination.is_dir() and list(destination.iterdir()) == []
    assert (user_cache / 'unrelated-download').read_text() == 'belongs to another project'


def test_job_ports_skip_protected_and_previously_assigned_candidates(tmp_path, monkeypatch):
    reservations = [socket.socket() for _ in range(5)]
    try:
        for listener in reservations:
            listener.bind(('127.0.0.1', 0))
        protected, first, second, third, fourth = [listener.getsockname()[1] for listener in reservations]
    finally:
        for listener in reservations:
            listener.close()
    candidates = iter([protected, first, first, second, first, second, third, third, fourth])
    real_socket = socket.socket

    class CandidateSocket(real_socket):
        def bind(self, address):
            assert address == ('127.0.0.1', 0)
            return super().bind(('127.0.0.1', next(candidates)))

    monkeypatch.setattr(socket, 'socket', CandidateSocket)
    executor = LocalIsolatedExecutor(tmp_path / 'control', protected_ports=[protected])
    assert executor._ports_for_job('first') == (first, second)
    assert executor._ports_for_job('first') == (first, second)
    assert executor._ports_for_job('second') == (third, fourth)


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Measured macOS isolation requires macOS')
async def test_local_executor_allows_two_stable_job_ports_and_denies_other_ports(tmp_path):
    listeners = [socket.socket(), socket.socket()]
    for listener in listeners:
        listener.bind(('127.0.0.1', 0))
        listener.listen(4)
    protected, unrelated = [listener.getsockname()[1] for listener in listeners]
    executor = LocalIsolatedExecutor(tmp_path / 'control', protected_ports=[protected])
    recorded = []
    try:
        for job, command_index in [('first', 0), ('first', 1), ('second', 0)]:
            workspace = tmp_path / job / 'workspace'
            workspace.mkdir(parents=True, exist_ok=True)
            denied = [protected, unrelated, *(recorded[0] if job == 'second' else [])]
            script = f'''
import json, os, socket
main = int(os.environ['AGENTFLOW_TEST_PORT'])
secondary = int(os.environ['AGENTFLOW_TEST_SECONDARY_PORT'])
assert os.environ['AGENTFLOW_WEB_PORT'] == str(main)
assert main != secondary
servers = []
for port in (main, secondary):
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', port))
    server.listen(1)
    servers.append(server)
for port, server in zip((main, secondary), servers):
    with socket.create_connection(('127.0.0.1', port), timeout=1) as client:
        client.sendall(b'job port')
        connection, _ = server.accept()
        with connection:
            assert connection.recv(8) == b'job port'
for port in {denied!r}:
    try:
        socket.create_connection(('127.0.0.1', port), timeout=1)
        raise AssertionError(f'unauthorized connection allowed: {{port}}')
    except PermissionError:
        pass
    with socket.socket() as rejected:
        try:
            rejected.bind(('127.0.0.1', port))
            raise AssertionError(f'unauthorized bind allowed: {{port}}')
        except PermissionError:
            pass
with socket.socket() as rejected:
    try:
        rejected.bind(('127.0.0.1', 0))
        raise AssertionError('uncontrolled ephemeral bind allowed')
    except PermissionError:
        pass
for server in servers:
    server.close()
print(json.dumps([main, secondary]))
'''
            command = CommandSpec(argv=(sys.executable, '-c', script), cwd=workspace,
                label='two isolated test services', environment={
                    'AGENTFLOW_TEST_PORT': str(unrelated), 'AGENTFLOW_WEB_PORT': str(unrelated),
                    'AGENTFLOW_TEST_SECONDARY_PORT': str(unrelated)})
            output = tmp_path / job / 'evidence' / f'command-{command_index}'
            result = await executor.execute(command, output, JobLimits(maximum_active_seconds=10))
            assert result.execution_status == 'completed', Path(result.stderr_path).read_text()
            assert result.cleanup_verified
            ports = json.loads(Path(result.stdout_path).read_text())
            evidence = json.loads((output / 'isolation.json').read_text())
            assert evidence['verified'] and evidence['allowed_loopback_ports'] == sorted(ports)
            recorded.append(ports)
        assert recorded[0] == recorded[1]
        assert set(recorded[0]).isdisjoint(recorded[2])
    finally:
        await executor.sandbox.close()
        for listener in listeners:
            listener.close()


@pytest.mark.skipif(platform.system() != "Darwin", reason="Measured macOS isolation requires macOS")
async def test_real_product_sandbox_preserves_runtime_writes_and_denies_controller_and_public_access(tmp_path):
    data, product, runtime = tmp_path / "control", tmp_path / "product", tmp_path / "runtime"
    (data / "state").mkdir(parents=True)
    (data / "state/secret").write_text("controller secret must stay outside product")
    product.mkdir()
    (product / "config.json").write_text('{"product":true}')
    listeners = []
    for _ in range(3):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        listeners.append(listener)
    allowed, forbidden, bind_port = [listener.getsockname()[1] for listener in listeners]
    listeners[2].close()
    sandbox = LocalExecutionSandbox(data, protected_ports=[forbidden])
    try:
        prefix, evidence = await sandbox.prepare(Path(sys.executable), read_roots=[product], write_roots=[runtime],
            evidence_dir=runtime / "isolation", allowed_loopback_ports=[allowed, bind_port])
        script = f"""
import json,socket
from pathlib import Path
assert json.loads(Path({str(product / 'config.json')!r}).read_text())['product']
Path({str(runtime / 'data.json')!r}).write_text('runtime data')
try: Path({str(data / 'state/secret')!r}).read_text();raise AssertionError('controller read allowed')
except PermissionError: pass
try: Path({str(data / 'state/secret')!r}).write_text('changed');raise AssertionError('controller write allowed')
except PermissionError: pass
s=socket.create_connection(('127.0.0.1',{allowed}),timeout=1);s.close()
try: s=socket.create_connection(('127.0.0.1',{forbidden}),timeout=1);raise AssertionError('owner connection allowed')
except PermissionError: pass
s=socket.socket();s.bind(('127.0.0.1',{bind_port}));s.listen(1);s.close()
print('protected')
"""
        process = await asyncio.create_subprocess_exec(*prefix, sys.executable, "-c", script,
            cwd=product, env=child_environment(runtime / "home"), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
        assert process.returncode == 0, stderr.decode()
        assert stdout.strip() == b"protected" and evidence["verified"]
        assert (runtime / "data.json").read_text() == "runtime data"
        assert (data / "state/secret").read_text() == "controller secret must stay outside product"
        assert evidence["listen_address_enforcement"] == "caller_must_monitor"
    finally:
        await sandbox.close()
        for listener in listeners:
            listener.close()


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS scope validation")
async def test_protected_scope_and_controller_port_cannot_be_granted(tmp_path):
    sandbox = LocalExecutionSandbox(tmp_path / "data", protected_ports=[8787])
    with pytest.raises(DomainError, match="controller state"):
        await sandbox.prepare(Path(sys.executable), read_roots=[tmp_path / "data"], write_roots=[tmp_path / "runtime"],
                              evidence_dir=tmp_path / "evidence")
    with pytest.raises(DomainError, match="separate from controller"):
        await sandbox.prepare(Path(sys.executable), read_roots=[tmp_path / "product"], write_roots=[tmp_path / "runtime"],
                              evidence_dir=tmp_path / "evidence", allowed_loopback_ports=[8787])


async def test_package_proxy_rejects_other_hosts_and_non_connect_requests():
    proxy = await _RegistryTunnel([]).start()
    try:
        for first_line in [b"CONNECT example.org:443 HTTP/1.1", b"CONNECT registry.npmjs.org:80 HTTP/1.1",
                           b"CONNECT 127.0.0.1:443 HTTP/1.1", b"GET http://registry.npmjs.org/ HTTP/1.1"]:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(first_line + b"\r\nHost: registry.npmjs.org\r\n\r\n")
            await writer.drain()
            assert (await reader.read()).startswith(b"HTTP/1.1 403")
            writer.close()
            await writer.wait_closed()
    finally:
        await proxy.close()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", proxy.port)


async def test_package_proxy_closes_a_connected_upstream_that_stops_responding(monkeypatch):
    """A CONNECT success must not let a stalled TLS handshake consume the job's full budget."""
    received = asyncio.Event()
    finished = asyncio.Event()
    async def blackhole(reader, writer):
        try:
            assert await reader.readexactly(5) == b'hello'
            received.set()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()
    upstream = await asyncio.start_server(blackhole, '127.0.0.1', 0)
    connect = asyncio.open_connection
    async def local_upstream(host, port, **kwargs):
        return await connect(host, upstream.sockets[0].getsockname()[1] if port == 443 else port, **kwargs)
    monkeypatch.setattr(asyncio, 'open_connection', local_upstream)
    proxy = _RegistryTunnel(['127.0.0.1'])
    proxy.idle_timeout_seconds = .05
    await proxy.start()
    writer = None
    try:
        reader, writer = await connect('127.0.0.1', proxy.port)
        writer.write(b'CONNECT registry.npmjs.org:443 HTTP/1.1\r\nHost: registry.npmjs.org\r\n\r\n')
        await writer.drain()
        assert b'200' in await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), .5)
        writer.write(b'hello')
        await writer.drain()
        await asyncio.wait_for(received.wait(), .5)
        try:
            assert await asyncio.wait_for(reader.read(), .5) == b''
        except TimeoutError:
            pytest.fail('The registry relay left a stalled connection open past its configured idle deadline')
        await asyncio.wait_for(finished.wait(), .5)
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
        await proxy.close()
        upstream.close()
        await upstream.wait_closed()


async def test_package_proxy_keeps_a_download_alive_while_upload_side_is_idle(monkeypatch):
    complete = asyncio.Event()
    async def download(reader, writer):
        try:
            assert await reader.readexactly(5) == b'hello'
            for _ in range(12):
                writer.write(b'chunk')
                await writer.drain()
                await asyncio.sleep(.025)  # Simulate an active response longer than the idle limit.
        finally:
            writer.close()
            await writer.wait_closed()
            complete.set()
    upstream = await asyncio.start_server(download, '127.0.0.1', 0)
    connect = asyncio.open_connection
    async def local_upstream(host, port, **kwargs):
        return await connect(host, upstream.sockets[0].getsockname()[1] if port == 443 else port, **kwargs)
    monkeypatch.setattr(asyncio, 'open_connection', local_upstream)
    proxy = await _RegistryTunnel(['127.0.0.1'], idle_timeout_seconds=.15).start()
    writer = None
    try:
        reader, writer = await connect('127.0.0.1', proxy.port)
        writer.write(b'CONNECT registry.npmjs.org:443 HTTP/1.1\r\nHost: registry.npmjs.org\r\n\r\n')
        await writer.drain()
        assert b'200' in await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), .5)
        writer.write(b'hello')
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 2) == b'chunk' * 12
        await asyncio.wait_for(complete.wait(), .5)
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
        await proxy.close()
        upstream.close()
        await upstream.wait_closed()


async def test_package_proxy_preserves_a_large_download_during_client_backpressure(monkeypatch):
    body = b'package bytes\n' * (1024 * 1024)
    complete = asyncio.Event()

    async def download(_reader, writer):
        try:
            writer.write(body)
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            complete.set()

    upstream = await asyncio.start_server(download, '127.0.0.1', 0)
    connect = asyncio.open_connection

    async def local_upstream(host, port, **kwargs):
        return await connect(host, upstream.sockets[0].getsockname()[1] if port == 443 else port, **kwargs)

    monkeypatch.setattr(asyncio, 'open_connection', local_upstream)
    proxy = _RegistryTunnel(['127.0.0.1'], idle_timeout_seconds=.1)
    handle = proxy._handle

    async def constrained_sender(reader, writer):
        writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        writer.transport.set_write_buffer_limits(high=4096, low=2048)
        await handle(reader, writer)

    monkeypatch.setattr(proxy, '_handle', constrained_sender)
    await proxy.start()
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.setblocking(False)
    writer = None
    try:
        await asyncio.get_running_loop().sock_connect(sock, ('127.0.0.1', proxy.port))
        reader, writer = await connect(sock=sock, limit=1024)
        writer.write(b'CONNECT registry.npmjs.org:443 HTTP/1.1\r\nHost: registry.npmjs.org\r\n\r\n')
        await writer.drain()
        assert b'200' in await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 1)
        chunks = []
        for _ in range(30):
            chunks.append(await asyncio.wait_for(reader.read(512), 1))
            await asyncio.sleep(.01)
        # The slow consumer stays active for three idle windows. Once its
        # backpressure clears, the rest of the package must arrive intact.
        chunks.append(await asyncio.wait_for(reader.read(), 5))
        assert b''.join(chunks) == body
        await asyncio.wait_for(complete.wait(), 1)
    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
        else:
            sock.close()
        await proxy.close()
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.parametrize('timeout', [0, -1, True, '30', float('nan'), float('inf')])
def test_package_proxy_rejects_invalid_idle_deadlines(timeout):
    with pytest.raises(ValueError):
        _RegistryTunnel([], idle_timeout_seconds=timeout)


@pytest.mark.skipif(platform.system() != "Darwin", reason="Measured macOS isolation requires macOS")
async def test_local_executor_stops_a_non_loopback_listener(tmp_path):
    workspace = tmp_path / "job/workspace"
    workspace.mkdir(parents=True)
    command = CommandSpec(argv=(sys.executable, "-c", "import os,socket,time;s=socket.socket();s.bind(('0.0.0.0',int(os.environ['AGENTFLOW_TEST_PORT'])));s.listen(2);time.sleep(30)"),
                          cwd=workspace, label="deliberate public binding")
    executor = LocalIsolatedExecutor(tmp_path / "control")
    result = await executor.execute(command, tmp_path / "job/evidence/command-0", JobLimits(maximum_active_seconds=10))
    assert result.execution_status == "error" and result.reason == "non_loopback_listener_or_inspection_unavailable"
    assert result.cleanup_verified
    await executor.sandbox.close()

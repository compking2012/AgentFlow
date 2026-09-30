"""Real Unix-socket launcher tests; no model request or controller restart."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import sys
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from agentflow.common import DomainError
from agentflow.control.api import create_app
from agentflow.control.owner_ipc import OwnerBroker, launch_url, socket_path
from agentflow.control.security import TokenAuthority
from agentflow.settings import Settings
from agentflow.storage import LocalArtifactStore, Store

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Owner IPC explicitly requires a POSIX controller")
ORIGIN = "http://127.0.0.1:48765"


def bootstrap(url):
    return parse_qs(urlsplit(url).fragment)["bootstrap"][0]


async def test_first_and_fresh_launcher_codes_are_single_use_without_revoking_owner_sessions(tmp_path):
    tokens = TokenAuthority()
    original = tokens.bootstrap_code
    broker = OwnerBroker(tmp_path, tokens, ORIGIN)
    try:
        await broker.start()
        first = bootstrap(await launch_url(tmp_path))
        assert first != original
        with pytest.raises(DomainError, match="invalid or expired"):
            tokens.exchange(original)
        owner = tokens.exchange(first)
        with pytest.raises(DomainError):
            tokens.exchange(first)
        second = bootstrap(await launch_url(tmp_path))
        third = bootstrap(await launch_url(tmp_path))
        assert len({first, second, third}) == 3
        with pytest.raises(DomainError):
            tokens.exchange(second)
        tokens.exchange(third)
        assert tokens.require(owner, "agentflow_owner", "owner:control").subject == "owner"
    finally:
        await broker.close()


async def test_launcher_has_private_directory_and_socket_permissions(tmp_path):
    broker = OwnerBroker(tmp_path, TokenAuthority(), ORIGIN)
    try:
        await broker.start()
        assert stat.S_IMODE(broker.path.parent.stat().st_mode) == 0o700
        info = broker.path.lstat()
        assert stat.S_ISSOCK(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_uid == os.getuid()
        assert socket_path(tmp_path / "other") != broker.path
        broker.path.chmod(0o666)
        with pytest.raises(DomainError) as caught:
            await launch_url(tmp_path)
        assert caught.value.code == "unsafe_owner_ipc"
    finally:
        broker.path.chmod(0o600)
        await broker.close()


async def test_start_is_idempotent_and_another_broker_cannot_replace_a_live_listener(tmp_path):
    first = OwnerBroker(tmp_path, TokenAuthority(), ORIGIN)
    second = OwnerBroker(tmp_path, TokenAuthority(), ORIGIN)
    try:
        await first.start()
        inode = first.path.stat().st_ino
        server = first.server
        await first.start()
        assert first.server is server and first.path.stat().st_ino == inode
        with pytest.raises(DomainError) as caught:
            await second.start()
        assert caught.value.code == "owner_ipc_in_use"
        first.tokens.exchange(bootstrap(await launch_url(tmp_path)))
    finally:
        await second.close()
        await first.close()


async def test_concurrent_start_on_one_broker_keeps_a_single_listener(tmp_path):
    broker = OwnerBroker(tmp_path, TokenAuthority(), ORIGIN)
    try:
        await asyncio.gather(broker.start(), broker.start(), broker.start())
        server = broker.server
        inode = broker.path.stat().st_ino
        broker.tokens.exchange(bootstrap(await launch_url(tmp_path)))
        assert broker.server is server and broker.path.stat().st_ino == inode
    finally:
        await broker.close()


async def test_live_legacy_socket_without_new_lock_is_not_replaced(tmp_path):
    path = socket_path(tmp_path)
    async def legacy(reader, writer):
        await reader.read()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_unix_server(legacy, path=path)
    path.chmod(0o600)
    inode = path.stat().st_ino
    broker = OwnerBroker(tmp_path, TokenAuthority(), ORIGIN)
    try:
        with pytest.raises(DomainError) as caught:
            await broker.start()
        assert caught.value.code == "owner_ipc_in_use"
        assert path.stat().st_ino == inode and server.is_serving()
    finally:
        await broker.close()
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)


async def test_stale_owned_socket_is_reclaimed(tmp_path):
    path = socket_path(tmp_path)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    path.chmod(0o600)
    broker = OwnerBroker(tmp_path, TokenAuthority(), ORIGIN)
    try:
        await broker.start()
        broker.tokens.exchange(bootstrap(await launch_url(tmp_path)))
    finally:
        await broker.close()


@pytest.mark.parametrize("linked", [False, True])
async def test_existing_non_socket_path_is_not_replaced(tmp_path, linked):
    broker = OwnerBroker(tmp_path, TokenAuthority(), ORIGIN)
    target = tmp_path / "keep.txt"
    target.write_text("preserve this file")
    if linked:
        broker.path.symlink_to(target)
    else:
        broker.path.write_text("preserve this path")
    try:
        with pytest.raises(DomainError) as caught:
            await broker.start()
        assert caught.value.code == "unsafe_owner_ipc"
        assert target.read_text() == "preserve this file"
        assert broker.path.is_symlink() if linked else broker.path.read_text() == "preserve this path"
    finally:
        broker.path.unlink(missing_ok=True)
        await broker.close()


async def test_close_disconnects_existing_clients_and_cannot_issue_a_later_bootstrap(tmp_path):
    tokens = TokenAuthority()
    broker = OwnerBroker(tmp_path, tokens, ORIGIN)
    await broker.start()
    reader, writer = await asyncio.open_unix_connection(broker.path)
    await asyncio.sleep(0)
    before = tokens.bootstrap_code
    await broker.close()
    try:
        try:
            writer.write(b"open\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        try:
            remaining = await asyncio.wait_for(reader.read(), 0.5)
        except (BrokenPipeError, ConnectionResetError):
            remaining = b""
        assert remaining == b""
        assert tokens.bootstrap_code == before
        assert not broker.path.exists()
        with pytest.raises(DomainError) as caught:
            await launch_url(tmp_path)
        assert caught.value.code == "controller_unavailable"
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


async def test_close_does_not_remove_a_replacement_path(tmp_path):
    broker = OwnerBroker(tmp_path, TokenAuthority(), ORIGIN)
    await broker.start()
    broker.path.unlink()
    broker.path.write_text("replacement must survive")
    try:
        await broker.close()
        assert broker.path.read_text() == "replacement must survive"
    finally:
        broker.path.unlink(missing_ok=True)


@pytest.mark.parametrize("message", [
    [], {"url": 7}, {"url": "https://example.com/#bootstrap=" + "a" * 43},
    {"url": ORIGIN + "/#bootstrap=" + "a" * 43 + "&bootstrap=" + "b" * 43},
    {"url": ORIGIN + "/#bootstrap=" + "a" * 43 + "&ignored="},
    {"url": ORIGIN + "/#bootstrap=short"},
])
async def test_client_rejects_malformed_or_ambiguous_launch_responses(tmp_path, message):
    path = socket_path(tmp_path)
    async def respond(reader, writer):
        await reader.readline()
        writer.write(json.dumps(message).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_unix_server(respond, path=path)
    path.chmod(0o600)
    try:
        with pytest.raises(DomainError) as caught:
            await launch_url(tmp_path)
        assert caught.value.code == "controller_unavailable"
    finally:
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)


async def test_new_bootstrap_exchanges_against_live_api_without_restarting_work(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", port=48765)
    store = Store(settings.data_dir)
    await store.start()
    app = create_app(settings, store=store, artifacts=LocalArtifactStore(settings.data_dir / "artifacts"))
    broker = OwnerBroker(settings.data_dir, app.state.tokens, settings.origin)
    worker = await asyncio.create_subprocess_exec(sys.executable, "-u", "-c", "import sys; print('ready', flush=True); sys.stdin.read()",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    await worker.stdout.readline()
    pid = worker.pid
    await store.command("fixture.work", "seed", {}, lambda tx: tx.put("work_item", "active", {"status": "running", "worker_pid": pid}))
    try:
        await broker.start()
        inode = broker.path.stat().st_ino
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=settings.origin) as client:
            issued = []
            for index in range(2):
                code = bootstrap(await launch_url(settings.data_dir))
                response = await client.post("/api/v1/session", json={"bootstrap_token": code},
                    headers={"Origin": settings.origin, "Idempotency-Key": f"exchange-{index}"})
                assert response.status_code == 201
                issued.append(response.json()["owner_token"])
            for token in issued:
                response = await client.get("/api/v1/session", headers={"Authorization": "Bearer " + token})
                assert response.status_code == 200
        assert worker.returncode is None and worker.pid == pid
        assert (await store.read("work_item", "active"))["revision"] == 1
        assert broker.path.stat().st_ino == inode
    finally:
        worker.stdin.close()
        await asyncio.wait_for(worker.wait(), 3)
        await broker.close()
        await store.close()

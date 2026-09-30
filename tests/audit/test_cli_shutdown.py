from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from anyio import CancelScope
from fastapi.responses import StreamingResponse

from agentflow import server
from agentflow.control.api import create_app
from agentflow.control.owner_ipc import launch_url, socket_path
from agentflow.execution.pki import NodeCertificateAuthority
from agentflow.settings import Settings
from agentflow.storage import Store


@pytest.mark.parametrize("stream_kind", ["events", "uncooperative"])
async def test_cli_stops_with_an_owner_stream_still_connected(tmp_path, monkeypatch, stream_kind):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    settings = Settings(data_dir=tmp_path / "controller", port=port)
    store = Store(settings.data_dir)
    await store.start()
    await store.command("audit", "run", {}, lambda tx: tx.put("run", "run", {
        "run_id": "run", "execution_state": "running", "quality_result": "unknown"}))
    if stream_kind == "events":
        await store.command("audit", "event", {}, lambda tx: {
            "sequence": tx.event("run.updated", {"state": "running"}, "run")})
    app = create_app(settings, store=store)
    closed = asyncio.Event()
    response_finished = asyncio.Event()

    @app.get("/api/v1/audit/held-response")
    async def held_response():
        async def stream():
            try:
                yield b": heartbeat\n\n"
                await asyncio.Event().wait()
            finally:
                with CancelScope(shield=True):
                    await asyncio.sleep(.05)
                    await store.command("audit", "request-cleanup", {}, lambda tx: tx.put(
                        "shutdown", "request-cleanup", {"state": "persisted"}))
                    response_finished.set()
        return StreamingResponse(stream(), media_type="text/event-stream")

    class LocalApplicationFixture:
        def __init__(self, _settings):
            self.owner_app, self.executor_app = app, None

        async def start(self):
            return self

        async def close(self):
            if stream_kind == "uncooperative":
                assert response_finished.is_set(), "HTTP cancellation must precede writer cleanup"
            await store.command("audit", "shutdown", {}, lambda tx: tx.put(
                "shutdown", "completed", {"state": "closed"}))
            await store.close()
            closed.set()

    # Exercise the internal server's real listener shutdown order without models or
    # installing process signal handlers into pytest itself.
    monkeypatch.setattr(server, "Application", LocalApplicationFixture)
    callbacks = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda number, callback: callbacks.__setitem__(number, callback))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda number: callbacks.pop(number, None))
    task = asyncio.create_task(server.serve(settings, open_browser=False))
    token = app.state.tokens.issue("agentflow_owner", {"owner:*"}, "owner", 60)
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            for _ in range(100):
                try:
                    if (await client.get(settings.origin + "/health")).status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(.02)
            path = "/api/v1/runs/run/events" if stream_kind == "events" else "/api/v1/audit/held-response"
            async with client.stream("GET", settings.origin + path,
                                     headers={"Authorization": "Bearer " + token}) as response:
                assert response.status_code == 200
                stream = response.aiter_raw()
                first_chunk = await anext(stream)
                assert (b"event: change" if stream_kind == "events" else b": heartbeat") in first_chunk
                assert not response.is_closed
                callbacks[signal.SIGTERM]()
                done, _ = await asyncio.wait({task}, timeout=3 if stream_kind == "events" else 7)
                assert task in done and closed.is_set(), "An open Dashboard SSE stream prevented CLI and runtime shutdown"
                await task
                async with asyncio.timeout(1):
                    if stream_kind == "events":
                        async for _chunk in stream:
                            pass
                        assert response.is_closed, "The server must finish the stream without a client disconnect"
                    else:
                        with pytest.raises(httpx.RemoteProtocolError):
                            await anext(stream)
        recovered = Store(settings.data_dir)
        await recovered.start()
        try:
            assert (await recovered.read("shutdown", "completed"))["state"] == "closed"
            assert (await recovered.read("run", "run"))["execution_state"] == "running"
            if stream_kind == "uncooperative":
                assert (await recovered.read("shutdown", "request-cleanup"))["state"] == "persisted"
        finally:
            await recovered.close()
    finally:
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=3)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await store.close()


def isolated_configuration(tmp_path, *, data_dir, port, executor_port=None):
    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    (home / ".config").mkdir(mode=0o700, exist_ok=True)
    config = home / ".config/agentflow/config.toml"
    config.parent.mkdir(mode=0o700, exist_ok=True)
    body = f"[app]\ndata_dir = {json.dumps(str(data_dir))}\nport = {port}\n"
    if executor_port is not None:
        body += f'executor_host = "127.0.0.1"\nexecutor_port = {executor_port}\n'
    config.write_text(body)
    config.chmod(0o600)
    environment = {**os.environ, "HOME": str(home), "BROWSER": "/usr/bin/true",
                   "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    return config, environment


async def public_command(environment, *arguments):
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "agentflow.cli", *arguments,
        env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        assert process.returncode == 0, stdout.decode() + stderr.decode()
        return stdout.decode()
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()


@pytest.mark.parametrize("blocked_listener", ["owner", "executor"])
async def test_internal_dual_listener_bind_failure_closes_application(tmp_path, blocked_listener):
    reservations = {name: socket.socket() for name in ("owner", "executor")}
    for listener in reservations.values():
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
    ports = {name: listener.getsockname()[1] for name, listener in reservations.items()}
    peer = "executor" if blocked_listener == "owner" else "owner"
    reservations[peer].close()
    data = tmp_path / "controller"
    NodeCertificateAuthority(data / "nodes/pki", "127.0.0.1")
    config, environment = isolated_configuration(tmp_path, data_dir=data, port=ports["owner"],
                                                 executor_port=ports["executor"])
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "agentflow.server",
        env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        assert process.returncode != 0, stderr.decode()
        assert b"AgentFlow ready:" not in stdout, "A partially started controller reported readiness"
        assert not socket_path(data).exists(), "Failed startup left the owner IPC endpoint behind"
        assert not (config.parent / "active-instance.json").exists()
        with socket.socket() as released:
            released.bind(("127.0.0.1", ports[peer]))
        store = Store(data)
        await store.start()
        await store.close()
    finally:
        for listener in reservations.values():
            listener.close()
        if process.returncode is None:
            process.terminate()
            await process.wait()


async def test_public_stop_restart_and_changed_data_directory_find_original_instance(tmp_path):
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        first_port, second_port = first.getsockname()[1], second.getsockname()[1]
    first_data, second_data = tmp_path / "first", tmp_path / "second"
    store = Store(first_data)
    await store.start()
    try:
        await store.command("audit", "run", {}, lambda tx: tx.put("run", "run", {
            "run_id": "run", "execution_state": "completed", "quality_result": "unknown"}))
    finally:
        await store.close()
    config, environment = isolated_configuration(tmp_path, data_dir=first_data, port=first_port)
    instance = config.parent / "active-instance.json"
    try:
        assert f"http://127.0.0.1:{first_port}" in await public_command(environment, "start")
        first_identity = json.loads(instance.read_text())
        origin = f"http://127.0.0.1:{first_port}"
        bootstrap = parse_qs(urlsplit(await launch_url(first_data)).fragment)["bootstrap"][0]
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            session = await client.post(origin + "/api/v1/session",
                headers={"Origin": origin, "Idempotency-Key": "shutdown-session"},
                json={"bootstrap_token": bootstrap})
            assert session.status_code == 201
            async with client.stream("GET", origin + "/api/v1/runs/run/events",
                headers={"Authorization": "Bearer " + session.json()["owner_token"]}) as response:
                assert response.status_code == 200
                stream = response.aiter_raw()
                assert b": heartbeat" in await anext(stream)
                await public_command(environment, "stop")
                async with asyncio.timeout(1):
                    assert [chunk async for chunk in stream] == []
                assert response.is_closed
        assert not instance.exists(), "Controller stop left the active instance registered"
        assert not socket_path(first_data).exists(), "Controller stop left the owner IPC endpoint behind"
        await store.start()
        try:
            assert (await store.read("run", "run"))["execution_state"] == "completed"
        finally:
            await store.close()
        assert f"http://127.0.0.1:{first_port}" in await public_command(environment, "start")
        restarted = json.loads(instance.read_text())
        assert restarted["pid"] != first_identity["pid"]
        isolated_configuration(tmp_path, data_dir=second_data, port=second_port)
        assert "还没有产品" in await public_command(environment, "status")
        output = await public_command(environment, "start")
        assert f"http://127.0.0.1:{first_port}" in output
        assert json.loads(instance.read_text()) == restarted
        assert not (second_data / "state/agentflow.sqlite3").exists()
        await public_command(environment, "stop")
        assert f"http://127.0.0.1:{second_port}" in await public_command(environment, "start")
        moved = json.loads(instance.read_text())
        assert moved["data_dir"] == str(second_data.resolve()) and moved["pid"] != restarted["pid"]
        assert not socket_path(first_data).exists()
    finally:
        await public_command(environment, "stop")

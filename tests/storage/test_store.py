from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import threading

import pytest

from agentflow.common import DomainError
from agentflow.storage import LocalArtifactStore, Store


@pytest.fixture
async def store(tmp_path):
    value = Store(tmp_path / "data")
    await value.start()
    yield value
    await value.close()


async def test_create_update_and_copy_boundaries(store):
    original = {"nested": {"items": [1]}}
    first = await store.command("objects", "create", {}, lambda tx: tx.put("thing", "b", original))
    original["nested"]["items"].append(2)
    first["nested"]["items"].append(3)
    assert await store.read("thing", "b") == {"id": "b", "revision": 1, "nested": {"items": [1]}}
    row = await store.read("thing", "b")
    row["nested"]["items"].append(4)
    result = await store.command("objects", "update", {}, lambda tx: tx.put("thing", "b", row, 1))
    assert result["revision"] == 2
    assert result["nested"]["items"] == [1, 4]
    assert await store.read("thing", "missing") is None
    await store.command("objects", "a", {}, lambda tx: tx.put("thing", "a", {"x": 1}))
    assert [r["id"] for r in await store.list("thing")] == ["a", "b"]
    listed = await store.list("thing")
    listed[1]["nested"]["items"].append(99)
    assert (await store.read("thing", "b"))["nested"]["items"] == [1, 4]


async def test_canonical_idempotency_returns_original_receipt(store):
    calls = 0

    def handler(tx):
        nonlocal calls
        calls += 1
        tx.put("thing", "a", {"value": "original"})
        tx.event("created", {"value": [1]}, "run-1")
        return {"result": [1]}

    result = await store.command("scope", "key", {"b": 2, "a": 1}, handler)
    result["result"].append(9)
    assert await store.command("scope", "key", {"a": 1, "b": 2}, handler) == {"result": [1]}
    assert calls == 1
    with pytest.raises(DomainError, match="different request"):
        await store.command("scope", "key", {"a": 2}, handler)
    assert len(await store.events(0)) == 1
    assert (await store.events(0))[0]["id"] == 1


async def test_atomic_rollback_including_events_and_receipt(store):
    def fail(tx):
        tx.put("x", "x", {"value": 1})
        tx.event("created", {})
        raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError):
        await store.command("scope", "retry", {}, fail)
    assert await store.read("x", "x") is None
    assert await store.events(0) == []

    def recover(tx):
        tx.put("x", "x", {"value": 2})
        tx.event("created", {})
        return {"ok": True}

    assert await store.command("scope", "retry", {}, recover) == {"ok": True}
    assert len(await store.events(0)) == 1


async def test_compare_and_swap_and_transaction_list(store):
    def create(tx):
        tx.put("x", "b", {"n": 0})
        tx.put("x", "a", {"n": 1})
        return {"rows": tx.list("x")}

    result = await store.command("s", "create", {}, create)
    assert [x["id"] for x in result["rows"]] == ["a", "b"]
    for expected in (None, 2):
        with pytest.raises(DomainError) as caught:
            await store.command("s", str(expected), {}, lambda tx: tx.put("x", "a", {}, expected))
        assert caught.value.code == "revision_conflict"
    with pytest.raises(DomainError):
        await store.command("s", "missing", {}, lambda tx: tx.put("x", "missing", {}, 1))


async def test_concurrent_commands_have_one_writer_and_no_lost_updates(store):
    await store.command("c", "init", {}, lambda tx: tx.put("counter", "one", {"n": 0}))
    threads = set()

    def increment(tx):
        threads.add(threading.get_ident())
        value = tx.get("counter", "one")
        value["n"] += 1
        result = tx.put("counter", "one", value, value["revision"])
        tx.event("incremented", {"n": result["n"]}, "run")
        return result

    results = await asyncio.gather(*(store.command("c", str(i), {"i": i}, increment) for i in range(100)))
    assert sorted(r["n"] for r in results) == list(range(1, 101))
    assert len(threads) == 1
    assert (await store.read("counter", "one"))["n"] == 100
    assert [e["id"] for e in await store.events(0, limit=1000)] == list(range(1, 101))


async def test_concurrent_duplicate_command_executes_once(store):
    calls = []

    def create(tx):
        calls.append(1)
        return tx.put("x", "x", {})

    results = await asyncio.gather(*(store.command("s", "same", {}, create) for _ in range(25)))
    assert len(calls) == 1
    assert all(r == results[0] for r in results)


async def test_event_filter_cursor_and_copy(store):
    def write(tx):
        tx.event("a", {"x": []}, "one")
        tx.event("b", {}, "two")
        tx.event("c", {}, "one")
        return {}

    await store.command("s", "k", {}, write)
    assert [e["type"] for e in await store.events(0, "one", limit=1)] == ["a"]
    assert [e["type"] for e in await store.events(1, "one")] == ["c"]
    first = await store.events(0)
    first[0]["body"]["x"].append(7)
    assert (await store.events(0))[0]["body"]["x"] == []
    for after, limit in [(-1, 2), (True, 2), (0, 0), (0, 10001)]:
        with pytest.raises(DomainError):
            await store.events(after, limit=limit)


async def test_escaped_transaction_cannot_be_reused(store):
    escaped = []

    def handler(tx):
        escaped.append(tx)
        return {}

    await store.command("s", "k", {}, handler)
    for operation in [lambda: escaped[0].get("x", "x"), lambda: escaped[0].list("x"),
                      lambda: escaped[0].put("x", "x", {}), lambda: escaped[0].event("x", {})]:
        with pytest.raises(DomainError) as caught:
            operation()
        assert caught.value.code == "transaction_closed"


async def test_cancelled_waiter_does_not_discard_accepted_command(store):
    started, release = threading.Event(), threading.Event()

    def slow(tx):
        started.set()
        assert release.wait(5)
        tx.event("committed", {})
        return tx.put("x", "x", {"done": True})

    operation = asyncio.create_task(store.command("s", "k", {}, slow))
    await asyncio.to_thread(started.wait, 5)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    release.set()
    assert (await store.read("x", "x"))["done"]
    assert await store.command("s", "k", {}, slow) == {"id": "x", "revision": 1, "done": True}
    assert len(await store.events(0)) == 1


async def test_lock_released_on_close_and_failed_start(tmp_path):
    a, b = Store(tmp_path / "db"), Store(tmp_path / "db")
    await a.start()
    with pytest.raises(DomainError) as caught:
        await b.start()
    assert caught.value.code == "store_locked"
    await a.close()
    await b.start()
    await b.close()
    await b.close()
    with pytest.raises(DomainError):
        await b.read("x", "x")


async def test_process_crash_preserves_wal_receipt(tmp_path):
    path = tmp_path / "crash"
    script = """
import asyncio, os, sys
from pathlib import Path
from agentflow.storage import Store
async def main():
    store=Store(Path(sys.argv[1])); await store.start()
    def write(tx):
        tx.event('persisted', {'value':7}, 'run')
        return tx.put('record','one',{'value':7})
    await store.command('s','op',{'a':1},write)
    os._exit(0)
asyncio.run(main())
"""
    result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(path)],
                                     capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    store = Store(path)
    await store.start()
    try:
        assert (await store.read("record", "one"))["value"] == 7
        assert len(await store.events(0)) == 1
        replay = await store.command("s", "op", {"a": 1}, lambda tx: pytest.fail("must not execute"))
        assert replay["value"] == 7
    finally:
        await store.close()


async def test_separate_process_cannot_steal_lock(store):
    script = """
import asyncio,sys
from pathlib import Path
from agentflow.storage import Store
from agentflow.common import DomainError
async def main():
    try: await Store(Path(sys.argv[1])).start()
    except DomainError as e:
        print(e.code); return
    raise RuntimeError('lock not enforced')
asyncio.run(main())
"""
    result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(store.data_dir)],
                                     capture_output=True, timeout=20)
    assert result.returncode == 0 and b"store_locked" in result.stdout


async def test_backup_is_consistent_and_restore_ready(store, tmp_path):
    artifacts = LocalArtifactStore(store.data_dir / "artifacts")
    artifact = await artifacts.put_bytes(b"evidence")

    def write(tx):
        tx.put("artifact", artifact["id"], {"size": artifact["size"]})
        tx.event("artifact", {"id": artifact["id"]})
        return {}

    await store.command("s", "one", {}, write)
    destination = tmp_path / "backup"
    receipt = await store.backup(destination, artifacts)
    await store.command("s", "two", {}, lambda tx: tx.put("later", "x", {}))
    assert receipt["event_watermark"] == 1
    assert not (destination / ".incomplete").exists()
    assert json.loads((destination / "backup.json").read_text())["artifacts"]["count"] == 1
    restored = Store(destination)
    await restored.start()
    try:
        assert await restored.read("later", "x") is None
        assert await restored.read("artifact", artifact["id"])
        assert await LocalArtifactStore(destination / "artifacts").read(artifact["id"]) == b"evidence"
    finally:
        await restored.close()
    with pytest.raises(DomainError):
        await store.backup(destination)


async def test_newer_schema_and_unsafe_database_are_rejected(tmp_path):
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True)
    db = data / "state/agentflow.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(DomainError) as caught:
        await Store(data).start()
    assert caught.value.code == "schema_too_new"
    db.unlink()
    target = tmp_path / "elsewhere"
    target.write_text("untouched")
    db.symlink_to(target)
    with pytest.raises(DomainError):
        await Store(data).start()
    assert target.read_text() == "untouched"


@pytest.mark.parametrize("payload", [{"nan": float("nan")}, {"bad": {1, 2}}, {"bad": "\ud800"}, []])
async def test_invalid_json_does_not_reach_handler(store, payload):
    with pytest.raises(DomainError):
        await store.command("s", "k", payload, lambda tx: pytest.fail("must not execute"))


async def test_invalid_handler_result_rolls_back(store):
    def handler(tx):
        tx.put("x", "x", {})
        return {"bad": object()}

    with pytest.raises(DomainError):
        await store.command("s", "k", {}, handler)
    assert await store.read("x", "x") is None

    async def asynchronous(tx):
        return {}

    with pytest.raises(DomainError):
        await store.command("s", "async", {}, asynchronous)


async def test_verified_restore_and_tamper_detection(store, tmp_path):
    artifacts = LocalArtifactStore(store.data_dir / "artifacts")
    artifact = await artifacts.put_bytes(b"proof")
    await store.command("s", "record", {}, lambda tx: tx.put("artifact", artifact["id"], {"size": 5}))
    backup = tmp_path / "snapshot"
    await store.backup(backup, artifacts)
    destination = tmp_path / "restored"
    result = await Store.restore_backup(backup, destination)
    assert result["artifact_count"] == 1
    restored = Store(destination)
    await restored.start()
    try:
        assert await restored.read("artifact", artifact["id"])
    finally:
        await restored.close()
    assert await LocalArtifactStore(destination / "artifacts").read(artifact["id"]) == b"proof"
    with pytest.raises(DomainError):
        await Store.restore_backup(backup, destination)
    with sqlite3.connect(backup / "state/agentflow.sqlite3") as connection:
        connection.execute("UPDATE records SET body='{}'")
    with pytest.raises(DomainError) as caught:
        await Store.restore_backup(backup, tmp_path / "tampered")
    assert caught.value.code == "backup_corrupt"
    assert (tmp_path / "tampered/.incomplete").exists()


async def test_invalid_schema_version_marker_is_not_treated_as_ready(tmp_path):
    data = tmp_path / "invalid"
    (data / "state").mkdir(parents=True)
    with sqlite3.connect(data / "state/agentflow.sqlite3") as connection:
        connection.execute("PRAGMA user_version=1")
    with pytest.raises(DomainError) as caught:
        await Store(data).start()
    assert caught.value.code == "schema_invalid"


@pytest.mark.parametrize("body,revision", [({"id": "wrong"}, None), ({"revision": 4}, None), ({}, True)])
async def test_reserved_record_fields_are_not_overridable(store, body, revision):
    with pytest.raises(DomainError):
        await store.command("s", "k", {}, lambda tx: tx.put("x", "x", body, revision))

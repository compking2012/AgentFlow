import asyncio
import json
import platform
import time
from types import SimpleNamespace

import psutil
import pytest

from agentflow.common import DomainError, canonical_digest
from agentflow.execution.capabilities import probe_target_sync
from agentflow.execution.models import TargetConfig
from agentflow.local_execution import LocalExecutionService
from agentflow.runtime.process_identity import current_boot_identity
from agentflow.storage import Store
from node_agent.daemon import NodeDaemon
from node_agent.journal import NodeJournal


def test_capability_boot_identity_survives_boot_time_drift(monkeypatch):
    expected = current_boot_identity()[1]
    target = TargetConfig(app_target="api", os_name=platform.system(), os_version_constraint="*",
                          cpu_architecture="*", required_display_protocol="not_required",
                          required_device_mode="not_required")
    now = psutil.boot_time()
    monkeypatch.setattr(psutil, "boot_time", lambda: now)
    first = probe_target_sync(target)
    monkeypatch.setattr(psutil, "boot_time", lambda: now + 2)
    second = probe_target_sync(target)
    assert first.boot_fingerprint == second.boot_fingerprint == expected


@pytest.fixture
def claim_daemon(tmp_path, monkeypatch):
    journal = NodeJournal(tmp_path / "journal.sqlite")
    daemon = object.__new__(NodeDaemon)
    daemon.journal = journal
    daemon.boot = canonical_digest("current boot")
    daemon.capability_ids, daemon.resource_ids = ["current-capability"], ["workspace"]
    daemon._last_capability_refresh = time.monotonic()
    daemon.capability_refresh_seconds = 1800
    monkeypatch.setattr(daemon, "_maintain_local_files", lambda: None)

    class Client:
        config = {"node_id": "node", "node_revision": 1}

        async def json_request(self, method, path, **kwargs):
            return {"revision": 1}

    daemon.client = Client()
    try:
        yield daemon
    finally:
        journal.close()


def pending_claim(daemon, *, current=False):
    return daemon.journal.pending_operation("claim", {
        "node_revision": 1, "boot_fingerprint": daemon.boot if current else canonical_digest("old boot"),
        "capability_ids": ["old-capability"], "available_resource_ids": ["workspace"],
    })


def old_assignment(operation):
    return {"job_id": "accepted-before-restart", "attempt_id": "old-attempt", "fencing_token": 1,
            "input_fingerprint": canonical_digest("input"),
            "boot_fingerprint": operation["payload"]["boot_fingerprint"]}


async def test_explicit_old_boot_rejection_retires_claim_with_history_then_claims_new_id(claim_daemon):
    daemon = claim_daemon
    original = pending_claim(daemon)
    requests = []

    async def claim(payload):
        requests.append(payload)
        if len(requests) == 1:
            raise DomainError("boot_mismatch", "Node must register its current boot heartbeat first")
        return {"assignment": None}

    daemon.client.claim = claim
    assert await daemon.run_once() == {"state": "idle"}
    assert daemon.journal.get_pending_operation("claim") is None
    assert await daemon.run_once() == {"state": "idle"}
    assert requests[0] == {**original["payload"], "operation_id": original["operation_id"]}
    assert requests[1]["operation_id"] != original["operation_id"]
    assert requests[1]["boot_fingerprint"] == daemon.boot
    assert requests[1]["capability_ids"] == ["current-capability"]
    history = daemon.journal.connection.execute("SELECT body FROM events WHERE type='claim.rejected'").fetchall()
    assert [json.loads(row["body"]) for row in history] == [{
        "operation_id": original["operation_id"], "payload": original["payload"], "reason": "boot_mismatch",
    }]
    assert daemon.journal.recoverable() == []


async def test_network_unknown_replays_exact_claim_and_quarantines_old_assignment(claim_daemon):
    daemon = claim_daemon
    original = pending_claim(daemon)
    requests = []

    async def claim(payload):
        requests.append(payload)
        if len(requests) == 1:
            raise ConnectionError("claim may have been accepted, response lost")
        return {"assignment": old_assignment(original)}

    daemon.client.claim = claim
    with pytest.raises(ConnectionError):
        await daemon.run_once()
    assert daemon.journal.get_pending_operation("claim") == original
    result = await daemon.run_once()
    assert result["state"] == "execution_unknown" and result["reason"] == "boot_changed"
    assert requests[0] == requests[1]
    saved = daemon.journal.get("accepted-before-restart")
    assert saved["state"] == "received" and saved["pid"] is None
    assert saved["assignment"] == old_assignment(original)


@pytest.mark.parametrize("kind", ["current_boot", "other_rejection", "received_assignment"])
async def test_claim_recovery_never_discards_unproven_or_received_operation(claim_daemon, kind):
    daemon = claim_daemon
    original = pending_claim(daemon, current=kind == "current_boot")
    code = "revision_conflict" if kind == "other_rejection" else "boot_mismatch"

    async def claim(payload):
        if kind == "received_assignment":
            daemon.journal.record_assignment(old_assignment(original))
        raise DomainError(code, "claim rejected")

    daemon.client.claim = claim
    with pytest.raises(DomainError) as error:
        await daemon.run_once()
    assert error.value.code == code
    assert daemon.journal.get_pending_operation("claim") == original


async def test_wait_job_preserves_worker_root_error(tmp_path):
    store = Store(tmp_path / "data")
    await store.start()
    service = LocalExecutionService(store, store.data_dir)

    async def failed_worker():
        raise DomainError("boot_mismatch", "original worker failure")

    service._daemon = SimpleNamespace(run_once=failed_worker)
    try:
        await store.command("fixture", "queued", {}, lambda tx: tx.put("node_job", "queued", {"state": "queued"}))
        service._worker_task = asyncio.create_task(service._work())
        await service._worker_task
        with pytest.raises(DomainError) as error:
            await service._wait_job({"id": "queued"}, "build")
        assert error.value.code == "boot_mismatch"
        assert str(error.value) == "original worker failure"
        assert (await store.read("node_job", "queued"))["state"] == "queued"
    finally:
        service._daemon = None
        await service.close()
        await store.close()

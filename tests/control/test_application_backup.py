"""Application recovery must survive deletion of the original managed directory."""
from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

import pytest

from agentflow.common import DomainError, canonical_digest
from agentflow.control.backups import ApplicationBackup
from agentflow.execution.manifests import file_digest
from agentflow.execution.service import NodeService
from agentflow.models.budget import BudgetLedger, account_id
from agentflow.models.profiles import AttemptContext
from agentflow.repository import RepositoryAdapter
from agentflow.runtime.supervisor import Supervisor
from agentflow.runtime.workspace import WorkspaceManager
from agentflow.storage import LocalArtifactStore, Store


@pytest.fixture
async def checkpoint(tmp_path, monkeypatch):
    data = tmp_path / "original"
    store = Store(data)
    await store.start()
    adapter = RepositoryAdapter(git_path=shutil.which("git") or "git")
    external = tmp_path / "user-repository"
    external.mkdir()
    adapter._run(None, ["init", "-b", "main", str(external)])
    (external / "product.txt").write_text("baseline\n")
    adapter._run(external, ["add", "."])
    adapter._run(external, ["-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "base"])
    base = adapter._run(external, ["rev-parse", "HEAD"]).decode().strip()
    workspace = await WorkspaceManager(data, adapter).create_clone(external, base, "attempt")
    (workspace / "product.txt").write_text("unreferenced candidate object survives\n")
    snapshot = await adapter.freeze_workspace(workspace, base, "Candidate snapshot")
    assert adapter._run(workspace, ["rev-parse", "HEAD"]).decode().strip() != snapshot["commit_oid"]
    (workspace / "product-link").symlink_to("product.txt")
    (workspace / "Cargo.lock").write_text('version = 3\n# Uncommitted dependency lock\n')
    (workspace / "go.json").write_text('{"application_setting":true}')
    (workspace / "auth.json").write_text('{"role_schema":["manager","member"]}')
    secret = "DO_NOT_BACKUP_RUNTIME_ENVIRONMENT_9375"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    (data / ".env").write_text("DEEPSEEK_API_KEY=" + secret)
    (workspace / ".env.local").write_text(secret)
    (data / "auth").mkdir()
    (data / "auth/credentials.json").write_text(secret)
    nodes = NodeService(store, data)
    artifact = await nodes.import_input(b"verified native raw report and build package", name="evidence.tar", run_id=None)
    domain_artifact = await LocalArtifactStore(data / "artifacts").put_bytes(b"accepted product specification")
    invocation = data / "model_invocations/call.sse"
    invocation.parent.mkdir()
    invocation.write_bytes(b"data: {\"text\":\"recorded model response\"}\n\n")
    json_invocation = data / "model_invocations/json-call.json"
    json_payload = {"z": 1, "a": {"environment": "product-defined-field", "workspace": str(workspace)}}
    json_invocation.write_text(json.dumps(json_payload, indent=4))
    supervision = data / "supervisor/attempt"
    supervision.mkdir(parents=True)
    (supervision / "launch.json").write_text(json.dumps({"environment": {"DEEPSEEK_API_KEY": secret}, "redact_values": [secret]}))
    (supervision / "go.json").write_text('{"nonce":"old-permit"}')
    (supervision / "stdout.jsonl").write_text('{"text":"redacted runtime evidence"}\n')
    (supervision / "result.json").write_text(json.dumps({"execution_status": "completed", "nonce": "old-nonce", "fencing_token": 2}))
    rows = [
        ("project", "project", {"local_path": str(external), "base_commit": base}),
        ("run", "run", {"execution_state": "running", "quality_result": "unknown", "iteration_id": "iteration"}),
        ("run", "finished-run", {"execution_state": "completed", "quality_result": "passed"}),
        ("work_item", "work", {"run_id": "run", "status": "running", "fencing_token": 2, "attempt_id": "attempt"}),
        ("work_item", "pending", {"run_id": "run", "status": "pending", "fencing_token": 0}),
        ("approval", "pending-approval", {"run_id": "run", "work_item_id": "pending", "decision": None, "stale": False}),
        ("attempt", "attempt", {"run_id": "run", "work_item_id": "work", "status": "running", "fencing_token": 2}),
        ("dispatch_context", "attempt", {"task": {"workspace": str(workspace), "artifact_dir": str(data / "attempt_artifacts/attempt"),
            "allowed_read_paths": [str(data / "artifacts"), str(external)], "task_token": secret}}),
        ("task_authorization", "authority", {"attempt_id": "attempt", "fencing_token": 2}),
        ("supervised_attempt", "attempt", {"attempt_id": "attempt", "operation_id": "operation", "run_id": "run", "state": "running",
            "backend": "codex_exec", "backend_version": "fixture", "directory": str(supervision), "pid": 999999,
            "process_started_at": 123, "boot_fingerprint": canonical_digest("old-boot"), "nonce": "old-nonce",
            "boot_identity_source": "macos_bootsessionuuid", "process_birth_source": "macos_proc_bsdinfo",
            "process_birth_fingerprint": canonical_digest("old-process-birth"),
            "fencing_token": 2, "input_fingerprint": canonical_digest("input"), "exit_code": None}),
        ("code_snapshot", "snapshot", {"repository_path": str(workspace), "commit_oid": snapshot["commit_oid"], "base_oid": base}),
        ("candidate", "candidate", {"source_repository": str(workspace), "source_commit": snapshot["commit_oid"]}),
        ("node", "node", {"state": "online", "config_revision": 1, "active_job_ids": ["job"]}),
        ("node_resource", "desktop", {"node_id": "node", "state": "leased", "fencing_token": 5, "owner_job_id": "job", "current_lease_id": "old-lease"}),
        ("node_job", "job", {"node_id": "node", "state": "running", "fencing_token": 3, "lease_revision": 2, "lease_expires_at": "2030-01-01T00:00:00Z"}),
        ("node_pairing", "pair", {"state": "pending", "expires_at": "2030-01-01T00:00:00Z"}),
        ("node_capability", "cap", {"verification_state": "functional_verified", "functional_result_id": "old-report"}),
        ("delivery_intent", "delivery", {"status": "prepared", "source_repository": str(workspace), "target_repository": str(external), "candidate_commit": snapshot["commit_oid"]}),
        ("cross_scenario", "scene", {"status": "creating", "terminal": False, "run_id": "run"}),
        ("model_attempt_budget", "attempt", {"request_count": 2, "uncertain_invocations": 0}),
        ("model_invocation", "call", {"state": "dispatching", "attempt_id": "attempt", "run_id": "run", "iteration_id": "iteration", "amount_micros": 7,
            "response_receipt": {"media_type": "text/event-stream", "path": str(invocation), "digest": file_digest(invocation)}}),
        ("model_invocation", "json-call", {"state": "settled", "attempt_id": "finished-attempt", "run_id": "finished-run",
            "response_receipt": {"media_type": "application/json", "path": str(json_invocation),
                                 "digest": canonical_digest(json_payload), "body": json_payload}}),
    ]
    for owner_kind, owner_id in [("run", "run"), ("iteration", "iteration")]:
        rows.append(("budget_account", account_id(owner_kind, owner_id), {"owner_kind": owner_kind, "owner_id": owner_id,
            "currency": "USD", "limit_micros": 100, "reserved_micros": 7, "settled_micros": 11,
            "uncertain_micros": 0, "request_count": 2, "max_requests": 10}))

    def save(tx):
        for kind, identity, body in rows:
            tx.put(kind, identity, body)
        tx.event("fixture", {"task_token": secret})
        return {"task_token": secret, "attempt_token": secret, "path": str(workspace)}

    await store.command("fixture", "seed", {}, save)
    backup = tmp_path / "private-backup"
    result = await ApplicationBackup(store, data).create(backup)
    yield {"store": store, "data": data, "backup": backup, "result": result, "workspace": workspace,
           "snapshot": snapshot, "adapter": adapter, "artifact": artifact, "domain_artifact": domain_artifact,
           "secret": secret, "external": external}
    await store.close()


async def test_complete_checkpoint_survives_original_deletion_and_disables_execution(checkpoint, tmp_path):
    env = checkpoint
    assert (await env["store"].read("run", "run"))["execution_state"] == "running"
    assert (await env["store"].read("dispatch_context", "attempt"))["task"]["task_token"] == env["secret"]
    assert env["result"]["sensitive_backup"] and env["result"]["contains_controller_private_keys"]
    assert any(row["path"] == str(env["external"]) and not row["included"] for row in env["result"]["external_repositories"])
    for path in env["backup"].rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert env["secret"].encode() not in path.read_bytes()
    await env["store"].close()
    shutil.rmtree(env["data"])
    destination = tmp_path / "restored"
    result = await ApplicationBackup.restore(env["backup"], destination)
    assert not result["automatic_resume_allowed"] and not result["process_stop_verified"]
    assert result["paused_run_ids"] == ["run"]
    restored = Store(destination)
    await restored.start()
    try:
        node_artifact = await restored.read("node_artifact", env["artifact"]["artifact_version_id"])
        blob = destination / "nodes/artifacts/objects" / node_artifact["digest"][7:]
        assert blob.read_bytes() == b"verified native raw report and build package"
        assert await LocalArtifactStore(destination / "artifacts").read(env["domain_artifact"]["id"]) == b"accepted product specification"
        snapshot = await restored.read("code_snapshot", "snapshot")
        repo = Path(snapshot["repository_path"])
        assert repo.is_relative_to(destination)
        assert env["adapter"]._run(repo, ["show", f"{snapshot['commit_oid']}:product.txt"]) == b"unreferenced candidate object survives\n"
        assert (repo / "product-link").is_symlink() and (repo / "product-link").read_text() == "unreferenced candidate object survives\n"
        assert (repo / "Cargo.lock").read_text() == 'version = 3\n# Uncommitted dependency lock\n'
        assert (repo / "go.json").read_text() == '{"application_setting":true}'
        assert (repo / "auth.json").read_text() == '{"role_schema":["manager","member"]}'
        metadata = json.loads(next((destination / "workspace_metadata").glob("*.json")).read_text())
        assert metadata["path"] == str(repo) and metadata["restore_revalidation_required"]
        context = await restored.read("dispatch_context", "attempt")
        assert context["task"]["workspace"] == str(repo) and "task_token" not in context["task"]
        assert str(env["external"]) in context["task"]["allowed_read_paths"]
        invocation = await restored.read("model_invocation", "call")
        assert invocation["state"] == "uncertain" and invocation["restore_uncertain"]
        assert Path(invocation["response_receipt"]["path"]).read_bytes().startswith(b"data:")
        json_receipt = (await restored.read("model_invocation", "json-call"))["response_receipt"]
        assert Path(json_receipt["path"]).read_text().startswith('{\n    "z":')
        assert canonical_digest(json.loads(Path(json_receipt["path"]).read_text())) == json_receipt["digest"]
        assert json_receipt["body"]["a"]["environment"] == "product-defined-field"
        assert json_receipt["body"]["a"]["workspace"] == str(env["workspace"])
        assert canonical_digest(json_receipt["body"]) == json_receipt["digest"]
        assert await BudgetLedger(restored).reconcile_interrupted() == []
        account = await restored.read("budget_account", account_id("run", "run"))
        assert (account["reserved_micros"], account["settled_micros"], account["request_count"], account["uncertain_micros"]) == (7, 11, 2, 7)
        assert account["restore_uncertain"]
        fresh = AttemptContext(attempt_id="new-attempt", run_id="run", iteration_id="iteration", model_profile_id="profile",
            fencing_token=1, input_fingerprint=canonical_digest("new-input"), expires_at="2030-01-01T00:00:00Z",
            max_model_requests=10, max_output_tokens=10)
        with pytest.raises(DomainError, match="Restored budget history"):
            await BudgetLedger(restored).reserve(fresh, protocol="responses", request_fingerprint=canonical_digest("new-request"),
                profile_revision=1, amount_micros=1, currency="USD")
        assert (await restored.read("run", "run"))["execution_state"] == "paused"
        assert (await restored.read("run", "finished-run"))["execution_state"] == "completed"
        assert (await restored.read("work_item", "work"))["status"] == "execution_unknown"
        assert (await restored.read("work_item", "pending"))["status"] == "blocked"
        assert (await restored.read("approval", "pending-approval"))["stale"]
        assert (await restored.read("attempt", "attempt"))["fencing_token"] == 3
        assert (await restored.read("node", "node"))["state"] == "revoked"
        assert (await restored.read("node_resource", "desktop"))["state"] == "quarantined"
        assert (await restored.read("node_job", "job"))["state"] == "execution_unknown"
        assert (await restored.read("delivery_intent", "delivery"))["status"] == "execution_unknown"
        assert (await restored.read("cross_scenario", "scene"))["terminal"]
        assert await restored.read("task_authorization", "authority") is None
        process = await restored.read("supervised_attempt", "attempt")
        for field, expected in {
            'boot_identity_source': 'macos_bootsessionuuid',
            'process_birth_source': 'macos_proc_bsdinfo',
            'process_birth_fingerprint': canonical_digest('old-process-birth'),
        }.items():
            assert process['restore_previous_identity'][field] == expected
            assert process[field] is None
        handle = await Supervisor(restored, destination).inspect("attempt")
        assert handle.state == "execution_unknown" and handle.pid is None
        assert not (destination / "supervisor/attempt/go.json").exists()
        assert not (destination / "supervisor/attempt/launch.json").exists()
        assert not (destination / ".env").exists()
    finally:
        await restored.close()
    for root in (env["backup"], destination):
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        for path in root.rglob("*"):
            if path.is_symlink():
                continue
            assert not stat.S_IMODE(path.stat().st_mode) & 0o077
            if "pki" in path.parts and path.is_file():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("location", ["node_artifact", "model_receipt", "git_object", "database"])
async def test_corruption_is_rejected_before_destination_is_created(checkpoint, tmp_path, location):
    backup = checkpoint["backup"]
    if location == "node_artifact":
        path = next((backup / "managed/nodes/artifacts/objects").iterdir())
    elif location == "model_receipt":
        path = backup / "managed/model_invocations/call.sse"
    elif location == "git_object":
        path = next(p for p in (backup / "managed/workspaces").rglob("*") if p.is_file() and "objects" in p.parts)
    else:
        path = backup / "store/state/agentflow.sqlite3"
    path.write_bytes(b"corrupt")
    destination = tmp_path / "must-remain-absent"
    with pytest.raises(DomainError, match="hashes do not match"):
        await ApplicationBackup.restore(backup, destination)
    assert not destination.exists()


async def test_backup_and_restore_do_not_overwrite_existing_paths(checkpoint, tmp_path):
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    marker = occupied / "user-file"
    marker.write_text("preserve")
    with pytest.raises(DomainError):
        await ApplicationBackup(checkpoint["store"], checkpoint["data"]).create(occupied)
    with pytest.raises(DomainError):
        await ApplicationBackup.restore(checkpoint["backup"], occupied)
    assert marker.read_text() == "preserve"
    with pytest.raises(DomainError):
        await ApplicationBackup(checkpoint["store"], checkpoint["data"]).create(checkpoint["data"] / "nested-backup")


async def test_escaping_managed_symlink_is_never_followed(tmp_path):
    data, external = tmp_path / "data", tmp_path / "outside"
    external.mkdir()
    (external / "private").write_text("outside credential contents")
    store = Store(data)
    await store.start()
    try:
        (data / "workspaces").mkdir()
        (data / "workspaces/escape").symlink_to(external, target_is_directory=True)
        destination = tmp_path / "refused"
        with pytest.raises(DomainError, match="escapes"):
            await ApplicationBackup(store, data).create(destination)
        assert (destination / ".incomplete").exists()
        assert not (destination / "managed/workspaces/escape").exists()
        assert (external / "private").read_text() == "outside credential contents"
    finally:
        await store.close()


async def test_missing_referenced_native_evidence_cannot_produce_complete_backup(tmp_path):
    store = Store(tmp_path / "data")
    await store.start()
    try:
        await store.command("seed", "missing", {}, lambda tx: tx.put("node_artifact", "missing", {
            "state": "complete", "digest": canonical_digest("missing"), "size": 7}))
        destination = tmp_path / "refused"
        with pytest.raises(DomainError, match="missing or corrupt"):
            await ApplicationBackup(store, store.data_dir).create(destination)
        assert (destination / ".incomplete").exists()
    finally:
        await store.close()


async def test_owner_parent_alias_is_canonicalized_but_backup_root_symlink_rejected(tmp_path):
    parent = tmp_path / 'real-parent'
    parent.mkdir()
    alias = tmp_path / 'directory-alias'
    alias.symlink_to(parent, target_is_directory=True)
    store = Store(parent / 'data')
    await store.start()
    try:
        artifacts = LocalArtifactStore(store.data_dir / 'artifacts')
        blob = await artifacts.put_bytes(b'original framework evidence')
        created = await ApplicationBackup(store, store.data_dir).create(alias / 'backup')
        assert Path(created['path']) == parent / 'backup'
    finally:
        await store.close()
    result = await ApplicationBackup.restore(alias / 'backup', alias / 'restored')
    assert Path(result['path']) == parent / 'restored'
    restored = LocalArtifactStore(parent / 'restored/artifacts')
    assert await restored.read(blob['id']) == b'original framework evidence'
    (parent / 'backup-link').symlink_to(parent / 'backup', target_is_directory=True)
    with pytest.raises(DomainError, match='root must be a real directory'):
        await ApplicationBackup.restore(parent / 'backup-link', parent / 'refused')


async def test_restored_product_preparation_and_preview_never_auto_resume(tmp_path):
    data = tmp_path / 'product-control'
    store = Store(data)
    await store.start()
    try:
        export = data / 'product_exports/example'
        export.mkdir(parents=True)
        (export / 'product.zip').write_bytes(b'fixture archive persistence, not product acceptance')
        def seed(tx):
            tx.put('product', 'product', {'state': 'preparing', 'run_id': None})
            tx.put('product_launch', 'product', {'state': 'starting', 'url': 'http://127.0.0.1:12345'})
            tx.put('local_execution', 'managed-local', {'state': 'ready'})
            return {}
        await store.command('fixture', 'products', {}, seed)
        await ApplicationBackup(store, data).create(tmp_path / 'product-backup')
    finally:
        await store.close()
    await ApplicationBackup.restore(tmp_path / 'product-backup', tmp_path / 'product-restored')
    restored = Store(tmp_path / 'product-restored')
    await restored.start()
    try:
        assert (await restored.read('product', 'product'))['state'] == 'blocked'
        assert (await restored.read('product_launch', 'product'))['state'] == 'execution_unknown'
        assert (await restored.read('local_execution', 'managed-local'))['state'] == 'blocked'
        assert (tmp_path / 'product-restored/product_exports/example/product.zip').read_bytes().startswith(b'fixture')
    finally:
        await restored.close()

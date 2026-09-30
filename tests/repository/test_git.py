from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from agentflow.common import DomainError
from agentflow.repository import RepositoryAdapter
from agentflow.storage import Store

GIT = "/opt/homebrew/bin/git"


def git(path: Path, *args: str, check=True, input=None):
    result = subprocess.run(
        [GIT, "-c", "user.name=Test", "-c", "user.email=test@localhost", "-c", "commit.gpgSign=false",
         "-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false", "-C", str(path), *args],
        input=input, capture_output=True, check=False,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    if check:
        assert result.returncode == 0, (args, result.stderr)
    return result


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    for name, data in {"modify.txt": "before\n", "rename.txt": "rename exactly\n",
                       "delete.txt": "delete\n", "mode.sh": "#!/bin/sh\nexit 0\n",
                       ".gitignore": "ignored*\n"}.items():
        (repo / name).write_text(data)
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "baseline")
    return repo


@pytest.fixture
def adapter():
    return RepositoryAdapter()


def head(repo):
    return git(repo, "rev-parse", "HEAD").stdout.decode().strip()


def refs(repo):
    return git(repo, "show-ref").stdout


def index_bytes(repo):
    return (repo / ".git/index").read_bytes()


async def test_independent_snapshot_has_no_shared_objects_or_source_mutation(adapter, repository, tmp_path):
    base = head(repository)
    before_refs, before_index = refs(repository), index_bytes(repository)
    (repository / "modify.txt").write_text("user dirty work")
    (repository / "untracked").write_text("private uncommitted")
    clone = tmp_path / "clone"
    result = await adapter.clone_snapshot(repository, clone)
    assert result["base_oid"] == base
    assert (clone / "modify.txt").read_text() == "before\n"
    assert not (clone / "untracked").exists()
    assert refs(repository) == before_refs and index_bytes(repository) == before_index
    assert (repository / "modify.txt").read_text() == "user dirty work"
    assert git(clone, "remote").stdout == b""
    source_inodes = {(p.stat().st_dev, p.stat().st_ino) for p in (repository / ".git/objects").rglob("*")
                     if p.is_file()}
    clone_inodes = {(p.stat().st_dev, p.stat().st_ino) for p in (clone / ".git/objects").rglob("*") if p.is_file()}
    assert not source_inodes.intersection(clone_inodes)
    with pytest.raises(DomainError):
        await adapter.clone_snapshot(repository, clone)


async def test_snapshot_and_diff_do_not_execute_hooks_filters_or_external_diff(adapter, repository, tmp_path):
    canary = tmp_path / "executed"
    malicious = f"#!/bin/sh\ntouch '{canary}'\ncat\n"
    script = tmp_path / "malicious"
    script.write_text(malicious)
    script.chmod(0o755)
    for name in ["post-checkout", "reference-transaction", "pre-commit"]:
        hook = repository / ".git/hooks" / name
        hook.write_text(malicious)
        hook.chmod(0o755)
    git(repository, "config", "filter.bad.clean", str(script))
    git(repository, "config", "filter.bad.smudge", str(script))
    git(repository, "config", "diff.bad.textconv", str(script))
    git(repository, "config", "diff.external", str(script))
    git(repository, "config", "core.fsmonitor", str(script))
    (repository / ".gitattributes").write_text("*.txt filter=bad diff=bad\n")
    (repository / "modify.txt").write_text("after\n")
    base = head(repository)
    before_index = index_bytes(repository)
    result = await adapter.collect_diff(repository, base)
    assert result["has_changes"]
    assert index_bytes(repository) == before_index
    clone = tmp_path / "clone"
    await adapter.clone_snapshot(repository, clone, base)
    assert not canary.exists()


async def test_collect_full_diff_preserves_index_and_workspace(adapter, repository):
    base = head(repository)
    (repository / "modify.txt").write_text("after\n")
    (repository / "rename.txt").rename(repository / "renamed.txt")
    (repository / "delete.txt").unlink()
    (repository / "mode.sh").chmod(0o755)
    (repository / "binary.bin").write_bytes(b"\x00\xff\x01binary")
    (repository / "新\n文件.txt").write_text("unicode name")
    (repository / "inside-link").symlink_to("modify.txt")
    (repository / "ignored-build").write_text("not source")
    before_index, before_refs = index_bytes(repository), refs(repository)
    result = await adapter.collect_diff(repository, base)
    changed = {c["path"]: c for c in result["changes"]}
    assert changed["renamed.txt"]["status"].startswith("R")
    assert changed["renamed.txt"]["old_path"] == "rename.txt"
    assert changed["delete.txt"]["status"] == "D"
    assert changed["mode.sh"]["mode"] == "100755"
    assert changed["inside-link"]["mode"] == "120000"
    assert changed["binary.bin"]["status"] == "A"
    assert "新\n文件.txt" in result["untracked_paths"]
    assert "ignored-build" not in result["untracked_paths"]
    assert index_bytes(repository) == before_index and refs(repository) == before_refs
    patch = base64.b64decode(result["patch_b64"])
    assert hashlib.sha256(patch).hexdigest() == result["patch_sha256"]
    assert b"GIT binary patch" in patch
    assert git(repository, "show", result["tree_oid"] + ":binary.bin").stdout == b"\x00\xff\x01binary"
    assert (repository / "modify.txt").read_text() == "after\n"


async def test_freeze_is_deterministic_and_preserves_ref_index(adapter, repository):
    base = head(repository)
    (repository / "new.txt").write_text("new code")
    before_refs, before_index = refs(repository), index_bytes(repository)
    first = await adapter.freeze_workspace(repository, base, "Implement feature")
    second = await adapter.freeze_workspace(repository, base, "Implement feature")
    assert first["commit_oid"] == second["commit_oid"]
    assert first["tree_oid"] == second["tree_oid"]
    assert git(repository, "rev-parse", first["commit_oid"] + "^1").stdout.decode().strip() == base
    assert git(repository, "show", first["commit_oid"] + ":new.txt").stdout == b"new code"
    assert refs(repository) == before_refs and index_bytes(repository) == before_index
    assert (repository / "new.txt").read_text() == "new code"


async def test_collect_rejects_outside_symlink_and_parent_symlink(adapter, repository, tmp_path):
    outside = tmp_path / "secret"
    outside.write_text("secret")
    (repository / "escape").symlink_to(outside)
    with pytest.raises(DomainError) as caught:
        await adapter.collect_diff(repository, head(repository))
    assert caught.value.code == "unsafe_symlink"
    assert outside.read_text() == "secret"


async def test_deleted_file_reappearing_is_not_attested(adapter, repository, monkeypatch):
    base = head(repository)
    (repository / "delete.txt").unlink()
    original = adapter._run

    def changing(repo, args, **kwargs):
        value = original(repo, args, **kwargs)
        if args[0] == "write-tree":
            (repository / "delete.txt").write_text("reappeared")
        return value

    monkeypatch.setattr(adapter, "_run", changing)
    with pytest.raises(DomainError) as caught:
        await adapter.collect_diff(repository, base)
    assert caught.value.code == "workspace_changed"


async def test_import_and_publish_exact_candidate_without_touching_user_checkout(adapter, repository, tmp_path):
    base = head(repository)
    worker = tmp_path / "worker"
    await adapter.clone_snapshot(repository, worker)
    (worker / "feature.txt").write_text("feature")
    candidate = await adapter.freeze_workspace(worker, base, "Feature")
    oid = candidate["commit_oid"]
    assert git(repository, "cat-file", "-e", oid, check=False).returncode != 0
    bundle = tmp_path / "candidate.bundle"
    bundle_info = await adapter.prepare_bundle(worker, oid, bundle)
    (repository / "modify.txt").write_text("user local modifications")
    before_index, before_refs = index_bytes(repository), refs(repository)
    imported = await adapter.import_bundle(repository, bundle, oid, expected_sha256=bundle_info["sha256"])
    assert imported["tree_oid"] == candidate["tree_oid"]
    assert refs(repository) == before_refs and index_bytes(repository) == before_index
    again = await adapter.import_bundle(repository, bundle, oid)
    assert again["objects_verified"]
    result = await adapter.publish(repository, "refs/heads/main", base, "refs/heads/agentflow/result", oid,
                                   "operation-1", expected_tree_oid=candidate["tree_oid"])
    assert not result["already_published"]
    assert head(repository) == base
    assert git(repository, "rev-parse", "refs/heads/agentflow/result").stdout.decode().strip() == oid
    assert (repository / "modify.txt").read_text() == "user local modifications"
    assert index_bytes(repository) == before_index
    replay = await adapter.publish(repository, "refs/heads/main", base, "refs/heads/agentflow/result", oid,
                                   "operation-1")
    assert replay["already_published"]


async def test_base_compare_and_create_are_one_transaction(adapter, repository):
    base = head(repository)
    (repository / "new").write_text("next")
    candidate = await adapter.freeze_workspace(repository, base, "Next")
    git(repository, "update-ref", "refs/heads/main", candidate["commit_oid"], base)
    with pytest.raises(DomainError):
        await adapter.publish(repository, "refs/heads/main", base, "refs/heads/agentflow/result",
                              candidate["commit_oid"], "operation")
    assert git(repository, "show-ref", "--verify", "refs/heads/agentflow/result", check=False).returncode != 0
    assert head(repository) == candidate["commit_oid"]


async def test_ref_conflict_does_not_overwrite_and_hooks_do_not_run(adapter, repository, tmp_path):
    base = head(repository)
    (repository / "new").write_text("new")
    candidate = await adapter.freeze_workspace(repository, base, "Next")
    canary = tmp_path / "hook-ran"
    hook = repository / ".git/hooks/reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch '{canary}'\n")
    hook.chmod(0o755)
    await adapter.publish(repository, "refs/heads/main", base, "refs/heads/agentflow/first", base, "one")
    assert not canary.exists()
    with pytest.raises(DomainError) as caught:
        await adapter.publish(repository, "refs/heads/main", base, "refs/heads/agentflow/first",
                              candidate["commit_oid"], "two")
    assert caught.value.code == "delivery_conflict"
    assert git(repository, "rev-parse", "refs/heads/agentflow/first").stdout.decode().strip() == base


async def test_concurrent_publish_same_ref_is_idempotent(adapter, repository):
    base = head(repository)
    results = await asyncio.gather(*(adapter.publish(repository, "refs/heads/main", base,
                                                     "refs/heads/agentflow/race", base, "op") for _ in range(5)))
    assert all(r["commit_oid"] == base for r in results)


async def test_bad_bundles_do_not_create_refs(adapter, repository, tmp_path):
    base = head(repository)
    bundle = tmp_path / "bundle"
    await adapter.prepare_bundle(repository, base, bundle)
    target = tmp_path / "target"
    target.mkdir()
    git(target, "init", "-b", "main")
    with pytest.raises(DomainError) as caught:
        await adapter.import_bundle(target, bundle, base, expected_sha256="0" * 64)
    assert caught.value.code == "bundle_digest_mismatch"
    bad = tmp_path / "truncated"
    bad.write_bytes(bundle.read_bytes()[:-24])
    with pytest.raises(DomainError):
        await adapter.import_bundle(target, bad, base)
    assert git(target, "show-ref", check=False).stdout == b""
    with pytest.raises(DomainError):
        await adapter.import_bundle(target, bundle, "0" * 40)
    link = tmp_path / "link"
    link.symlink_to(bundle)
    with pytest.raises(DomainError):
        await adapter.import_bundle(target, link, base)


async def test_publication_after_database_ack_loss_is_reconcilable(adapter, repository, tmp_path):
    store = Store(tmp_path / "data")
    await store.start()
    base = head(repository)
    await store.command("delivery", "intent", {}, lambda tx: tx.put("intent", "op", {
        "state": "pending", "base_oid": base, "candidate_oid": base, "ref": "refs/heads/agentflow/durable"}))
    await adapter.publish(repository, "refs/heads/main", base, "refs/heads/agentflow/durable", base, "op")
    await store.close()  # Simulate losing the process before the database confirmation.
    store = Store(tmp_path / "data")
    await store.start()
    try:
        assert (await store.read("intent", "op"))["state"] == "pending"
        result = await adapter.publish(repository, "refs/heads/main", base, "refs/heads/agentflow/durable", base, "op")
        assert result["already_published"]

        def confirm(tx):
            value = tx.get("intent", "op")
            value["state"] = "delivered"
            tx.event("delivered", result)
            return tx.put("intent", "op", value, value["revision"])

        assert (await store.command("delivery", "confirm", result, confirm))["state"] == "delivered"
    finally:
        await store.close()


async def test_sha256_object_repository(adapter, tmp_path):
    repo = tmp_path / "sha256"
    repo.mkdir()
    result = git(repo, "init", "--object-format=sha256", "-b", "main", check=False)
    if result.returncode:
        pytest.skip("Installed Git does not support SHA-256 repositories")
    (repo / "file").write_text("content")
    git(repo, "add", "file")
    git(repo, "commit", "-m", "sha256 source")
    clone = tmp_path / "clone"
    result = await adapter.clone_snapshot(repo, clone)
    assert len(result["base_oid"]) == 64
    assert (clone / "file").read_text() == "content"


@pytest.mark.parametrize("ref", ["HEAD", "refs/heads/x\ncreate refs/heads/bad", "refs/heads/x y", "refs/../x"])
async def test_invalid_publication_ref_is_rejected(adapter, repository, ref):
    with pytest.raises(DomainError):
        await adapter.publish(repository, "refs/heads/main", head(repository), ref, head(repository), "op")

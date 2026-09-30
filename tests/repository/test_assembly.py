from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from agentflow.common import DomainError
from agentflow.repository import RepositoryAdapter
from agentflow.repository.assembly import AssemblyManager

GIT = "/opt/homebrew/bin/git"


def git(repo, *args):
    result = subprocess.run([GIT, "-c", "user.name=Test", "-c", "user.email=test@localhost",
        "-c", "commit.gpgSign=false", "-c", f"core.hooksPath={os.devnull}", "-C", str(repo), *args],
        capture_output=True, env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def base(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "shared.txt").write_text("baseline\n")
    git(repo, "add", "shared.txt")
    git(repo, "commit", "-m", "baseline")
    return repo, git(repo, "rev-parse", "HEAD").decode().strip()


async def snapshot(adapter, base, destination, path, content):
    repo, oid = base
    await adapter.clone_snapshot(repo, destination, oid)
    (destination / path).write_text(content)
    result = await adapter.freeze_workspace(destination, oid, "Independent change")
    return {"repository_path": str(destination), "commit_oid": result["commit_oid"], "base_oid": oid}


async def test_real_git_parallel_assembly_keeps_every_branch_and_user_checkout(base, tmp_path):
    adapter = RepositoryAdapter()
    one = await snapshot(adapter, base, tmp_path / "worker-one", "one.py", "ONE = 1\n")
    two = await snapshot(adapter, base, tmp_path / "worker-two", "two.py", "TWO = 2\n")
    source, oid = base
    refs = git(source, "show-ref")
    index = (source / ".git/index").read_bytes()
    (source / "shared.txt").write_text("user's uncommitted edit\n")
    (source / "untracked").write_text("user private work")
    manager = AssemblyManager(tmp_path / "controller", repository=adapter)
    result = await manager.assemble([one, two], source, oid, "assemble-once")
    assembled = Path(result["repository_path"])
    assert assembled.is_relative_to(tmp_path / "controller")
    assert (assembled / "one.py").read_text() == "ONE = 1\n"
    assert (assembled / "two.py").read_text() == "TWO = 2\n"
    assert (assembled / "shared.txt").read_text() == "baseline\n"
    assert not (assembled / "untracked").exists()
    assert set(result["parent_commit_oids"]) == {one["commit_oid"], two["commit_oid"]}
    assert git(assembled, "show", "-s", "--format=%P", result["commit_oid"]).decode().split() == result["parent_commit_oids"]
    assert result["requires_review_and_tests"] is True
    assert (source / "shared.txt").read_text() == "user's uncommitted edit\n"
    assert git(source, "show-ref") == refs and (source / ".git/index").read_bytes() == index
    assert not (assembled / ".git/objects/info/alternates").exists()
    assert git(assembled, "remote") == b""


async def test_conflicting_branches_fail_explicitly_and_do_not_mutate_sources(base, tmp_path):
    adapter = RepositoryAdapter()
    one = await snapshot(adapter, base, tmp_path / "one", "shared.txt", "one\n")
    two = await snapshot(adapter, base, tmp_path / "two", "shared.txt", "two\n")
    manager = AssemblyManager(tmp_path / "controller", adapter)
    for _ in range(2):
        with pytest.raises(DomainError) as caught:
            await manager.assemble([one, two], *base, "conflict")
        assert caught.value.code == "assembly_conflict"
    assert (base[0] / "shared.txt").read_text() == "baseline\n"
    assert (Path(one["repository_path"]) / "shared.txt").read_text() == "one\n"
    assert not list((tmp_path / "controller/assemblies").glob("*/result.json"))


async def test_concurrent_and_restarted_assembly_replays_identical_result(base, tmp_path):
    adapter = RepositoryAdapter()
    one = await snapshot(adapter, base, tmp_path / "one", "one.txt", "one")
    two = await snapshot(adapter, base, tmp_path / "two", "two.txt", "two")
    root = tmp_path / "controller"
    results = await asyncio.gather(*[AssemblyManager(root, adapter).assemble([one, two], *base, "same") for _ in range(4)])
    assert all(value == results[0] for value in results)
    assert await AssemblyManager(root, adapter).assemble([two, one], *base, "same") == results[0]
    with pytest.raises(DomainError) as caught:
        await AssemblyManager(root, adapter).assemble([one], *base, "same")
    assert caught.value.code == "idempotency_conflict"


async def test_duplicate_old_base_and_unrelated_history_are_rejected(base, tmp_path):
    adapter = RepositoryAdapter()
    one = await snapshot(adapter, base, tmp_path / "one", "one.txt", "one")
    manager = AssemblyManager(tmp_path / "controller", adapter)
    with pytest.raises(DomainError, match="only once"):
        await manager.assemble([one, one], *base, "duplicate")
    with pytest.raises(DomainError, match="exact requested base"):
        await manager.assemble([{**one, "base_oid": "0" * 40}], *base, "old")
    other = tmp_path / "unrelated"
    other.mkdir()
    git(other, "init", "-b", "main")
    git(other, "commit", "--allow-empty", "-m", "unrelated root")
    unrelated = {"repository_path": str(other), "commit_oid": git(other, "rev-parse", "HEAD").decode().strip(), "base_oid": base[1]}
    with pytest.raises(DomainError, match="descend"):
        await manager.assemble([unrelated], *base, "unrelated")


async def test_assembly_does_not_run_source_hooks_filters_or_merge_drivers(base, tmp_path):
    adapter = RepositoryAdapter()
    one = await snapshot(adapter, base, tmp_path / "one", "one.txt", "one")
    two = await snapshot(adapter, base, tmp_path / "two", "two.txt", "two")
    for record in (one, two):
        worker = Path(record["repository_path"])
        (worker / ".gitattributes").write_text("*.txt filter=bad merge=bad\n")
        frozen = await adapter.freeze_workspace(worker, base[1], "Snapshot with declared attributes")
        record["commit_oid"] = frozen["commit_oid"]
    canary = tmp_path / "executed"
    script = tmp_path / "malicious"
    script.write_text(f"#!/bin/sh\ntouch '{canary}'\ncat\n")
    script.chmod(0o755)
    for repo in [base[0], Path(one["repository_path"]), Path(two["repository_path"])]:
        git(repo, "config", "filter.bad.smudge", str(script))
        git(repo, "config", "merge.bad.driver", str(script))
        git(repo, "config", "core.fsmonitor", str(script))
        hook = repo / ".git/hooks/post-checkout"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(script.read_text())
        hook.chmod(0o755)
    result = await AssemblyManager(tmp_path / "controller", adapter).assemble([one, two], *base, "safe")
    assert result["commit_oid"] and not canary.exists()


async def test_assembly_staging_cannot_be_inside_user_repository(base, tmp_path):
    adapter = RepositoryAdapter()
    one = await snapshot(adapter, base, tmp_path / "one", "one.txt", "one")
    with pytest.raises(DomainError, match="outside source"):
        await AssemblyManager(base[0] / "controller", adapter).assemble([one], *base, "bad")
    assert not (base[0] / "controller").exists()


async def test_completion_receipt_loss_is_recovered_without_remerging(base, tmp_path, monkeypatch):
    adapter = RepositoryAdapter()
    one = await snapshot(adapter, base, tmp_path / "one", "one.txt", "one")
    manager = AssemblyManager(tmp_path / "controller", adapter)
    result = await manager.assemble([one], *base, "receipt-loss")
    (Path(result["repository_path"]).parent / "result.json").unlink()

    def never_remerge(*args, **kwargs):
        raise AssertionError("Recovery must use the retained verified completion receipt")

    monkeypatch.setattr(adapter, "_clone_snapshot", never_remerge)
    recovered = await AssemblyManager(tmp_path / "controller", adapter).assemble([one], *base, "receipt-loss")
    assert recovered == result


async def test_interrupted_private_build_can_be_rebuilt_without_source_changes(base, tmp_path, monkeypatch):
    adapter = RepositoryAdapter()
    one = await snapshot(adapter, base, tmp_path / "one", "one.txt", "one")
    two = await snapshot(adapter, base, tmp_path / "two", "two.txt", "two")
    original = adapter._run
    before = git(base[0], "show-ref")

    def interrupted(repo, args, **kwargs):
        if "merge-tree" in args:
            raise RuntimeError("Simulated controller interruption")
        return original(repo, args, **kwargs)

    monkeypatch.setattr(adapter, "_run", interrupted)
    manager = AssemblyManager(tmp_path / "controller", adapter)
    with pytest.raises(RuntimeError, match="interruption"):
        await manager.assemble([one, two], *base, "interrupted")
    monkeypatch.setattr(adapter, "_run", original)
    result = await AssemblyManager(tmp_path / "controller", adapter).assemble([one, two], *base, "interrupted")
    assert (Path(result["repository_path"]) / "one.txt").read_text() == "one"
    assert (Path(result["repository_path"]) / "two.txt").read_text() == "two"
    assert git(base[0], "show-ref") == before


async def test_case_collisions_cannot_silently_drop_a_branch_on_case_insensitive_filesystem(base, tmp_path):
    probe = tmp_path / "CaseProbe"
    probe.write_text("probe")
    case_insensitive = (tmp_path / "caseprobe").exists()
    probe.unlink()
    if not case_insensitive:
        pytest.skip("This filesystem faithfully represents case-distinct filenames")
    adapter = RepositoryAdapter()
    one = await snapshot(adapter, base, tmp_path / "one", "Same.txt", "upper case branch")
    two = await snapshot(adapter, base, tmp_path / "two", "same.txt", "lower case branch")
    with pytest.raises(DomainError) as caught:
        await AssemblyManager(tmp_path / "controller", adapter).assemble([one, two], *base, "case-collision")
    assert caught.value.code in {"assembly_materialization_mismatch", "git_error"}
    assert not list((tmp_path / "controller/assemblies").glob("*/result.json"))

from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

from agentflow.common import DomainError
from agentflow.storage import LocalArtifactStore, Store


@pytest.fixture
def artifacts(tmp_path):
    return LocalArtifactStore(tmp_path / "objects")


async def test_content_identity_empty_and_parallel_dedup(artifacts):
    data = b"\x00artifact\xff"
    results = await asyncio.gather(*(artifacts.put_bytes(data) for _ in range(30)))
    assert len({r["id"] for r in results}) == 1
    assert results[0]["sha256"] == hashlib.sha256(data).hexdigest()
    assert await artifacts.read(results[0]["id"]) == data
    assert await artifacts.verify(results[0]["id"]) == results[0]
    empty = await artifacts.put_bytes(b"")
    assert empty["size"] == 0 and await artifacts.read(empty["id"]) == b""
    assert list((artifacts.root / ".tmp").iterdir()) == []


async def test_file_is_copied_not_linked(artifacts, tmp_path):
    source = tmp_path / "input"
    source.write_bytes(b"original")
    result = await artifacts.put_file(source, media_type="application/octet-stream")
    assert os.stat(source).st_ino != os.stat(result["path"]).st_ino
    source.write_bytes(b"modified")
    assert await artifacts.read(result["id"]) == b"original"


@pytest.mark.parametrize("id", ["../x", "/tmp/x", "sha256:../x", "0" * 63, "A" * 64, "0" * 64 + "/x"])
async def test_invalid_ids(artifacts, id):
    with pytest.raises(DomainError) as caught:
        await artifacts.read(id)
    assert caught.value.code == "invalid_artifact_id"


async def test_corruption_is_never_returned_or_silently_repaired(artifacts):
    result = await artifacts.put_bytes(b"correct")
    from pathlib import Path

    Path(result["path"]).write_bytes(b"corrupt")
    for action in [lambda: artifacts.read(result["id"]), lambda: artifacts.verify(result["id"]),
                   lambda: artifacts.put_bytes(b"correct")]:
        with pytest.raises(DomainError) as caught:
            await action()
        assert caught.value.code == "artifact_corrupt"


async def test_import_rejects_links_and_special_files(artifacts, tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"secret")
    link = tmp_path / "link"
    link.symlink_to(target)
    parent = tmp_path / "parent"
    parent.symlink_to(tmp_path, target_is_directory=True)
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    for path in [link, parent / "target", fifo]:
        with pytest.raises(DomainError):
            await artifacts.put_file(path)


async def test_object_and_prefix_symlink_rejected(artifacts, tmp_path):
    result = await artifacts.put_bytes(b"content")
    from pathlib import Path

    path = Path(result["path"])
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"content")
    path.symlink_to(outside)
    with pytest.raises(DomainError):
        await artifacts.read(result["id"])
    path.unlink()
    prefix = path.parent
    prefix.rmdir()
    prefix.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DomainError):
        await artifacts.put_bytes(b"content")


async def test_size_limit_and_temp_cleanup(tmp_path):
    artifacts = LocalArtifactStore(tmp_path / "store", max_bytes=3)
    with pytest.raises(DomainError) as caught:
        await artifacts.put_bytes(b"1234")
    assert caught.value.status == 413
    assert list((artifacts.root / ".tmp").iterdir()) == []
    assert (await artifacts.put_bytes(b"123"))["size"] == 3


async def test_subset_backup_and_corrupt_backup_marker(artifacts, tmp_path):
    a = await artifacts.put_bytes(b"one")
    await artifacts.put_bytes(b"two")
    result = await artifacts.backup(tmp_path / "subset", [a["id"]])
    assert result["count"] == 1
    assert await LocalArtifactStore(tmp_path / "subset").read(a["id"]) == b"one"
    from pathlib import Path

    Path(a["path"]).write_bytes(b"broken")
    with pytest.raises(DomainError):
        await artifacts.backup(tmp_path / "broken")
    assert (tmp_path / "broken/.incomplete").exists()
    assert not (tmp_path / "broken/manifest.json").exists()
    with pytest.raises(DomainError) as caught:
        await Store(tmp_path / "broken").start()
    assert caught.value.code == "incomplete_backup"


async def test_source_mutation_during_import_is_rejected(artifacts, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"one")
    original = artifacts._put

    def modifying(stream, media_type):
        result = original(stream, media_type)
        source.write_bytes(b"second version")
        return result

    monkeypatch.setattr(artifacts, "_put", modifying)
    with pytest.raises(DomainError) as caught:
        await artifacts.put_file(source)
    assert caught.value.code == "source_changed"


async def test_temp_directory_symlink_rejected(artifacts, tmp_path):
    (artifacts.root / ".tmp").rmdir()
    (artifacts.root / ".tmp").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DomainError):
        await artifacts.put_bytes(b"x")

"""Content-addressed local artifacts with exclusive atomic publication.

The store owns its directories; callers address bytes by digest, never by paths.
POSIX directory descriptors prevent symlink substitution within the object tree.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import stat
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

from agentflow.common import DomainError, canonical_json, utc_now

from .store import _fsync_directory, _private_directory

_DIGEST = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
_CHUNK = 1024 * 1024


def _parse_id(id: str) -> str:
    match = _DIGEST.fullmatch(id) if isinstance(id, str) else None
    if match is None:
        raise DomainError("invalid_artifact_id", "Expected a SHA-256 artifact id", 422)
    return match.group(1)


def _directory_fd(path: Path) -> int:
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise DomainError("unsafe_path", "Artifact directory is missing or unsafe", 422) from exc


def _file_fd(path: Path) -> int:
    """Reject symlinks in an import path, including intermediate directories."""
    path = path.absolute()
    if ".." in path.parts:
        raise DomainError("unsafe_path", "Parent traversal is not allowed", 422)
    directory = _directory_fd(Path(path.anchor))
    try:
        for component in path.parts[1:-1]:
            try:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=directory)
            except OSError as exc:
                raise DomainError("unsafe_path", "Import path contains an unsafe directory", 422) from exc
            os.close(directory)
            directory = next_fd
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except OSError as exc:
            raise DomainError("unsafe_path", "Import file is missing or a symbolic link", 422) from exc
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise DomainError("unsafe_path", "Only regular files can be imported", 422)
        return fd
    finally:
        os.close(directory)


class LocalArtifactStore:
    def __init__(self, root: Path, *, max_bytes: int = 1024 * 1024 * 1024):
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if os.name != "posix":
            raise DomainError("unsupported_host", "The local control artifact store requires POSIX", 503)
        self.root = _private_directory(Path(root).absolute())
        self.max_bytes = max_bytes
        _private_directory(self.root / "sha256")
        _private_directory(self.root / ".tmp")

    def _metadata(self, digest: str, size: int, media_type: str | None = None) -> dict:
        result = {"id": f"sha256:{digest}", "sha256": digest, "size": size,
                  "path": str(self.root / "sha256" / digest[:2] / digest)}
        if media_type is not None:
            result["media_type"] = media_type
        return result

    def _object_directory(self, digest: str, *, create: bool) -> int:
        root = _directory_fd(self.root)
        objects = None
        try:
            objects = os.open("sha256", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            if create:
                try:
                    os.mkdir(digest[:2], mode=0o700, dir_fd=objects)
                    os.fsync(objects)
                except FileExistsError:
                    pass
            return os.open(digest[:2], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=objects)
        except FileNotFoundError as exc:
            raise DomainError("artifact_not_found", "Artifact does not exist", 404) from exc
        except OSError as exc:
            raise DomainError("unsafe_path", "Artifact path contains an unsafe directory", 422) from exc
        finally:
            if objects is not None:
                os.close(objects)
            os.close(root)

    def _open_object(self, digest: str) -> BinaryIO:
        directory = self._object_directory(digest, create=False)
        try:
            try:
                fd = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError as exc:
                raise DomainError("artifact_not_found", "Artifact does not exist", 404) from exc
            except OSError as exc:
                raise DomainError("unsafe_path", "Artifact is not a safe regular file", 422) from exc
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.max_bytes:
                os.close(fd)
                raise DomainError("invalid_artifact", "Artifact is not a regular bounded file", 422)
            return os.fdopen(fd, "rb")
        finally:
            os.close(directory)

    def _verify_stream(self, stream: BinaryIO, digest: str, output: BinaryIO | None = None) -> int:
        hasher = hashlib.sha256()
        size = 0
        before = os.fstat(stream.fileno())
        while chunk := stream.read(_CHUNK):
            hasher.update(chunk)
            size += len(chunk)
            if size > self.max_bytes:
                raise DomainError("artifact_too_large", "Artifact exceeds configured size limit", 413)
            if output is not None:
                output.write(chunk)
        after = os.fstat(stream.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ) or hasher.hexdigest() != digest:
            raise DomainError("artifact_corrupt", "Artifact content does not match its identity", 409)
        return size

    def _put(self, source: BinaryIO, media_type: str | None) -> dict:
        root_fd = _directory_fd(self.root)
        try:
            tempdir = os.open(".tmp", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        except OSError as exc:
            raise DomainError("unsafe_path", "Artifact temporary directory is unsafe", 422) from exc
        finally:
            os.close(root_fd)
        name = uuid.uuid4().hex
        size = 0
        hasher = hashlib.sha256()
        directory = None
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=tempdir)
            with os.fdopen(fd, "wb") as target:
                while chunk := source.read(_CHUNK):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise DomainError("artifact_too_large", "Artifact exceeds configured size limit", 413)
                    hasher.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            digest = hasher.hexdigest()
            directory = self._object_directory(digest, create=True)
            try:
                # link is an atomic no-replace publication inside this private store;
                # imported source files are always copied and are never hard-linked.
                os.link(name, digest, src_dir_fd=tempdir, dst_dir_fd=directory, follow_symlinks=False)
                os.fsync(directory)
            except FileExistsError:
                with self._open_object(digest) as existing:
                    self._verify_stream(existing, digest)
            return self._metadata(digest, size, media_type)
        finally:
            if directory is not None:
                os.close(directory)
            try:
                os.unlink(name, dir_fd=tempdir)
                os.fsync(tempdir)
            except FileNotFoundError:
                pass
            os.close(tempdir)

    async def put_bytes(self, data: bytes, *, media_type: str | None = None) -> dict:
        if not isinstance(data, bytes):
            raise DomainError("invalid_artifact", "Artifact data must be bytes", 422)
        return await asyncio.to_thread(self._put, io.BytesIO(data), media_type)

    async def put_file(self, path: Path, *, media_type: str | None = None) -> dict:
        return await asyncio.to_thread(self._put_file, Path(path), media_type)

    def _put_file(self, path: Path, media_type: str | None) -> dict:
        with os.fdopen(_file_fd(path), "rb") as source:
            before = os.fstat(source.fileno())
            result = self._put(source, media_type)
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns
            ):
                # The immutable bytes may remain as an unreferenced object; never return a successful import.
                raise DomainError("source_changed", "Import file changed while being copied", 409)
            return result

    async def read(self, id: str) -> bytes:
        return await asyncio.to_thread(self._read, _parse_id(id))

    def _read(self, digest: str) -> bytes:
        result = io.BytesIO()
        with self._open_object(digest) as source:
            self._verify_stream(source, digest, result)
        return result.getvalue()

    async def verify(self, id: str) -> dict:
        return await asyncio.to_thread(self._verify, _parse_id(id))

    def _verify(self, digest: str) -> dict:
        with self._open_object(digest) as source:
            size = self._verify_stream(source, digest)
        return self._metadata(digest, size)

    async def backup(self, destination: Path, artifact_ids: Iterable[str] | None = None) -> dict:
        ids = None if artifact_ids is None else sorted({_parse_id(i) for i in artifact_ids})
        return await asyncio.to_thread(self._backup, Path(destination).absolute(), ids)

    def _backup(self, destination: Path, ids: list[str] | None) -> dict:
        if destination.exists() or destination.is_symlink() or destination.is_relative_to(self.root):
            raise DomainError("unsafe_backup", "Artifact backup must be a new directory outside the store", 409)
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        marker = destination / ".incomplete"
        marker.write_text("artifact backup in progress")
        backup_store = LocalArtifactStore(destination, max_bytes=self.max_bytes)
        if ids is None:
            ids = []
            for directory in (self.root / "sha256").iterdir():
                if directory.is_symlink() or not directory.is_dir():
                    raise DomainError("unsafe_path", "Invalid artifact prefix directory", 422)
                for path in directory.iterdir():
                    digest = _parse_id(path.name)
                    if digest[:2] != directory.name:
                        raise DomainError("artifact_corrupt", "Artifact is in the wrong prefix directory", 409)
                    ids.append(digest)
        records = []
        for digest in sorted(ids):
            with self._open_object(digest) as source:
                # Verify before copying; the destination content hash is checked again after copying.
                self._verify_stream(source, digest)
                source.seek(0)
                copied = backup_store._put(source, None)
                if copied["sha256"] != digest:
                    raise DomainError("artifact_corrupt", "Artifact changed during backup", 409)
                records.append({k: copied[k] for k in ("id", "sha256", "size")})
        result = {"created_at": utc_now(), "artifacts": records, "count": len(records)}
        with (destination / "manifest.json").open("x", encoding="utf-8") as output:
            output.write(canonical_json(result))
            output.flush()
            os.fsync(output.fileno())
        marker.unlink()
        _fsync_directory(destination)
        return result

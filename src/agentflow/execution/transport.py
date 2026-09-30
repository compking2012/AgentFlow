"""Bounded artifact transfer, immutable completion, and safe archive extraction."""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tarfile
from pathlib import Path

from agentflow.common import DomainError, utc_now
from agentflow.execution.manifests import file_digest
from agentflow.execution.models import new_id

MAX_CHUNK_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


def safe_extract_tar(archive: Path, destination: Path, *, maximum_bytes: int = 512 * 1024 * 1024,
                     maximum_files: int = 20000) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    base = destination.resolve()
    total = 0
    with tarfile.open(archive, "r:*") as stream:
        members = []
        for member in stream:
            if len(members) >= maximum_files:
                raise DomainError("archive_limit", "Archive file count exceeds limit", 413)
            members.append(member)
        seen = set()
        for item in members:
            name = Path(item.name)
            if name.is_absolute() or ".." in name.parts or not (item.isfile() or item.isdir()):
                raise DomainError("unsafe_archive", "Archive links, traversal and special files are forbidden", 422)
            output = (base / name).resolve()
            if not output.is_relative_to(base) or str(output) in seen:
                raise DomainError("unsafe_archive", "Archive has escaping or duplicate paths", 422)
            seen.add(str(output))
            total += item.size
            if total > maximum_bytes:
                raise DomainError("archive_limit", "Unpacked archive exceeds limit", 413)
        stream.extractall(base, members=members, filter="data")


class ArtifactTransport:
    def __init__(self, store, directory: Path):
        self.store = store
        self.directory = directory
        (directory / "staging").mkdir(parents=True, exist_ok=True, mode=0o700)
        (directory / "objects").mkdir(parents=True, exist_ok=True, mode=0o700)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, upload_id: str) -> asyncio.Lock:
        return self._locks.setdefault(upload_id, asyncio.Lock())

    def object_path(self, digest: str) -> Path:
        if not digest.startswith("sha256:") or len(digest) != 71 or any(c not in "0123456789abcdef" for c in digest[7:]):
            raise DomainError("invalid_digest", "Invalid content digest", 422)
        return self.directory / "objects" / digest[7:]

    async def register_input(self, path: Path, expected_digest: str, *, artifact_id: str | None = None) -> dict:
        """Trusted controller-only registration; never expose an arbitrary-path node route."""
        if file_digest(path, maximum_bytes=MAX_ARTIFACT_BYTES) != expected_digest:
            raise DomainError("artifact_digest_mismatch", "Input artifact differs from its frozen digest", 422)
        artifact_id = artifact_id or new_id()
        output = self.object_path(expected_digest)
        if not output.exists():
            tmp = self.directory / "staging" / new_id()
            shutil.copyfile(path, tmp)
            if file_digest(tmp) != expected_digest:
                tmp.unlink(missing_ok=True)
                raise DomainError("artifact_changed_during_import", "Input changed while being copied")
            with tmp.open("rb") as file:
                os.fsync(file.fileno())
            os.replace(tmp, output)
            output.chmod(0o400)
        if file_digest(output) != expected_digest:
            raise DomainError("artifact_corrupt", "An existing content object is corrupt")
        body = {"artifact_version_id": artifact_id, "digest": expected_digest, "size": output.stat().st_size,
                "state": "complete", "kind": "input", "created_at": utc_now()}

        def write(tx):
            return tx.put("node_artifact", artifact_id, body)
        return await self.store.command("node_artifact", artifact_id,
                                        {k: v for k, v in body.items() if k != "created_at"}, write)

    async def begin(self, node_id: str, job_id: str, digest: str, size: int, name: str,
                    idempotency_key: str, maximum_bytes: int = MAX_ARTIFACT_BYTES) -> dict:
        self.object_path(digest)
        if not 0 <= size <= min(maximum_bytes, MAX_ARTIFACT_BYTES):
            raise DomainError("artifact_limit", "Artifact size exceeds the job transfer limit", 413)
        if not name or len(name) > 255 or Path(name).name != name or name in {".", ".."} or "/" in name or "\\" in name:
            raise DomainError("unsafe_artifact_name", "Use a file name, not a filesystem path", 422)
        upload_id, artifact_id = new_id(), new_id()
        payload = {"node_id": node_id, "job_id": job_id, "digest": digest, "size": size, "name": name}

        def create(tx):
            reserved = sum(u["size"] for u in tx.list("node_upload") if u["job_id"] == job_id)
            if reserved + size > maximum_bytes:
                raise DomainError("job_artifact_limit", "Combined job uploads exceed reserved output limit", 413)
            return tx.put("node_upload", upload_id, {**payload, "artifact_version_id": artifact_id,
                                                     "received_bytes": 0, "state": "open", "created_at": utc_now()})
        return await self.store.command(f"node_upload:{job_id}", idempotency_key, payload, create)

    async def append(self, upload_id: str, node_id: str, job_id: str, offset: int, data: bytes,
                     chunk_digest: str, idempotency_key: str) -> dict:
        if not data or len(data) > MAX_CHUNK_BYTES or offset < 0:
            raise DomainError("chunk_limit", "Invalid chunk range or size", 413)
        if "sha256:" + hashlib.sha256(data).hexdigest() != chunk_digest:
            raise DomainError("chunk_digest_mismatch", "Chunk digest mismatch", 422)
        async with self._lock(upload_id):
            record = await self.store.read("node_upload", upload_id)
            self._belongs(record, node_id, job_id)
            if record["state"] != "open" or offset + len(data) > record["size"]:
                raise DomainError("upload_conflict", "Upload is closed or chunk exceeds declared size")
            path = self.directory / "staging" / upload_id
            if path.is_symlink():
                raise DomainError("unsafe_upload", "Staging link detected", 403)
            received = record["received_bytes"]
            if offset > received or (offset < received and offset + len(data) > received):
                raise DomainError("chunk_offset_conflict", "Chunk must start at the acknowledged offset")
            mode = "r+b" if path.exists() else "w+b"
            with path.open(mode) as file:
                file.seek(offset)
                existing = file.read(len(data))
                if existing and existing != data[:len(existing)]:
                    raise DomainError("chunk_content_conflict", "Stored chunk differs from retransmission")
                if offset < received and len(existing) != len(data):
                    raise DomainError("upload_corrupt", "Acknowledged bytes are missing")
                file.seek(offset)
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            payload = {"node_id": node_id, "job_id": job_id, "offset": offset,
                       "size": len(data), "digest": chunk_digest}

            def acknowledge(tx):
                row = tx.get("node_upload", upload_id)
                self._belongs(row, node_id, job_id)
                if row["state"] != "open" or row["received_bytes"] not in {offset, offset + len(data)}:
                    raise DomainError("upload_conflict", "Upload changed while committing chunk")
                return tx.put("node_upload", upload_id, {**row, "received_bytes": max(received, offset + len(data))},
                              expected_revision=row["revision"])
            return await self.store.command(f"node_chunk:{upload_id}", idempotency_key, payload, acknowledge)

    async def complete(self, upload_id: str, node_id: str, job_id: str, idempotency_key: str) -> dict:
        async with self._lock(upload_id):
            record = await self.store.read("node_upload", upload_id)
            self._belongs(record, node_id, job_id)
            artifact_id = record["artifact_version_id"]
            if record["state"] == "complete":
                return await self.store.read("node_artifact", artifact_id)
            if record["received_bytes"] != record["size"]:
                raise DomainError("upload_incomplete", "Not all declared artifact bytes have arrived")
            staged = self.directory / "staging" / upload_id
            output = self.object_path(record["digest"])
            if record["size"] == 0 and not staged.exists() and not output.exists():
                staged.touch(mode=0o600, exist_ok=False)
            input_path = staged if staged.exists() else output
            if not input_path.exists() or input_path.stat().st_size != record["size"] or file_digest(input_path) != record["digest"]:
                raise DomainError("artifact_digest_mismatch", "Final artifact size or digest mismatch", 422)
            if staged.exists():
                os.replace(staged, output)
                output.chmod(0o400)

            def commit(tx):
                row = tx.get("node_upload", upload_id)
                self._belongs(row, node_id, job_id)
                body = {"artifact_version_id": artifact_id, "digest": row["digest"], "size": row["size"],
                        "name": row["name"], "state": "complete", "kind": "node_output",
                        "node_id": node_id, "job_id": job_id, "created_at": utc_now()}
                artifact = tx.put("node_artifact", artifact_id, body)
                tx.put("node_upload", upload_id, {**row, "state": "complete"}, expected_revision=row["revision"])
                tx.event("node.artifact.completed", {"artifact_id": artifact_id, "job_id": job_id})
                return artifact
            return await self.store.command(f"node_upload_complete:{upload_id}", idempotency_key,
                                            {"node_id": node_id, "job_id": job_id, "digest": record["digest"]}, commit)

    async def read_chunk(self, artifact_id: str, allowed_ids: list[str], offset: int, length: int) -> dict:
        if artifact_id not in allowed_ids:
            raise DomainError("artifact_forbidden", "Artifact is outside this job's download allowlist", 403)
        if offset < 0 or not 1 <= length <= MAX_CHUNK_BYTES:
            raise DomainError("range_invalid", "Invalid bounded artifact range", 416)
        record = await self.store.read("node_artifact", artifact_id)
        if not record or record["state"] != "complete":
            raise DomainError("artifact_missing", "Artifact has not been verified", 404)
        if offset >= record["size"] and not (offset == 0 and record["size"] == 0):
            raise DomainError("range_invalid", "Offset is beyond the artifact", 416)
        path = self.object_path(record["digest"])
        if path.stat().st_size != record["size"] or (offset == 0 and file_digest(path) != record["digest"]):
            raise DomainError("artifact_corrupt", "Frozen artifact changed on disk", 409)
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(min(length, record["size"] - offset))
        return {"data": data, "offset": offset, "total_size": record["size"], "digest": record["digest"],
                "chunk_digest": "sha256:" + hashlib.sha256(data).hexdigest()}

    @staticmethod
    def _belongs(record, node_id: str, job_id: str) -> None:
        if not record:
            raise DomainError("upload_missing", "Unknown upload", 404)
        if record["node_id"] != node_id or record["job_id"] != job_id:
            raise DomainError("upload_forbidden", "Upload belongs to a different node/job", 403)

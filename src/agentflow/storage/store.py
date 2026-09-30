"""A single SQLite writer actor; accepted commands outlive a cancelled waiter."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sqlite3
import stat
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentflow.common import DomainError, canonical_digest, canonical_json, utc_now

if TYPE_CHECKING:
    from .artifacts import LocalArtifactStore

SCHEMA_VERSION = 1


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(canonical_json(value).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DomainError("invalid_json", "Value must be finite JSON data", 422) from exc


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value:
        raise DomainError("invalid_identifier", f"Invalid {name}", 422)


def _private_directory(path: Path) -> Path:
    if path.is_symlink():
        raise DomainError("unsafe_path", "A storage directory cannot be a symbolic link", 422)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise DomainError("unsafe_path", "Storage path must be a directory", 422)
    return path.resolve()


def _fsync_directory(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class _InstanceLock:
    def __init__(self, path: Path):
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.fd = os.open(path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(self.fd).st_mode):
                raise OSError("lock is not a regular file")
            if os.name == "nt":
                import msvcrt

                if os.fstat(self.fd).st_size == 0:
                    os.write(self.fd, b"0")
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if hasattr(self, "fd"):
                os.close(self.fd)
            raise DomainError("store_locked", "Another process owns this data directory", 409) from exc

    def close(self) -> None:
        if self.fd is None:
            return
        if os.name == "nt":
            import msvcrt

            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        self.fd = None


class Transaction:
    """Only valid synchronously, on the writer thread, during one command."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection
        self._thread = threading.get_ident()
        self._active = True

    def _check(self) -> None:
        if not self._active or threading.get_ident() != self._thread:
            raise DomainError("transaction_closed", "Transaction is outside its command", 409)

    def get(self, kind: str, id: str) -> dict | None:
        self._check()
        _identifier(kind, "kind")
        _identifier(id, "id")
        row = self._connection.execute(
            "SELECT revision, body FROM records WHERE kind=? AND id=?", (kind, id)
        ).fetchone()
        return None if row is None else {**json.loads(row[1]), "id": id, "revision": row[0]}

    def list(self, kind: str) -> list[dict]:
        self._check()
        _identifier(kind, "kind")
        rows = self._connection.execute(
            "SELECT id, revision, body FROM records WHERE kind=? ORDER BY id", (kind,)
        ).fetchall()
        return [{**json.loads(row[2]), "id": row[0], "revision": row[1]} for row in rows]

    def list_linked(self, kind: str, links: dict[str, list[str]]) -> list[dict]:
        """Read the union of explicit record links without materializing unrelated bodies."""
        self._check()
        _identifier(kind, 'kind')
        allowed = {'id', 'run_id', 'iteration_id', 'work_item_id', 'parent_work_item_id', 'attempt_id'}
        if not isinstance(links, dict) or not set(links) <= allowed:
            raise DomainError('invalid_record_links', 'Unsupported record link field', 422)
        clauses, parameters = [], [kind]
        for field, values in sorted(links.items()):
            if not isinstance(values, (list, tuple, set)):
                raise DomainError('invalid_record_links', 'Record links require explicit identities', 422)
            for value in values:
                _identifier(value, 'link identity')
            if not values:
                continue
            # Field names come only from the fixed allowlist; every value is bound.
            expression = 'id' if field == 'id' else f"json_extract(body, '$.{field}')"
            clauses.append(expression + ' IN (SELECT value FROM json_each(?))')
            parameters.append(json.dumps(sorted(set(values))))
        if not clauses:
            return []
        rows = self._connection.execute(
            'SELECT id,revision,body FROM records WHERE kind=? AND (' + ' OR '.join(clauses) + ') ORDER BY id',
            parameters).fetchall()
        return [{**json.loads(row[2]), 'id': row[0], 'revision': row[1]} for row in rows]

    def put(self, kind: str, id: str, body: dict, expected_revision: int | None = None) -> dict:
        self._check()
        _identifier(kind, "kind")
        _identifier(id, "id")
        if not isinstance(body, dict):
            raise DomainError("invalid_record", "Record body must be an object", 422)
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 1
        ):
            raise DomainError("invalid_revision", "Expected revision must be a positive integer", 422)
        clean = _json_copy(body)
        if "id" in clean and clean["id"] != id:
            raise DomainError("invalid_record", "Body id does not match record id", 422)
        if "revision" in clean and clean["revision"] != expected_revision:
            raise DomainError("invalid_revision", "Body revision does not match expected revision", 422)
        clean.pop("id", None)
        clean.pop("revision", None)
        current = self.get(kind, id)
        actual = None if current is None else current["revision"]
        if actual != expected_revision:
            raise DomainError(
                "revision_conflict", "Record changed or already exists", 409,
                {"kind": kind, "id": id, "expected": expected_revision, "actual": actual},
            )
        revision = 1 if actual is None else actual + 1
        if actual is None:
            self._connection.execute(
                "INSERT INTO records(kind,id,revision,body) VALUES(?,?,?,?)",
                (kind, id, revision, canonical_json(clean)),
            )
        else:
            self._connection.execute(
                "UPDATE records SET revision=?,body=? WHERE kind=? AND id=? AND revision=?",
                (revision, canonical_json(clean), kind, id, expected_revision),
            )
        return _json_copy({**clean, "id": id, "revision": revision})

    def event(self, type: str, body: dict, run_id: str | None = None) -> int:
        self._check()
        _identifier(type, "event type")
        if run_id is not None:
            _identifier(run_id, "run id")
        if not isinstance(body, dict):
            raise DomainError("invalid_event", "Event body must be an object", 422)
        cursor = self._connection.execute(
            "INSERT INTO events(type,body,run_id,created_at) VALUES(?,?,?,?)",
            (type, canonical_json(_json_copy(body)), run_id, utc_now()),
        )
        return int(cursor.lastrowid)


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).absolute()
        self.database_path = self.data_dir / "state" / "agentflow.sqlite3"
        self._executor: ThreadPoolExecutor | None = None
        self._connection: sqlite3.Connection | None = None
        self._instance_lock: _InstanceLock | None = None
        self._lifecycle = asyncio.Lock()
        self._running = False

    async def start(self) -> None:
        async with self._lifecycle:
            if self._running:
                return
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agentflow-sqlite")
            try:
                await asyncio.shield(asyncio.wrap_future(self._executor.submit(self._open)))
            except BaseException as exc:
                # Opening continues even if its original waiter is cancelled; cleanup is queued after it.
                await asyncio.shield(asyncio.wrap_future(self._executor.submit(self._close)))
                self._executor.shutdown(wait=True)
                self._executor = None
                if isinstance(exc, sqlite3.DatabaseError):
                    raise DomainError("storage_corrupt", "Database cannot be opened or migrated safely", 500) from exc
                raise
            self._running = True

    def _open(self) -> None:
        self.data_dir = _private_directory(self.data_dir)
        if (self.data_dir / ".incomplete").exists():
            raise DomainError("incomplete_backup", "An incomplete backup cannot be opened as a store", 409)
        state = _private_directory(self.data_dir / "state")
        self.database_path = state / "agentflow.sqlite3"
        self._instance_lock = _InstanceLock(state / ".writer.lock")
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(str(self.database_path) + suffix)
            if path.is_symlink():
                raise DomainError("unsafe_path", "Database files cannot be symbolic links", 422)
        connection = sqlite3.connect(self.database_path, isolation_level=None, timeout=5)
        self._connection = connection
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise DomainError("schema_too_new", "Database needs a newer AgentFlow version", 409)
        if version == 0:
            connection.execute("BEGIN EXCLUSIVE")
            try:
                connection.execute("""CREATE TABLE records(
                    kind TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),
                    body TEXT NOT NULL CHECK(json_valid(body)),PRIMARY KEY(kind,id))""")
                connection.execute("""CREATE TABLE commands(
                    scope TEXT NOT NULL,key TEXT NOT NULL,digest TEXT NOT NULL,
                    result TEXT NOT NULL CHECK(json_valid(result)),created_at TEXT NOT NULL,
                    PRIMARY KEY(scope,key))""")
                connection.execute("""CREATE TABLE events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT NOT NULL,
                    body TEXT NOT NULL CHECK(json_valid(body)),run_id TEXT,created_at TEXT NOT NULL)""")
                connection.execute("CREATE INDEX events_run_id ON events(run_id,id)")
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise DomainError("storage_corrupt", "Database integrity check failed", 500)
        expected_columns = {"records": {"kind", "id", "revision", "body"},
                            "commands": {"scope", "key", "digest", "result", "created_at"},
                            "events": {"id", "type", "body", "run_id", "created_at"}}
        for table, columns in expected_columns.items():
            actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if actual != columns:
                raise DomainError("schema_invalid", "Database schema does not match its version", 500)
        os.chmod(self.database_path, 0o600)
        _fsync_directory(state)

    async def _submit(self, function: Callable, *args: Any) -> Any:
        async with self._lifecycle:
            if not self._running or self._executor is None:
                raise DomainError("store_closed", "Store must be started before use", 503)
            future = self._executor.submit(function, *args)
        return await asyncio.shield(asyncio.wrap_future(future))

    async def close(self) -> None:
        async with self._lifecycle:
            if self._executor is None:
                return
            self._running = False
            executor = self._executor
            try:
                await asyncio.shield(asyncio.wrap_future(executor.submit(self._close)))
            finally:
                executor.shutdown(wait=True)
                self._executor = None

    def _close(self) -> None:
        try:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
        finally:
            if self._instance_lock is not None:
                self._instance_lock.close()
                self._instance_lock = None

    async def command(self, scope: str, key: str, payload: dict,
                      handler: Callable[[Transaction], dict]) -> dict:
        _identifier(scope, "scope")
        _identifier(key, "idempotency key")
        if not isinstance(payload, dict):
            raise DomainError("invalid_payload", "Command payload must be an object", 422)
        digest = canonical_digest(_json_copy(payload))
        return await self._submit(self._command, scope, key, digest, handler)

    def _command(self, scope: str, key: str, digest: str, handler: Callable) -> dict:
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        tx = Transaction(connection)
        try:
            row = connection.execute(
                "SELECT digest,result FROM commands WHERE scope=? AND key=?", (scope, key)
            ).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise DomainError("idempotency_conflict", "Command key has a different request", 409)
                result = json.loads(row[1])
            else:
                result = handler(tx)
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    raise DomainError("invalid_handler", "Command handlers must be synchronous", 422)
                if not isinstance(result, dict):
                    raise DomainError("invalid_result", "Command result must be a JSON object", 422)
                result = _json_copy(result)
                connection.execute(
                    "INSERT INTO commands(scope,key,digest,result,created_at) VALUES(?,?,?,?,?)",
                    (scope, key, digest, canonical_json(result), utc_now()),
                )
            connection.execute("COMMIT")
            return result
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            tx._active = False

    def _read(self, kind: str, id: str | None = None) -> Any:
        tx = Transaction(self._connection)
        try:
            return tx.list(kind) if id is None else tx.get(kind, id)
        finally:
            tx._active = False

    async def read(self, kind: str, id: str) -> dict | None:
        return await self._submit(self._read, kind, id)

    async def list(self, kind: str) -> list[dict]:
        return await self._submit(self._read, kind)

    def _read_linked(self, kind, links):
        tx = Transaction(self._connection)
        try:
            return tx.list_linked(kind, links)
        finally:
            tx._active = False

    async def list_linked(self, kind: str, links: dict[str, list[str]]) -> list[dict]:
        return await self._submit(self._read_linked, kind, _json_copy(links))

    async def record_page(self, kind: str, prefix: str, *, after: str | None = None,
                          before: str | None = None, limit: int = 50, reverse: bool = False) -> list[dict]:
        """Bounded primary-key range, used for append-only attempt traces."""
        _identifier(kind, 'kind')
        _identifier(prefix, 'prefix')
        if type(limit) is not int or not 1 <= limit <= 101 or (after is not None and before is not None):
            raise DomainError('invalid_cursor', 'Invalid record page', 422)
        for cursor in (after, before):
            if cursor is not None and (not isinstance(cursor, str) or not cursor.startswith(prefix)):
                raise DomainError('invalid_cursor', 'Cursor belongs to another record range', 422)
        def read_page():
            query = 'SELECT id,revision,body FROM records WHERE kind=? AND id>=? AND id<?'
            parameters = [kind, prefix, prefix + '\uffff']
            if after is not None:
                query += ' AND id>?'
                parameters.append(after)
            if before is not None:
                query += ' AND id<?'
                parameters.append(before)
            query += ' ORDER BY id ' + ('DESC' if reverse else 'ASC') + ' LIMIT ?'
            rows = self._connection.execute(query, [*parameters, limit]).fetchall()
            return [{**json.loads(body), 'id': identity, 'revision': revision} for identity, revision, body in rows]
        return await self._submit(read_page)

    async def events(self, after: int, run_id: str | None = None, limit: int = 100) -> list[dict]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 10000:
            raise DomainError("invalid_cursor", "Invalid event cursor or limit", 422)
        if run_id is not None:
            _identifier(run_id, "run id")
        return await self._submit(self._events, after, run_id, limit)

    def _events(self, after: int, run_id: str | None, limit: int) -> list[dict]:
        query = "SELECT id,type,body,run_id,created_at FROM events WHERE id>?"
        parameters: list = [after]
        if run_id is not None:
            query += " AND run_id=?"
            parameters.append(run_id)
        rows = self._connection.execute(query + " ORDER BY id LIMIT ?", [*parameters, limit]).fetchall()
        return [{"id": r[0], "sequence": r[0], "type": r[1], "body": json.loads(r[2]),
                 "run_id": r[3], "created_at": r[4]} for r in rows]

    async def backup(self, destination: Path, artifacts: LocalArtifactStore | None = None) -> dict:
        """Create a new restore-ready directory. A backup.json marker denotes completion.

        Artifacts are immutable and never garbage-collected by this module. Copying them after
        the database cut therefore includes every previously committed reference (and may include extras).
        """
        destination = Path(destination).absolute()
        if destination.exists() or destination.is_symlink():
            raise DomainError("backup_exists", "Backup destination must not exist", 409)
        if artifacts is not None and destination.is_relative_to(artifacts.root):
            raise DomainError("unsafe_path", "Backup cannot be inside the artifact store", 422)
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        marker = destination / ".incomplete"
        marker.write_text(uuid.uuid4().hex)
        result = await self._submit(self._backup, destination)
        if artifacts is not None:
            result["artifacts"] = await artifacts.backup(destination / "artifacts")
        encoded = canonical_json(result).encode()
        with (destination / "backup.json").open("xb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        marker.unlink()
        _fsync_directory(destination)
        return result

    def _backup(self, destination: Path) -> dict:
        import hashlib

        state = _private_directory(destination / "state")
        database = state / "agentflow.sqlite3"
        target = sqlite3.connect(database)
        try:
            self._connection.backup(target)
            # A backup is a self-contained database, never a db/WAL pair.
            target.execute("PRAGMA journal_mode=DELETE")
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise DomainError("backup_corrupt", "Backup verification failed", 500)
            watermark = target.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        finally:
            target.close()
        with database.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        os.chmod(database, 0o600)
        _fsync_directory(state)
        return {"schema_version": SCHEMA_VERSION, "database": "state/agentflow.sqlite3",
                "sha256": digest, "event_watermark": watermark, "created_at": utc_now()}

    @staticmethod
    async def restore_backup(backup: Path, destination: Path) -> dict:
        """Restore only to a new directory after checking the recorded database and artifact identities."""
        import hashlib

        from .artifacts import LocalArtifactStore, _file_fd

        backup, destination = Path(backup).absolute(), Path(destination).absolute()
        if backup.is_symlink() or (backup / ".incomplete").exists():
            raise DomainError("incomplete_backup", "Backup is incomplete or unsafe", 409)
        if destination.exists() or destination.is_symlink() or destination.is_relative_to(backup):
            raise DomainError("unsafe_restore", "Restore requires a new directory outside the backup", 409)

        def copy_database() -> dict:
            try:
                with os.fdopen(_file_fd(backup / "backup.json"), "rb") as stream:
                    manifest = json.load(stream)
                if (manifest["database"] != "state/agentflow.sqlite3"
                        or manifest["schema_version"] != SCHEMA_VERSION):
                    raise ValueError("unexpected backup version or database path")
            except (KeyError, TypeError, ValueError) as exc:
                raise DomainError("invalid_backup", "Backup manifest is invalid", 422) from exc
            wal = backup / "state/agentflow.sqlite3-wal"
            if wal.exists() and wal.stat().st_size:
                raise DomainError("backup_corrupt", "Backup must not have an active WAL", 409)
            destination.mkdir(mode=0o700, parents=True, exist_ok=False)
            (destination / ".incomplete").write_text("restore in progress")
            state = _private_directory(destination / "state")
            database = state / "agentflow.sqlite3"
            with os.fdopen(_file_fd(backup / "state/agentflow.sqlite3"), "rb") as source:
                with database.open("xb") as target:
                    digest = hashlib.sha256()
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if digest.hexdigest() != manifest["sha256"]:
                    raise DomainError("backup_corrupt", "Backup database hash does not match", 409)
            os.chmod(database, 0o600)
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            try:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise DomainError("backup_corrupt", "Backup database integrity check failed", 409)
            finally:
                connection.close()
            _fsync_directory(state)
            return manifest

        manifest = await asyncio.to_thread(copy_database)
        count = 0
        if "artifacts" in manifest:
            entries = manifest["artifacts"]["artifacts"]
            ids = [entry["id"] for entry in entries]
            if not (backup / "artifacts").is_dir() or (backup / "artifacts/.incomplete").exists():
                raise DomainError("incomplete_backup", "Artifact backup is incomplete", 409)
            source_store = LocalArtifactStore(backup / "artifacts")
            result = await source_store.backup(destination / "artifacts", ids)
            actual = {entry["id"]: entry for entry in result["artifacts"]}
            if len(actual) != len(entries) or any(actual.get(entry["id"]) != entry for entry in entries):
                raise DomainError("backup_corrupt", "Artifact manifest does not match copied files", 409)
            count = len(entries)
        (destination / ".incomplete").unlink()
        _fsync_directory(destination)
        return {"path": str(destination), "event_watermark": manifest["event_watermark"],
                "schema_version": SCHEMA_VERSION, "artifact_count": count}

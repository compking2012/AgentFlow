"""Durable local job identity; receiving a duplicate assignment never starts again."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import psutil

from agentflow.common import DomainError, canonical_digest, canonical_json, utc_now


class NodeJournal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise DomainError("unsafe_journal", "Node journal cannot be a symlink", 403)
        lock_path = path.with_suffix(path.suffix + ".lock")
        if lock_path.is_symlink():
            raise DomainError("unsafe_journal", "Node journal lock cannot be a symlink", 403)
        self.lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(self.lock_fd).st_size == 0:
                    os.write(self.lock_fd, b"0")
                os.lseek(self.lock_fd, 0, os.SEEK_SET)
                msvcrt.locking(self.lock_fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.lock_fd)
            raise DomainError("node_already_running", "Another node supervisor owns this journal", 409) from exc
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, input_fingerprint TEXT NOT NULL, assignment TEXT NOT NULL,
                state TEXT NOT NULL, pid INTEGER, process_created REAL, process_fingerprint TEXT,
                result TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, type TEXT NOT NULL,
                body TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
                name TEXT PRIMARY KEY, operation_id TEXT NOT NULL, payload TEXT NOT NULL, completed INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
                job_id TEXT PRIMARY KEY, receipt TEXT NOT NULL, cleanup_complete INTEGER NOT NULL DEFAULT 0
            );
        """)
        if os.name != "nt":
            path.chmod(0o600)

    def close(self) -> None:
        self.connection.close()
        os.close(self.lock_fd)

    def next_counter(self, name: str) -> int:
        with self.connection:
            self.connection.execute("INSERT INTO counters VALUES(?,1) ON CONFLICT(name) DO UPDATE SET value=value+1", (name,))
            return self.connection.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()[0]

    def update_assignment(self, assignment: dict) -> None:
        self.record_assignment(assignment)  # Immutable job/attempt/fence/input identities must still match.
        with self.connection:
            self.connection.execute("UPDATE jobs SET assignment=?,updated_at=? WHERE job_id=?",
                                    (canonical_json(assignment), utc_now(), assignment["job_id"]))

    def record_delivery(self, job_id: str, receipt: dict) -> None:
        with self.connection:
            self.connection.execute("INSERT INTO deliveries(job_id,receipt) VALUES(?,?) ON CONFLICT(job_id) DO UPDATE SET receipt=excluded.receipt",
                                    (job_id, canonical_json(receipt)))

    def finish_cleanup(self, job_id: str) -> None:
        with self.connection:
            self.connection.execute("UPDATE deliveries SET cleanup_complete=1 WHERE job_id=?", (job_id,))

    def undelivered_results(self) -> list[dict]:
        rows = self.connection.execute("SELECT j.job_id FROM jobs j LEFT JOIN deliveries d ON j.job_id=d.job_id WHERE j.result IS NOT NULL AND d.job_id IS NULL").fetchall()
        return [self.get(row["job_id"]) for row in rows]

    def pending_cleanups(self) -> list[dict]:
        rows = self.connection.execute("SELECT job_id FROM deliveries WHERE cleanup_complete=0").fetchall()
        return [self.get(row["job_id"]) for row in rows]

    def maintenance_snapshot(self) -> dict:
        """Read durable delivery/cleanup fences without updating job or operation state."""
        rows = self.connection.execute("""
            SELECT j.job_id, d.receipt, d.cleanup_complete, o.payload AS delivered_payload,
                   o.completed AS delivery_operation_complete
            FROM jobs j LEFT JOIN deliveries d ON j.job_id=d.job_id
            LEFT JOIN operations o ON o.name='result:' || j.job_id ORDER BY j.job_id
        """).fetchall()
        jobs = [{**self.get(row['job_id']),
            'delivery_receipt': json.loads(row['receipt']) if row['receipt'] else None,
            'cleanup_complete': row['cleanup_complete'] == 1,
            'delivered_payload': json.loads(row['delivered_payload']) if row['delivered_payload'] else None,
            'delivery_operation_complete': row['delivery_operation_complete'] == 1} for row in rows]
        idle = not any(job['state'] in {'received', 'starting', 'running', 'execution_unknown'}
            or (job['result'] is not None and job['delivery_receipt'] is None)
            or (job['delivery_receipt'] is not None and not job['cleanup_complete']) for job in jobs)
        return {'jobs': jobs, 'idle': idle}

    def _event(self, job_id: str | None, event_type: str, payload: dict) -> None:
        self.connection.execute("INSERT INTO events(job_id,type,body,created_at) VALUES(?,?,?,?)",
                                (job_id, event_type, canonical_json(payload), utc_now()))

    def pending_operation(self, name: str, payload: dict) -> dict:
        from uuid import uuid4
        row = self.connection.execute("SELECT * FROM operations WHERE name=?", (name,)).fetchone()
        if row and not row["completed"]:
            return {"operation_id": row["operation_id"], "payload": json.loads(row["payload"])}
        operation_id = str(uuid4())
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO operations VALUES(?,?,?,0)",
                                    (name, operation_id, canonical_json(payload)))
        return {"operation_id": operation_id, "payload": payload}

    def finish_operation(self, name: str) -> None:
        with self.connection:
            self.connection.execute("UPDATE operations SET completed=1 WHERE name=?", (name,))

    def reject_claim(self, operation_id: str) -> None:
        """Retain the exact claim explicitly rejected by the server before replacing it."""
        with self.connection:
            row = self.connection.execute("SELECT * FROM operations WHERE name='claim' AND completed=0").fetchone()
            if not row or row["operation_id"] != operation_id:
                raise DomainError("claim_operation_changed", "Pending claim identity changed before rejection")
            self._event(None, "claim.rejected", {"operation_id": operation_id,
                        "payload": json.loads(row["payload"]), "reason": "boot_mismatch"})
            self.connection.execute("UPDATE operations SET completed=1 WHERE name='claim'")

    def get_pending_operation(self, name: str) -> dict | None:
        row = self.connection.execute("SELECT * FROM operations WHERE name=? AND completed=0", (name,)).fetchone()
        return {"operation_id": row["operation_id"], "payload": json.loads(row["payload"])} if row else None

    def record_assignment(self, assignment: dict) -> str:
        job_id = assignment["job_id"]
        with self.connection:
            previous = self.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if previous:
                old = json.loads(previous["assignment"])
                if (previous["input_fingerprint"] != assignment["input_fingerprint"]
                        or old["attempt_id"] != assignment["attempt_id"]
                        or old["fencing_token"] != assignment["fencing_token"]):
                    raise DomainError("journal_assignment_conflict", "Same job ID arrived with different immutable identity")
                return "terminal" if previous["state"] in {"completed", "failed", "cancelled"} else "observe_existing"
            self.connection.execute("INSERT INTO jobs(job_id,input_fingerprint,assignment,state,updated_at) VALUES(?,?,?,?,?)",
                                    (job_id, assignment["input_fingerprint"], canonical_json(assignment), "received", utc_now()))
            self._event(job_id, "assignment.received", {"attempt_id": assignment["attempt_id"], "fence": assignment["fencing_token"]})
        return "start_new"

    def mark_starting(self, job_id: str) -> None:
        with self.connection:
            row = self.connection.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or row["state"] != "received":
                raise DomainError("duplicate_start", "Only a newly received job may start")
            self.connection.execute("UPDATE jobs SET state='starting',updated_at=? WHERE job_id=?", (utc_now(), job_id))
            self._event(job_id, "process.start_intent", {})

    def record_process(self, job_id: str, pid: int, fingerprint: str) -> None:
        process = psutil.Process(pid)
        created = process.create_time()
        with self.connection:
            self.connection.execute("UPDATE jobs SET state='running',pid=?,process_created=?,process_fingerprint=?,updated_at=? WHERE job_id=?",
                                    (pid, created, fingerprint, utc_now(), job_id))
            self._event(job_id, "process.identified", {"pid": pid, "created": created, "fingerprint": fingerprint})

    def record_result(self, job_id: str, result: dict) -> None:
        row = self.get(job_id)
        if not row:
            raise DomainError("job_missing", "Cannot record a result without a journaled assignment", 404)
        if row["result"]:
            if canonical_digest(row["result"]) != canonical_digest(result):
                raise DomainError("journal_result_conflict", "A final result is immutable")
            return
        state = "completed" if result.get("execution_status") == "completed" else (
            "cancelled" if result.get("execution_status") == "cancelled" else "failed")
        with self.connection:
            self.connection.execute("UPDATE jobs SET state=?,result=?,updated_at=? WHERE job_id=?",
                                    (state, canonical_json(result), utc_now(), job_id))
            self._event(job_id, "job.result", {"state": state, "result_digest": canonical_digest(result)})

    def get(self, job_id: str) -> dict | None:
        row = self.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["assignment"] = json.loads(result["assignment"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def recoverable(self) -> list[dict]:
        rows = self.connection.execute("SELECT job_id FROM jobs WHERE state IN ('received','starting','running','execution_unknown')").fetchall()
        results = []
        for row in rows:
            job = self.get(row["job_id"])
            alive = False
            if job["pid"] and job["process_created"]:
                try:
                    p = psutil.Process(job["pid"])
                    alive = p.create_time() == job["process_created"] and p.status() != psutil.STATUS_ZOMBIE
                except psutil.NoSuchProcess:
                    pass
                except psutil.AccessDenied:
                    alive = True  # Inability to inspect is not proof that the previous process stopped.
            results.append({**job, "directive": "observe_existing" if alive else "execution_unknown"})
        return results

    @property
    def sequence(self) -> int:
        return self.connection.execute("SELECT coalesce(max(sequence),0) FROM events").fetchone()[0]

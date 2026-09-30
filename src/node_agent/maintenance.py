"""Remove only acknowledged local transfer copies, retaining node evidence and keys.

Sweeps are synchronous and run on the journal-owning daemon thread. There is no
await between the idle check and input-cache unlink, so a new local reader cannot
start in that window. The journal's supervisor lock excludes another daemon.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path

import psutil

from agentflow.common import canonical_digest, utc_now

ROLES = frozenset({'product', 'test', 'service', 'data'})
HEX = re.compile(r'[0-9a-f]{64}\Z')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')


class _Retain(Exception):
    pass


def _process_stopped(pid, created):
    if (type(pid) is not int or pid <= 0 or type(created) not in {int, float}
            or not math.isfinite(created) or created <= 0):
        raise _Retain('process_identity_missing')
    try:
        process = psutil.Process(pid)
        if process.create_time() == created and process.status() != psutil.STATUS_ZOMBIE:
            raise _Retain('process_alive')
    except psutil.NoSuchProcess:
        pass
    except psutil.Error as error:
        raise _Retain('process_state_unknown') from error
    if os.name == 'posix':
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError as error:
            raise _Retain('process_group_unknown') from error
        raise _Retain('process_group_present')


def _ready(job):
    result, receipt, payload = job.get('result'), job.get('delivery_receipt'), job.get('delivered_payload')
    assignment = job['assignment']
    if (job.get('state') not in {'completed', 'failed', 'cancelled'} or not isinstance(result, dict)
            or result.get('execution_status') not in {'completed', 'failed', 'error', 'cancelled'}
            or assignment.get('job_id') != job['job_id']):
        raise _Retain('job_not_terminal')
    if (not isinstance(receipt, dict) or receipt.get('job_id') != job['job_id']
            or not isinstance(receipt.get('result_id'), str) or not receipt['result_id']
            or receipt.get('assessment_state') not in {'validated', 'rejected'}
            or not job.get('delivery_operation_complete') or not isinstance(payload, dict)):
        raise _Retain('result_ack_missing')
    if (payload.get('operation_id') != canonical_digest({'job': job['job_id'], 'result': result})[7:39]
            or payload.get('input_fingerprint') != assignment.get('input_fingerprint')
            or payload.get('fencing_token') != assignment.get('fencing_token')):
        raise _Retain('result_ack_identity_mismatch')
    if not job.get('cleanup_complete') or result.get('cleanup_verified') is not True:
        raise _Retain('cleanup_unconfirmed')
    if any(not isinstance(value, list) for value in (result.get('built_artifacts'), result.get('artifact_files'),
            payload.get('built_artifacts'), payload.get('artifact_version_ids'))):
        raise _Retain('transfer_evidence_missing')
    processes = result.get('process_results')
    if not isinstance(processes, list):
        raise _Retain('process_evidence_missing')
    identities = []
    for process in processes:
        if (not isinstance(process, dict) or process.get('cleanup_verified') is not True
                or process.get('execution_status') not in {'completed', 'error', 'cancelled'}):
            raise _Retain('process_state_unknown')
        if process.get('pid') is None:
            if process.get('process_created') is not None or process.get('process_fingerprint') is not None:
                raise _Retain('process_identity_missing')
            continue
        if not isinstance(process.get('process_fingerprint'), str) or not DIGEST.fullmatch(process['process_fingerprint']):
            raise _Retain('process_identity_missing')
        _process_stopped(process['pid'], process.get('process_created'))
        identities.append((process['pid'], process.get('process_created'), process['process_fingerprint']))
    if job.get('pid') is not None and (job['pid'], job.get('process_created'), job.get('process_fingerprint')) not in identities:
        raise _Retain('journal_process_mismatch')


class NodeMaintenance:
    def __init__(self, directory: Path, journal):
        supplied = Path(directory)
        if supplied.is_symlink():
            raise ValueError('Node maintenance directory cannot be a symlink')
        self.directory, self.journal = supplied.resolve(), journal

    @contextmanager
    def _database(self):
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise _Retain('unsafe_maintenance_directory')
        path = self.directory / 'maintenance.sqlite'
        for suffix in ('', '-wal', '-shm', '-journal'):
            candidate = Path(str(path) + suffix)
            if candidate.is_symlink() or (candidate.exists() and (
                    not candidate.is_file() or candidate.stat().st_nlink != 1)):
                raise _Retain('unsafe_maintenance_database')
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            if os.name != 'nt':
                path.chmod(0o600)
            connection.execute('PRAGMA synchronous=FULL')
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS files (
                    relative TEXT PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    pending_size INTEGER NOT NULL DEFAULT 0, removed_files INTEGER NOT NULL DEFAULT 0,
                    removed_bytes INTEGER NOT NULL DEFAULT 0, last_error TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _record(connection, relative, status, *, size=0, error=None):
        with connection:
            connection.execute('''INSERT INTO files(relative,status,attempts,pending_size,last_error,updated_at)
                VALUES(?,?,1,?,?,?) ON CONFLICT(relative) DO UPDATE SET status=excluded.status,
                attempts=files.attempts+1, pending_size=excluded.pending_size,
                last_error=excluded.last_error, updated_at=excluded.updated_at''',
                (relative, status, size, error, utc_now()))

    @staticmethod
    def _removed(connection, relative, summary):
        row = connection.execute('SELECT * FROM files WHERE relative=?', (relative,)).fetchone()
        if row and row['status'] == 'pending':
            with connection:
                connection.execute('''UPDATE files SET status='removed', removed_files=removed_files+1,
                    removed_bytes=removed_bytes+pending_size, pending_size=0, last_error=NULL, updated_at=?
                    WHERE relative=?''', (utc_now(), relative))
            summary['removed_files'] += 1
            summary['removed_bytes'] += row['pending_size']

    @staticmethod
    def _same(left, right):
        return (left.st_dev, left.st_ino, left.st_size, left.st_mtime_ns, left.st_nlink) == (
            right.st_dev, right.st_ino, right.st_size, right.st_mtime_ns, right.st_nlink)

    @staticmethod
    def _unlink(name, directory):
        os.unlink(name, dir_fd=directory)
        os.fsync(directory)

    def _remove(self, connection, parts, summary, *, digest=None, size=None):
        relative = '/'.join(parts)
        intent_started = False
        try:
            with ExitStack() as stack:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                directory = os.open(self.directory, flags)
                stack.callback(os.close, directory)
                for component in parts[:-1]:
                    directory = os.open(component, flags, dir_fd=directory)
                    stack.callback(os.close, directory)
                descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), dir_fd=directory)
                stack.callback(os.close, descriptor)
                observed = os.fstat(descriptor)
                if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
                    raise _Retain('not_a_private_regular_file')
                if size is not None and observed.st_size != size:
                    raise _Retain('artifact_size_mismatch')
                if digest is not None:
                    hashed = hashlib.sha256()
                    read = 0
                    while block := os.read(descriptor, 1024 * 1024):
                        read += len(block)
                        if read > observed.st_size:
                            raise _Retain('file_changed')
                        hashed.update(block)
                    if 'sha256:' + hashed.hexdigest() != digest:
                        raise _Retain('artifact_digest_mismatch')
                current = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or not self._same(observed, current) or not self._same(observed, os.fstat(descriptor)):
                    raise _Retain('file_changed')
                self._record(connection, relative, 'pending', size=observed.st_size)
                intent_started = True
                current = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or not self._same(observed, current) or not self._same(observed, os.fstat(descriptor)):
                    raise _Retain('file_changed')
                self._unlink(parts[-1], directory)
                self._removed(connection, relative, summary)
        except FileNotFoundError:
            # A crash after unlink leaves a durable intent; acknowledge its absence once.
            self._removed(connection, relative, summary)
        except (OSError, _Retain) as error:
            code = str(error) if isinstance(error, _Retain) else type(error).__name__
            if intent_started:
                with connection:
                    connection.execute('UPDATE files SET last_error=?,updated_at=? WHERE relative=?', (code, utc_now(), relative))
            else:
                self._record(connection, relative, 'retained', error=code)
            summary['failed'] += 1

    def _built_files(self, connection, job, summary):
        result, payload = job['result'], job['delivered_payload']
        for item in result.get('built_artifacts', []):
            if not isinstance(item, dict) or item.get('component_role') not in ROLES:
                continue
            role = item['component_role']
            parts = ('workspaces', hashlib.sha256(job['job_id'].encode()).hexdigest(), 'evidence', f'built-{role}.tar')
            expected = str(self.directory.joinpath(*parts))
            matches = [uploaded for uploaded in payload.get('built_artifacts', []) if isinstance(uploaded, dict)
                and all(uploaded.get(field) == item.get(field) for field in ('component_id', 'component_role', 'digest'))
                and isinstance(uploaded.get('artifact_version_id'), str)
                and uploaded['artifact_version_id'] in payload.get('artifact_version_ids', [])]
            if (item.get('path') != expected or not isinstance(item.get('digest'), str) or not DIGEST.fullmatch(item['digest'])
                    or not isinstance(item.get('component_id'), str) or not item['component_id']
                    or type(item.get('size')) is not int or item['size'] < 0 or len(matches) != 1
                    or not any(entry.get('path') == expected for entry in result.get('artifact_files', []) if isinstance(entry, dict))):
                self._record(connection, '/'.join(parts), 'retained', error='artifact_transfer_identity_mismatch')
                summary['failed'] += 1
                continue
            self._remove(connection, parts, summary, digest=item['digest'], size=item['size'])

    def sweep(self) -> dict:
        summary = {'removed_files': 0, 'removed_bytes': 0, 'failed': 0, 'retained_jobs': {}, 'inputs_blocked': False}
        if (not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY')
                or not {os.open, os.stat, os.unlink} <= os.supports_dir_fd):
            return {**summary, 'inputs_blocked': True, 'reason': 'safe_unlink_unavailable'}
        snapshot = self.journal.maintenance_snapshot()
        with self._database() as connection:
            for job in snapshot['jobs']:
                try:
                    _ready(job)
                except _Retain as error:
                    summary['retained_jobs'][job['job_id']] = str(error)
                    continue
                self._built_files(connection, job, summary)
            if not snapshot['idle'] or summary['retained_jobs']:
                summary['inputs_blocked'] = True
            else:
                names = {row['relative'].split('/', 1)[1] for row in connection.execute(
                    "SELECT relative FROM files WHERE status='pending' AND relative LIKE 'inputs/%'")
                    if HEX.fullmatch(row['relative'].split('/', 1)[1])}
                try:
                    with ExitStack() as stack:
                        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                        root = os.open(self.directory, flags)
                        stack.callback(os.close, root)
                        inputs = os.open('inputs', flags, dir_fd=root)
                        stack.callback(os.close, inputs)
                        names.update(os.listdir(inputs))
                except FileNotFoundError:
                    pass
                except OSError:
                    summary['inputs_blocked'] = True
                    summary['failed'] += 1
                if not summary['inputs_blocked']:
                    for name in names:
                        if HEX.fullmatch(name):
                            self._remove(connection, ('inputs', name), summary)
            with connection:
                connection.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', ('last_sweep', json.dumps(summary)))
        return summary

    def statistics(self) -> dict:
        with self._database() as connection:
            row = connection.execute('SELECT coalesce(sum(removed_files),0),coalesce(sum(removed_bytes),0) FROM files').fetchone()
            last = connection.execute("SELECT value FROM metadata WHERE name='last_sweep'").fetchone()
            return {'removed_files': row[0], 'removed_bytes': row[1],
                    'last_sweep': json.loads(last[0]) if last else None,
                    'retained': [dict(value) for value in connection.execute("SELECT * FROM files WHERE status!='removed'")]}

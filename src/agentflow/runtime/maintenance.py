"""Reclaim verified ephemeral homes and an idle oversized private npm cache.

Only these scratch roots are eligible. Code repositories, execution/model evidence,
configuration and budget accounts are never removed or rewritten by this service.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
from collections import Counter
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5
from weakref import WeakValueDictionary

import psutil
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.runtime.failures import _private_attempt_directory, _private_evidence
from agentflow.runtime.process_identity import boot_relation, process_is_stopped, same_launcher_identity

TERMINAL = {'completed', 'failed', 'cancelled'}
STOPPED = TERMINAL | {'blocked'}
HOMES = {'codex_homes': 'codex_exec', 'openhands_homes': 'openhands_role'}
KINDS = ('attempt', 'supervised_attempt', 'dispatch_context', 'model_invocation',
         'model_attempt_budget', 'node_job', 'node_resource', 'node_cleanup_receipt', 'product_launch')
TRASH = '.runtime-maintenance-trash'
_LOCKS = WeakValueDictionary()


class _Skip(Exception):
    pass


def _stop(reason):
    raise _Skip(reason)


def _measure(path):
    """No link traversal; allocated bytes are an estimate on copy-on-write filesystems."""
    first = path.lstat()
    if not stat.S_ISDIR(first.st_mode) or path.is_symlink() or first.st_uid != os.getuid():
        _stop('unsafe_source')
    allocated, files, seen = 0, 0, set()
    stack = [path]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if info.st_dev != first.st_dev:
                    _stop('nested_mount')
                if stat.S_ISDIR(info.st_mode):
                    stack.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    files += 1
                    identity = (info.st_dev, info.st_ino)
                    if identity not in seen and info.st_nlink == 1:
                        allocated += info.st_blocks * 512
                    seen.add(identity)
                elif not stat.S_ISLNK(info.st_mode):
                    _stop('special_file_in_scratch')
    return {'device': first.st_dev, 'inode': first.st_ino, 'allocated_bytes': allocated, 'files': files}


def _process_stopped(identity):
    try:
        stopped = process_is_stopped(identity)
    except ValueError:
        _stop('process_identity_unverified')
    if not stopped:
        _stop('process_alive')


def _verify_stopped(data_dir, record):
    if (record.get('state') not in TERMINAL or record.get('attempt_id') != record['id']
            or type(record.get('fencing_token')) is not int or record['fencing_token'] < 1):
        _stop('supervisor_not_terminal')
    name = canonical_digest({'attempt_id': record['id']}).split(':')[1]
    if record.get('directory') != str(data_dir / 'supervisor' / name):
        _stop('supervisor_path_mismatch')
    with _private_attempt_directory(data_dir, 'supervisor', name) as directory:
        result = json.loads(_private_evidence(directory, 'result.json', 65536))
        if (not isinstance(result, dict) or not record.get('nonce')
                or not same_launcher_identity(result, record)
                or result.get('execution_status') != record['state']):
            _stop('process_receipt_mismatch')
        _process_stopped(result)
        try:
            child = json.loads(_private_evidence(directory, 'child.json', 65536))
        except FileNotFoundError:
            if result.get('reason') not in {'launch_not_authorized', 'cancelled_before_child_start', 'invalid_launch_permit'}:
                _stop('child_receipt_missing')
            child = None
        if child is not None:
            if not isinstance(child, dict) or not same_launcher_identity(child, record) or not isinstance(child.get('child'), dict):
                _stop('child_receipt_mismatch')
            _process_stopped({**child['child'], 'boot_fingerprint': record['boot_fingerprint'],
                              'boot_identity_source': record.get('boot_identity_source')})
        if os.name == 'posix' and boot_relation(record) != 'changed':
            for process in psutil.process_iter(['pid', 'status'], ad_value=None):
                try:
                    if os.getpgid(process.pid) == record['pid'] and process.info['status'] != psutil.STATUS_ZOMBIE:
                        _stop('process_group_alive')
                except ProcessLookupError:
                    pass


def _select(records, target):
    if target['kind'] == 'npm_cache':
        return records
    identity = target['attempt_id']
    return {kind: [r for r in rows if (r['id'] == identity if kind in {
        'attempt', 'supervised_attempt', 'dispatch_context', 'model_attempt_budget'} else r.get('attempt_id') == identity)]
        for kind, rows in records.items() if kind in {'attempt', 'supervised_attempt', 'dispatch_context',
                                                    'model_attempt_budget', 'model_invocation'}}


def _transaction_state(tx, target):
    if target['kind'] == 'npm_cache':
        return {kind: tx.list(kind) for kind in KINDS}
    result = {}
    for kind in ('attempt', 'supervised_attempt', 'dispatch_context', 'model_attempt_budget'):
        value = tx.get(kind, target['attempt_id'])
        result[kind] = [value] if value else []
    result['model_invocation'] = [i for i in tx.list('model_invocation') if i.get('attempt_id') == target['attempt_id']]
    return result


def _guard(records, target):
    if any(i.get('state') not in {'settled', 'completed_unpriced', 'released'} for i in records['model_invocation']):
        _stop('model_calls_unsettled')
    if any(i.get('uncertain_invocations', 0) for i in records['model_attempt_budget']):
        _stop('model_calls_unsettled')
    if any(a.get('status') not in STOPPED for a in records['attempt']):
        _stop('attempts_active_or_unknown')
    if any(a.get('state') not in TERMINAL for a in records['supervised_attempt']):
        _stop('supervisor_active_or_unknown')
    if target['kind'] == 'ephemeral_home':
        if any(len(records[k]) != 1 for k in ('attempt', 'supervised_attempt', 'dispatch_context')):
            _stop('attempt_evidence_missing')
        attempt, supervised, context = (records[k][0] for k in ('attempt', 'supervised_attempt', 'dispatch_context'))
        task = context.get('task')
        if (not isinstance(task, dict) or supervised.get('backend') != HOMES[target['folder']]
                or supervised.get('attempt_id') != attempt['id'] or task.get('attempt_id') != attempt['id']
                or any(supervised.get(k) != attempt.get(k) for k in ('run_id', 'fencing_token', 'input_fingerprint'))
                or any(task.get(k) != attempt.get(k) for k in ('run_id', 'iteration_id', 'work_item_id', 'fencing_token', 'input_fingerprint'))
                or type(attempt.get('fencing_token')) is not int or not attempt.get('input_fingerprint')):
            _stop('attempt_identity_mismatch')
        if not isinstance(task.get('output_schema'), dict):
            _stop('frozen_schema_missing')
        Draft202012Validator.check_schema(task['output_schema'])
        if any(any(i.get(k) != attempt.get(k) for k in ('run_id', 'iteration_id', 'fencing_token', 'input_fingerprint'))
               for i in records['model_invocation']):
            _stop('invocation_identity_mismatch')
        return
    if any(j.get('state') not in TERMINAL for j in records['node_job']):
        _stop('node_jobs_active_or_unknown')
    receipts = {r['id']: r for r in records['node_cleanup_receipt']}
    for resource in records['node_resource']:
        if resource.get('state') != 'available' or resource.get('current_lease_id') or resource.get('owner_job_id'):
            _stop('node_resource_not_released')
        if resource.get('fencing_token', 0):
            receipt = receipts.get(resource.get('last_cleanup_receipt_id'))
            if (not receipt or not receipt.get('verified') or receipt.get('alive_process_count') != 0
                    or receipt.get('resource_id') != resource['id'] or receipt.get('fencing_token') != resource['fencing_token']):
                _stop('node_cleanup_unverified')
    for preview in records['product_launch']:
        if preview.get('state') != 'stopped' or preview.get('restore_reconciliation_required'):
            _stop('preview_active_or_unknown')
        if not any(s['id'] == preview.get('attempt_id') for s in records['supervised_attempt']):
            _stop('preview_evidence_missing')


class RuntimeMaintenance:
    def __init__(self, store, data_dir: Path, *, package_cache_max_bytes: int = 256 * 1024 * 1024):
        if type(package_cache_max_bytes) is not int or package_cache_max_bytes < 0:
            raise ValueError('package_cache_max_bytes must be a nonnegative integer')
        supplied = Path(data_dir)
        if supplied.is_symlink():
            raise ValueError('Maintenance data directory cannot be a symlink')
        self.store, self.data_dir = store, supplied.resolve()
        self.package_cache_max_bytes = package_cache_max_bytes
        self._lock = _LOCKS.setdefault(str(self.data_dir), asyncio.Lock())

    async def _state(self, target):
        if target['kind'] == 'ephemeral_home':
            kinds = ('attempt', 'supervised_attempt', 'dispatch_context', 'model_attempt_budget')
            values = await asyncio.gather(*(self.store.read(k, target['attempt_id']) for k in kinds))
            result = {kind: [value] if value else [] for kind, value in zip(kinds, values, strict=True)}
            result['model_invocation'] = [i for i in await self.store.list('model_invocation') if i.get('attempt_id') == target['attempt_id']]
            return result
        rows = await asyncio.gather(*(self.store.list(kind) for kind in KINDS))
        return _select(dict(zip(KINDS, rows, strict=True)), target)

    def _parts(self, target):
        if target.get('kind') == 'npm_cache':
            return 'package_cache', 'npm'
        if (target.get('kind') != 'ephemeral_home' or target.get('folder') not in HOMES
                or not isinstance(target.get('attempt_id'), str) or not target['attempt_id']):
            _stop('invalid_maintenance_target')
        return target['folder'], canonical_digest(target['attempt_id']).split(':')[1]

    def _validate_receipt(self, record):
        target = {k: record[k] for k in ('kind', 'folder', 'attempt_id') if k in record}
        folder, name = self._parts(target)
        if (str(UUID(record['id'])) != record['id'] or record.get('source_relative') != folder + '/' + name
                or any(type(record.get(k)) is not int or record[k] < 0 for k in ('device', 'inode', 'allocated_bytes', 'files'))
                or record['id'] != str(uuid5(NAMESPACE_URL, canonical_digest({'target': target,
                    'device': record['device'], 'inode': record['inode']})))):
            _stop('invalid_maintenance_receipt')
        return target

    def _open(self, target, *, create_trash=False):
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        root = parent = trash = None
        try:
            root = os.open(self.data_dir, flags)
            folder, name = self._parts(target)
            parent = os.open(folder, flags, dir_fd=root)
            if create_trash:
                try:
                    os.mkdir(TRASH, mode=0o700, dir_fd=root)
                except FileExistsError:
                    pass
            trash = os.open(TRASH, flags, dir_fd=root)
            info = os.fstat(trash)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                _stop('unsafe_trash')
            return root, parent, trash, name
        except BaseException:
            for fd in (trash, parent, root):
                if fd is not None:
                    os.close(fd)
            raise

    @staticmethod
    def _close(opened):
        for fd in opened[:3]:
            os.close(fd)

    async def _prepare(self, target):
        folder, name = self._parts(target)
        source = self.data_dir / folder / name
        if (self.data_dir / folder).is_symlink():
            _stop('unsafe_source_root')
        measurement = await asyncio.to_thread(_measure, source)
        if (target['kind'] == 'npm_cache' and self.package_cache_max_bytes > 0
                and measurement['allocated_bytes'] <= self.package_cache_max_bytes):
            _stop('cache_within_limit')
        state = await self._state(target)
        _guard(state, target)
        for record in state['supervised_attempt']:
            await asyncio.to_thread(_verify_stopped, self.data_dir, record)
        snapshot = canonical_digest(state)
        identity = str(uuid5(NAMESPACE_URL, canonical_digest({'target': target,
            'device': measurement['device'], 'inode': measurement['inode']})))
        def prepare(tx):
            current = _transaction_state(tx, target)
            if canonical_digest(current) != snapshot:
                _stop('state_changed')
            _guard(current, target)
            return tx.put('runtime_maintenance', identity, {**target, **measurement,
                'source_relative': folder + '/' + name, 'state': 'prepared', 'created_at': utc_now()})
        return await self.store.command('runtime.maintenance.prepare', identity,
            {'target': target, 'device': measurement['device'], 'inode': measurement['inode']}, prepare)

    async def _quarantine(self, record):
        target = self._validate_receipt(record)
        folder, name = self._parts(target)
        if record.get('source_relative') != folder + '/' + name:
            _stop('invalid_maintenance_target')
        state = await self._state(target)
        _guard(state, target)
        for supervised in state['supervised_attempt']:
            await asyncio.to_thread(_verify_stopped, self.data_dir, supervised)
        snapshot = canonical_digest(state)
        opened = self._open(target, create_trash=True)
        _, parent, trash, name = opened
        def rename(tx):
            current = tx.get('runtime_maintenance', record['id'])
            if current['state'] != 'prepared':
                return current
            now = _transaction_state(tx, target)
            if canonical_digest(now) != snapshot:
                _stop('state_changed')
            _guard(now, target)
            try:
                info = os.stat(record['id'], dir_fd=trash, follow_symlinks=False)
            except FileNotFoundError:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (record['device'], record['inode']):
                    _stop('source_identity_changed')
                # This one atomic rename holds the writer's admission guard: a new
                # node lease cannot begin using the old npm path between idle check
                # and quarantine. The durable intent already exists before rename.
                os.rename(name, record['id'], src_dir_fd=parent, dst_dir_fd=trash)
                os.fsync(parent)
                os.fsync(trash)
                info = os.stat(record['id'], dir_fd=trash, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (record['device'], record['inode']):
                _stop('trash_identity_changed')
            return tx.put('runtime_maintenance', record['id'], {**current, 'state': 'quarantined',
                'quarantined_at': utc_now()}, current['revision'])
        try:
            return await self.store.command('runtime.maintenance.quarantine', record['id'], {'id': record['id']}, rename)
        finally:
            self._close(opened)

    async def _remove(self, record):
        target = self._validate_receipt(record)
        opened = self._open(target)
        trash = opened[2]
        try:
            try:
                info = os.stat(record['id'], dir_fd=trash, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None:
                if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (record['device'], record['inode']):
                    _stop('trash_identity_changed')
                # shutil's descriptor-based implementation unlinks internal symlinks
                # without following them and protects against directory substitution.
                await asyncio.to_thread(shutil.rmtree, record['id'], dir_fd=trash)
                os.fsync(trash)
        finally:
            self._close(opened)
        def complete(tx):
            current = tx.get('runtime_maintenance', record['id'])
            if current['state'] == 'completed':
                return current
            if current['state'] != 'quarantined':
                _stop('invalid_maintenance_state')
            result = tx.put('runtime_maintenance', record['id'], {**current, 'state': 'completed',
                'completed_at': utc_now(), 'reclaimed_bytes': current['allocated_bytes']}, current['revision'])
            tx.event('runtime.scratch_cleaned', {'maintenance_id': record['id'], 'kind': record['kind'],
                'reclaimed_bytes': result['reclaimed_bytes']})
            return result
        return await self.store.command('runtime.maintenance.complete', record['id'], {'id': record['id']}, complete)

    async def sweep(self):
        async with self._lock:
            result = {'scanned': 0, 'cleaned': 0, 'reclaimed_bytes': 0, 'skipped': {}, 'pending': 0, 'by_kind': {}}
            skips = Counter()
            if not getattr(shutil.rmtree, 'avoids_symlink_attacks', False) or os.name != 'posix':
                return {**result, 'skipped': {'safe_delete_unavailable': 1}}
            pending = [r for r in await self.store.list('runtime_maintenance') if r.get('state') in {'prepared', 'quarantined'}]
            targets = [(None, r) for r in pending]
            pending_sources = {r.get('source_relative') for r in pending}
            attempts = {canonical_digest(a['id']).split(':')[1]: a['id'] for a in await self.store.list('attempt')}
            for folder in HOMES:
                base = self.data_dir / folder
                if base.is_symlink():
                    skips['unsafe_source_root'] += 1
                    continue
                if not base.is_dir():
                    continue
                for path in base.iterdir():
                    if folder + '/' + path.name in pending_sources:
                        continue
                    if path.name not in attempts:
                        skips['unmatched_home'] += 1
                        continue
                    targets.append(({'kind': 'ephemeral_home', 'folder': folder, 'attempt_id': attempts[path.name]}, None))
            cache = self.data_dir / 'package_cache' / 'npm'
            if 'package_cache/npm' not in pending_sources and (cache.exists() or cache.is_symlink()):
                targets.append(({'kind': 'npm_cache'}, None))
            for target, receipt in targets:
                result['scanned'] += 1
                try:
                    receipt = receipt or await self._prepare(target)
                    if receipt['state'] == 'prepared':
                        receipt = await self._quarantine(receipt)
                    if receipt['state'] == 'quarantined':
                        completed = await self._remove(receipt)
                        result['cleaned'] += 1
                        result['reclaimed_bytes'] += completed['reclaimed_bytes']
                        bucket = result['by_kind'].setdefault(completed['kind'], {'cleaned': 0, 'reclaimed_bytes': 0})
                        bucket['cleaned'] += 1
                        bucket['reclaimed_bytes'] += completed['reclaimed_bytes']
                except _Skip as error:
                    skips[str(error)] += 1
                except (OSError, ValueError, TypeError, KeyError, DomainError, SchemaError, psutil.Error):
                    skips['unverified_or_unavailable_evidence'] += 1
            result['skipped'] = dict(skips)
            result['pending'] = sum(r.get('state') in {'prepared', 'quarantined'} for r in await self.store.list('runtime_maintenance'))
            return result

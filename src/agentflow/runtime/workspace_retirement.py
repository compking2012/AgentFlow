"""Journaled retirement of a caller-verified, completed read-only workspace."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

from agentflow.common import DomainError, canonical_digest, utc_now

from .launcher import atomic_json
from .workspace import PROJECT_MARKER, _private_json, _real_directory

MAXIMUM_RETIREMENT_JOURNAL = 32 * 1024 * 1024


def _location(manager, metadata, attempt_id):
    name = canonical_digest(attempt_id).split(':')[1]
    if metadata.get('attempt_id') != attempt_id:
        raise ValueError('retirement_attempt_mismatch')
    if metadata.get('version', 1) == 2:
        root = manager._project_root(metadata['project_root'], metadata['project_id'])
        parent = root / '.agentflow/workspaces'
        _real_directory(root / '.agentflow')
        _real_directory(parent)
        if (_private_json(root / '.agentflow' / PROJECT_MARKER) != manager._marker(root, metadata['project_id'])
                or metadata.get('layout') != 'project'
                or metadata.get('input_fingerprint') != canonical_digest(metadata.get('request'))):
            raise ValueError('retirement_project_mismatch')
    elif metadata.get('version', 1) == 1:
        parent = manager.root
        _real_directory(parent)
    else:
        raise ValueError('unsupported_retirement_layout')
    source = parent / name
    if metadata.get('path') != str(source):
        raise ValueError('retirement_path_mismatch')
    return source, parent / ('.retired-' + name)


def _clean(manager, path, metadata):
    _real_directory(path)
    _real_directory(path / '.git')
    repository = manager.repository
    if (repository._integrity(path, metadata['base_oid']) != metadata['tree_oid']
            or repository._run(path, ['rev-parse', 'HEAD']).decode().strip() != metadata['base_oid']
            or repository._collect_diff(path, metadata['base_oid'])['has_changes']
            or repository._run(path, ['ls-files', '--others', '--ignored', '--exclude-standard', '-z'])):
        raise DomainError('workspace_not_disposable', 'Workspace has changed or contains additional files; it was preserved')


def _private_directory(path):
    if path.is_symlink():
        raise ValueError('linked_retirement_directory')
    path.mkdir(mode=0o700, exist_ok=True)
    _real_directory(path)


def _protect(source, trash, protected_paths):
    for value in protected_paths:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError('protected_workspace_path_must_be_absolute')
        candidates = (path, Path(os.path.normpath(path)), path.resolve())
        if any(candidate.is_relative_to(root) for candidate in candidates for root in (source, trash)):
            raise DomainError('workspace_referenced', 'Workspace is still referenced by protected source or delivery records')


def _inventory(path):
    """Fingerprint entries without following links, including private Git metadata."""
    inventory = {}
    device = path.stat().st_dev
    for directory, subdirs, files, directory_fd in os.fwalk(path, follow_symlinks=False):
        for name in [*subdirs, *files]:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if info.st_dev != device:
                raise ValueError('workspace_nested_mount')
            signature = {'device': info.st_dev, 'inode': info.st_ino, 'mode': info.st_mode}
            if stat.S_ISLNK(info.st_mode):
                signature.update(target=os.readlink(name, dir_fd=directory_fd), bytes=info.st_size,
                                 modified=info.st_mtime_ns, changed=info.st_ctime_ns)
            elif stat.S_ISREG(info.st_mode):
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                with os.fdopen(descriptor, 'rb') as stream:
                    before = os.fstat(stream.fileno())
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                    after = os.fstat(stream.fileno())
                fields = ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
                if any(getattr(info, key) != getattr(before, key) or getattr(before, key) != getattr(after, key)
                       for key in fields):
                    raise ValueError('workspace_changed_during_inventory')
                signature.update(sha256=digest, bytes=info.st_size, links=info.st_nlink,
                                 modified=info.st_mtime_ns, changed=info.st_ctime_ns)
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError('workspace_special_file')
            inventory[str((Path(directory) / name).relative_to(path))] = signature
    return inventory


def _unchanged(path, intent, *, partially_deleted=False):
    _real_directory(path)
    info = path.stat()
    if (info.st_dev, info.st_ino) != (intent['device'], intent['inode']):
        raise ValueError('retirement_directory_replaced')
    expected = intent['inventory']
    observed = _inventory(path)
    if (not partially_deleted and observed.keys() != expected.keys()
            or any(expected.get(name) != signature for name, signature in observed.items())):
        raise DomainError('workspace_not_disposable', 'Workspace contents changed after retirement began; remaining files were preserved')


def _retire(manager, attempt_id, protected_paths):
    name = canonical_digest(attempt_id).split(':')[1]
    _real_directory(manager.metadata)
    retiring = manager.metadata / 'retiring'
    retired = manager.metadata / 'retired'
    for path in (retiring, retired):
        _private_directory(path)
    intent_path, tombstone_path = retiring / (name + '.json'), retired / (name + '.json')
    active_path = manager.metadata / (name + '.json')
    tombstone = _private_json(tombstone_path) if tombstone_path.exists() or tombstone_path.is_symlink() else None
    if tombstone and (tombstone.get('attempt_id') != attempt_id or tombstone.get('kind') != 'workspace_retirement'
                      or tombstone.get('state') != 'retired'):
        raise ValueError('invalid_retirement_tombstone')
    if not intent_path.exists() and not intent_path.is_symlink():
        if tombstone:
            source = Path(tombstone['workspace']['path'])
            _protect(source, source.parent / ('.retired-' + name), protected_paths)
            return tombstone['result']
        metadata = manager.registration(attempt_id)
        source, trash = _location(manager, metadata, attempt_id)
        _protect(source, trash, protected_paths)
        if trash.exists() or trash.is_symlink():
            raise ValueError('retirement_trash_already_exists')
        _clean(manager, source, metadata)
        info = source.stat()
        inventory = _inventory(source)
        intent = {'version': 1, 'kind': 'workspace_retirement', 'state': 'retiring', 'attempt_id': attempt_id,
                  'workspace': metadata, 'path': str(source), 'trash_path': str(trash),
                  'device': info.st_dev, 'inode': info.st_ino, 'inventory': inventory,
                  'bytes': sum(item.get('bytes', 0) for item in inventory.values()), 'created_at': utc_now()}
        if len(json.dumps(intent, ensure_ascii=False, allow_nan=False).encode()) > MAXIMUM_RETIREMENT_JOURNAL:
            raise ValueError('workspace_retirement_inventory_too_large')
        atomic_json(intent_path, intent)
    else:
        intent = _private_json(intent_path, maximum=MAXIMUM_RETIREMENT_JOURNAL)
    if intent.get('kind') != 'workspace_retirement' or intent.get('attempt_id') != attempt_id:
        raise ValueError('invalid_retirement_intent')
    metadata = intent['workspace']
    source, trash = _location(manager, metadata, attempt_id)
    _protect(source, trash, protected_paths)
    if intent.get('path') != str(source) or intent.get('trash_path') != str(trash):
        raise ValueError('retirement_intent_path_mismatch')
    if active_path.exists() or active_path.is_symlink():
        if _private_json(active_path) != metadata:
            raise ValueError('retirement_registration_changed')
    if tombstone and (tombstone.get('workspace_digest') != canonical_digest(metadata)
                      or tombstone.get('inventory_digest') != canonical_digest(intent['inventory'])):
        raise ValueError('retirement_tombstone_changed')
    if source.exists() or source.is_symlink():
        if trash.exists() or trash.is_symlink():
            raise ValueError('ambiguous_retirement_directories')
        _unchanged(source, intent)
        source.rename(trash)
    if trash.exists() or trash.is_symlink():
        # After partial deletion only unchanged survivors from the journal may remain.
        _unchanged(trash, intent, partially_deleted=bool(tombstone))
    elif not tombstone:
        raise ValueError('retirement_workspace_disappeared')
    if tombstone is None:
        result = {'attempt_id': attempt_id, 'state': 'retired', 'retired': True, 'path': str(source),
                  'bytes': intent.get('bytes', 0), 'bytes_kind': 'logical', 'retired_at': utc_now()}
        tombstone = {'version': 1, 'kind': 'workspace_retirement', 'state': 'retired', 'attempt_id': attempt_id,
                     'workspace_digest': canonical_digest(metadata), 'inventory_digest': canonical_digest(intent['inventory']),
                     'workspace': metadata, 'result': result}
        atomic_json(tombstone_path, tombstone)
    if active_path.exists():
        active_path.unlink()
    if trash.exists():
        shutil.rmtree(trash)
    intent_path.unlink()
    return tombstone['result']


async def retire_workspace(manager, attempt_id, protected_paths=()):
    """Caller must prove terminal read-only work, stopped processes and no source references."""
    name = canonical_digest(attempt_id).split(':')[1]
    protected_paths = tuple(protected_paths)
    async with manager._locks.setdefault(name, asyncio.Lock()):
        try:
            return await asyncio.to_thread(_retire, manager, attempt_id, protected_paths)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise DomainError('workspace_retirement_conflict', 'Workspace retirement needs reconciliation; existing files were preserved') from error

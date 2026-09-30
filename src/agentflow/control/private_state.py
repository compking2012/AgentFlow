"""Small private state files; no linked files, shared permissions, or fixed temp names."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from agentflow.common import DomainError
from agentflow.storage.store import _InstanceLock


def _private(info, *, directory=False):
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return (correct_type and (os.name == 'nt' or not info.st_mode & 0o077)
            and (not hasattr(os, 'getuid') or info.st_uid == os.getuid())
            and (directory or info.st_nlink == 1))


def private_directory(path: Path, code: str, *, create=False) -> bool:
    try:
        if create and not path.exists() and not path.is_symlink():
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not _private(info, directory=True):
            raise DomainError(code, '本机状态目录必须是当前用户私有的普通目录')
        return True
    except FileNotFoundError:
        if not create:
            return False
        raise DomainError(code, '无法创建本机私有状态目录') from None
    except OSError:
        raise DomainError(code, '无法访问本机私有状态目录') from None


def _no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate field')
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError('non-finite JSON value')


def read_private_json(path: Path, code: str, *, maximum: int) -> tuple[dict, tuple[int, int]] | None:
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        with os.fdopen(os.open(path, flags), 'rb') as file:
            info = os.fstat(file.fileno())
            if not _private(info) or info.st_size > maximum:
                raise DomainError(code, '本机状态记录必须是当前用户私有且大小受限的普通文件')
            raw = file.read(maximum + 1)
            if len(raw) > maximum:
                raise ValueError('record too large')
        value = json.loads(raw, object_pairs_hook=_no_duplicates, parse_constant=_invalid_constant)
        if not isinstance(value, dict):
            raise ValueError('record must be an object')
        return value, (info.st_dev, info.st_ino)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, UnicodeError):
        raise DomainError(code, '本机状态记录无法安全读取；原记录已保留，请先核对平台状态') from None


def fsync_directory(path: Path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def write_private_json(path: Path, value: dict, code: str):
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.tmp', dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
            json.dump(value, file, ensure_ascii=False, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        if path.is_symlink():
            raise DomainError(code, '本机状态记录不能是符号链接')
        temporary.replace(path)
        fsync_directory(path.parent)
    except OSError:
        raise DomainError(code, '无法保存本机私有状态记录') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def private_lock(path: Path, code: str, busy_code: str, busy_message: str) -> _InstanceLock:
    try:
        if path.exists() or path.is_symlink():
            if not _private(path.lstat()):
                raise DomainError(code, '本机状态锁必须是当前用户私有的普通文件')
        lock = _InstanceLock(path)
    except OSError:
        raise DomainError(code, '无法访问本机状态锁') from None
    except DomainError as error:
        if error.code == 'store_locked':
            raise DomainError(busy_code, busy_message) from None
        raise
    if not _private(os.fstat(lock.fd)):
        lock.close()
        raise DomainError(code, '本机状态锁必须是当前用户私有的普通文件')
    return lock

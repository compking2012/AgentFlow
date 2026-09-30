"""Private runtime discovery; this file is state, never user configuration."""
from __future__ import annotations

import math
import os
from pathlib import Path

import psutil

from agentflow.common import DomainError
from agentflow.control.private_state import (
    fsync_directory,
    private_directory,
    private_lock,
    read_private_json,
    write_private_json,
)


def instance_path() -> Path:
    return Path.home() / '.config/agentflow/active-instance.json'


def _record(path):
    loaded = read_private_json(path, 'unsafe_instance_record', maximum=4096)
    if loaded is None:
        return None
    value, identity = loaded
    try:
        if set(value) != {'pid', 'created_at', 'data_dir'}:
            raise ValueError('invalid fields')
        if type(value['pid']) is not int or not 0 < value['pid'] < 2 ** 31:
            raise ValueError('invalid process identity')
        if (type(value['created_at']) not in {int, float} or not 0 < value['created_at'] < 1e12
                or not math.isfinite(value['created_at'])):
            raise ValueError('invalid process creation time')
        if not isinstance(value['data_dir'], str) or len(value['data_dir']) > 2048:
            raise ValueError('invalid data directory')
        data = Path(value['data_dir'])
        if not data.is_absolute() or data.is_symlink() or data.resolve() != data or (data.exists() and not data.is_dir()):
            raise ValueError('invalid data directory')
        return value, identity
    except (OSError, ValueError, TypeError):
        raise DomainError('invalid_instance_record', '本机运行记录无效，原记录已保留，请检查 active-instance.json') from None


def _alive(record):
    try:
        process = psutil.Process(record['pid'])
        return (abs(process.create_time() - record['created_at']) < .01
                and process.status() != psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        raise DomainError('instance_identity_unknown', '暂时无法核对本机运行进程，请稍后重试') from None


def active_data_dir() -> Path | None:
    path = instance_path()
    if not private_directory(path.parent, 'unsafe_instance_record'):
        return None
    loaded = _record(path)
    return Path(loaded[0]['data_dir']) if loaded and _alive(loaded[0]) else None


def publish_instance(data_dir: Path):
    path = instance_path()
    private_directory(path.parent, 'unsafe_instance_record', create=True)
    lock = private_lock(path.with_suffix('.lock'), 'unsafe_instance_record',
                        'instance_busy', '另一进程正在更新本机运行记录，请稍后重试')
    try:
        current = _record(path)
        mine = {'pid': os.getpid(), 'created_at': psutil.Process().create_time(), 'data_dir': str(Path(data_dir).resolve())}
        if current and _alive(current[0]) and current[0] != mine:
            raise DomainError('active_instance_exists', '已有平台实例正在运行，请先停止原实例')
        write_private_json(path, mine, 'unsafe_instance_record')
    finally:
        lock.close()


def clear_instance():
    """A closing process may remove only its exact, still-current record."""
    path = instance_path()
    lock = None
    try:
        if not private_directory(path.parent, 'unsafe_instance_record'):
            return False
        lock = private_lock(path.with_suffix('.lock'), 'unsafe_instance_record',
                            'instance_busy', '本机运行记录正在更新')
        loaded = _record(path)
        if loaded is None:
            return False
        record, identity = loaded
        if record['pid'] != os.getpid() or abs(record['created_at'] - psutil.Process().create_time()) >= .01:
            return False
        latest = path.lstat()
        if (latest.st_dev, latest.st_ino) != identity:
            return False
        path.unlink()
        fsync_directory(path.parent)
        return True
    except (DomainError, OSError, psutil.Error):
        return False
    finally:
        if lock is not None:
            lock.close()

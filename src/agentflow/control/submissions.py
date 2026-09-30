"""Keep only uncertain CLI submissions, without exposing idempotency switches."""
from __future__ import annotations

import copy
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ValidationError

from agentflow.common import DomainError, canonical_digest
from agentflow.configuration import configuration_path
from agentflow.control.private_state import (
    fsync_directory,
    private_directory,
    private_lock,
    read_private_json,
    write_private_json,
)
from agentflow.control.product_models import ProductRequest


class Submission:
    def __init__(self, data_dir: Path, task_input: dict, payload: dict):
        self.root = configuration_path().parent / 'cli_submissions'
        self.origin_data_dir = str(Path(data_dir).resolve())
        if Path(data_dir).is_symlink():
            raise DomainError('unsafe_cli_state', 'CLI运行状态目录不能是符号链接')
        private_directory(self.root.parent, 'unsafe_cli_state', create=True)
        private_directory(self.root, 'unsafe_cli_state', create=True)
        self.fingerprint = canonical_digest(task_input)
        self.path = self.root / (self.fingerprint[7:] + '.json')
        self.lock = None
        self.payload = copy.deepcopy(payload)
        self._task_fields = {name: payload.get(name) for name in ('name', 'goal', 'output_directory')}
        self.key = str(uuid4())
        self._identity = None
        self._record_digest = None
        self._acknowledged = False
        self.recovered = False

    def _validate(self, value):
        try:
            if (set(value) != {'version', 'input_fingerprint', 'key', 'payload', 'payload_fingerprint', 'origin_data_dir'}
                    or type(value['version']) is not int or value['version'] != 1):
                raise ValueError('invalid record fields')
            if value['input_fingerprint'] != self.fingerprint or not isinstance(value['payload'], dict):
                raise ValueError('invalid task identity')
            key = value['key']
            if not isinstance(key, str) or str(UUID(key)) != key or UUID(key).version != 4:
                raise ValueError('invalid idempotency identity')
            origin = value['origin_data_dir']
            if not isinstance(origin, str) or not Path(origin).is_absolute() or str(Path(origin).resolve()) != origin:
                raise ValueError('invalid controller identity')
            payload = value['payload']
            ProductRequest.model_validate(payload)
            if (canonical_digest(payload) != value['payload_fingerprint']
                    or any(payload.get(name) != expected for name, expected in self._task_fields.items())):
                raise ValueError('payload differs from original task')
        except (ValueError, TypeError, KeyError, ValidationError):
            raise DomainError('invalid_cli_submission', 'CLI提交记录无效；原记录已保留，请先用 agentflow status 核对原提交') from None

    def __enter__(self):
        private_directory(self.root, 'unsafe_cli_state')
        self.lock = private_lock(self.path.with_suffix('.lock'), 'unsafe_cli_state',
                                'submission_in_progress', '同一目标正在提交，请稍后用 agentflow status 查看进展')
        try:
            loaded = read_private_json(self.path, 'unsafe_cli_state', maximum=128 * 1024)
            self.recovered = loaded is not None
            if loaded is None:
                value = {'version': 1, 'input_fingerprint': self.fingerprint, 'key': self.key,
                         'payload': self.payload, 'payload_fingerprint': canonical_digest(self.payload),
                         'origin_data_dir': self.origin_data_dir}
                self._validate(value)
                write_private_json(self.path, value, 'unsafe_cli_state')
                loaded = read_private_json(self.path, 'unsafe_cli_state', maximum=128 * 1024)
                if loaded is None:
                    raise DomainError('unsafe_cli_state', 'CLI提交记录未能持久保存')
            value, self._identity = loaded
            self._validate(value)
            if value['origin_data_dir'] != self.origin_data_dir:
                raise DomainError('submission_controller_changed',
                    '原提交尚未确认，平台数据目录已变化。请恢复原 data_dir 配置后用 agentflow status 核对；提交记录已保留')
            self._record_digest = canonical_digest(value)
            self.key, self.payload = value['key'], value['payload']
            return self
        except BaseException:
            self.lock.close()
            self.lock = None
            raise

    def acknowledged(self):
        if self._acknowledged:
            return
        if self.lock is None or self.lock.fd is None:
            raise DomainError('submission_not_locked', '提交记录只能在当前操作持有锁时确认')
        loaded = read_private_json(self.path, 'unsafe_cli_state', maximum=128 * 1024)
        if loaded is None or loaded[1] != self._identity or canonical_digest(loaded[0]) != self._record_digest:
            raise DomainError('submission_record_changed', 'CLI提交记录已变化，原请求可能已接受，请先核对产品状态')
        latest = self.path.lstat()
        if (latest.st_dev, latest.st_ino) != self._identity:
            raise DomainError('submission_record_changed', 'CLI提交记录已被替换，请先核对产品状态')
        self.path.unlink()
        fsync_directory(self.root)
        self._acknowledged = True

    def __exit__(self, *_):
        if self.lock:
            self.lock.close()
            self.lock = None

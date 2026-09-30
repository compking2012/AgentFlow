"""Immutable owner-provided API credentials kept outside projects and event records."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from agentflow.common import DomainError


class LocalSecretStore:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir).resolve() / 'secrets'
        if self.root.is_symlink():
            raise DomainError('unsafe_secret_directory', 'Credential storage cannot be linked')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def _path(self, identity: str) -> Path:
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', identity):
            raise DomainError('invalid_secret_reference', 'Invalid local credential reference')
        return self.root / identity

    def put(self, identity: str, value: str) -> str:
        if not value.strip() or value != value.strip() or len(value) > 8192 or '\x00' in value or '\n' in value or '\r' in value:
            raise DomainError('invalid_credential', 'An API credential must be a nonempty single line', 422)
        path = self._path(identity)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            if self.read('local:' + identity) != value:
                raise DomainError('credential_conflict', 'This setup operation already contains a different credential')
        else:
            with os.fdopen(fd, 'w') as out:
                out.write(value)
                out.flush()
                os.fsync(out.fileno())
        return 'local:' + identity

    def read(self, reference: str) -> str:
        if reference.startswith('env:') and reference[4:].isidentifier():
            value = os.environ.get(reference[4:])
            if not value:
                raise DomainError('credential_missing', 'The configured environment credential is unavailable')
            return value
        if not reference.startswith('local:'):
            raise DomainError('credential_reference_invalid', 'Use a local credential or an explicit environment reference')
        try:
            fd = os.open(self._path(reference[6:]), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'r') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 8192 or info.st_mode & 0o077:
                    raise DomainError('unsafe_credential', 'Local credential permissions or type are invalid')
                return stream.read()
        except OSError as exc:
            raise DomainError('credential_missing', 'The configured local credential is unavailable') from exc

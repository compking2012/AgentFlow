from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

from agentflow.common import DomainError, canonical_digest
from agentflow.repository.git import RepositoryAdapter

from .launcher import atomic_json

PROJECT_MARKER = 'workspace-owner.json'


def _real_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir() or path.resolve() != path:
        raise DomainError('workspace_not_isolated', 'Workspace directories must be real canonical directories', 403)


def _private_json(path: Path, *, maximum: int = 65536) -> dict:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum or info.st_mode & 0o077
                or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
            raise ValueError('unsafe_workspace_registration')
        raw = os.read(descriptor, maximum + 1)
        if len(raw) > maximum:
            raise ValueError('oversized_workspace_registration')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('invalid_workspace_registration')
        return value
    finally:
        os.close(descriptor)


def _git_pointer(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 8192
                or (hasattr(os, 'getuid') and info.st_uid != os.getuid())):
            raise ValueError('unsafe_git_pointer')
        return os.read(descriptor, 8193).decode().strip()
    finally:
        os.close(descriptor)


class WorkspaceManager:
    def __init__(self, data_dir: Path, repository: RepositoryAdapter | None = None, *, create: bool = True):
        self.data_dir = Path(data_dir).resolve()
        self.root = self.data_dir / 'workspaces'
        self.metadata = self.data_dir / 'workspace_metadata'
        if create:
            for directory in (self.root, self.metadata):
                if directory.is_symlink():
                    raise DomainError('workspace_not_isolated', 'Workspace storage cannot be a symbolic link', 403)
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                _real_directory(directory)
        self.repository = repository or RepositoryAdapter()
        self._locks: dict[str, asyncio.Lock] = {}

    def _assert_not_retired(self, attempt_id):
        name = canonical_digest(attempt_id).split(':')[1]
        for state in ('retiring', 'retired'):
            directory = self.metadata / state
            if directory.is_symlink():
                raise DomainError('workspace_not_isolated', 'Workspace retirement records cannot be symbolic links', 403)
            record = directory / (name + '.json')
            if record.exists() or record.is_symlink():
                raise DomainError('workspace_retired', 'This attempt workspace was retired or is being retired; use a new attempt')

    @staticmethod
    def _marker(project_root, project_id):
        return {'version': 1, 'kind': 'agentflow_project_workspaces', 'project_id': project_id,
                'project_root': str(project_root)}

    def _project_root(self, value, project_id):
        root = Path(value).absolute()
        _real_directory(root)
        if (not isinstance(project_id, str) or not project_id or len(project_id) > 200 or '\x00' in project_id
                or root == self.data_dir or root.is_relative_to(self.data_dir) or self.data_dir.is_relative_to(root)):
            raise DomainError('invalid_workspace_project', 'Project workspace requires a separate registered project', 403)
        return root

    def _prepare_project(self, project_root, project_id):
        root = self._project_root(project_root, project_id)
        git = root / '.git'
        if git.is_symlink():
            raise DomainError('workspace_namespace_conflict', 'Project Git metadata cannot be a symbolic link')
        if Path(self.repository._run(root, ['rev-parse', '--show-toplevel']).decode().strip()).resolve() != root:
            raise DomainError('invalid_workspace_project', 'Project workspaces require the Git repository root', 403)
        git_dir = Path(self.repository._run(root, ['rev-parse', '--absolute-git-dir']).decode().strip())
        common_dir = Path(self.repository._run(root, ['rev-parse', '--path-format=absolute', '--git-common-dir']).decode().strip())
        exclude = Path(self.repository._run(root, ['rev-parse', '--path-format=absolute', '--git-path', 'info/exclude']).decode().strip())
        for directory in {git_dir, common_dir}:
            _real_directory(directory)
            if (directory.is_relative_to(self.data_dir) or self.data_dir.is_relative_to(directory)
                    or (hasattr(os, 'getuid') and directory.stat().st_uid != os.getuid())):
                raise DomainError('invalid_workspace_project', 'Git administration must belong to this owner outside controller storage', 403)
        if git.is_file():
            pointer = _git_pointer(git)
            if not pointer.startswith('gitdir: '):
                raise DomainError('workspace_namespace_conflict', 'Invalid project Git directory pointer')
            declared = Path(pointer[len('gitdir: '):])
            if not declared.is_absolute():
                declared = root / declared
            if declared.resolve() != git_dir:
                raise DomainError('workspace_namespace_conflict', 'Project Git directory identity changed')
            if git_dir != common_dir:
                back_pointer = Path(_git_pointer(git_dir / 'gitdir'))
                common_pointer = Path(_git_pointer(git_dir / 'commondir'))
                if not back_pointer.is_absolute():
                    back_pointer = git_dir / back_pointer
                if not common_pointer.is_absolute():
                    common_pointer = git_dir / common_pointer
                if (git_dir.parent != common_dir / 'worktrees' or back_pointer.resolve() != git
                        or common_pointer.resolve() != common_dir):
                    raise DomainError('workspace_namespace_conflict', 'Linked worktree administration does not match this project')
        elif git_dir != git or common_dir != git:
            raise DomainError('workspace_namespace_conflict', 'Project Git administration does not match its repository')
        if exclude not in {git_dir / 'info/exclude', common_dir / 'info/exclude'}:
            raise DomainError('workspace_namespace_conflict', 'Git local exclusions are outside the verified administration directories')
        if self.repository._run(root, ['ls-files', '-z', '--', '.agentflow']):
            raise DomainError('workspace_namespace_conflict', 'The project already tracks .agentflow content; it cannot be hidden or replaced')
        managed = root / '.agentflow'
        marker = managed / PROJECT_MARKER
        expected = self._marker(root, project_id)
        if managed.exists() or managed.is_symlink():
            _real_directory(managed)
            if not marker.exists() or _private_json(marker) != expected:
                raise DomainError('workspace_namespace_conflict', 'Existing .agentflow content has no matching project ownership')
        info = exclude.parent
        if info.is_symlink():
            raise DomainError('workspace_namespace_conflict', 'Git local exclusion directory cannot be a symbolic link')
        info.mkdir(mode=0o700, exist_ok=True)
        _real_directory(info)
        descriptor = os.open(exclude, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
                             | getattr(os, 'O_NONBLOCK', 0), 0o600)
        try:
            if os.name == 'posix':
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 or status.st_size > 1024 * 1024:
                raise DomainError('workspace_namespace_conflict', 'Git local exclusion file is not safe to update')
            original = os.read(descriptor, 1024 * 1024 + 1)
            if b'/.agentflow/' not in original.splitlines():
                os.lseek(descriptor, 0, os.SEEK_END)
                os.write(descriptor, (b'' if not original or original.endswith(b'\n') else b'\n')
                         + b'# AgentFlow per-attempt workspaces (local only)\n/.agentflow/\n')
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            self.repository._run(root, ['check-ignore', '--no-index', '-q', '--', '.agentflow/workspaces/.probe'])
        except DomainError as error:
            raise DomainError('workspace_namespace_conflict', 'Project ignore rules expose the managed workspace directory') from error
        if not managed.exists():
            managed.mkdir(mode=0o700)
            atomic_json(marker, expected)
        workspaces = managed / 'workspaces'
        if workspaces.is_symlink():
            raise DomainError('workspace_namespace_conflict', 'Managed project workspaces cannot be a symbolic link')
        workspaces.mkdir(mode=0o700, exist_ok=True)
        _real_directory(workspaces)
        return workspaces

    async def create_clone(self, source_repo: Path, ref: str, attempt_id: str, *,
                           project_root: Path | None = None, project_id: str | None = None) -> Path:
        if (project_root is None) != (project_id is None):
            raise DomainError('invalid_workspace_project', 'Provide both project root and project identity', 422)
        name = canonical_digest(attempt_id).split(':')[1]
        record = self.metadata / f'{name}.json'
        request = {'source': str(Path(source_repo).resolve()), 'ref': ref, 'attempt_id': attempt_id}
        async with self._locks.setdefault(name, asyncio.Lock()):
            self._assert_not_retired(attempt_id)
            if record.exists() or record.is_symlink():
                metadata = self.registration(attempt_id)
                expected = dict(request)
                if metadata.get('version', 1) == 2:
                    if project_root is not None and (str(self._project_root(project_root, project_id)) != metadata['project_root']
                                                     or project_id != metadata['project_id']):
                        raise DomainError('workspace_conflict', 'Attempt already belongs to another project workspace')
                    expected.update(project_root=metadata['project_root'], project_id=metadata['project_id'])
                if metadata['input_fingerprint'] != canonical_digest(expected):
                    raise DomainError('workspace_conflict', 'Workspace input identity changed')
                return Path(metadata['path'])
            if project_root is None:
                destination = self.root / name
            else:
                root = self._project_root(project_root, project_id)
                async with self._locks.setdefault('project:' + str(root), asyncio.Lock()):
                    try:
                        directory = await asyncio.to_thread(self._prepare_project, root, project_id)
                    except (OSError, ValueError) as error:
                        raise DomainError('workspace_namespace_conflict', 'Project workspace ownership or local Git exclusion is unsafe') from error
                destination = directory / name
                request.update(project_root=str(root), project_id=project_id)
            if destination.exists() or destination.is_symlink():
                raise DomainError('workspace_unknown', 'Unconfirmed workspace exists; do not overwrite')
            result = await self.repository.clone_snapshot(Path(source_repo), destination, ref)
            metadata = {**result, 'attempt_id': attempt_id, 'input_fingerprint': canonical_digest(request)}
            if project_root is not None:
                metadata.update(version=2, layout='project', project_root=str(root), project_id=project_id, request=request)
            atomic_json(record, metadata)
            return destination

    def registration(self, attempt_id: str) -> dict:
        """Resolve only the private controller receipt, never a task-supplied path."""
        try:
            _real_directory(self.metadata)
            self._assert_not_retired(attempt_id)
            name = canonical_digest(attempt_id).split(':')[1]
            metadata = _private_json(self.metadata / f'{name}.json')
            if metadata.get('attempt_id') != attempt_id:
                raise ValueError('workspace_attempt_mismatch')
            version = metadata.get('version', 1)
            if version == 2:
                root = self._project_root(metadata['project_root'], metadata['project_id'])
                managed = root / '.agentflow'
                _real_directory(managed)
                _real_directory(managed / 'workspaces')
                if (_private_json(managed / PROJECT_MARKER) != self._marker(root, metadata['project_id'])
                        or metadata.get('layout') != 'project'
                        or metadata.get('input_fingerprint') != canonical_digest(metadata.get('request'))
                        or metadata.get('request', {}).get('project_root') != str(root)
                        or metadata.get('request', {}).get('project_id') != metadata['project_id']
                        or metadata.get('request', {}).get('attempt_id') != attempt_id):
                    raise ValueError('workspace_project_mismatch')
                expected = managed / 'workspaces' / name
            elif version == 1:
                _real_directory(self.root)
                expected = self.root / name
            else:
                raise ValueError('unsupported_workspace_registration')
            workspace = Path(metadata['path'])
            if workspace != expected:
                raise ValueError('workspace_path_mismatch')
            _real_directory(workspace)
            _real_directory(workspace / '.git')
            if self.repository._integrity(workspace, metadata['base_oid']) != metadata['tree_oid']:
                raise ValueError('workspace_base_mismatch')
            return metadata
        except (OSError, ValueError, TypeError, KeyError, DomainError) as error:
            raise DomainError('workspace_not_isolated', 'Workspace registration, path or Git baseline cannot be verified', 403) from error

    def assert_owned(self, path: Path, attempt_id: str | None = None) -> None:
        try:
            path = Path(path).absolute()
            if attempt_id is None:
                _real_directory(self.metadata)
                name = path.name
                if len(name) != 64 or any(char not in '0123456789abcdef' for char in name):
                    raise ValueError('invalid_workspace_name')
                metadata = _private_json(self.metadata / f'{name}.json')
                attempt_id = metadata['attempt_id']
            metadata = self.registration(attempt_id)
            if str(path) != metadata['path']:
                raise ValueError('workspace_path_mismatch')
        except (OSError, ValueError, KeyError, TypeError, DomainError) as error:
            raise DomainError('workspace_not_isolated', 'Coding requires its registered isolated workspace', 403) from error

    async def retire(self, attempt_id: str, *, protected_paths=()) -> dict:
        """Caller verifies completed read-only work, stopped execution and no code references."""
        from .workspace_retirement import retire_workspace
        return await retire_workspace(self, attempt_id, protected_paths)

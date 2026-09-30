"""Prepare and verify exact coding paths before entering the Agent sandbox."""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path

from agentflow.common import DomainError

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _identity(info):
    return info.st_dev, info.st_ino


def _open_directory(path: Path) -> int:
    """Walk every component, including ancestors, without following links."""
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Coding paths must be absolute without parent traversal')
    descriptor = os.open('/', _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _assert_directory_binding(path: Path, descriptor: int) -> None:
    current = _open_directory(path)
    try:
        if _identity(os.fstat(current)) != _identity(os.fstat(descriptor)):
            raise ValueError('Coding directory changed during preparation')
    finally:
        os.close(current)


def prepare_write_parents(workspace: Path, scopes: list[Path]) -> None:
    # Recheck path/FD bindings around each creation and roll back owned empty
    # directories on failure. These checks detect observed renames; they do not
    # provide an atomic filesystem lock against the machine owner moving trees.
    descriptors, created = [], []
    try:
        root = _open_directory(workspace)
        descriptors.append(root)
        for scope in scopes:
            relative = scope.relative_to(workspace)
            if '..' in relative.parts:
                raise ValueError('Parent traversal in coding scope')
            parent, path = root, workspace
            for part in relative.parts[:-1]:
                _assert_directory_binding(path, parent)
                made = False
                try:
                    os.mkdir(part, mode=0o755, dir_fd=parent)
                    made = True
                except FileExistsError:
                    pass
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
                descriptors.append(child)
                if made:
                    created.append((parent, part, _identity(os.fstat(child))))
                path = path / part
                _assert_directory_binding(path, child)
                parent = child
            _assert_directory_binding(path, parent)
            if relative.parts:
                try:
                    info = os.stat(relative.parts[-1], dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
                    raise ValueError('Coding scope is linked or not a regular file/directory')
    except (OSError, ValueError) as error:
        for parent, name, identity in reversed(created):
            try:
                if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) == identity:
                    os.rmdir(name, dir_fd=parent)
            except OSError:
                pass  # Never remove a replacement or a directory containing user data.
        raise DomainError('coding_workspace_unwritable',
            '无法安全准备获准源码路径的父目录；Agent 尚未启动，请检查该工作区的路径与权限。', 409) from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _remove_probe(parent, name, identity, token=None):
    descriptor = None
    owned = False
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or _identity(info) != identity:
            return
        owned = True
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or _identity(info) != identity:
            return
        if token is not None and os.read(descriptor, len(token) + 1) != token:
            return
        if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) == identity:
            os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        pass
    except OSError:
        if owned:
            raise  # Failed removal of an owned probe must block Agent launch.
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def coding_write_probe(scopes: list[Path], suffix: str):
    """Own all probe inodes before the subprocess can create source-path links.

    Staging is next to each target, so imported projects on another volume work.
    Only the temporary probe profile grants staging writes; the Agent profile
    never includes these sibling paths. The controller cleans after child exit,
    including SIGKILL on timeout/cancellation, and preserves replacement files.
    """
    entries, resources, staging_paths = [], [], []
    try:
        for index, scope in enumerate(dict.fromkeys(scopes)):
            parent = _open_directory(scope.parent)
            resources.append([parent, None, None, None, None])
            resource = resources[-1]
            try:
                info = os.stat(scope.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            target = scope
            if info is not None and stat.S_ISDIR(info.st_mode):
                child = os.open(scope.name, _DIRECTORY_FLAGS, dir_fd=parent)
                os.close(parent)
                parent = child
                resource[0] = child
                target = scope / ('.agentflow-coding-probe-' + suffix)
                info = None
            _assert_directory_binding(target.parent, parent)
            staging, identity, token = None, None, None
            if info is None:
                staging = '.agentflow-coding-stage-' + suffix + '-' + str(index)
                token = ('agentflow-coding-probe:' + suffix + ':' + str(index)).encode()
                descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=parent)
                try:
                    identity = _identity(os.fstat(descriptor))
                    resource[1:] = [target.name, staging, identity, token]
                    if os.write(descriptor, token) != len(token):
                        raise OSError('Incomplete coding probe token')
                finally:
                    os.close(descriptor)
                staging_paths.append(target.parent / staging)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                identity = _identity(info)
            else:
                raise ValueError('Coding probe target is linked or not a regular file')
            entries.append((str(target), _identity(os.fstat(parent)), staging, identity, token))
        yield write_probe_script(entries), staging_paths
    except (OSError, ValueError) as error:
        raise DomainError('coding_workspace_unwritable',
            '无法安全检查获准源码路径；Agent 尚未启动，请检查该工作区的路径与权限。', 409) from error
    finally:
        cleanup_error = None
        for parent, target, staging, identity, token in reversed(resources):
            try:
                if staging is not None:
                    for name, expected_token in [(target, token), (staging, None)]:
                        try:
                            _remove_probe(parent, name, identity, expected_token)
                        except OSError as error:
                            cleanup_error = error
            finally:
                os.close(parent)
        if cleanup_error is not None:
            raise DomainError('coding_workspace_unwritable',
                '源码写入检查的临时文件无法安全清理，Agent 尚未启动，请检查工作区权限。', 409) from cleanup_error


def write_probe_script(entries: list[tuple]) -> str:
    # A hardlink creates a target with an inode/token already known to the
    # controller. There is no create-before-token gap on forced child exit.
    # Existing files are opened without writing or truncating any source bytes.
    return f'''import os, stat
from pathlib import Path
flags = os.O_SEARCH | os.O_DIRECTORY | os.O_NOFOLLOW
for target, parent_identity, staging, identity, token in {entries!r}:
    target = Path(target)
    parent = os.open('/', flags)
    descriptor = None
    try:
        for part in target.parent.parts[1:]:
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        info = os.fstat(parent)
        assert (info.st_dev, info.st_ino) == parent_identity
        if staging is not None:
            os.link(staging, target.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(target.name, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(descriptor)
        assert stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == identity
        assert info.st_nlink == (2 if staging is not None else 1)
        if staging is not None:
            assert os.write(descriptor, token) == len(token)
            os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)
'''

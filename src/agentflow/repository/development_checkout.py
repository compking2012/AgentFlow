"""Update a visible checkout under real Git index, HEAD and reference locks.

Candidate objects must already be imported. A failed transaction never force-resets
files. An installed candidate index with the old HEAD is a recognized retry state.
"""
from __future__ import annotations

import os
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from agentflow.common import DomainError


def _overrides(repository, root):
    keys = repository._run(root, ['config', '--includes', '--name-only', '--get-regexp',
        r'^filter\..*\.(clean|smudge|process|required)$'], check=False).decode().splitlines()
    result = ['-c', 'core.autocrlf=false']
    for key in keys:
        result += ['-c', key + ('=false' if key.lower().endswith('.required') else '=')]
    return result


def _git(repository, root, args, *, index=None, data=None, check=True):
    return repository._run(root, _overrides(repository, root) + args, data=data, check=check,
                           extra_env={'GIT_INDEX_FILE': str(index)} if index else None)


def _symbolic_head(repository, root):
    return _git(repository, root, ['symbolic-ref', '--no-recurse', '-q', 'HEAD'], check=False).decode().strip()


def _refresh_stat_cache(repository, root, index):
    # Refresh only the private copy, never stage or rewrite source content. A
    # restored file can have the original bytes but a different mtime; diff-files
    # without this refresh would report that stale stat cache as a real edit.
    try:
        _git(repository, root, ['update-index', '--refresh'], index=index)
    except DomainError as error:
        # Exit 1 means some tracked files genuinely need an update. Preserve the
        # index entries and let the subsequent dirty checks reject those changes.
        if error.code != 'git_error' or not isinstance(error.details, dict) or error.details.get('exit_code') != 1:
            raise


class _OwnedLock:
    def __init__(self, path):
        self.path, self.fd, self.identity = path, None, None

    def acquire(self):
        if self.path.parent.resolve() != self.path.parent:
            raise DomainError('unsafe_development_checkout', 'Git 锁目录包含符号链接，未修改项目。')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError as error:
            raise DomainError('project_git_busy', '项目正在进行其他 Git 操作，未修改源码或分支。') from error
        info = os.fstat(self.fd)
        self.identity = (info.st_dev, info.st_ino)
        return self

    def release(self):
        if self.fd is None:
            return
        try:
            try:
                info = self.path.lstat()
                if (info.st_dev, info.st_ino) == self.identity and stat.S_ISREG(info.st_mode):
                    self.path.unlink()
            except FileNotFoundError:
                pass
        finally:
            os.close(self.fd)
            self.fd = None


class _References:
    """A native update-ref process keeps prepared HEAD/ref locks until commit/abort."""
    def __init__(self, repository, root):
        self.timeout = min(repository.timeout, 30)
        self.buffer = b''
        self.finished = False
        self.errors = tempfile.TemporaryFile()
        environment = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT') if k in os.environ}
        environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_CONFIG_SYSTEM=os.devnull, GIT_NO_REPLACE_OBJECTS='1', GIT_OPTIONAL_LOCKS='0')
        command = [repository.git_path, '-c', 'core.hooksPath=' + os.devnull,
                   '-c', 'core.fsmonitor=false', '-c', 'maintenance.auto=false', '-c', 'gc.auto=0',
                   '-C', str(root), 'update-ref', '--stdin']
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.errors, env=environment, start_new_session=os.name == 'posix', bufsize=0)

    def _send(self, value):
        content = memoryview(value.encode())
        while content:
            written = os.write(self.process.stdin.fileno(), content)
            content = content[written:]

    def _expect(self, expected):
        deadline = time.monotonic() + self.timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b'\n' not in self.buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise DomainError('development_ref_timeout', 'Git 分支事务超时，已停止自动同步。')
                chunk = os.read(self.process.stdout.fileno(), 4096)
                if not chunk or len(self.buffer) + len(chunk) > 8192:
                    raise DomainError('development_ref_changed', '项目 HEAD 或分支已变化，未覆盖其他 Git 操作。')
                self.buffer += chunk
        line, self.buffer = self.buffer.split(b'\n', 1)
        if line != expected.encode():
            raise DomainError('development_ref_changed', 'Git 未确认所需的分支事务，未继续同步。')

    def prepare(self, commands):
        self._send('start\n')
        self._expect('start: ok')
        self._send(commands + 'prepare\n')
        self._expect('prepare: ok')

    def commit(self):
        self._send('commit\n')
        self._expect('commit: ok')
        self.finished = True

    def close(self):
        try:
            if not self.finished and self.process.poll() is None:
                try:
                    self._send('abort\n')
                    self._expect('abort: ok')
                except (OSError, DomainError):
                    self.process.terminate()
            try:
                self.process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.communicate(timeout=5)
        finally:
            self.errors.close()


def _probe_symref_transactions(repository, root, branch, expected_ref, expected_head):
    previous = 'ref ' + expected_ref if expected_ref else 'oid ' + expected_head
    probe = ('start\noption no-deref\nsymref-update HEAD ' + branch + ' ' + previous + '\nabort\n').encode()
    output = _git(repository, root, ['update-ref', '--stdin'], data=probe, check=False)
    if output != b'start: ok\nabort: ok\n':
        raise DomainError('atomic_development_checkout_unavailable',
                          '当前 Git 不支持安全的 HEAD 符号引用事务，项目源码未自动同步。')
    if _git(repository, root, ['rev-parse', '--show-ref-format']).strip() != b'files':
        raise DomainError('atomic_development_checkout_unavailable', '当前 Git 引用存储不支持所需的安全锁定。')


def _ignored_conflict(repository, root, candidate):
    ignored = _git(repository, root, ['ls-files', '--others', '--ignored', '--exclude-standard', '-z']).split(b'\0')
    names = _git(repository, root, ['ls-tree', '-r', '--name-only', '-z', candidate]).split(b'\0')
    insensitive = _git(repository, root, ['config', '--bool', 'core.ignorecase'], check=False).strip() == b'true'
    def normalized(value):
        text = os.fsdecode(value)
        return text.casefold() if insensitive else text
    ignored = {normalized(path) for path in ignored if path}
    names = {normalized(path) for path in names if path}
    candidate_parents = {'/'.join(path.split('/')[:index]) for path in names for index in range(1, len(path.split('/')))}
    return bool(ignored & (names | candidate_parents)) or any(
        '/'.join(path.split('/')[:index]) in names for path in ignored for index in range(1, len(path.split('/'))))


def checkout_development(repository, root: Path, *, candidate: str, branch: str,
                         expected_head: str, expected_ref: str | None, expected_branch: str | None) -> dict:
    """Synchronous helper; callers may run it in their existing I/O thread.

    expected_branch is the development ref's prior OID (None if absent), not a
    branch name. None/empty expected_ref means a detached HEAD. Objects must already
    exist in this repository. Interruptions preserve files instead of resetting them.
    """
    root = repository._repo(Path(root), worktree=True)
    candidate, expected_head = repository._oid(candidate), repository._oid(expected_head)
    expected_ref, expected_branch = expected_ref or '', expected_branch or None
    repository._validate_ref(branch)
    if not branch.startswith('refs/heads/'):
        raise DomainError('invalid_development_branch', '开发分支必须位于本地 heads 命名空间。')
    if expected_ref:
        repository._validate_ref(expected_ref)
    if expected_branch:
        repository._oid(expected_branch)
    if expected_ref == branch and expected_branch != expected_head:
        raise DomainError('development_ref_changed', '开发分支与预期 HEAD 不一致，未同步。')
    tree = repository._integrity(root, candidate)
    _probe_symref_transactions(repository, root, branch, expected_ref, expected_head)
    git_dir = Path(_git(repository, root, ['rev-parse', '--absolute-git-dir']).decode().strip())
    common = Path(_git(repository, root, ['rev-parse', '--path-format=absolute', '--git-common-dir']).decode().strip())
    if git_dir.resolve() != git_dir or common.resolve() != common:
        raise DomainError('unsafe_development_checkout', 'Git 元数据目录归属无法核验，未同步。')
    index = git_dir / 'index'
    index_lock = _OwnedLock(git_dir / 'index.lock')
    old_ref_lock = None
    transaction = None
    touched = False
    try:
        index_lock.acquire()
        if index.is_symlink() or (index.exists() and (not index.is_file() or index.stat().st_nlink != 1)):
            raise DomainError('unsafe_development_checkout', '项目索引不是独立常规文件，未同步。')
        if any((git_dir / name).exists() for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                                                      'rebase-merge', 'rebase-apply', 'sequencer')):
            raise DomainError('project_git_busy', '项目有未结束的 Git 操作，未自动同步。')
        if expected_ref and expected_ref != branch:
            # Git rejects an explicit HEAD symref update together with verification
            # of its old referent in one transaction. Keep that referent locked
            # ourselves; native update-ref owns HEAD and the destination branch.
            old_path = Path(_git(repository, root, ['rev-parse', '--path-format=absolute', '--git-path', expected_ref]).decode().strip())
            if not old_path.is_relative_to(common):
                raise DomainError('unsafe_development_checkout', '原分支锁不在共享 Git 目录内。')
            old_ref_lock = _OwnedLock(Path(str(old_path) + '.lock')).acquire()
        if _symbolic_head(repository, root) != expected_ref or repository._commit(root, 'HEAD') != expected_head:
            raise DomainError('project_checkout_changed', '项目分支或提交已变化，未切换或覆盖。')
        if expected_ref and repository._commit(root, expected_ref) != expected_head:
            raise DomainError('project_checkout_changed', '原分支已变化，未同步。')
        if _git(repository, root, ['symbolic-ref', '-q', branch], check=False):
            raise DomainError('invalid_development_branch', '开发分支不能是另一个引用的别名。')
        with tempfile.TemporaryDirectory(prefix='agentflow-development-index-', dir=git_dir) as temporary:
            temporary_index = Path(temporary) / 'index'
            if index.exists():
                shutil.copyfile(index, temporary_index)
            else:
                _git(repository, root, ['read-tree', expected_head], index=temporary_index)
            flags = _git(repository, root, ['ls-files', '-v', '-z'], index=temporary_index).split(b'\0')
            if any(entry and (entry[:1] == b'S' or b'a' <= entry[:1] <= b'z') for entry in flags):
                raise DomainError('project_code_modified',
                    '项目索引设置可能隐藏本地改动，已保留文件；请先核对 assume-unchanged / skip-worktree 标志。')
            _refresh_stat_cache(repository, root, temporary_index)
            index_tree = _git(repository, root, ['write-tree'], index=temporary_index).decode().strip()
            untracked = _git(repository, root, ['ls-files', '--others', '--exclude-standard', '-z'], index=temporary_index)
            modified = _git(repository, root, ['diff-files', '--name-only', '-z'], index=temporary_index)
            old_tree = _git(repository, root, ['rev-parse', expected_head + '^{tree}']).decode().strip()
            if untracked or modified or index_tree not in {old_tree, tree} or _ignored_conflict(repository, root, candidate):
                raise DomainError('project_code_modified', '项目存在本地改动或中断状态，已保留；请先核对后再同步。')
            transaction = _References(repository, root)
            if expected_ref == branch:
                # Dereferencing HEAD locks both HEAD and its actual referent. Check
                # the literal target after prepare: a prior external switch aborts
                # before any working-tree change, even if both refs have the same OID.
                commands = f'update HEAD {candidate} {expected_head}\n'
            else:
                previous = 'ref ' + expected_ref if expected_ref else 'oid ' + expected_head
                commands = f'option no-deref\nsymref-update HEAD {branch} {previous}\n'
                commands += (f'update {branch} {candidate} {expected_branch}\n' if expected_branch
                             else f'create {branch} {candidate}\n')
            transaction.prepare(commands)
            if _symbolic_head(repository, root) != expected_ref or repository._commit(root, 'HEAD') != expected_head:
                raise DomainError('project_checkout_changed', '项目在锁定前已切换分支，已中止同步。')
            if index_tree != tree:
                touched = True
                _git(repository, root, ['read-tree', '-m', '-u', expected_head, candidate], index=temporary_index)
            if (_git(repository, root, ['write-tree'], index=temporary_index).decode().strip() != tree
                    or _git(repository, root, ['diff-files', '--name-only', '-z'], index=temporary_index)):
                raise DomainError('project_code_modified', '同步期间出现文件变动，已保留当前文件并中止分支提交。')
            with temporary_index.open('rb') as file:
                os.fsync(file.fileno())
            # Keep the index.lock pathname present through reference commit. Native
            # checkout/add/commit remain excluded even after the new index is installed.
            os.replace(temporary_index, index)
            touched = True
            directory = os.open(git_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            transaction.commit()
        return {'path': str(root), 'commit_oid': candidate, 'development_ref': branch, 'tree_oid': tree}
    except (OSError, subprocess.SubprocessError) as error:
        raise DomainError('development_checkout_interrupted' if touched else 'development_checkout_failed',
                          '项目同步未完成，当前文件和索引已保留，未强制恢复或覆盖。') from error
    finally:
        if transaction:
            transaction.close()
        if old_ref_lock:
            old_ref_lock.release()
        index_lock.release()

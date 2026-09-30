"""Real local Git locks and transactions; no user repository or model is touched."""
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from agentflow.common import DomainError
from agentflow.repository import RepositoryAdapter
from agentflow.repository import development_checkout as module
from agentflow.repository.development_checkout import checkout_development


def git(root, *args):
    return subprocess.run(['/opt/homebrew/bin/git', '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=Fixture',
        '-c', 'user.email=fixture@localhost', '-c', 'commit.gpgSign=false', '-C', str(root), *args],
        env={'PATH': '/opt/homebrew/bin:/usr/bin:/bin', 'GIT_CONFIG_GLOBAL': os.devnull,
             'GIT_CONFIG_NOSYSTEM': '1', 'LC_ALL': 'C'}, capture_output=True)


@pytest_asyncio.fixture(params=[False, True], ids=['regular', 'linked'])
async def checkout(tmp_path, request):
    origin = tmp_path / 'origin'
    origin.mkdir()
    assert git(origin, 'init', '-b', 'main').returncode == 0
    (origin / 'README.md').write_text('baseline\n')
    (origin / '.gitignore').write_text('local*\n')
    git(origin, 'add', '.')
    assert git(origin, 'commit', '-m', 'baseline').returncode == 0
    base = git(origin, 'rev-parse', 'HEAD').stdout.decode().strip()
    root = tmp_path / 'linked' if request.param else origin
    if request.param:
        assert git(origin, 'worktree', 'add', '-b', 'project-view', str(root), base).returncode == 0
    expected_ref = git(root, 'symbolic-ref', 'HEAD').stdout.decode().strip()
    repository = RepositoryAdapter(timeout=10)
    source = tmp_path / 'source'
    await repository.clone_snapshot(root, source, base)
    (source / 'src').mkdir()
    (source / 'src/app.mjs').write_text('export const version = 1;\n')
    frozen = await repository.freeze_workspace(source, base, 'candidate')
    bundle = tmp_path / 'candidate.bundle'
    await repository.prepare_bundle(source, frozen['commit_oid'], bundle)
    await repository.import_bundle(root, bundle, frozen['commit_oid'])
    branch = 'refs/heads/agentflow/development/fixture'
    git_dir = Path(git(root, 'rev-parse', '--absolute-git-dir').stdout.decode().strip())
    value = SimpleNamespace(root=root, origin=origin, source=source, base=base, expected_ref=expected_ref,
        candidate=frozen['commit_oid'], tree=frozen['tree_oid'], repository=repository, branch=branch,
        git_dir=git_dir, temporary=tmp_path)
    yield value
    assert not (git_dir / 'index.lock').exists() or (git_dir / 'index.lock').read_text() == 'foreign lock'


def apply(env, **changes):
    return checkout_development(env.repository, env.root, **{
        'candidate': env.candidate, 'branch': env.branch, 'expected_head': env.base,
        'expected_ref': env.expected_ref, 'expected_branch': None, **changes})


async def next_candidate(env):
    (env.source / 'src/app.mjs').write_text('export const version = 2;\n')
    frozen = await env.repository.freeze_workspace(env.source, env.candidate, 'candidate two')
    bundle = env.temporary / 'next.bundle'
    await env.repository.prepare_bundle(env.source, frozen['commit_oid'], bundle)
    await env.repository.import_bundle(env.root, bundle, frozen['commit_oid'])
    return frozen['commit_oid']


async def test_atomic_first_checkout_and_same_branch_advance_preserve_original_branch(checkout):
    env = checkout
    first = apply(env)
    assert first['tree_oid'] == env.tree and first['development_ref'] == env.branch
    assert git(env.root, 'rev-parse', env.expected_ref).stdout.decode().strip() == env.base
    assert git(env.root, 'symbolic-ref', 'HEAD').stdout.decode().strip() == env.branch
    assert (env.root / 'src/app.mjs').read_text() == 'export const version = 1;\n'
    second = await next_candidate(env)
    result = apply(env, candidate=second, expected_head=env.candidate, expected_ref=env.branch, expected_branch=env.candidate)
    assert result['commit_oid'] == second
    assert (env.root / 'src/app.mjs').read_text() == 'export const version = 2;\n'
    assert git(env.root, 'rev-parse', env.expected_ref).stdout.decode().strip() == env.base
    assert git(env.root, 'status', '--porcelain').stdout == b''
    assert apply(env, candidate=second, expected_head=second, expected_ref=env.branch, expected_branch=second) == result


@pytest.mark.parametrize('advance', [False, True])
async def test_native_checkout_commit_head_switch_and_ref_update_are_locked_during_materialization(checkout, monkeypatch, advance):
    env = checkout
    arguments = {}
    if advance:
        apply(env)
        second = await next_candidate(env)
        arguments = {'candidate': second, 'expected_head': env.candidate, 'expected_ref': env.branch,
                     'expected_branch': env.candidate}
    assert git(env.root, 'branch', 'user-work').returncode == 0
    original = env.repository._run
    blocked = []
    def guarded(root, args, **kwargs):
        if 'read-tree' in args and '-u' in args:
            commands = [('checkout', 'user-work'), ('add', 'README.md'), ('commit', '--allow-empty', '-m', 'external'),
                        ('symbolic-ref', 'HEAD', 'refs/heads/user-work'), ('update-ref', env.branch, env.base)]
            for command in commands:
                response = git(env.root, *command)
                blocked.append(response.returncode != 0 and b'.lock' in response.stderr)
        return original(root, args, **kwargs)
    monkeypatch.setattr(env.repository, '_run', guarded)
    result = apply(env, **arguments)
    assert len(blocked) == 5 and all(blocked)
    assert git(env.root, 'symbolic-ref', 'HEAD').stdout.decode().strip() == env.branch
    assert git(env.root, 'rev-parse', env.branch).stdout.decode().strip() == result['commit_oid']


async def test_clean_external_switch_before_index_lock_is_rejected(checkout, monkeypatch):
    env = checkout
    git(env.root, 'branch', 'user-work')
    original = module._probe_symref_transactions
    def switched(*args, **kwargs):
        original(*args, **kwargs)
        assert git(env.root, 'checkout', 'user-work').returncode == 0
    monkeypatch.setattr(module, '_probe_symref_transactions', switched)
    with pytest.raises(DomainError) as error:
        apply(env)
    assert error.value.code == 'project_checkout_changed'
    assert git(env.root, 'symbolic-ref', 'HEAD').stdout.strip() == b'refs/heads/user-work'
    assert not (env.root / 'src/app.mjs').exists()


@pytest.mark.parametrize('advance', [False, True])
async def test_same_oid_external_symbolic_switch_before_prepare_aborts_before_files_or_refs_change(checkout, monkeypatch, advance):
    env = checkout
    arguments = {}
    if advance:
        apply(env)
        second = await next_candidate(env)
        arguments = {'candidate': second, 'expected_head': env.candidate, 'expected_ref': env.branch,
                     'expected_branch': env.candidate}
    git(env.root, 'branch', 'user-work')
    old_head = git(env.root, 'rev-parse', 'HEAD').stdout
    original = module._References.prepare
    def switched(transaction, commands):
        assert git(env.root, 'symbolic-ref', 'HEAD', 'refs/heads/user-work').returncode == 0
        return original(transaction, commands)
    monkeypatch.setattr(module._References, 'prepare', switched)
    with pytest.raises(DomainError):
        apply(env, **arguments)
    assert git(env.root, 'symbolic-ref', 'HEAD').stdout.strip() == b'refs/heads/user-work'
    assert git(env.root, 'rev-parse', 'HEAD').stdout == old_head
    if advance:
        assert (env.root / 'src/app.mjs').read_text() == 'export const version = 1;\n'
    else:
        assert not (env.root / 'src/app.mjs').exists()


@pytest.mark.parametrize('mode', ['unstaged', 'staged', 'untracked'])
async def test_local_edits_are_never_reset_or_overwritten(checkout, mode):
    env = checkout
    target = env.root / ('user.txt' if mode == 'untracked' else 'README.md')
    target.write_text('user changes\n')
    if mode == 'staged':
        git(env.root, 'add', 'README.md')
    with pytest.raises(DomainError) as error:
        apply(env)
    assert error.value.code == 'project_code_modified'
    assert target.read_text() == 'user changes\n'
    assert git(env.root, 'rev-parse', 'HEAD').stdout.decode().strip() == env.base


async def test_ignored_user_file_is_preserved(checkout):
    env = checkout
    (env.root / 'local.txt').write_text('user ignored file\n')
    (env.source / 'local.txt').write_text('candidate file\n')
    git(env.source, 'add', '-f', 'local.txt')
    frozen = await env.repository.freeze_workspace(env.source, env.base, 'candidate with ignored name')
    bundle = env.temporary / 'ignored.bundle'
    await env.repository.prepare_bundle(env.source, frozen['commit_oid'], bundle)
    await env.repository.import_bundle(env.root, bundle, frozen['commit_oid'])
    with pytest.raises(DomainError) as error:
        apply(env, candidate=frozen['commit_oid'])
    assert error.value.code == 'project_code_modified'
    assert (env.root / 'local.txt').read_text() == 'user ignored file\n'


@pytest.mark.parametrize('included', [False, True])
async def test_project_filters_are_disabled_during_status_and_read_tree(checkout, included):
    env = checkout
    canary = env.temporary / 'filter-ran'
    command = 'touch ' + str(canary)
    if included:
        configuration = env.temporary / 'included.config'
        configuration.write_text('[filter "bad"]\nclean = ' + command + '\nsmudge = ' + command + '\nrequired = true\n')
        git(env.root, 'config', 'include.path', str(configuration))
    else:
        git(env.root, 'config', 'filter.bad.clean', command)
        git(env.root, 'config', 'filter.bad.smudge', command)
        git(env.root, 'config', 'filter.bad.required', 'true')
    (env.source / '.gitattributes').write_text('*.mjs filter=bad\n')
    frozen = await env.repository.freeze_workspace(env.source, env.base, 'candidate attributes')
    bundle = env.temporary / 'filtered.bundle'
    await env.repository.prepare_bundle(env.source, frozen['commit_oid'], bundle)
    await env.repository.import_bundle(env.root, bundle, frozen['commit_oid'])
    result = apply(env, candidate=frozen['commit_oid'])
    assert result['commit_oid'] == frozen['commit_oid'] and not canary.exists()


async def test_candidate_index_with_old_head_is_a_safe_retry_after_aborted_ref_commit(checkout, monkeypatch):
    env = checkout
    original = module._References.commit
    def crash(_):
        raise OSError('fixture interruption after index installation')
    monkeypatch.setattr(module._References, 'commit', crash)
    with pytest.raises(DomainError) as error:
        apply(env)
    assert error.value.code == 'development_checkout_interrupted'
    assert git(env.root, 'rev-parse', 'HEAD').stdout.decode().strip() == env.base
    assert git(env.root, 'write-tree').stdout.decode().strip() == env.tree
    assert (env.root / 'src/app.mjs').read_text() == 'export const version = 1;\n'
    assert not (env.git_dir / 'HEAD.lock').exists()
    monkeypatch.setattr(module._References, 'commit', original)
    assert apply(env)['commit_oid'] == env.candidate
    assert git(env.root, 'status', '--porcelain').stdout == b''


async def test_foreign_index_lock_is_not_removed(checkout):
    env = checkout
    lock = env.git_dir / 'index.lock'
    lock.write_text('foreign lock')
    with pytest.raises(DomainError) as error:
        apply(env)
    assert error.value.code == 'project_git_busy'
    assert lock.read_text() == 'foreign lock'
    assert not (env.root / 'src/app.mjs').exists()


async def test_unsupported_symref_protocol_never_falls_back_to_unlocked_checkout(checkout, monkeypatch):
    env = checkout
    original = module._git
    def unsupported(repository, root, args, **kwargs):
        if args == ['update-ref', '--stdin']:
            return b''
        return original(repository, root, args, **kwargs)
    monkeypatch.setattr(module, '_git', unsupported)
    with pytest.raises(DomainError) as error:
        apply(env)
    assert error.value.code == 'atomic_development_checkout_unavailable'
    assert not (env.root / 'src/app.mjs').exists()
    assert not (env.git_dir / 'index.lock').exists()


async def test_detached_expected_head_can_attach_atomically(checkout):
    env = checkout
    assert git(env.root, 'checkout', '--detach', env.base).returncode == 0
    result = apply(env, expected_ref=None)
    assert result['commit_oid'] == env.candidate
    assert git(env.root, 'symbolic-ref', 'HEAD').stdout.decode().strip() == env.branch


async def test_prepared_reference_transaction_eof_aborts_without_attaching_head(checkout):
    env = checkout
    transaction = module._References(env.repository, env.root)
    try:
        transaction.prepare(f'option no-deref\nsymref-update HEAD {env.branch} ref {env.expected_ref}\n'
                            f'create {env.branch} {env.candidate}\n')
        transaction.process.stdin.close()
        transaction.process.stdin = None
        transaction.process.wait(timeout=5)
        assert git(env.root, 'symbolic-ref', 'HEAD').stdout.decode().strip() == env.expected_ref
        assert git(env.root, 'show-ref', '--verify', env.branch).returncode != 0
        assert not (env.git_dir / 'HEAD.lock').exists()
    finally:
        transaction.close()


@pytest.mark.parametrize('which', ['head', 'old_ref'])
async def test_foreign_reference_locks_are_never_removed(checkout, which):
    env = checkout
    lock = (env.git_dir / 'HEAD.lock' if which == 'head' else Path(git(env.root, 'rev-parse',
        '--path-format=absolute', '--git-path', env.expected_ref).stdout.decode().strip() + '.lock'))
    lock.write_text('foreign ref lock')
    with pytest.raises(DomainError):
        apply(env)
    assert lock.read_text() == 'foreign ref lock'
    assert not (env.root / 'src/app.mjs').exists()


async def test_interrupted_files_without_installed_index_are_preserved_for_inspection(checkout, monkeypatch):
    env = checkout
    original = env.repository._run
    def interrupt(root, args, **kwargs):
        result = original(root, args, **kwargs)
        if 'read-tree' in args and '-u' in args:
            raise OSError('fixture failure after files but before index installation')
        return result
    monkeypatch.setattr(env.repository, '_run', interrupt)
    with pytest.raises(DomainError) as error:
        apply(env)
    assert error.value.code == 'development_checkout_interrupted'
    assert git(env.root, 'rev-parse', 'HEAD').stdout.decode().strip() == env.base
    assert (env.root / 'src/app.mjs').read_text() == 'export const version = 1;\n'
    monkeypatch.setattr(env.repository, '_run', original)
    with pytest.raises(DomainError) as error:
        apply(env)
    assert error.value.code == 'project_code_modified'
    assert (env.root / 'src/app.mjs').read_text() == 'export const version = 1;\n'


async def test_reverted_user_bytes_with_changed_stat_cache_do_not_block_later_candidate(checkout):
    env = checkout
    apply(env)
    second = await next_candidate(env)
    path = env.root / 'README.md'
    original = path.read_bytes()
    previous_stat = path.stat()
    path.write_text('temporary user edit\n')
    arguments = {'candidate': second, 'expected_head': env.candidate, 'expected_ref': env.branch,
                 'expected_branch': env.candidate}
    original_index = (env.git_dir / 'index').read_bytes()
    with pytest.raises(DomainError) as error:
        apply(env, **arguments)
    assert error.value.code == 'project_code_modified'
    assert path.read_text() == 'temporary user edit\n'
    assert (env.git_dir / 'index').read_bytes() == original_index
    path.write_bytes(original)
    os.utime(path, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns + 2_000_000_000))
    result = apply(env, **arguments)
    assert result['commit_oid'] == second and path.read_bytes() == original
    assert (env.root / 'src/app.mjs').read_text() == 'export const version = 2;\n'


@pytest.mark.parametrize('flag', ['--assume-unchanged', '--skip-worktree'])
async def test_hidden_index_flags_cannot_allow_overwriting_real_user_changes(checkout, flag):
    env = checkout
    apply(env)
    second = await next_candidate(env)
    path = env.root / 'src/app.mjs'
    assert git(env.root, 'update-index', flag, 'src/app.mjs').returncode == 0
    path.write_text('export const userVersion = 900;\n')
    index_before = (env.git_dir / 'index').read_bytes()
    with pytest.raises(DomainError):
        apply(env, candidate=second, expected_head=env.candidate, expected_ref=env.branch, expected_branch=env.candidate)
    assert path.read_text() == 'export const userVersion = 900;\n'
    assert (env.git_dir / 'index').read_bytes() == index_before
    assert git(env.root, 'rev-parse', 'HEAD').stdout.decode().strip() == env.candidate

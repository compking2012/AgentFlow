import asyncio
import json
import os
from pathlib import Path

import pytest

from agentflow.common import DomainError, canonical_digest
from agentflow.repository import RepositoryAdapter
from agentflow.runtime.launcher import atomic_json
from agentflow.runtime.workspace import PROJECT_MARKER, WorkspaceManager


@pytest.fixture
def project(tmp_path):
    path = tmp_path / 'project'
    path.mkdir()
    repository = RepositoryAdapter()
    repository._run(path, ['init', '-b', 'main'])
    (path / 'app.py').write_text('VALUE = 1\n')
    repository._run(path, ['add', 'app.py'])
    repository._run(path, ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '-m', 'base'])
    return path, repository._run(path, ['rev-parse', 'HEAD']).decode().strip(), repository


async def test_parallel_project_clones_are_visible_isolated_registered_and_ignored(project, tmp_path):
    root, base, repository = project
    manager = WorkspaceManager(tmp_path / 'controller', repository)
    exclude = root / '.git/info/exclude'
    original_exclude = exclude.read_bytes()
    workspaces = await asyncio.gather(*(manager.create_clone(root, base, attempt,
        project_root=root, project_id='project-one') for attempt in ('attempt-a', 'attempt-b')))
    assert workspaces[0] != workspaces[1]
    for attempt, path in zip(('attempt-a', 'attempt-b'), workspaces, strict=True):
        name = canonical_digest(attempt).split(':')[1]
        assert path == root / '.agentflow/workspaces' / name
        assert path.is_dir() and (path / '.git').is_dir()
        assert not (path / '.git/objects/info/alternates').exists()
        assert not (manager.root / name).exists()
        record = manager.registration(attempt)
        assert record['project_id'] == 'project-one' and record['project_root'] == str(root)
        assert record['path'] == str(path) and record['version'] == 2
        manager.assert_owned(path, attempt_id=attempt)
        manager.assert_owned(path)
    (workspaces[0] / 'app.py').write_text('VALUE = 2\n')
    assert (workspaces[1] / 'app.py').read_text() == (root / 'app.py').read_text() == 'VALUE = 1\n'
    assert await manager.create_clone(root, base, 'attempt-a', project_root=root, project_id='project-one') == workspaces[0]
    assert (workspaces[0] / 'app.py').read_text() == 'VALUE = 2\n'
    assert repository._run(root, ['rev-parse', 'HEAD']).decode().strip() == base
    assert repository._run(root, ['status', '--porcelain', '--untracked-files=all']) == b''
    assert exclude.read_bytes().startswith(original_exclude) and exclude.read_bytes().count(b'/.agentflow/') == 1
    assert not (root / '.gitignore').exists()


async def test_legacy_registered_attempt_replays_without_migration_when_project_options_are_added(project, tmp_path):
    root, base, repository = project
    manager = WorkspaceManager(tmp_path / 'controller', repository)
    legacy = await manager.create_clone(root, base, 'old-attempt')
    manager.assert_owned(legacy)
    manager.assert_owned(legacy, attempt_id='old-attempt')
    assert legacy.parent == manager.root
    record = manager.registration('old-attempt')
    assert 'version' not in record
    assert await manager.create_clone(root, base, 'old-attempt', project_root=root, project_id='project-one') == legacy
    assert not (root / '.agentflow').exists()
    assert manager.registration('old-attempt') == record


@pytest.mark.parametrize('kind', ['unowned', 'tracked', 'linked'])
async def test_existing_project_namespace_is_never_taken_over(project, tmp_path, kind):
    root, base, repository = project
    namespace = root / '.agentflow'
    if kind == 'linked':
        outside = tmp_path / 'outside'
        outside.mkdir()
        (outside / 'owner.txt').write_text('preserve')
        namespace.symlink_to(outside, target_is_directory=True)
    else:
        namespace.mkdir()
        (namespace / 'owner.txt').write_text('preserve')
        if kind == 'tracked':
            repository._run(root, ['add', '.agentflow/owner.txt'])
            repository._run(root, ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '-m', 'user namespace'])
            base = repository._run(root, ['rev-parse', 'HEAD']).decode().strip()
    manager = WorkspaceManager(tmp_path / 'controller', repository)
    before = (root / '.git/info/exclude').read_bytes()
    with pytest.raises(DomainError):
        await manager.create_clone(root, base, 'attempt', project_root=root, project_id='project-one')
    assert (namespace / 'owner.txt').read_text() == 'preserve'
    assert (root / '.git/info/exclude').read_bytes() == before
    assert not list(manager.metadata.glob('*.json'))


async def test_owned_checks_reject_project_root_sibling_attempt_descendant_and_symlink(project, tmp_path):
    root, base, repository = project
    manager = WorkspaceManager(tmp_path / 'controller', repository)
    first = await manager.create_clone(root, base, 'first', project_root=root, project_id='project')
    second = await manager.create_clone(root, base, 'second', project_root=root, project_id='project')
    alias = tmp_path / 'alias'
    alias.symlink_to(first, target_is_directory=True)
    (first / 'src').mkdir()
    for path in (root, second, first / 'src', alias):
        with pytest.raises(DomainError) as error:
            manager.assert_owned(path, attempt_id='first')
        assert error.value.code == 'workspace_not_isolated'


@pytest.mark.parametrize('tamper', ['metadata_link', 'metadata_path', 'project_marker', 'workspace_parent_link'])
async def test_registration_cannot_be_redirected(project, tmp_path, tamper):
    root, base, repository = project
    manager = WorkspaceManager(tmp_path / 'controller', repository)
    path = await manager.create_clone(root, base, 'attempt', project_root=root, project_id='project')
    record_path = manager.metadata / (canonical_digest('attempt').split(':')[1] + '.json')
    if tamper == 'metadata_link':
        external = tmp_path / 'untrusted.json'
        external.write_bytes(record_path.read_bytes())
        external.chmod(0o600)
        record_path.unlink()
        record_path.symlink_to(external)
    elif tamper == 'metadata_path':
        atomic_json(record_path, {**json.loads(record_path.read_text()), 'path': str(root)})
    elif tamper == 'project_marker':
        marker = root / '.agentflow' / PROJECT_MARKER
        atomic_json(marker, {**json.loads(marker.read_text()), 'project_id': 'different-project'})
    else:
        parent = path.parent
        external = tmp_path / 'moved-workspaces'
        parent.rename(external)
        parent.symlink_to(external, target_is_directory=True)
    with pytest.raises(DomainError):
        manager.assert_owned(path, attempt_id='attempt')


async def test_project_and_source_identity_are_checked_on_replay(project, tmp_path):
    root, base, repository = project
    manager = WorkspaceManager(tmp_path / 'controller', repository)
    path = await manager.create_clone(root, base, 'attempt', project_root=root, project_id='project')
    with pytest.raises(DomainError, match='another project'):
        await manager.create_clone(root, base, 'attempt', project_root=root, project_id='different')
    with pytest.raises(DomainError, match='input identity'):
        await manager.create_clone(root, 'HEAD', 'attempt', project_root=root, project_id='project')
    assert (path / 'app.py').read_text() == 'VALUE = 1\n'


async def test_readonly_registration_lookup_creates_no_directories(tmp_path):
    data = tmp_path / 'missing'
    manager = WorkspaceManager(data, create=False)
    with pytest.raises(DomainError):
        manager.registration('absent')
    assert not data.exists()


async def test_linked_git_exclusion_file_is_not_modified(project, tmp_path):
    root, base, repository = project
    outside = tmp_path / 'owner-file'
    outside.write_text('preserve')
    exclude = root / '.git/info/exclude'
    exclude.unlink()
    exclude.symlink_to(outside)
    with pytest.raises(DomainError):
        await WorkspaceManager(tmp_path / 'controller', repository).create_clone(root, base, 'attempt',
            project_root=root, project_id='project')
    assert outside.read_text() == 'preserve'


async def test_special_git_exclusion_file_is_rejected_without_blocking(project, tmp_path):
    root, base, repository = project
    exclude = root / '.git/info/exclude'
    exclude.unlink()
    os.mkfifo(exclude)
    with pytest.raises(DomainError):
        await asyncio.wait_for(WorkspaceManager(tmp_path / 'controller', repository).create_clone(root, base, 'attempt',
            project_root=root, project_id='project'), timeout=3)


async def test_linked_worktree_projects_keep_project_local_independent_agent_clones(project, tmp_path):
    root, base, repository = project
    linked = [tmp_path / 'linked-a', tmp_path / 'linked-b']
    for index, path in enumerate(linked):
        repository._run(root, ['worktree', 'add', '-b', f'linked-{index}', str(path), base])
        assert (path / '.git').is_file()
    exclude = root / '.git/info/exclude'
    before = exclude.read_bytes()
    manager = WorkspaceManager(tmp_path / 'controller', repository)
    clones = await asyncio.gather(*(manager.create_clone(path, base, f'attempt-{index}',
        project_root=path, project_id=f'linked-project-{index}') for index, path in enumerate(linked)))
    for index, (path, clone) in enumerate(zip(linked, clones, strict=True)):
        assert clone.parent == path / '.agentflow/workspaces'
        manager.assert_owned(clone, attempt_id=f'attempt-{index}')
        assert (clone / '.git').is_dir() and not (clone / '.git/objects/info/alternates').exists()
        assert repository._run(path, ['status', '--porcelain', '--untracked-files=all']) == b''
        assert repository._run(path, ['rev-parse', 'HEAD']).decode().strip() == base
        assert (path / 'app.py').read_text() == 'VALUE = 1\n'
    assert exclude.read_bytes().startswith(before) and exclude.read_bytes().count(b'/.agentflow/') == 1
    assert repository._run(root, ['status', '--porcelain', '--untracked-files=all']) == b''
    assert not list(manager.root.iterdir())


async def test_linked_worktree_admin_back_pointer_must_match_the_registered_project(project, tmp_path):
    root, base, repository = project
    linked = tmp_path / 'linked'
    repository._run(root, ['worktree', 'add', '-b', 'linked-branch', str(linked), base])
    git_dir = Path(repository._run(linked, ['rev-parse', '--absolute-git-dir']).decode().strip())
    (git_dir / 'gitdir').write_text(str(root / '.git') + '\n')
    before = (root / '.git/info/exclude').read_bytes()
    manager = WorkspaceManager(tmp_path / 'controller', repository)
    with pytest.raises(DomainError, match='does not match this project'):
        await manager.create_clone(linked, base, 'attempt', project_root=linked, project_id='project')
    assert (root / '.git/info/exclude').read_bytes() == before
    assert not (linked / '.agentflow').exists()

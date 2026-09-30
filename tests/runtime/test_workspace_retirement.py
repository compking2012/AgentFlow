"""Retirement mechanics only; caller-side task/process/reference gates are separate."""
import asyncio
import json

import pytest
from test_project_workspaces import project as project

from agentflow.common import DomainError, canonical_digest
from agentflow.control.backups import ApplicationBackup
from agentflow.runtime import workspace_retirement
from agentflow.runtime.launcher import atomic_json
from agentflow.runtime.workspace import WorkspaceManager
from agentflow.storage import Store


async def disposable(project, tmp_path, project_layout=True):
    root, base, repository = project
    manager = WorkspaceManager(tmp_path / 'controller', repository)
    options = {'project_root': root, 'project_id': 'project'} if project_layout else {}
    path = await manager.create_clone(root, base, 'readonly-attempt', **options)
    return manager, path, options


@pytest.mark.parametrize('project_layout', [False, True])
async def test_retirement_is_idempotent_and_tombstone_prevents_recreating_the_attempt(project, tmp_path, project_layout):
    manager, path, options = await disposable(project, tmp_path, project_layout)
    root, base, repository = project
    name = canonical_digest('readonly-attempt').split(':')[1]
    first = await manager.retire('readonly-attempt')
    assert first['state'] == 'retired' and first['path'] == str(path)
    assert first['retired'] is True
    assert first['bytes'] > (root / 'app.py').stat().st_size
    assert first['bytes_kind'] == 'logical'
    assert not path.exists() and not (manager.metadata / (name + '.json')).exists()
    assert (manager.metadata / 'retired' / (name + '.json')).is_file()
    assert not list((manager.metadata / 'retiring').iterdir())
    assert await manager.retire('readonly-attempt') == first
    with pytest.raises(DomainError) as error:
        await manager.create_clone(root, base, 'readonly-attempt', **options)
    assert error.value.code == 'workspace_retired'
    assert not path.exists()
    assert (root / 'app.py').read_text() == 'VALUE = 1\n'
    assert repository._run(root, ['rev-parse', 'HEAD']).decode().strip() == base
    assert repository._run(root, ['status', '--porcelain', '--untracked-files=all']) == b''


@pytest.mark.parametrize('project_layout', [False, True])
@pytest.mark.parametrize('child', ['', 'app.py', 'future-artifacts/source.bundle'])
async def test_source_references_preserve_workspace_before_retirement(project, tmp_path, project_layout, child):
    manager, path, _ = await disposable(project, tmp_path, project_layout)
    with pytest.raises(DomainError) as error:
        await manager.retire('readonly-attempt', protected_paths=[path / child])
    assert error.value.code == 'workspace_referenced'
    assert (path / 'app.py').read_text() == 'VALUE = 1\n'
    assert manager.registration('readonly-attempt')['path'] == str(path)
    assert not list((manager.metadata / 'retiring').glob('*.json'))
    assert not list(path.parent.glob('.retired-*'))


async def test_parent_project_reference_does_not_prevent_child_workspace_retirement(project, tmp_path):
    manager, path, _ = await disposable(project, tmp_path)
    result = await manager.retire('readonly-attempt', protected_paths=[project[0]])
    assert result['retired'] is True and not path.exists()
    assert (project[0] / 'app.py').read_text() == 'VALUE = 1\n'


async def test_relative_protected_paths_are_rejected_without_retiring_workspace(project, tmp_path):
    manager, path, _ = await disposable(project, tmp_path)
    with pytest.raises(DomainError) as error:
        await manager.retire('readonly-attempt', protected_paths=['app.py'])
    assert error.value.code == 'workspace_retirement_conflict'
    assert manager.registration('readonly-attempt')['path'] == str(path)
    assert not list((manager.metadata / 'retiring').glob('*.json'))


@pytest.mark.parametrize('reference_location', ['link_inside', 'alias_outside'])
async def test_protected_symlink_paths_cannot_hide_workspace_references(project, tmp_path, reference_location):
    manager, path, _ = await disposable(project, tmp_path)
    outside = tmp_path / 'outside-data'
    outside.write_text('Keep external data.\n')
    if reference_location == 'link_inside':
        reference = path / '.git/source-link'
        reference.symlink_to(outside)
    else:
        reference = tmp_path / 'workspace-alias'
        reference.symlink_to(path / 'app.py')
    with pytest.raises(DomainError) as error:
        await manager.retire('readonly-attempt', protected_paths=[reference])
    assert error.value.code == 'workspace_referenced'
    assert path.exists() and reference.is_symlink()
    assert outside.read_text() == 'Keep external data.\n'


@pytest.mark.parametrize('change', ['tracked', 'untracked', 'ignored'])
async def test_changed_or_additional_workspace_files_are_preserved(project, tmp_path, change):
    manager, path, _ = await disposable(project, tmp_path)
    name = 'app.py' if change == 'tracked' else 'extra.txt'
    if change == 'ignored':
        (path / '.git/info').mkdir(exist_ok=True)
        (path / '.git/info/exclude').write_text('extra.txt\n')
    (path / name).write_text('Uncommitted user data must remain.\n')
    with pytest.raises(DomainError) as error:
        await manager.retire('readonly-attempt')
    assert error.value.code == 'workspace_not_disposable'
    assert (path / name).read_text() == 'Uncommitted user data must remain.\n'
    assert manager.registration('readonly-attempt')['path'] == str(path)
    assert not list((manager.metadata / 'retiring').glob('*.json'))


@pytest.mark.parametrize('interruption', ['after_rename', 'before_trash_removal'])
async def test_interrupted_retirement_replays_without_rebuilding_or_losing_ownership(project, tmp_path, monkeypatch, interruption):
    manager, path, options = await disposable(project, tmp_path)
    name = canonical_digest('readonly-attempt').split(':')[1]
    root, base, _ = project
    with monkeypatch.context() as patch:
        if interruption == 'after_rename':
            original = workspace_retirement.atomic_json
            def interrupted(target, value):
                if target.parent.name == 'retired':
                    raise OSError('Fixture interruption after the atomic directory rename')
                return original(target, value)
            patch.setattr(workspace_retirement, 'atomic_json', interrupted)
        else:
            original = workspace_retirement.shutil.rmtree
            def interrupted_remove(target, *args, **kwargs):
                if str(target) == str(path.parent / ('.retired-' + name)):
                    raise OSError('Fixture interruption before trash removal')
                return original(target, *args, **kwargs)
            patch.setattr(workspace_retirement.shutil, 'rmtree', interrupted_remove)
        with pytest.raises(DomainError):
            await manager.retire('readonly-attempt')
    assert not path.exists()
    trash = path.parent / ('.retired-' + name)
    assert (trash / 'app.py').read_text() == 'VALUE = 1\n'
    assert (manager.metadata / 'retiring' / (name + '.json')).exists()
    store = Store(manager.data_dir)
    await store.start()
    try:
        with pytest.raises(DomainError) as backup_error:
            await ApplicationBackup(store, manager.data_dir).create(tmp_path / 'unfinished-backup')
        assert backup_error.value.code == 'backup_workspace_retiring'
    finally:
        await store.close()
    with pytest.raises(DomainError) as error:
        await manager.create_clone(root, base, 'readonly-attempt', **options)
    assert error.value.code == 'workspace_retired'
    restarted = WorkspaceManager(manager.data_dir, manager.repository)
    result = await restarted.retire('readonly-attempt')
    assert result['state'] == 'retired' and not trash.exists()
    assert not (manager.metadata / (name + '.json')).exists()
    assert not list((manager.metadata / 'retiring').iterdir())


@pytest.mark.parametrize('reference_location', ['source', 'trash'])
async def test_new_reference_after_rename_blocks_interrupted_retirement_replay(project, tmp_path, monkeypatch, reference_location):
    manager, path, _ = await disposable(project, tmp_path)
    name = canonical_digest('readonly-attempt').split(':')[1]
    original = workspace_retirement.atomic_json
    def interrupted(target, value):
        if target.parent.name == 'retired':
            raise OSError('Fixture interruption after rename before tombstone')
        return original(target, value)
    with monkeypatch.context() as patch:
        patch.setattr(workspace_retirement, 'atomic_json', interrupted)
        with pytest.raises(DomainError):
            await manager.retire('readonly-attempt')
    trash = path.parent / ('.retired-' + name)
    referenced = (path if reference_location == 'source' else trash) / 'app.py'
    restarted = WorkspaceManager(manager.data_dir, manager.repository)
    with pytest.raises(DomainError) as error:
        await restarted.retire('readonly-attempt', protected_paths=[referenced])
    assert error.value.code == 'workspace_referenced'
    assert not path.exists() and (trash / 'app.py').read_text() == 'VALUE = 1\n'
    assert (manager.metadata / 'retiring' / (name + '.json')).exists()
    assert not (manager.metadata / 'retired' / (name + '.json')).exists()
    result = await restarted.retire('readonly-attempt')
    assert result['retired'] is True and not trash.exists()
    assert await restarted.retire('readonly-attempt') == result


async def test_partial_trash_deletion_after_tombstone_can_resume(project, tmp_path, monkeypatch):
    manager, path, _ = await disposable(project, tmp_path)
    metadata_files = path / '.git/retirement-fixture'
    metadata_files.mkdir()
    for index in range(260):
        (metadata_files / str(index)).write_text('Fixture metadata covered by the deletion inventory.\n')
    name = canonical_digest('readonly-attempt').split(':')[1]
    trash = path.parent / ('.retired-' + name)
    original = workspace_retirement.shutil.rmtree
    def partial_remove(target, *args, **kwargs):
        if str(target) == str(trash):
            original(trash / '.git')
            raise OSError('Fixture interruption after Git data was deleted')
        return original(target, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(workspace_retirement.shutil, 'rmtree', partial_remove)
        with pytest.raises(DomainError):
            await manager.retire('readonly-attempt')
    assert not (trash / '.git').exists()
    assert (trash / 'app.py').read_text() == 'VALUE = 1\n'
    assert not (manager.metadata / (name + '.json')).exists()
    assert (manager.metadata / 'retiring' / (name + '.json')).stat().st_size > 65536
    tombstone = json.loads((manager.metadata / 'retired' / (name + '.json')).read_text())
    assert tombstone['result']['retired'] is True and tombstone['result']['bytes'] > 0
    restarted = WorkspaceManager(manager.data_dir, manager.repository)
    result = await restarted.retire('readonly-attempt')
    assert result == tombstone['result']
    assert not trash.exists() and not path.exists()
    assert not list((manager.metadata / 'retiring').iterdir())
    assert await restarted.retire('readonly-attempt') == result
    assert (project[0] / 'app.py').read_text() == 'VALUE = 1\n'


@pytest.mark.parametrize('change', ['added_file', 'modified_file', 'added_directory'])
async def test_partial_deletion_replay_preserves_new_or_modified_survivors(project, tmp_path, monkeypatch, change):
    manager, path, _ = await disposable(project, tmp_path)
    name = canonical_digest('readonly-attempt').split(':')[1]
    trash = path.parent / ('.retired-' + name)
    original = workspace_retirement.shutil.rmtree
    def partial_remove(target, *args, **kwargs):
        if str(target) == str(trash):
            original(trash / '.git')
            raise OSError('Fixture interruption after partial deletion')
        return original(target, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(workspace_retirement.shutil, 'rmtree', partial_remove)
        with pytest.raises(DomainError):
            await manager.retire('readonly-attempt')
    if change == 'modified_file':
        changed = trash / 'app.py'
        changed.write_text('VALUE = 2\n')
    else:
        changed = trash / 'new-owner-data'
        if change == 'added_directory':
            changed.mkdir()
        else:
            changed.write_text('Preserve this new file.\n')
    restarted = WorkspaceManager(manager.data_dir, manager.repository)
    with pytest.raises(DomainError) as error:
        await restarted.retire('readonly-attempt')
    assert error.value.code == 'workspace_not_disposable'
    assert changed.exists() and trash.exists()
    if change == 'modified_file':
        assert changed.read_text() == 'VALUE = 2\n'
    elif change == 'added_file':
        assert changed.read_text() == 'Preserve this new file.\n'
    assert (manager.metadata / 'retiring' / (name + '.json')).exists()
    assert not path.exists()
    assert (project[0] / 'app.py').read_text() == 'VALUE = 1\n'


async def test_tombstone_replay_still_checks_current_source_references(project, tmp_path):
    manager, path, _ = await disposable(project, tmp_path)
    first = await manager.retire('readonly-attempt')
    with pytest.raises(DomainError) as error:
        await manager.retire('readonly-attempt', protected_paths=[path / 'app.py'])
    assert error.value.code == 'workspace_referenced'
    assert await manager.retire('readonly-attempt') == first


async def test_retirement_intent_cannot_redirect_trash_removal(project, tmp_path, monkeypatch):
    manager, path, _ = await disposable(project, tmp_path)
    original = workspace_retirement.atomic_json
    def interrupted(target, value):
        if target.parent.name == 'retired':
            raise OSError('Fixture interruption')
        return original(target, value)
    with monkeypatch.context() as patch:
        patch.setattr(workspace_retirement, 'atomic_json', interrupted)
        with pytest.raises(DomainError):
            await manager.retire('readonly-attempt')
    intent = next((manager.metadata / 'retiring').glob('*.json'))
    outside = tmp_path / 'owner-data'
    outside.mkdir()
    (outside / 'keep.txt').write_text('preserve')
    atomic_json(intent, {**json.loads(intent.read_text()), 'trash_path': str(outside)})
    with pytest.raises(DomainError):
        await manager.retire('readonly-attempt')
    assert (outside / 'keep.txt').read_text() == 'preserve'
    assert any(path.parent.glob('.retired-*'))


async def test_concurrent_retirement_has_one_stable_result(project, tmp_path):
    manager, path, _ = await disposable(project, tmp_path)
    results = await asyncio.gather(*(manager.retire('readonly-attempt') for _ in range(3)))
    assert results[0] == results[1] == results[2]
    assert not path.exists()
    assert len(list((manager.metadata / 'retired').glob('*.json'))) == 1


async def test_backup_ignores_retired_lost_workspace_and_restores_tombstone(project, tmp_path):
    manager, path, options = await disposable(project, tmp_path)
    await manager.retire('readonly-attempt')
    store = Store(manager.data_dir)
    await store.start()
    try:
        backup = tmp_path / 'backup'
        result = await ApplicationBackup(store, manager.data_dir).create(backup)
        assert result['project_workspaces'] == []
        restored = tmp_path / 'restored'
        await ApplicationBackup.restore(backup, restored)
        resumed = WorkspaceManager(restored, create=False)
        with pytest.raises(DomainError) as error:
            await resumed.create_clone(project[0], project[1], 'readonly-attempt', **options)
        assert error.value.code == 'workspace_retired'
        assert not path.exists()
    finally:
        await store.close()

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import pytest_asyncio

from agentflow.common import DomainError, canonical_digest
from agentflow.control.backups import MANIFEST, ApplicationBackup
from agentflow.control.project_code import ProjectCodeService
from agentflow.repository import RepositoryAdapter
from agentflow.runtime.workspace import WorkspaceManager
from agentflow.storage import Store


@pytest_asyncio.fixture(params=[False, True], ids=['regular-project', 'linked-project'])
async def project_checkpoint(tmp_path, request):
    data = tmp_path / 'original-data'
    project = tmp_path / 'owner-project'
    git_origin = tmp_path / 'owner-git-origin' if request.param else project
    git_origin.mkdir()
    repository = RepositoryAdapter()
    repository._run(git_origin, ['init', '-b', 'main'])
    (git_origin / 'app.py').write_text('VALUE = 1\n')
    repository._run(git_origin, ['add', 'app.py'])
    repository._run(git_origin, ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '-m', 'base'])
    base = repository._run(git_origin, ['rev-parse', 'HEAD']).decode().strip()
    if request.param:
        repository._run(git_origin, ['worktree', 'add', '-b', 'project-view', str(project), base])
    store = Store(data)
    await store.start()
    workspace = await WorkspaceManager(data, repository).create_clone(project, base, 'attempt',
        project_root=project, project_id='project')
    (workspace / 'app.py').write_text('VALUE = 2\n')
    snapshot = await repository.freeze_workspace(workspace, base, 'Unreferenced recovery snapshot')
    (workspace / 'unfinished.txt').write_text('Preserve this uncommitted work.\n')
    (workspace / '.env.local').write_text('LOCAL_FIXTURE_SECRET=excluded\n')
    def seed(tx):
        tx.put('project', 'project', {'local_path': str(project), 'base_commit': base})
        tx.put('run', 'run', {'project_id': 'project', 'iteration_id': 'iteration', 'execution_state': 'paused'})
        tx.put('attempt', 'attempt', {'run_id': 'run', 'work_item_id': 'work', 'status': 'failed'})
        tx.put('dispatch_context', 'attempt', {'task': {'workspace': str(workspace), 'source_commit': base}})
        tx.put('code_snapshot', 'snapshot', {'repository_path': str(workspace), 'run_id': 'run',
            'commit_oid': snapshot['commit_oid'], 'base_oid': base})
        return {}
    await store.command('fixture', 'seed-project-layout', {}, seed)
    backup = tmp_path / 'backup'
    receipt = await ApplicationBackup(store, data).create(backup)
    yield {'store': store, 'data': data, 'project': project, 'workspace': workspace, 'backup': backup,
           'receipt': receipt, 'snapshot': snapshot, 'repository': repository, 'git_origin': git_origin}
    await store.close()


async def test_project_workspace_backup_preserves_drafts_and_restores_without_writing_user_project(project_checkpoint, tmp_path):
    env = project_checkpoint
    manifest = json.loads((env['backup'] / MANIFEST).read_text())
    assert manifest['version'] == 2 and len(manifest['project_workspaces']) == 1
    entry = manifest['project_workspaces'][0]
    assert entry['source_path'] == str(env['workspace']) and entry['project_id'] == 'project'
    assert (env['backup'] / 'managed' / entry['backup_path'] / 'unfinished.txt').is_file()
    assert not (env['backup'] / 'managed' / entry['backup_path'] / '.env.local').exists()
    assert any(record['path'] == str(env['project']) and not record['included']
               for record in manifest['external_repositories'])
    assert not any(record['path'] == str(env['workspace']) for record in manifest['external_repositories'])
    await env['store'].close()
    shutil.rmtree(env['data'])
    shutil.rmtree(env['project'])
    if env['git_origin'] != env['project']:
        shutil.rmtree(env['git_origin'])
    restored_data = tmp_path / 'restored-data'
    report = await ApplicationBackup.restore(env['backup'], restored_data)
    assert not report['automatic_resume_allowed'] and not env['project'].exists()
    restored = Store(restored_data)
    await restored.start()
    try:
        context = await restored.read('dispatch_context', 'attempt')
        workspace = Path(context['task']['workspace'])
        assert workspace.parent == restored_data / 'workspaces'
        assert report['path_mapping'][str(env['workspace'])] == str(workspace)
        assert (workspace / 'unfinished.txt').read_text() == 'Preserve this uncommitted work.\n'
        assert (workspace / 'app.py').read_text() == 'VALUE = 2\n'
        snapshot = await restored.read('code_snapshot', 'snapshot')
        assert snapshot['repository_path'] == str(workspace)
        assert env['repository']._run(workspace, ['show', snapshot['commit_oid'] + ':app.py']) == b'VALUE = 2\n'
        manager = WorkspaceManager(restored_data, create=False)
        manager.assert_owned(workspace, attempt_id='attempt')
        registration = manager.registration('attempt')
        assert registration['version'] == 1 and registration['restore_revalidation_required']
        assert registration['restored_project_registration']['path'] == str(env['workspace'])
        assert (await restored.read('project', 'project'))['local_path'] == str(env['project'])
    finally:
        await restored.close()


async def test_backup_workspace_mapping_cannot_redirect_restore_outside_new_private_root(project_checkpoint, tmp_path):
    env = project_checkpoint
    path = env['backup'] / MANIFEST
    manifest = json.loads(path.read_text())
    manifest['project_workspaces'][0]['backup_path'] = '../outside'
    manifest['fingerprint'] = canonical_digest({key: value for key, value in manifest.items() if key != 'fingerprint'})
    path.write_text(json.dumps(manifest))
    destination = tmp_path / 'must-not-create'
    with pytest.raises(DomainError):
        await ApplicationBackup.restore(env['backup'], destination)
    assert not destination.exists()


@pytest.mark.parametrize('terminal_state', ['completed', 'cancelled'])
async def test_restored_terminal_product_never_syncs_code_into_original_external_repository(
        project_checkpoint, tmp_path, terminal_state):
    env = project_checkpoint
    store, repository, project = env['store'], env['repository'], env['project']
    base_ref = repository._run(project, ['symbolic-ref', 'HEAD']).decode().strip()
    quality = 'passed' if terminal_state == 'completed' else 'unknown'
    def terminal_product(tx):
        original = tx.get('project', 'project')
        tx.put('project', 'project', {**original, 'base_ref': base_ref}, original['revision'])
        run = tx.get('run', 'run')
        tx.put('run', 'run', {**run, 'execution_state': terminal_state, 'quality_result': quality,
            'base_commit': env['snapshot']['base_oid'], 'base_ref': base_ref,
            'input_fingerprint': 'terminal-product-input'}, run['revision'])
        tx.put('product', 'product', {'project_id': 'project', 'run_id': 'run', 'state': terminal_state,
            'output_directory': str(tmp_path / 'owner-output')})
        tx.put('work_item', 'work', {'run_id': 'run', 'step': 'implementation', 'kind': 'aggregation',
            'generation': 1, 'status': 'completed', 'attempt_id': 'snapshot', 'quality_result': 'unknown'})
        snapshot = tx.get('code_snapshot', 'snapshot')
        tx.put('code_snapshot', 'snapshot', {**snapshot, 'work_item_id': 'work', 'generation': 1,
            'tree_oid': env['snapshot']['tree_oid'], 'stale': False}, snapshot['revision'])
        return {}
    await store.command('fixture', 'terminal-product', {}, terminal_product)
    backup = tmp_path / 'terminal-backup'
    await ApplicationBackup(store, env['data']).create(backup)
    await store.close()

    def external_state():
        # Includes Git objects/index/refs as well as source files. Linked Git
        # worktrees keep shared objects in the separate owner Git repository.
        roots = {project, env['git_origin'] / '.git'}
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for root in roots for path in root.rglob('*') if path.is_file() and not path.is_symlink()}

    before = external_state()
    original_head = repository._run(project, ['rev-parse', 'HEAD'])
    restored_data = tmp_path / 'restored-terminal-data'
    report = await ApplicationBackup.restore(backup, restored_data)
    restored = Store(restored_data)
    await restored.start()
    try:
        product_record = await restored.read('product', 'product')
        run_record = await restored.read('run', 'run')
        assert product_record['state'] == run_record['execution_state'] == terminal_state
        assert run_record['quality_result'] == quality
        assert product_record['restore_reconciliation_required'] is True
        assert run_record['restore_reconciliation_required'] is True
        assert 'run' not in report['paused_run_ids']
        assert (await restored.read('project', 'project'))['local_path'] == str(project)
        restored_snapshot = await restored.read('code_snapshot', 'snapshot')
        assert Path(restored_snapshot['repository_path']).is_relative_to(restored_data)
        assert repository._run(Path(restored_snapshot['repository_path']),
            ['show', restored_snapshot['commit_oid'] + ':app.py']) == b'VALUE = 2\n'

        service = ProjectCodeService(restored)
        assert await service.reconcile() == []
        assert await service.sync('run') is None
        assert await service.reconcile() == []
        assert not await restored.list('project_code_sync')
        assert repository._run(project, ['rev-parse', 'HEAD']) == original_head
        assert repository._run(project, ['symbolic-ref', 'HEAD']).decode().strip() == base_ref
        assert (project / 'app.py').read_text() == 'VALUE = 1\n'
        assert external_state() == before
    finally:
        await restored.close()

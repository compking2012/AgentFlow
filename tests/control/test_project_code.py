import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from agentflow.control.project_code import ProjectCodeService
from agentflow.repository import RepositoryAdapter
from agentflow.storage import Store


def git(path, *args):
    return subprocess.check_output(['/opt/homebrew/bin/git', '-c', 'user.name=Fixture', '-c',
        'user.email=fixture@localhost', '-c', 'commit.gpgSign=false', '-C', str(path), *args],
        env={**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}, stderr=subprocess.PIPE).decode().strip()


@pytest_asyncio.fixture
async def code_project(tmp_path):
    store = Store(tmp_path / 'data')
    await store.start()
    root = tmp_path / 'project'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    (root / 'README.md').write_text('Product\n')
    (root / '.gitignore').write_text('local*\n')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'baseline')
    base = git(root, 'rev-parse', 'HEAD')
    repo = RepositoryAdapter()
    source = tmp_path / 'attempt'
    await repo.clone_snapshot(root, source, base)
    (source / 'src').mkdir()
    (source / 'src/api.mjs').write_text('export const version = 1;\n')
    frozen = await repo.freeze_workspace(source, base, 'implementation')
    def seed(tx):
        tx.put('project', 'project', {'local_path': str(root), 'base_commit': base, 'base_ref': 'refs/heads/main'})
        tx.put('run', 'run', {'project_id': 'project', 'base_commit': base, 'base_ref': 'refs/heads/main',
            'input_fingerprint': 'original'})
        tx.put('work_item', 'work', {'run_id': 'run', 'step': 'implementation', 'kind': 'aggregation',
            'generation': 1, 'status': 'completed', 'attempt_id': 'attempt'})
        return tx.put('code_snapshot', 'attempt', {'work_item_id': 'work', 'generation': 1, 'run_id': 'run',
            'repository_path': str(source), 'commit_oid': frozen['commit_oid'], 'tree_oid': frozen['tree_oid'],
            'base_oid': base, 'stale': False})
    await store.command('fixture', 'seed', {}, seed)
    try:
        yield SimpleNamespace(store=store, root=root, base=base, source=source, frozen=frozen,
                              service=ProjectCodeService(store), repository=repo, temporary=tmp_path)
    finally:
        await store.close()


async def patch(env, record_kind, identity, **values):
    def write(tx):
        old = tx.get(record_kind, identity)
        return tx.put(record_kind, identity, {**old, **values}, old['revision'])
    return await env.store.command('fixture', os.urandom(8).hex(), values, write)


async def test_completed_assembly_is_visible_in_project_without_publishing(code_project):
    env = code_project
    result = await env.service.sync('run')
    assert result['state'] == 'ready' and Path(result['path']) == env.root
    assert (env.root / 'src/api.mjs').read_bytes() == (env.source / 'src/api.mjs').read_bytes()
    assert git(env.root, 'rev-parse', 'refs/heads/main') == env.base
    assert git(env.root, 'symbolic-ref', 'HEAD') == result['development_ref']
    assert git(env.root, 'status', '--porcelain') == ''
    assert not await env.store.list('delivery')
    before = await env.store.list('project_code_sync')
    assert await env.service.sync('run') == result
    assert await env.store.list('project_code_sync') == before


@pytest.mark.parametrize('changes', [{'kind': 'stage_child'}, {'status': 'failed'}, {'status': 'running'}])
async def test_partial_or_running_work_does_not_replace_project_checkout(code_project, changes):
    await patch(code_project, 'work_item', 'work', **changes)
    assert await code_project.service.sync('run') is None
    assert not (code_project.root / 'src/api.mjs').exists()


async def test_user_edits_are_preserved_and_can_be_retried_after_saving(code_project):
    env = code_project
    (env.root / 'README.md').write_text('User changes\n')
    result = await env.service.sync('run')
    assert result['state'] == 'blocked' and '本地改动' in result['error']
    assert (env.root / 'README.md').read_text() == 'User changes\n'
    assert git(env.root, 'symbolic-ref', 'HEAD') == 'refs/heads/main'
    (env.root / 'README.md').write_text('Product\n')
    assert (await env.service.sync('run'))['state'] == 'ready'


async def test_next_completed_version_advances_development_branch_only(code_project):
    env = code_project
    original = await env.service.sync('run')
    clone = env.temporary / 'second-attempt'
    await env.repository.clone_snapshot(env.source, clone, env.frozen['commit_oid'])
    (clone / 'src/api.mjs').write_text('export const version = 2;\n')
    updated = await env.repository.freeze_workspace(clone, env.frozen['commit_oid'], 'unit tests')
    await patch(env, 'work_item', 'work', generation=2, attempt_id='attempt-2')
    await env.store.command('fixture', 'snapshot-2', {}, lambda tx: tx.put('code_snapshot', 'attempt-2', {
        'run_id': 'run', 'work_item_id': 'work', 'generation': 2, 'repository_path': str(clone),
        'commit_oid': updated['commit_oid'], 'tree_oid': updated['tree_oid'], 'base_oid': env.frozen['commit_oid']}))
    result = await env.service.sync('run')
    assert result['state'] == 'ready' and result['development_ref'] == original['development_ref']
    assert (env.root / 'src/api.mjs').read_text() == 'export const version = 2;\n'
    assert git(env.root, 'rev-parse', 'main') == env.base


async def test_blocked_target_does_not_replace_the_last_applied_checkout(code_project):
    env = code_project
    first = await env.service.sync('run')
    (env.root / 'README.md').write_text('User editing\n')
    previous_repo, previous_commit = env.source, env.frozen['commit_oid']
    for version in (2, 3):
        source = env.temporary / f'phase-{version}'
        await env.repository.clone_snapshot(previous_repo, source, previous_commit)
        (source / 'src/api.mjs').write_text(f'export const version = {version};\n')
        snapshot = await env.repository.freeze_workspace(source, previous_commit, f'phase {version}')
        identity = f'attempt-{version}'
        await patch(env, 'work_item', 'work', generation=version, attempt_id=identity)
        await env.store.command('fixture', identity, {}, lambda tx: tx.put('code_snapshot', identity, {
            'run_id': 'run', 'work_item_id': 'work', 'generation': version, 'repository_path': str(source),
            'commit_oid': snapshot['commit_oid'], 'tree_oid': snapshot['tree_oid'], 'base_oid': previous_commit}))
        result = await env.service.sync('run')
        if version == 2:
            assert result['state'] == 'blocked'
            assert result['last_applied_commit'] == first['commit_oid']
            assert result['last_applied_ref'] == first['development_ref']
            (env.root / 'README.md').write_text('Product\n')
        else:
            assert result['state'] == 'ready', result
            assert result['last_applied_commit'] == snapshot['commit_oid']
        previous_repo, previous_commit = source, snapshot['commit_oid']
    assert (env.root / 'src/api.mjs').read_text() == 'export const version = 3;\n'


@pytest.mark.parametrize('included', [False, True])
async def test_sync_never_runs_project_local_filters(code_project, included):
    env = code_project
    canary = env.temporary / 'filter-ran'
    if included:
        extra = env.temporary / 'extra.config'
        extra.write_text('[filter "bad"]\nsmudge = touch ' + str(canary) + '\nclean = touch ' + str(canary) + '\n')
        git(env.root, 'config', 'include.path', str(extra))
    else:
        git(env.root, 'config', 'filter.bad.smudge', 'touch ' + str(canary))
        git(env.root, 'config', 'filter.bad.clean', 'touch ' + str(canary))
    (env.source / '.gitattributes').write_text('*.mjs filter=bad\n')
    updated = await env.repository.freeze_workspace(env.source, env.base, 'with attributes')
    await patch(env, 'code_snapshot', 'attempt', commit_oid=updated['commit_oid'], tree_oid=updated['tree_oid'])
    result = await env.service.sync('run')
    assert result['state'] == 'ready'
    assert not canary.exists()


async def test_ignored_user_file_is_not_overwritten(code_project):
    env = code_project
    (env.root / 'local.txt').write_text('User private content\n')
    (env.source / 'local.txt').write_text('Generated content\n')
    git(env.source, 'add', '-f', 'local.txt')
    updated = await env.repository.freeze_workspace(env.source, env.base, 'add file')
    await patch(env, 'code_snapshot', 'attempt', commit_oid=updated['commit_oid'], tree_oid=updated['tree_oid'])
    result = await env.service.sync('run')
    assert result['state'] == 'blocked'
    assert (env.root / 'local.txt').read_text() == 'User private content\n'


async def test_interrupted_checkout_recovers_without_repeating_product_work(code_project, monkeypatch):
    from agentflow.repository.development_checkout import _References

    env = code_project
    original = _References.commit
    interrupted = False
    def stop(transaction):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise OSError('fixture interruption')
        return original(transaction)
    monkeypatch.setattr(_References, 'commit', stop)
    assert (await env.service.sync('run'))['state'] == 'blocked'
    assert (await env.service.sync('run'))['state'] == 'ready'
    assert len(await env.store.list('project_code_sync')) == 1
    assert (await env.store.read('work_item', 'work'))['generation'] == 1


async def test_external_branch_change_is_not_replaced(code_project):
    env = code_project
    git(env.root, 'checkout', '-b', 'user-work')
    result = await env.service.sync('run')
    assert result['state'] == 'blocked'
    assert git(env.root, 'symbolic-ref', 'HEAD') == 'refs/heads/user-work'
    assert not (env.root / 'src/api.mjs').exists()


async def test_stale_snapshot_cannot_become_project_source(code_project):
    env = code_project
    await patch(env, 'code_snapshot', 'attempt', stale=True)
    assert await env.service.sync('run') is None
    assert git(env.root, 'rev-parse', 'HEAD') == env.base


async def test_finished_stage_can_sync_while_a_later_isolated_role_runs(code_project):
    env = code_project
    await env.store.command('fixture', 'review-running', {}, lambda tx: tx.put('work_item', 'review', {
        'run_id': 'run', 'step': 'code_review', 'generation': 1, 'status': 'running', 'attempt_id': 'review-attempt'}))
    result = await env.service.sync('run')
    assert result['state'] == 'ready'
    assert (await env.store.read('work_item', 'review'))['status'] == 'running'


@pytest.mark.parametrize('flag', ['restore_reconciliation_required', 'needs_restart', 'deleted_at'])
async def test_restored_or_stale_product_cannot_touch_its_original_repository(code_project, flag):
    env = code_project
    await env.store.command('fixture', 'unavailable-product', {}, lambda tx: tx.put('product', 'product', {
        'run_id': 'run', 'project_id': 'project', flag: True}))
    assert await env.service.reconcile() == []
    assert await env.service.sync('run') is None
    assert git(env.root, 'rev-parse', 'HEAD') == env.base


async def review_repair_sources(env, *, source_step='implementation', assembly_complete=False):
    from copy import deepcopy

    from agentflow.common import canonical_digest
    await patch(env, 'work_item', 'work', step=source_step)
    original = await env.service.sync('run')
    snapshots = {}
    for name, version in [('migration', 20), ('production', 30), ('assembly', 40)]:
        folder = env.temporary / ('contract-' + name)
        await env.repository.clone_snapshot(env.source, folder, env.frozen['commit_oid'])
        (folder / 'src/api.mjs').write_text(f'export const version = {version};\n')
        snapshots[name] = {**await env.repository.freeze_workspace(folder, env.frozen['commit_oid'], name),
                           'repository_path': str(folder)}
    def seed(tx):
        specs = {}
        for name, step in [('migration', 'review_integration_migration'), ('production', 'implementation'), ('assembly', 'implementation')]:
            spec = {'run_id': 'run', 'project_id': 'project', 'key': 'repair-' + name, 'step': step,
                'kind': 'aggregation' if name == 'assembly' else 'stage', 'role': 'development',
                'write_paths': [], 'required': True, 'approval_required': False,
                'dependencies': ['migration', 'production'] if name == 'assembly' else [],
                'payload': {'review_contract_task': 'contract', 'review_contract_kind': 'assembly' if name == 'assembly'
                            else 'test_contract_migration' if name == 'migration' else 'production_fix'}}
            specs[name] = deepcopy(spec)
            tx.put('work_item', name, {**spec, 'generation': 1, 'status': 'completed' if name != 'assembly' or assembly_complete else 'pending',
                'quality_result': 'unknown', 'attempt_id': name + '-snapshot' if name != 'assembly' or assembly_complete else None})
            tx.put('code_snapshot', name + '-snapshot', {**snapshots[name], 'run_id': 'run',
                'work_item_id': name, 'generation': 1, 'stale': False})
        source = tx.get('code_snapshot', 'attempt')
        context = {'snapshot': source, 'source_snapshot_id': source['id']}
        tx.put('review_contract_repair', 'contract', {'actor': 'controller', 'run_id': 'run', 'state': 'repairing',
            'context': context, 'context_digest': canonical_digest(context), 'work_specs': specs,
            'assembly_work_item_id': 'assembly', 'repair_work_item_ids': ['migration', 'production']})
        return {}
    await env.store.command('fixture', 'contract-sources', {}, seed)
    return original, snapshots


async def test_internal_completed_actions_never_crash_or_replace_visible_source(code_project):
    env = code_project
    original, _ = await review_repair_sources(env)
    result = await env.service.sync('run')
    assert result is None or result['commit_oid'] == original['commit_oid']
    assert (env.root / 'src/api.mjs').read_text() == 'export const version = 1;\n'


@pytest.mark.parametrize('source_step', ['implementation', 'unit_test_implementation', 'integration_test_implementation'])
async def test_only_complete_bound_assembly_replaces_its_original_logical_phase(code_project, source_step):
    env = code_project
    _, snapshots = await review_repair_sources(env, source_step=source_step, assembly_complete=True)
    result = await env.service.sync('run')
    assert result and result['state'] == 'ready'
    assert result['snapshot_id'] == 'assembly-snapshot'
    assert result['commit_oid'] == snapshots['assembly']['commit_oid']
    assert (env.root / 'src/api.mjs').read_text() == 'export const version = 40;\n'
    assert git(env.root, 'rev-parse', 'refs/heads/main') == env.base


@pytest.mark.parametrize('damage', ['unmanaged', 'incomplete_child', 'stale_child'])
async def test_incomplete_or_unmanaged_review_assembly_preserves_original_source(code_project, damage):
    env = code_project
    original, _ = await review_repair_sources(env, assembly_complete=True)
    if damage == 'unmanaged':
        await patch(env, 'review_contract_repair', 'contract', actor='model')
    elif damage == 'incomplete_child':
        await patch(env, 'work_item', 'migration', status='running')
    else:
        await patch(env, 'code_snapshot', 'migration-snapshot', stale=True)
    result = await env.service.sync('run')
    assert result is None or result['commit_oid'] == original['commit_oid']
    assert (env.root / 'src/api.mjs').read_text() == 'export const version = 1;\n'

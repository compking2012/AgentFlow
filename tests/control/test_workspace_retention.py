"""Readonly retention with real temporary Store/Git data and stopped-process receipts."""
import asyncio
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import psutil
import pytest
import pytest_asyncio

from agentflow.common import DomainError, canonical_digest
from agentflow.control.service import WorkflowService
from agentflow.control.workspace_retention import ReadonlyWorkspaceRetention
from agentflow.repository import RepositoryAdapter
from agentflow.runtime import workspace_retirement
from agentflow.runtime.launcher import atomic_json
from agentflow.runtime.maintenance import RuntimeMaintenance
from agentflow.runtime.workspace import WorkspaceManager
from agentflow.storage import Store


async def update(store, kind, identity, **fields):
    def write(tx):
        current = tx.get(kind, identity)
        return tx.put(kind, identity, {**(current or {}), **fields}, current['revision'] if current else None)
    return await store.command('fixture.patch', str(uuid4()), {}, write)


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch, request):
    monkeypatch.setattr(psutil, 'process_iter', lambda *args, **kwargs: [])
    project = tmp_path / 'owner-project'
    project.mkdir()
    repository = RepositoryAdapter()
    repository._run(project, ['init', '-b', 'main'])
    (project / 'app.py').write_text('VALUE = 1\n')
    repository._run(project, ['add', 'app.py'])
    repository._run(project, ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '-m', 'base'])
    base = repository._run(project, ['rev-parse', 'HEAD']).decode().strip()
    store = Store(tmp_path / 'controller')
    await store.start()
    try:
        manager = WorkspaceManager(store.data_dir, repository)
        options = {'project_root': project, 'project_id': 'project'} if getattr(request, 'param', True) else {}
        path = await manager.create_clone(project, base, 'attempt', **options)
        attempt = await update(store, 'attempt', 'attempt', run_id='run', iteration_id='iteration',
            work_item_id='work', status='completed', generation=1, fencing_token=1, input_fingerprint='fixture-input')
        task = {key: attempt[key] for key in ('run_id', 'iteration_id', 'work_item_id', 'fencing_token', 'input_fingerprint')}
        task.update(attempt_id='attempt', step='research', role='research', allowed_write_paths=[],
                    output_schema={'type': 'object'}, workspace=str(path))
        await update(store, 'dispatch_context', 'attempt', task=task)
        await update(store, 'project', 'project', local_path=str(project), base_commit=base)
        directory = store.data_dir / 'supervisor' / canonical_digest({'attempt_id': 'attempt'}).split(':')[1]
        directory.mkdir(parents=True, mode=0o700)
        process = {'attempt_id': 'attempt', 'operation_id': 'attempt', 'nonce': 'fixture-nonce', 'fencing_token': 1,
                   'pid': 1073741000, 'process_started_at': 1.0,
                   'boot_fingerprint': canonical_digest({'boot_time': psutil.boot_time()})}
        atomic_json(directory / 'result.json', {**process, 'execution_status': 'completed', 'exit_code': 0})
        atomic_json(directory / 'child.json', {**process, 'child': {'pid': 1073741001, 'process_started_at': 1.0}})
        await update(store, 'supervised_attempt', 'attempt', **process, run_id='run', state='completed',
            backend='openhands_role', directory=str(directory), input_fingerprint='fixture-input')
        await update(store, 'model_invocation', 'call', attempt_id='attempt', run_id='run', iteration_id='iteration',
            fencing_token=1, input_fingerprint='fixture-input', state='completed_unpriced')
        await update(store, 'model_attempt_budget', 'attempt', request_count=2, uncertain_invocations=0)
        maintenance = RuntimeMaintenance(store, store.data_dir)
        name = canonical_digest('attempt').split(':')[1]
        yield SimpleNamespace(store=store, manager=manager, maintenance=maintenance,
            retention=ReadonlyWorkspaceRetention(store, manager, maintenance), project=project,
            path=path, task=task, directory=directory, repository=repository, base=base,
            tombstone=manager.metadata / 'retired' / (name + '.json'))
    finally:
        await store.close()


@pytest.mark.parametrize('env', [False, True], indirect=True, ids=['legacy', 'project'])
async def test_completed_readonly_workspace_is_retired_once_with_evidence_and_project_preserved(env):
    retained = ('attempt', 'supervised_attempt', 'dispatch_context', 'model_invocation', 'model_attempt_budget', 'project')
    before = {kind: await env.store.list(kind) for kind in retained}
    proofs = {name: (env.directory / name).read_bytes() for name in ('result.json', 'child.json')}
    result = await env.retention.sweep()
    assert result == {'retired': 1, 'skipped': {}}
    assert not env.path.exists() and env.tombstone.is_file()
    record = await env.store.read('workspace_retirement', 'attempt')
    assert record['result'] == json.loads(env.tombstone.read_text())['result']
    assert record['result']['retired'] is True and record['result']['bytes'] > 0
    assert record['result']['bytes_kind'] == 'logical'
    assert not list((env.manager.metadata / 'retiring').glob('*.json'))
    for kind, rows in before.items():
        assert await env.store.list(kind) == rows
    assert {name: (env.directory / name).read_bytes() for name in proofs} == proofs
    assert (env.project / 'app.py').read_text() == 'VALUE = 1\n'
    assert env.repository._run(env.project, ['rev-parse', 'HEAD']).decode().strip() == env.base
    assert env.repository._run(env.project, ['status', '--porcelain', '--untracked-files=all']) == b''
    assert await env.retention.sweep() == {'retired': 0, 'skipped': {}}
    assert await env.store.list('workspace_retirement') == [record]


@pytest.mark.parametrize('step,role,write_paths', [
    ('implementation', 'development', ['app.py']),
    ('unit_test_implementation', 'unit_test', []),
    ('integration_test_implementation', 'integration_test', []),
    ('research', 'development', []),
    ('research', 'research', ['notes.md']),
    ('unknown_step', 'research', []),
])
async def test_coding_or_unproven_readonly_tasks_keep_their_clone(env, step, role, write_paths):
    await update(env.store, 'dispatch_context', 'attempt', task={**env.task, 'step': step,
                 'role': role, 'allowed_write_paths': write_paths})
    assert (await env.retention.sweep())['retired'] == 0
    assert env.path.is_dir() and not env.tombstone.exists()
    assert not await env.store.list('workspace_retirement')


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'blocked', 'running', 'execution_unknown'])
async def test_attempt_must_have_completed_successfully_before_retirement(env, status):
    await update(env.store, 'attempt', 'attempt', status=status)
    assert (await env.retention.sweep())['retired'] == 0
    assert env.manager.registration('attempt')['path'] == str(env.path)
    assert not env.tombstone.exists() and not await env.store.list('workspace_retirement')


@pytest.mark.parametrize('status', ['running', 'execution_unknown', 'unrecognized_state'])
async def test_any_other_active_or_unknown_attempt_preserves_completed_clones(env, status):
    await update(env.store, 'attempt', 'another-attempt', status=status)
    assert await env.retention.sweep() == {'retired': 0, 'skipped': {'execution_active_or_unknown': 1}}
    assert env.path.is_dir() and not env.tombstone.exists()


@pytest.mark.parametrize('kind,field', [('code_snapshot', 'repository_path'), ('candidate', 'source_repository'),
    ('delivery_intent', 'target_repository'), ('project', 'local_path')])
async def test_persistent_source_and_delivery_references_prevent_retirement(env, kind, field):
    await update(env.store, kind, 'protected-reference', **{field: str(env.path)})
    result = await env.retention.sweep()
    assert result == {'retired': 0, 'skipped': {'workspace_referenced': 1}}
    assert env.manager.registration('attempt')['path'] == str(env.path)
    assert not await env.store.list('workspace_retirement')


async def test_project_registered_during_retirement_preparation_is_preserved(env, monkeypatch):
    workflow = WorkflowService(env.store, None, SimpleNamespace(data_dir=env.store.data_dir))
    original = env.maintenance._state
    created = None
    async def register_during_preparation(target):
        nonlocal created
        if created is None:
            created = await workflow.create_project({'name': 'Owner registered this clone',
                'local_path': str(env.path), 'import_mode': 'snapshot_existing'}, 'new-project')
        return await original(target)
    monkeypatch.setattr(env.maintenance, '_state', register_during_preparation)
    result = await env.retention.sweep()
    assert created is not None and created['local_path'] == str(env.path)
    assert result['retired'] == 0
    assert env.path.is_dir() and not env.tombstone.exists()
    assert (await env.store.read('project', created['id']))['local_path'] == str(env.path)


async def test_interrupted_project_registration_intent_preserves_its_source_clone(env, monkeypatch):
    workflow = WorkflowService(env.store, None, SimpleNamespace(data_dir=env.store.data_dir))
    async def interrupted_git(*args, **kwargs):
        raise DomainError('fixture_interruption', 'Stop fixture import after its durable intent')
    with monkeypatch.context() as patch:
        patch.setattr(workflow, '_git', interrupted_git)
        with pytest.raises(DomainError) as error:
            await workflow.create_project({'name': 'Interrupted registration', 'local_path': str(env.path),
                'import_mode': 'snapshot_existing'}, 'pending-project')
    assert error.value.code == 'fixture_interruption'
    intents = await env.store.list('project_intent')
    assert len(intents) == 1 and intents[0]['payload']['local_path'] == str(env.path)
    assert (await env.retention.sweep())['retired'] == 0
    assert env.path.is_dir() and not env.tombstone.exists()
    assert await env.store.list('project_intent') == intents


@pytest.mark.parametrize('suffix', ['', 'nested-import'])
async def test_retirement_reservation_rejects_later_project_import_without_leaving_an_intent(env, monkeypatch, suffix):
    workflow = WorkflowService(env.store, None, SimpleNamespace(data_dir=env.store.data_dir))
    original = env.manager.retire
    projects_before = await env.store.list('project')
    rejected = False
    async def import_after_reservation(attempt_id, *, protected_paths=()):
        nonlocal rejected
        reservation = await env.store.read('workspace_retirement_intent', attempt_id)
        assert reservation['state'] == 'retiring' and reservation['path'] == str(env.path)
        with pytest.raises(DomainError) as error:
            await workflow.create_project({'name': 'Import arrived after retirement reservation',
                'local_path': str(env.path / suffix), 'import_mode': 'snapshot_existing'}, 'late-project')
        assert error.value.code == 'workspace_retiring'
        assert not await env.store.list('project_intent')
        assert await env.store.list('project') == projects_before
        rejected = True
        return await original(attempt_id, protected_paths=protected_paths)
    monkeypatch.setattr(env.manager, 'retire', import_after_reservation)
    assert await env.retention.sweep() == {'retired': 1, 'skipped': {}}
    assert rejected and not env.path.exists() and env.tombstone.is_file()
    assert (await env.store.read('workspace_retirement_intent', 'attempt'))['state'] == 'retired'
    assert not await env.store.list('project_intent')
    assert await env.store.list('project') == projects_before


@pytest.mark.parametrize('case_alias', [False, True], ids=['exact-trash-path', 'case-variant-trash-path'])
async def test_renamed_trash_rejects_project_registration_until_removal_completes(env, monkeypatch, case_alias):
    workflow = WorkflowService(env.store, None, SimpleNamespace(data_dir=env.store.data_dir))
    trash = env.path.with_name('.retired-' + canonical_digest('attempt').split(':')[1])
    registration_path = trash.with_name(trash.name.upper()) if case_alias else trash
    renamed = asyncio.Event()
    resume = threading.Event()
    loop = asyncio.get_running_loop()
    original = workspace_retirement._unchanged
    def pause_after_rename(path, intent, *, partially_deleted=False):
        result = original(path, intent, partially_deleted=partially_deleted)
        if path == trash:
            loop.call_soon_threadsafe(renamed.set)
            if not resume.wait(10):
                raise RuntimeError('Timed out waiting for the fixture registration attempt')
        return result
    monkeypatch.setattr(workspace_retirement, '_unchanged', pause_after_rename)
    sweep = asyncio.create_task(env.retention.sweep())
    try:
        await asyncio.wait_for(renamed.wait(), timeout=10)
        assert not env.path.exists() and (trash / '.git').is_dir()
        intent = await env.store.read('workspace_retirement_intent', 'attempt')
        assert intent['state'] == 'retiring' and intent['trash_path'] == str(trash)
        with pytest.raises(DomainError) as error:
            await workflow.create_project({'name': 'Register a renamed workspace',
                'local_path': str(registration_path), 'import_mode': 'snapshot_existing'}, 'trash-project')
        assert error.value.code == 'workspace_retiring'
        assert not await env.store.list('project_intent')
        assert len(await env.store.list('project')) == 1
    finally:
        resume.set()
        result = await asyncio.wait_for(sweep, timeout=10)
    assert result == {'retired': 1, 'skipped': {}}
    assert not env.path.exists() and not trash.exists() and env.tombstone.is_file()
    assert (await env.store.read('workspace_retirement_intent', 'attempt'))['state'] == 'retired'


async def test_concurrent_sweeps_keep_the_first_reservation_until_retirement_finishes(env, monkeypatch):
    reserved, resume, second_started, second_entered = (asyncio.Event() for _ in range(4))
    original = env.manager.retire
    async def pause_before_journal(attempt_id, *, protected_paths=()):
        reserved.set()
        await asyncio.wait_for(resume.wait(), timeout=10)
        return await original(attempt_id, protected_paths=protected_paths)
    monkeypatch.setattr(env.manager, 'retire', pause_before_journal)
    other = ReadonlyWorkspaceRetention(env.store, WorkspaceManager(env.store.data_dir, create=False),
        RuntimeMaintenance(env.store, env.store.data_dir))
    original_second = other._sweep
    async def observe_second_entry():
        second_entered.set()
        return await original_second()
    monkeypatch.setattr(other, '_sweep', observe_second_entry)
    async def run_second():
        second_started.set()
        return await other.sweep()
    first = asyncio.create_task(env.retention.sweep())
    second = None
    try:
        await asyncio.wait_for(reserved.wait(), timeout=10)
        before = await env.store.read('workspace_retirement_intent', 'attempt')
        assert before['state'] == 'retiring'
        assert not (env.manager.metadata / 'retiring' / env.tombstone.name).exists()
        second = asyncio.create_task(run_second())
        await asyncio.wait_for(second_started.wait(), timeout=10)
        # The second coroutine has reached sweep admission; it cannot enter
        # cleanup or release the first reservation while its journal is absent.
        assert not second_entered.is_set() and not second.done()
        assert await env.store.read('workspace_retirement_intent', 'attempt') == before
        workflow = WorkflowService(env.store, None, SimpleNamespace(data_dir=env.store.data_dir))
        with pytest.raises(DomainError) as error:
            await workflow.create_project({'name': 'Import while two sweep calls overlap',
                'local_path': str(env.path), 'import_mode': 'snapshot_existing'}, 'concurrent-project')
        assert error.value.code == 'workspace_retiring'
        assert not await env.store.list('project_intent')
    finally:
        resume.set()
        results = await asyncio.wait_for(asyncio.gather(first, *([second] if second else [])), timeout=10)
    assert results == [{'retired': 1, 'skipped': {}}, {'retired': 0, 'skipped': {}}]
    assert second_entered.is_set() and not env.path.exists()
    intent = await env.store.read('workspace_retirement_intent', 'attempt')
    assert intent['state'] == 'retired' and intent['reservation_id'] == before['reservation_id']
    assert len(await env.store.list('workspace_retirement')) == 1


async def test_case_variant_source_reference_conservatively_preserves_workspace(env):
    alias = env.path.with_name(env.path.name.upper())
    assert str(alias) != str(env.path)
    await update(env.store, 'code_snapshot', 'case-reference', repository_path=str(alias))
    assert await env.retention.sweep() == {'retired': 0, 'skipped': {'workspace_referenced': 1}}
    assert env.path.exists() and not env.tombstone.exists()
    assert not await env.store.list('workspace_retirement')


@pytest.mark.parametrize('change', ['tracked', 'untracked', 'ignored'])
async def test_user_changes_and_additional_files_preserve_readonly_clone(env, change):
    changed = env.path / ('app.py' if change == 'tracked' else 'owner-notes.txt')
    if change == 'ignored':
        (env.path / '.git/info').mkdir(exist_ok=True)
        (env.path / '.git/info/exclude').write_text('owner-notes.txt\n')
    changed.write_text('Preserve user work.\n')
    result = await env.retention.sweep()
    assert result == {'retired': 0, 'skipped': {'workspace_not_disposable': 1}}
    assert changed.read_text() == 'Preserve user work.\n'
    assert not env.tombstone.exists() and not await env.store.list('workspace_retirement')


@pytest.mark.parametrize('proof', ['wrong_backend', 'supervisor_unknown', 'missing_result', 'missing_child',
                                 'model_uncertain', 'budget_uncertain'])
async def test_retention_requires_matching_stopped_process_and_settled_model_proofs(env, proof):
    if proof == 'wrong_backend':
        await update(env.store, 'supervised_attempt', 'attempt', backend='codex_exec')
    elif proof == 'supervisor_unknown':
        await update(env.store, 'supervised_attempt', 'attempt', state='execution_unknown')
    elif proof == 'missing_result':
        (env.directory / 'result.json').unlink()
    elif proof == 'missing_child':
        (env.directory / 'child.json').unlink()
    elif proof == 'model_uncertain':
        await update(env.store, 'model_invocation', 'call', state='uncertain')
    else:
        await update(env.store, 'model_attempt_budget', 'attempt', uncertain_invocations=1)
    result = await env.retention.sweep()
    assert result['retired'] == 0 and result['skipped']
    assert env.path.is_dir() and not env.tombstone.exists()
    assert not await env.store.list('workspace_retirement')


async def test_tombstone_replay_finishes_interrupted_database_receipt_without_recreating_clone(env, monkeypatch):
    original = env.store.command
    async def interrupted(scope, *args, **kwargs):
        if scope == 'workspace.retirement':
            raise OSError('Fixture interruption after filesystem retirement before database receipt')
        return await original(scope, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(env.store, 'command', interrupted)
        result = await env.retention.sweep()
    assert result == {'retired': 0, 'skipped': {'retirement_deferred': 1}}
    assert not env.path.exists() and env.tombstone.is_file()
    assert not await env.store.list('workspace_retirement')
    finished = json.loads(env.tombstone.read_text())['result']
    restarted = ReadonlyWorkspaceRetention(env.store, WorkspaceManager(env.store.data_dir, create=False),
        RuntimeMaintenance(env.store, env.store.data_dir))
    assert await restarted.sweep() == {'retired': 1, 'skipped': {}}
    assert (await env.store.read('workspace_retirement', 'attempt'))['result'] == finished
    assert not env.path.exists()
    assert await restarted.sweep() == {'retired': 0, 'skipped': {}}
    assert len(await env.store.list('workspace_retirement')) == 1

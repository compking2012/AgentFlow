"""Owner correction of the legacy lost review baseline with real Git and Store."""
import asyncio
from pathlib import Path
from uuid import uuid4

import psutil
import pytest
import pytest_asyncio
from test_parallel_remediation import CollectedFixtureRuntime, complete_repair, update
from test_parallel_remediation import parallel_env as parallel_env
from test_parallel_review_child_remediation import collect_review

from agentflow.common import DomainError, canonical_digest
from agentflow.control.scheduler import Scheduler
from agentflow.runtime.launcher import atomic_json
from agentflow.runtime.workspace import WorkspaceManager


async def stopped_task(env, claim, source, commit, *, failed):
    work, attempt = claim['work_item'], claim['attempt']
    path = await WorkspaceManager(env.settings.data_dir).create_clone(Path(source), commit, attempt['id'])
    task = {'attempt_id': attempt['id'], 'work_item_id': work['id'], 'run_id': 'run', 'iteration_id': 'iteration',
        'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint'],
        'workspace': str(path), 'allowed_write_paths': work['write_paths'], 'step': work['step'], 'source_commit': commit}
    directory = env.settings.data_dir / 'supervisor' / canonical_digest({'attempt_id': attempt['id']}).split(':')[1]
    directory.mkdir(parents=True, mode=0o700)
    identity = {'attempt_id': attempt['id'], 'operation_id': str(uuid4()), 'nonce': 'fixture',
        'pid': 1073741824, 'process_started_at': 1.0, 'boot_fingerprint': canonical_digest({'boot_time': psutil.boot_time()}),
        'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint']}
    status = 'failed' if failed else 'completed'
    atomic_json(directory / 'result.json', {**identity, 'execution_status': status, 'exit_code': 0})
    await update(env.store, 'dispatch_context', attempt['id'], task=task)
    await update(env.store, 'supervised_attempt', attempt['id'], **identity, run_id='run', state=status, directory=str(directory))
    return path, task


@pytest_asyncio.fixture
async def broken_baseline(parallel_env):
    env = parallel_env
    repair = await env.remediation.repair('review-work')
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    failed = claim['attempt']
    old_path, old_task = await stopped_task(env, claim, env.aggregate['repository_path'], env.aggregate['commit_oid'], failed=True)
    await env.workflow.finish_attempt(failed['id'], {'fencing_token': failed['fencing_token'],
        'input_fingerprint': failed['input_fingerprint'], 'execution_status': 'failed', 'quality_result': 'unknown',
        'runtime_failure_code': 'worker_timeout'}, str(uuid4()), verified_artifacts=[])
    # Reproduce the historical bad receipt exactly, without invoking the now-fixed recovery.
    def lost_source(tx):
        work = tx.get('work_item', 'module-a')
        env.workflow._invalidate(tx, tx.list('work_item'), {'module-a'}, 'legacy timeout retry',
                                 expand_roots=False, preserve_stage_ids=frozenset({'implementation'}))
        tx.put('failure_analysis', 'legacy-analysis', {'run_id': 'run', 'work_item_id': 'module-a',
            'attempt_id': failed['id'], 'generation': work['generation'], 'failure_code': 'worker_timeout',
            'status': 'repair_scheduled', 'repair_receipt_id': 'empty-recovery', 'repair_receipt_kind': 'run_recovery'})
        tx.put('run_recovery', 'empty-recovery', {'run_id': 'run', 'iteration_id': 'iteration', 'actor': 'system',
            'mode': 'retry', 'work_item_id': 'module-a', 'failure_analysis_id': 'legacy-analysis',
            'affected_work_item_ids': ['module-a', 'implementation', 'review-work', 'unit'],
            'checkpoint': {'kind': 'upstream_checkpoint', 'items': []}})
        return {}
    await env.store.command('fixture.legacy-source-loss', str(uuid4()), {}, lost_source)
    wrong_claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    wrong_path, wrong_task = await stopped_task(env, wrong_claim, env.project['local_path'], env.project['base_commit'], failed=False)
    (wrong_path / 'src').mkdir(exist_ok=True)
    (wrong_path / 'src/a.mjs').write_text('export const a = 99;\n')
    await Scheduler(env.workflow, env.store, CollectedFixtureRuntime(env), None, env.settings)._execute_existing(wrong_task)
    aggregate = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    await env.workflow.block_attempt(aggregate['attempt']['id'], 'assembly_base_mismatch', str(uuid4()),
                                     failure_code='assembly_base_mismatch')
    await update(env.store, 'run', 'run', execution_state='paused')
    env.original_repair, env.failed_attempt, env.old_path, env.wrong_path = repair, failed, old_path, wrong_path
    return env


async def preview(env):
    from agentflow.control.review_baseline_recovery import ReviewBaselineRecovery
    service = ReviewBaselineRecovery(env.store, env.workflow)
    value = await service.preview('run', 'module-a', env.original_repair['id'], 'empty-recovery')
    return service, value


def request(view):
    return {key: view[key] for key in ('work_item_id', 'review_repair_id', 'empty_recovery_id', 'expected_revision', 'evidence_digest')} | {
        'reason': 'Restore the verified reviewed baseline after the legacy empty timeout recovery.'}


async def test_owner_restores_exact_reviewed_source_then_repairs_and_rechecks(broken_baseline):
    env = broken_baseline
    service, view = await preview(env)
    protected = {kind: await env.store.list(kind) for kind in ('budget_account', 'coding_work_budget', 'attempt', 'model_invocation')}
    old_receipt = await env.store.read('run_recovery', 'empty-recovery')
    siblings = {key: await env.store.read('work_item', key) for key in ('module-b', 'module-c', 'module-d')}
    heads = [await asyncio.to_thread(env.repository._run, path, ['rev-parse', 'HEAD']) for path in (env.old_path, env.wrong_path)]
    payload = request(view)
    results = await asyncio.gather(*(service.restore('run', payload, 'same-owner-command') for _ in range(2)))
    assert results[0] == results[1]
    result = results[0]
    assert result['actor'] == 'owner' and result['run']['execution_state'] == 'paused'
    assert set(result['affected_work_item_ids']) == {'module-a', 'implementation', 'review-work', 'unit'}
    assert {kind: await env.store.list(kind) for kind in protected} == protected
    assert await env.store.read('run_recovery', 'empty-recovery') == old_receipt
    assert {key: await env.store.read('work_item', key) for key in siblings} == siblings
    assert [await asyncio.to_thread(env.repository._run, path, ['rev-parse', 'HEAD']) for path in (env.old_path, env.wrong_path)] == heads
    child = await env.store.read('work_item', 'module-a')
    assert 'Correct module a behavior' in child['payload']['change_expectation']
    scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    source, commit = await scheduler._source(result['run'], child)
    assert commit == env.aggregate['commit_oid']
    assert (source / 'src/b.mjs').is_file()
    await env.workflow.control_run('run', {'action': 'resume', 'expected_revision': result['run']['revision'], 'reason': 'Resume verified owner correction'}, str(uuid4()))
    repaired = await complete_repair(env, ['a'], 1)
    assert (Path(repaired['repository_path']) / 'src/a.mjs').read_text() == 'export const a = 1;\n'
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert (await collect_review(env, claim, []))['quality_result'] == 'passed'
    assert await service.restore('run', payload, 'same-owner-command') == result


@pytest.mark.parametrize('kind,identity,fields', [
    ('run', 'run', {'execution_state': 'running'}),
    ('run', 'run', {'restore_reconciliation_required': True}),
    ('run_recovery', 'empty-recovery', {'checkpoint': {'items': [{'snapshot_id': 'existing'}]}}),
    ('failure_analysis', 'legacy-analysis', {'attempt_id': 'another-attempt'}),
    ('work_item', 'implementation', {'runtime_failure_code': 'some_other_failure'}),
    ('work_item', 'module-a', {'write_paths': ['src']}),
    ('work_item', 'unit', {'status': 'waiting_approval'}),
    ('model_invocation', 'unknown', {'run_id': 'run', 'state': 'uncertain'}),
    ('candidate', 'frozen', {'run_id': 'run'}),
])
async def test_owner_baseline_recovery_requires_narrow_proof_and_existing_guards(broken_baseline, kind, identity, fields):
    env = broken_baseline
    service, view = await preview(env)
    await update(env.store, kind, identity, **fields)
    before = {key: await env.store.list(key) for key in ('work_item', 'code_snapshot', 'run_recovery', 'budget_account')}
    with pytest.raises(DomainError):
        await service.preview('run', 'module-a', env.original_repair['id'], 'empty-recovery')
    with pytest.raises(DomainError):
        await service.restore('run', request(view), str(uuid4()))
    assert {key: await env.store.list(key) for key in before} == before


async def test_owner_route_requires_auth_and_replays_the_previewed_command(broken_baseline):
    import httpx

    from agentflow.control.api import create_app
    env = broken_baseline
    app = create_app(env.settings, store=env.store, artifacts=env.artifacts)
    route = '/api/v1/runs/run/review_baseline_recovery'
    params = {'work_item_id': 'module-a', 'review_repair_id': env.original_repair['id'], 'empty_recovery_id': 'empty-recovery'}
    token = app.state.tokens.issue('agentflow_owner', {'owner:*'}, 'owner', 60)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=env.settings.origin) as client:
        assert (await client.get(route, params=params)).status_code == 401
        client.headers.update({'Authorization': 'Bearer ' + token, 'Origin': env.settings.origin})
        response = await client.get(route, params=params)
        assert response.status_code == 200, response.text
        payload = request(response.json())
        result = await client.post(route, json=payload, headers={'Idempotency-Key': 'owner-restore'})
        assert result.status_code == 200, result.text
        assert result.json()['run']['execution_state'] == 'paused'
        assert result.json()['actor'] == 'owner'


async def test_owner_rejects_uncollected_changes_in_wrong_base_workspace(broken_baseline):
    env = broken_baseline
    service, view = await preview(env)
    (env.wrong_path / 'src/a.mjs').write_text('export const a = 101;\n')
    with pytest.raises(DomainError):
        await service.restore('run', request(view), 'dirty-workspace')


async def test_original_review_fence_must_match_its_frozen_work_revision(broken_baseline):
    env = broken_baseline
    await update(env.store, 'attempt', env.original_repair['review_attempt_id'], fencing_token=99)
    with pytest.raises(DomainError):
        await preview(env)


async def test_real_initial_commit_cannot_replace_the_authorized_review_alias(broken_baseline):
    env = broken_baseline
    alias = env.original_repair['checkpoint_alias_ids']['module-a']
    base = env.project['base_commit']
    tree = await asyncio.to_thread(env.repository._integrity, Path(env.aggregate['repository_path']), base)
    await update(env.store, 'code_snapshot', alias, commit_oid=base, tree_oid=tree, base_oid=base)
    with pytest.raises(DomainError):
        await preview(env)


@pytest.mark.parametrize('workspace', ['source', 'failed', 'current'])
async def test_commit_rechecks_workspace_changes_after_async_budget_guard(broken_baseline, monkeypatch, workspace):
    env = broken_baseline
    service, view = await preview(env)
    before = {kind: await env.store.list(kind) for kind in ('run', 'work_item', 'work_revision', 'code_snapshot', 'run_recovery', 'budget_account')}
    original = service.recovery._target_blockers
    async def change_after_source_proof(state, target):
        result = await original(state, target)
        path = {'source': Path(env.aggregate['repository_path']), 'failed': env.old_path, 'current': env.wrong_path}[workspace]
        (path / 'src/a.mjs').write_text('export const a = 102;\n')
        return result
    monkeypatch.setattr(service.recovery, '_target_blockers', change_after_source_proof)
    with pytest.raises(DomainError):
        await service.restore('run', request(view), 'source-raced-with-budget')
    assert {kind: await env.store.list(kind) for kind in before} == before


async def test_missing_original_child_snapshot_fails_before_invalidation(broken_baseline):
    env = broken_baseline
    service, view = await preview(env)
    await update(env.store, 'code_snapshot', env.original_repair['checkpoint_alias_ids']['module-a'],
                 source_child_snapshot_id='missing-original-attempt')
    before = {kind: await env.store.list(kind) for kind in ('run', 'work_item', 'code_snapshot', 'run_recovery', 'budget_account')}
    with pytest.raises(DomainError):
        await service.preview('run', 'module-a', env.original_repair['id'], 'empty-recovery')
    with pytest.raises(DomainError):
        await service.restore('run', request(view), 'missing-original-child')
    assert {kind: await env.store.list(kind) for kind in before} == before

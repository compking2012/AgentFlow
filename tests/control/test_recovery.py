"""Owner recovery uses isolated Store/Git fixtures; no provider or user Run starts."""
import asyncio
import os
import sys
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import psutil
import pytest
import pytest_asyncio
from pydantic import ValidationError

from agentflow.common import DomainError, canonical_digest
from agentflow.control.api import create_app
from agentflow.control.recovery import RecoveryRequest, RunRecoveryService, resolve_recovery_model_profile
from agentflow.control.remediation import ReviewRemediation
from agentflow.control.scheduler import CODE_SCHEMA, Scheduler
from agentflow.control.service import WorkflowService
from agentflow.models.budget import BudgetLedger, account_id
from agentflow.models.profiles import ModelProfile
from agentflow.runtime.launcher import atomic_json
from agentflow.runtime.prelaunch import record_prelaunch_failure
from agentflow.runtime.process_birth import process_birth_identity
from agentflow.runtime.workspace import WorkspaceManager
from agentflow.settings import Settings
from agentflow.storage import LocalArtifactStore, Store


async def patch(env, record_kind, identity, **fields):
    def apply(tx):
        previous = tx.get(record_kind, identity)
        return tx.put(record_kind, identity, {**(previous or {}), **fields}, previous['revision'] if previous else None)
    return await env.store.command('fixture.patch', str(uuid4()), {}, apply)


@pytest_asyncio.fixture
async def env(tmp_path):
    settings = Settings(data_dir=tmp_path / 'data', agent_concurrency=3)
    store = Store(settings.data_dir)
    await store.start()
    artifacts = LocalArtifactStore(settings.data_dir / 'artifacts')
    workflow = WorkflowService(store, artifacts, settings)
    project = await workflow.create_project({'name': 'Recovery fixture', 'local_path': str(tmp_path / 'project'),
        'import_mode': 'initialize_managed', 'dirty_worktree_policy': 'require_clean'}, 'project')
    limit = {'currency': 'USD', 'limit_micros': 10000, 'max_model_requests': 10,
             'max_tool_calls': 30, 'max_active_seconds': 120, 'cost_mode': 'request_limited'}
    def seed(tx):
        tx.put('iteration', 'iteration', {'project_id': project['id'], 'budget_limit': limit})
        tx.put('plan', 'plan', {'project_id': project['id'], 'budget_limit': limit, 'product_contract': {},
            'approval_steps': ['research'], 'actual_steps': ['goal', 'research', 'prd']})
        tx.put('run', 'run', {'project_id': project['id'], 'plan_id': 'plan', 'iteration_id': 'iteration',
            'execution_state': 'paused', 'quality_result': 'failed', 'input_fingerprint': 'run-input',
            'budget_limit': limit, 'delivery_ids': [], 'blocking_reasons': ['research failed'],
            'goal': 'fixture', 'purpose': 'artifact_only', 'base_commit': project['base_commit']})
        for identity, step, status, dependencies in [('upstream', 'goal', 'completed', []),
                ('bad', 'research', 'failed', ['upstream']), ('after', 'prd', 'completed', ['bad']),
                ('unit', 'unit_test_execution', 'pending', ['after'])]:
            work = {'run_id': 'run', 'project_id': project['id'], 'key': identity, 'step': step,
                'role': 'research' if step == 'research' else 'product', 'generation': 1, 'fencing_token': 1,
                'input_fingerprint': identity + '-input', 'policy_fingerprint': 'policy', 'required': True,
                'status': status, 'quality_result': 'failed' if status == 'failed' else 'passed',
                'dependencies': dependencies, 'write_paths': [], 'attempt_id': identity + '-attempt' if status != 'pending' else None,
                'approval_required': identity == 'bad', 'artifact_ids': [], 'payload': {}, 'output_fingerprint': 'output'}
            tx.put('work_item', identity, work)
            if work['attempt_id']:
                tx.put('attempt', work['attempt_id'], {'run_id': 'run', 'iteration_id': 'iteration',
                    'work_item_id': identity, 'status': status, 'generation': 1, 'fencing_token': 1,
                    'input_fingerprint': work['input_fingerprint']})
        tx.put('artifact', 'old-artifact', {'run_id': 'run', 'work_item_id': 'after', 'stale': False})
        tx.put('approval', 'old-approval', {'run_id': 'run', 'work_item_id': 'after', 'stale': False, 'decision': 'approve'})
        tx.put('check', 'old-check', {'run_id': 'run', 'work_item_id': 'unit', 'stale': False, 'quality_result': 'passed'})
        tx.put('review', 'old-review', {'run_id': 'run', 'work_item_id': 'after', 'stale': False, 'quality_result': 'passed'})
        return {}
    await store.command('fixture.seed', 'seed', {}, seed)
    ledger = BudgetLedger(store)
    await ledger.setup_accounts('run', 'iteration', 10000, 10000, run_max_requests=10, iteration_max_requests=10)
    value = SimpleNamespace(store=store, settings=settings, workflow=workflow, artifacts=artifacts,
        service=RunRecoveryService(store, workflow), project=project, ledger=ledger, tmp_path=tmp_path)
    yield value
    await store.close()


def request(mode='retry', revision=1, target=None):
    return {'expected_revision': revision, 'mode': mode, **({'work_item_id': target} if target else {})}


async def test_retry_preserves_upstream_invalidates_descendants_and_keeps_human_gate(env):
    before = await env.store.read('work_item', 'upstream')
    accounts = await env.store.list('budget_account')
    old_attempts = await env.store.list('attempt')
    options = await env.service.options('run')
    assert options['retry_options'][0]['eligible']
    receipt = await env.service.recover('run', request(), 'retry')
    assert receipt['affected_work_item_ids'] == ['after', 'bad', 'unit']
    assert receipt['session_resume'] == 'unsupported_ephemeral' and receipt['execution'] == 'fresh_attempt'
    assert receipt['run']['execution_state'] == 'running'
    assert await env.store.read('work_item', 'upstream') == before
    assert await env.store.list('budget_account') == accounts
    assert await env.store.list('attempt') == old_attempts
    for kind, identity in [('artifact', 'old-artifact'), ('approval', 'old-approval'), ('check', 'old-check'), ('review', 'old-review')]:
        assert (await env.store.read(kind, identity))['stale']
    work = await env.store.read('work_item', 'bad')
    assert work['generation'] == 2 and work['attempt_id'] is None and work['approval_required']
    claim = await env.workflow.claim_next('run', 'fixture', 'claim')
    assert claim['work_item']['id'] == 'bad'
    blob = await env.artifacts.put_bytes(b'{"content":"fixture document"}')
    attempt = claim['attempt']
    await env.workflow.finish_attempt(attempt['id'], {'fencing_token': attempt['fencing_token'],
        'input_fingerprint': attempt['input_fingerprint'], 'execution_status': 'completed', 'quality_result': 'passed'},
        'finish', verified_artifacts=[{'digest': blob['id'], 'name': 'document.json'}])
    assert (await env.store.read('work_item', 'bad'))['status'] == 'waiting_approval'
    assert (await env.workflow.claim_next('run', 'fixture', 'still-gated'))['attempt'] is None


async def test_stage_retry_only_retries_failed_children_then_aggregation(env):
    bad = await patch(env, 'work_item', 'bad', kind='stage_child', parent_stage_id='stage')
    good = {k: v for k, v in bad.items() if k not in {'id', 'revision'}}
    good.update(key='good', status='completed', quality_result='passed', attempt_id=None)
    await patch(env, 'work_item', 'good', **good)
    stage = {k: v for k, v in bad.items() if k not in {'id', 'revision', 'parent_stage_id'}}
    stage.update(key='research', kind='aggregation', status='pending', attempt_id=None, dependencies=['bad', 'good'],
                 original_dependencies=['upstream'], original_write_paths=[], expanded_child_ids=['bad', 'good'])
    await patch(env, 'work_item', 'stage', **stage)
    await patch(env, 'work_item', 'after', dependencies=['stage'])
    before = await env.store.read('work_item', 'good')
    options = await env.service.options('run')
    assert options['retry_options'][0]['work_item_id'] == 'stage'
    receipt = await env.service.recover('run', request(target='stage'), 'retry-stage')
    assert receipt['affected_work_item_ids'] == ['after', 'bad', 'stage', 'unit']
    assert await env.store.read('work_item', 'good') == before
    assert (await env.store.read('work_item', 'stage'))['dependencies'] == ['bad', 'good']


async def test_paused_continuation_keeps_generations_approvals_and_inputs(env):
    await patch(env, 'work_item', 'bad', status='waiting_approval', quality_result='passed')
    await patch(env, 'attempt', 'bad-attempt', status='completed')
    before = await env.store.list('work_item')
    assert (await env.service.options('run'))['continue']['eligible']
    result = await env.service.recover('run', request('continue'), 'continue')
    assert result['execution'] == 'resume_scheduling' and result['affected_work_item_ids'] == []
    assert result['run']['input_fingerprint'] == 'run-input'
    assert await env.store.list('work_item') == before
    assert not (await env.store.read('approval', 'old-approval'))['stale']


async def cancel_fixture(env):
    run = await env.store.read('run', 'run')
    return await env.workflow.control_run('run', {'expected_revision': run['revision'],
        'action': 'cancel', 'reason': 'Owner stops this iteration'}, 'cancel-fixture')


async def test_cancelled_continuation_restores_all_pending_branches_preserving_completed_work(env):
    await patch(env, 'work_item', 'bad', status='completed', quality_result='passed')
    await patch(env, 'attempt', 'bad-attempt', status='completed')
    unit = await env.store.read('work_item', 'unit')
    await patch(env, 'work_item', 'delivery', **{k: v for k, v in unit.items() if k not in {'id', 'revision'}})
    await patch(env, 'work_item', 'delivery', key='delivery', step='delivery', role='system', dependencies=['unit'])
    # An independent branch must also resume; selecting only the first retry target loses it.
    await patch(env, 'work_item', 'parallel', **{k: v for k, v in unit.items() if k not in {'id', 'revision'}})
    await patch(env, 'work_item', 'parallel', key='parallel', step='research', dependencies=['upstream'])
    cancelled = await cancel_fixture(env)
    preserved = {i: await env.store.read('work_item', i) for i in ['upstream', 'bad', 'after']}
    budgets, attempts = await env.store.list('budget_account'), await env.store.list('attempt')
    options = await env.service.options('run')
    assert options['continue']['eligible'], options['continue']
    assert options['continue']['affected_work_item_ids'] == ['delivery', 'parallel', 'unit']
    payload = request('continue', cancelled['revision'])
    receipts = await asyncio.gather(*(env.service.recover('run', payload, 'resume-cancelled') for _ in range(2)))
    assert receipts[0] == receipts[1]
    assert receipts[0]['run']['execution_state'] == 'running'
    assert receipts[0]['affected_work_item_ids'] == ['delivery', 'parallel', 'unit']
    assert receipts[0]['execution'] == 'fresh_attempt'
    for i, previous in preserved.items():
        assert await env.store.read('work_item', i) == previous
    for i in ['delivery', 'parallel', 'unit']:
        work = await env.store.read('work_item', i)
        assert work['status'] == 'pending' and work['generation'] == 2 and work['attempt_id'] is None
    assert await env.store.list('budget_account') == budgets
    assert await env.store.list('attempt') == attempts
    assert not (await env.store.read('approval', 'old-approval'))['stale']
    claim = await env.workflow.claim_next('run', 'fixture', 'next')
    assert claim['work_item']['id'] in {'parallel', 'unit'}
    assert await env.service.recover('run', payload, 'resume-cancelled') == receipts[0]


@pytest.mark.parametrize('blocker', ['execution_unknown', 'active_work', 'delivered_run'])
async def test_cancelled_continue_keeps_execution_and_delivery_guards(env, blocker):
    cancelled = await cancel_fixture(env)
    if blocker == 'delivered_run':
        await patch(env, 'delivery', 'published', run_id='run', confirmed_at='2026-09-24')
    else:
        await patch(env, 'attempt', 'bad-attempt', status='execution_unknown' if blocker == 'execution_unknown' else 'running')
    before = await env.store.list('work_item')
    assert not (await env.service.options('run'))['continue']['eligible']
    with pytest.raises(DomainError) as error:
        await env.service.recover('run', request('continue', cancelled['revision']), 'blocked-resume')
    assert error.value.code == blocker
    assert await env.store.list('work_item') == before
    assert not await env.store.list('run_recovery')


async def test_cancelled_continue_keeps_new_generation_model_binding_usable(env):
    await stopped_workspace(env)
    profile = ModelProfile(model_profile_id='prior-model', provider='openai_compatible',
        protocols=['responses'], base_url='https://model.example.invalid/v1', requested_model='fixture-model',
        accepted_api_model='fixture-model', credential_reference='env:UNUSED_FIXTURE_KEY', acceptance_status='accepted')
    await patch(env, 'model_profile', 'prior-model', **profile.model_dump(mode='json', exclude={'revision'}))
    await patch(env, 'product_model_binding', 'default', coding_model_profile_id='prior-model')
    first = await env.service.recover('run', {**request(), 'use_current_model_settings': True}, 'first-model-choice')
    cancelled = await cancel_fixture(env)
    result = await env.service.recover('run', request('continue', cancelled['revision']), 'continue-model-choice')
    work = await env.store.read('work_item', 'bad')
    resolved = await resolve_recovery_model_profile(env.store, result['run'], work)
    assert resolved == 'prior-model'
    assert work['generation'] == 3 and first['run']['id'] == result['run']['id']


async def test_cancelled_continue_reactivates_the_current_product_for_automatic_delivery(env):
    await patch(env, 'plan', 'plan', product_contract={'product_id': 'product', 'config_revision': 1})
    await patch(env, 'product', 'product', run_id='run', state='cancelled', config_revision=1,
                phase='cancelled', blocking_reasons=[], name='Product', goal='Keep original goal')
    cancelled = await cancel_fixture(env)
    payload = request('continue', cancelled['revision'])
    await env.service.recover('run', payload, 'resume-product')
    product = await env.store.read('product', 'product')
    assert product['state'] == 'running' and product['phase'] == 'reconciling'
    assert product['run_id'] == 'run' and product['goal'] == 'Keep original goal'
    await env.service.recover('run', payload, 'resume-product')
    assert await env.store.read('product', 'product') == product


@pytest.mark.skipif(os.name != 'posix', reason='POSIX process groups')
@pytest.mark.parametrize('reused', [True, False])
@pytest.mark.parametrize('native_birth', [True, False])
async def test_recovery_distinguishes_reused_group_id_from_the_original_live_leader(env, reused, native_birth):
    _, _, directory, identity = await stopped_workspace(env)
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time;time.sleep(30)', start_new_session=True)
    try:
        started = psutil.Process(process.pid).create_time()
        identity.update(pid=process.pid, process_started_at=started - 120 if reused else started)
        if native_birth:
            identity.update(process_birth_identity(process.pid))
            if reused:
                identity['process_birth_fingerprint'] = 'sha256:' + 'f' * 64
        atomic_json(directory / 'result.json', {**identity, 'execution_status': 'completed', 'exit_code': 0})
        record = await patch(env, 'supervised_attempt', 'bad-attempt', **identity)
        attempts = {a['id']: a for a in await env.store.list('attempt')}
        if reused and not native_birth:
            with pytest.raises(ValueError, match='legacy_process_birth_unverified'):
                env.service._verify_process(record, attempts)
            assert process.returncode is None
        elif reused:
            env.service._verify_process(record, attempts)
            assert process.returncode is None  # Read-only recovery must never kill the unrelated process.
        else:
            with pytest.raises(DomainError, match='旧进程仍在运行'):
                env.service._verify_process(record, attempts)
    finally:
        if process.returncode is None:
            process.terminate()
        await process.wait()


@pytest.mark.skipif(os.name != 'posix', reason='POSIX process groups')
async def test_recovery_still_blocks_a_background_child_after_the_original_leader_exits(env):
    _, _, directory, identity = await stopped_workspace(env)
    script = ('import subprocess,sys,time;'
        'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],'
        'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);'
        'print(p.pid,flush=True);time.sleep(.2)')
    leader = await asyncio.create_subprocess_exec(sys.executable, '-c', script,
        start_new_session=True, stdout=asyncio.subprocess.PIPE)
    child = None
    try:
        identity.update(pid=leader.pid, process_started_at=psutil.Process(leader.pid).create_time())
        child = psutil.Process(int(await asyncio.wait_for(leader.stdout.readline(), 3)))
        await asyncio.wait_for(leader.wait(), 3)
        atomic_json(directory / 'result.json', {**identity, 'execution_status': 'completed', 'exit_code': 0})
        record = await patch(env, 'supervised_attempt', 'bad-attempt', **identity)
        with pytest.raises(DomainError, match='后台任务'):
            env.service._verify_process(record, {a['id']: a for a in await env.store.list('attempt')})
        assert child.is_running()
    finally:
        if child and child.is_running():
            child.terminate()
        if leader.returncode is None:
            leader.terminate()
        await leader.wait()


@pytest.mark.parametrize('kind,identity,fields,error', [
    ('work_item', 'bad', {'status': 'running'}, 'active_work'),
    ('work_item', 'bad', {'status': 'execution_unknown'}, 'execution_unknown'),
    ('attempt', 'bad-attempt', {'status': 'running'}, 'active_work'),
    ('attempt', 'bad-attempt', {'fencing_token': 99}, 'recovery_evidence_invalid'),
    ('node_job', 'node', {'run_id': 'run', 'state': 'execution_unknown'}, 'active_work'),
    ('node_job', 'node', {'run_id': 'run', 'state': 'leased'}, 'active_work'),
    ('supervised_attempt', 'bad-attempt', {'state': 'running'}, 'active_work'),
    ('model_invocation', 'call', {'run_id': 'run', 'state': 'uncertain'}, 'recovery_budget_uncertain'),
    ('model_invocation', 'call', {'run_id': 'run', 'state': 'dispatching'}, 'recovery_budget_uncertain'),
    ('run', 'run', {'delivery_ids': ['delivered']}, 'delivered_run'),
    ('delivery', 'delivered', {'run_id': 'run', 'confirmed_at': '2026-09-22'}, 'delivered_run'),
    ('delivery_intent', 'publishing', {'run_id': 'run', 'status': 'prepared'}, 'delivery_in_progress'),
])
async def test_unsafe_execution_or_delivery_is_blocked_without_mutation(env, kind, identity, fields, error):
    await patch(env, kind, identity, **fields)
    revision = (await env.store.read('run', 'run'))['revision']
    before = await env.store.list('work_item')
    with pytest.raises(DomainError) as raised:
        await env.service.recover('run', request(revision=revision), 'unsafe')
    assert raised.value.code == error
    assert await env.store.list('work_item') == before
    assert not await env.store.list('run_recovery')


@pytest.mark.parametrize('maximum,exhausted', [(10, True), (0, False)])
async def test_finite_exhaustion_and_unlimited_zero_keep_usage(env, maximum, exhausted):
    for kind, identity in [('run', 'run'), ('iteration', 'iteration')]:
        row = await env.store.read(kind, identity)
        await patch(env, kind, identity, budget_limit={**row['budget_limit'], 'max_model_requests': maximum})
        await patch(env, 'budget_account', account_id(kind, identity), max_requests=maximum, request_count=100)
    before = await env.store.list('budget_account')
    if exhausted:
        with pytest.raises(DomainError) as error:
            await env.service.recover('run', request(revision=2), 'budget')
        assert error.value.code == 'recovery_budget_exhausted'
    else:
        assert (await env.service.recover('run', request(revision=2), 'budget'))['execution'] == 'fresh_attempt'
    assert await env.store.list('budget_account') == before


async def test_replay_concurrency_stale_revision_and_changed_payload(env):
    results = await asyncio.gather(*(env.service.recover('run', request(), 'same-key') for _ in range(3)))
    assert results[0] == results[1] == results[2]
    await env.workflow.claim_next('run', 'fixture', 'claim')
    assert await env.service.recover('run', request(), 'same-key') == results[0]
    assert len(await env.store.list('run_recovery')) == 1
    assert len(await env.store.list('work_revision')) == 3
    with pytest.raises(DomainError) as error:
        await env.service.recover('run', {**request(), 'reason': 'changed'}, 'same-key')
    assert error.value.code == 'idempotency_conflict'
    with pytest.raises(DomainError) as error:
        await env.service.recover('run', request(), 'different-key')
    assert error.value.code == 'revision_conflict'


async def stopped_workspace(env, *, project_layout=False, project_id=None):
    await patch(env, 'work_item', 'bad', step='implementation', key='implementation', role='development',
                status='blocked', write_paths=['src'])
    await patch(env, 'attempt', 'bad-attempt', status='blocked')
    work = await env.store.read('work_item', 'bad')
    manager = WorkspaceManager(env.settings.data_dir)
    path = await manager.create_clone(env.tmp_path / 'project', env.project['base_commit'], 'bad-attempt',
        **({'project_root': env.tmp_path / 'project', 'project_id': project_id or env.project['id']} if project_layout else {}))
    (path / 'src').mkdir()
    (path / 'src' / 'keep.js').write_text('export const valuable = 42;\n')
    task = {'attempt_id': 'bad-attempt', 'work_item_id': 'bad', 'run_id': 'run', 'iteration_id': 'iteration',
        'fencing_token': work['fencing_token'], 'input_fingerprint': work['input_fingerprint'],
        'workspace': str(path), 'allowed_write_paths': ['src'], 'step': 'implementation',
        'source_commit': env.project['base_commit'], 'output_schema': CODE_SCHEMA}
    await patch(env, 'dispatch_context', 'bad-attempt', task=task)
    directory = env.settings.data_dir / 'supervisor' / canonical_digest({'attempt_id': 'bad-attempt'}).split(':')[1]
    directory.mkdir(parents=True, mode=0o700)
    # Synthetic stopped-process evidence, not an actual model execution.
    identity = {'attempt_id': 'bad-attempt', 'operation_id': 'operation', 'nonce': 'fixture-nonce',
        'pid': 1073741824, 'process_started_at': 1.0, 'boot_fingerprint': canonical_digest({'boot_time': psutil.boot_time()}),
        'fencing_token': 1}
    atomic_json(directory / 'result.json', {**identity, 'execution_status': 'completed', 'exit_code': 0})
    await patch(env, 'supervised_attempt', 'bad-attempt', **identity, state='completed', run_id='run',
                directory=str(directory), input_fingerprint=work['input_fingerprint'])
    return path, task, directory, identity


@pytest.mark.parametrize('project_layout', [False, True])
async def test_stopped_code_checkpoint_keeps_changes_and_schema_only_repair_can_finish(env, project_layout):
    path, task, _, _ = await stopped_workspace(env, project_layout=project_layout)
    repository = env.service.repository
    before = [await asyncio.to_thread(repository._run, path, args) for args in
              [['rev-parse', 'HEAD'], ['ls-files', '--stage'], ['status', '--porcelain']]]
    receipt = await env.service.recover('run', request(), 'code')
    after = [await asyncio.to_thread(repository._run, path, args) for args in
             [['rev-parse', 'HEAD'], ['ls-files', '--stage'], ['status', '--porcelain']]]
    assert before == after
    assert receipt['checkpoint']['kind'] == 'stopped_workspace'
    code = await env.store.read('work_item', 'bad')
    scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    run = await env.store.read('run', 'run')
    source, commit = await scheduler._source(run, code)
    manager = WorkspaceManager(env.settings.data_dir)
    claim = await env.workflow.claim_next('run', 'fixture', 'retry-code-claim')
    fresh = await manager.create_clone(source, commit, claim['attempt']['id'],
        **({'project_root': env.tmp_path / 'project', 'project_id': env.project['id']} if project_layout else {}))
    assert (fresh / 'src/keep.js').read_text() == 'export const valuable = 42;\n'
    assert not (await repository.collect_diff(fresh, commit))['has_changes']
    artifact = env.settings.data_dir / 'attempt_artifacts' / 'recovery-result.json'
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text('{"summary":"Validated preserved implementation"}')
    async def execute(_):
        return {'execution_status': 'completed', 'quality_result': 'unknown', 'artifacts': [
            {'path': str(artifact)}], 'result': {'summary': 'Validated preserved implementation'}}
    scheduler.runtime = SimpleNamespace(execute_task=execute)
    attempt = claim['attempt']
    fresh_task = {**task, 'attempt_id': attempt['id'], 'fencing_token': attempt['fencing_token'],
        'input_fingerprint': attempt['input_fingerprint'], 'workspace': str(fresh), 'source_commit': commit}
    await scheduler._execute_existing(fresh_task)
    updated = await env.store.read('work_item', 'bad')
    assert updated['status'] == 'waiting_approval'
    assert updated['approval_required'] and updated['write_paths'] == ['src']
    assert await env.store.read('code_snapshot', attempt['id'])
    assert (await env.workflow.claim_next('run', 'fixture', 'review-gated'))['attempt'] is None


async def test_project_workspace_registration_cannot_be_borrowed_for_another_project(env):
    await stopped_workspace(env, project_layout=True, project_id='a-different-project')
    with pytest.raises(DomainError) as error:
        await env.service.recover('run', request(), 'wrong-project-workspace')
    assert error.value.code == 'recovery_checkpoint_invalid'
    assert await env.store.list('run_recovery') == []


async def test_cancelled_before_dispatch_preserves_existing_recovery_checkpoint(env):
    await stopped_workspace(env)
    first = await env.service.recover('run', request(), 'first')
    run = await env.store.read('run', 'run')
    cancelled = await env.workflow.control_run('run', {'expected_revision': run['revision'],
        'action': 'cancel', 'reason': 'cancel before another attempt exists'}, 'cancel')
    second = await env.service.recover('run', request(revision=cancelled['revision'], target='bad'), 'second')
    assert second['checkpoint']['commit_oid'] == first['checkpoint']['commit_oid']
    work = await env.store.read('work_item', 'bad')
    scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    _, commit = await scheduler._source(second['run'], work)
    assert commit == first['checkpoint']['commit_oid']
    assert work['generation'] == 3 and work['payload']['recovery_checkpoint_id'] != first['checkpoint']['snapshot_id']


@pytest.mark.parametrize('corrupt', [False, True])
async def test_existing_immutable_snapshot_must_match_original_attempt_and_tree(env, corrupt):
    path, task, _, _ = await stopped_workspace(env)
    snapshot = await env.service.repository.freeze_workspace(path, task['source_commit'], 'fixture collected snapshot')
    await patch(env, 'code_snapshot', 'bad-attempt', run_id='run', work_item_id='bad', generation=1,
        repository_path=str(path), commit_oid=snapshot['commit_oid'], base_oid=snapshot['base_oid'],
        tree_oid='a' * 40 if corrupt else snapshot['tree_oid'], stale=False)
    if corrupt:
        with pytest.raises(DomainError) as error:
            await env.service.recover('run', request(), 'snapshot')
        assert error.value.code == 'recovery_checkpoint_invalid'
    else:
        result = await env.service.recover('run', request(), 'snapshot')
        assert result['checkpoint']['kind'] == 'prior_code_snapshot'
        assert result['checkpoint']['commit_oid'] == snapshot['commit_oid']
        assert (await env.store.read('code_snapshot', 'bad-attempt'))['stale']


@pytest.mark.parametrize('fields', [{'restore_uncertain': True}, {'uncertain_micros': 1},
    {'reserved_micros': 1}, {'max_requests': True}, {'request_count': -1}, {'currency': 'CNY'}])
async def test_unreconciled_or_invalid_budget_never_changes_quota(env, fields):
    await patch(env, 'budget_account', account_id('iteration', 'iteration'), **fields)
    before = await env.store.list('budget_account')
    with pytest.raises(DomainError) as error:
        await env.service.recover('run', request(), 'budget-uncertain')
    assert error.value.code == 'recovery_budget_uncertain'
    assert await env.store.list('budget_account') == before


@pytest.mark.parametrize('damage', ['missing_receipt', 'bad_json', 'wrong_nonce', 'live_pid', 'missing_context_metadata', 'out_of_scope', 'wrong_workspace'])
async def test_checkpoint_and_process_corruption_never_discards_code_or_retries(env, damage):
    path, task, directory, identity = await stopped_workspace(env)
    if damage == 'missing_receipt':
        (directory / 'result.json').unlink()
    elif damage == 'bad_json':
        (directory / 'result.json').write_text('{broken')
    elif damage == 'wrong_nonce':
        atomic_json(directory / 'result.json', {**identity, 'nonce': 'other', 'execution_status': 'completed'})
    elif damage == 'live_pid':
        identity.update(pid=os.getpid(), process_started_at=psutil.Process().create_time())
        atomic_json(directory / 'result.json', {**identity, 'execution_status': 'completed'})
        await patch(env, 'supervised_attempt', 'bad-attempt', **identity)
    elif damage == 'missing_context_metadata':
        (env.settings.data_dir / 'workspace_metadata' / (canonical_digest('bad-attempt').split(':')[1] + '.json')).unlink()
    elif damage == 'out_of_scope':
        (path / 'test.js').write_text('unauthorized change')
    elif damage == 'wrong_workspace':
        await patch(env, 'dispatch_context', 'bad-attempt', task={**task, 'workspace': str(env.tmp_path)})
    with pytest.raises(DomainError) as error:
        await env.service.recover('run', request(), 'unsafe-code')
    assert error.value.code in {'recovery_evidence_invalid', 'active_work', 'recovery_checkpoint_invalid', 'write_scope_violation'}
    assert (path / 'src/keep.js').is_file()
    assert (await env.store.read('work_item', 'bad'))['generation'] == 1
    assert not await env.store.list('run_recovery')


@pytest.mark.parametrize('change,code', [({'deleted_at': 'now'}, 'product_deleted'),
    ({'needs_restart': True}, 'product_restart_required'), ({'config_revision': 2}, 'product_restart_required'),
    ({'run_id': 'new-run'}, 'historical_product_run')])
async def test_changed_or_deleted_product_cannot_recover_or_use_legacy_controls(env, change, code):
    await patch(env, 'plan', 'plan', product_contract={'product_id': 'product', 'config_revision': 1})
    await patch(env, 'product', 'product', project_id=env.project['id'], run_id='run')
    await patch(env, 'product', 'product', **change)
    for call in [env.service.recover('run', request(), 'recover'),
                 env.workflow.control_run('run', {'expected_revision': 1, 'action': 'resume', 'reason': ''}, 'legacy'),
                 env.workflow.revise('run', {'expected_revision': 1, 'work_item_ids': ['bad'], 'reason': 'retry'}, 'revise')]:
        with pytest.raises(DomainError) as raised:
            await call
        assert raised.value.code == code


def test_recovery_request_is_strict_and_does_not_allow_budget_or_schema_changes():
    for payload in [request(revision=True), {**request(), 'mode': 'resume'}, {**request(), 'max_model_requests': 0},
                    {**request(), 'output_schema': {}}, {**request(), 'work_item_id': ''},
                    *[{**request(), 'use_current_model_settings': value} for value in ['true', 1, None]]]:
        with pytest.raises(ValidationError):
            RecoveryRequest.model_validate(payload)


async def test_owner_routes_preview_and_recovery_use_revision_idempotency_and_wake(env):
    wakeups = []
    app = create_app(env.settings, store=env.store, artifacts=env.artifacts, scheduler=SimpleNamespace(wake=lambda: wakeups.append(1)))
    token = app.state.tokens.issue('agentflow_owner', {'owner:*'}, 'owner', 60)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=env.settings.origin,
            headers={'Authorization': 'Bearer ' + token, 'Origin': env.settings.origin}) as client:
        preview = await client.get('/api/v1/runs/run/recovery_options')
        assert preview.status_code == 200 and preview.json()['retry_options'][0]['eligible']
        result = await client.post('/api/v1/runs/run/recover', json=request(), headers={'Idempotency-Key': 'owner'})
        assert result.status_code == 200 and result.json()['run']['execution_state'] == 'running'
        assert wakeups == [1]


async def test_confirmed_prelaunch_failure_can_retry_without_inventing_supervisor_receipts(env):
    work = await patch(env, 'work_item', 'bad', status='blocked')
    attempt = await patch(env, 'attempt', 'bad-attempt', status='blocked')
    task = {k: attempt[k] for k in ('run_id', 'iteration_id', 'work_item_id', 'fencing_token', 'input_fingerprint')}
    task.update(attempt_id=attempt['id'], step=work['step'], role=work['role'], output_schema={'type': 'object'},
                workspace=str(env.tmp_path / 'project'), goal='fixture')
    await patch(env, 'dispatch_context', attempt['id'], task=task)
    assert not (await env.service.options('run'))['retry_options'][0]['eligible']
    assert await record_prelaunch_failure(env.store, env.settings.data_dir, task,
        phase='sandbox_validation', failure_code='isolation_unverified')
    assert (await env.service.options('run'))['retry_options'][0]['eligible']
    result = await env.service.recover('run', request(), 'retry-prelaunch')
    assert result['execution'] == 'fresh_attempt'
    assert (await env.store.read('work_item', 'bad'))['generation'] == 2
    assert (await env.store.read('attempt', 'bad-attempt'))['status'] == 'blocked'
    assert not await env.store.list('supervised_attempt')
    assert not await env.store.list('model_invocation')
    assert (await env.store.read('prelaunch_failure', 'bad-attempt'))['outcome'] == 'not_started'


async def model_recovery_fixture(env):
    """Configured profiles and stopped coding work; no credentials or models used."""
    for identity in ('original-coding', 'current-coding', 'later-coding'):
        model = ModelProfile(model_profile_id=identity, provider='openai_compatible',
            requested_model='fixture-model', accepted_api_model='fixture-model', acceptance_status='accepted',
            base_url='https://fixture.invalid/v1', protocols=['responses'],
            credential_reference='env:UNUSED_FIXTURE_KEY', max_output_tokens=16384)
        await patch(env, 'model_profile', identity, **model.model_dump(exclude={'revision'}))
    await patch(env, 'product_model_binding', 'default', coding_model_profile_id='current-coding')
    bindings = {'coding_model_profile_id': 'original-coding', 'role_model_profile_id': 'original-role'}
    await patch(env, 'plan', 'plan', runtime_bindings=bindings, model_profile_revisions={'original-coding': 1})
    run = await patch(env, 'run', 'run', runtime_bindings=bindings)
    await patch(env, 'work_item', 'bad', step='implementation', role='development', write_paths=['src'])
    await patch(env, 'work_item', 'after', step='unit_test_implementation', role='development', write_paths=['tests'])
    prior = await env.store.read('work_item', 'upstream')
    sibling = {k: v for k, v in prior.items() if k not in {'id', 'revision'}}
    sibling.update(key='sibling', step='implementation', role='development', dependencies=['upstream'],
                   write_paths=['other'], attempt_id=None)
    await patch(env, 'work_item', 'sibling', **sibling)
    await patch(env, 'artifact', 'sibling-artifact', run_id='run', work_item_id='sibling', stale=False)
    return {**request(revision=run['revision'], target='bad'), 'use_current_model_settings': True}


async def test_current_model_choice_only_binds_new_affected_coding_generations(env):
    payload = await model_recovery_fixture(env)
    before = {kind: await env.store.list(kind) for kind in ('plan', 'attempt', 'budget_account', 'model_profile')}
    original_run = await env.store.read('run', 'run')
    sibling = await env.store.read('work_item', 'sibling')
    upstream = await env.store.read('work_item', 'upstream')
    result = await env.service.recover('run', payload, 'new-model')
    assert result['model_settings_source'] == 'current_configuration'
    assert set(result['model_profile_bindings']) == {'bad', 'after'}
    assert result['run']['runtime_bindings'] == original_run['runtime_bindings']
    assert result['run']['budget_limit'] == original_run['budget_limit']
    for kind, records in before.items():
        assert await env.store.list(kind) == records
    assert await env.store.read('work_item', 'sibling') == sibling
    assert await env.store.read('work_item', 'upstream') == upstream
    assert not (await env.store.read('artifact', 'sibling-artifact'))['stale']
    for identity in ('bad', 'after'):
        work = await env.store.read('work_item', identity)
        binding = work['payload']['recovery_model_binding']
        assert binding == {'recovery_id': result['id'], 'run_id': 'run', 'iteration_id': 'iteration',
            'work_item_id': identity, 'generation': 2, 'model_profile_id': 'current-coding', 'profile_revision': 1}
        assert result['model_profile_bindings'][identity] == {**binding, 'payload_digest': canonical_digest(binding)}
        assert await resolve_recovery_model_profile(env.store, result['run'], work) == 'current-coding'
    assert await resolve_recovery_model_profile(env.store, result['run'], sibling) is None
    for revision in await env.store.list('work_revision'):
        assert 'recovery_model_binding' not in revision['snapshot']['payload']
    claim = await env.workflow.claim_next('run', 'fixture', 'new-attempt')
    assert claim['work_item']['id'] == 'bad' and claim['attempt']['generation'] == 2
    assert await resolve_recovery_model_profile(env.store, claim['run'], claim['work_item']) == 'current-coding'


async def test_unchecked_retry_retains_prior_explicit_choice_with_new_recovery_identity(env):
    payload = await model_recovery_fixture(env)
    first = await env.service.recover('run', payload, 'first-model-choice')
    first_work = await env.store.read('work_item', 'bad')
    await patch(env, 'product_model_binding', 'default', coding_model_profile_id='later-coding')
    cancelled = await env.workflow.control_run('run', {'expected_revision': first['run']['revision'],
        'action': 'cancel', 'reason': 'fixture stopped before dispatch'}, 'stop-before-dispatch')
    stopped_work = await env.store.read('work_item', 'bad')
    assert stopped_work['payload']['recovery_model_binding'] == first_work['payload']['recovery_model_binding']
    second = await env.service.recover('run', request(revision=cancelled['revision'], target='bad'), 'keep-model-choice')
    work = await env.store.read('work_item', 'bad')
    assert second['model_settings_source'] == 'prior_recovery'
    assert work['generation'] == 3
    assert work['payload']['recovery_model_binding']['recovery_id'] == second['id'] != first['id']
    assert work['payload']['recovery_model_binding']['profile_revision'] == 1
    assert await resolve_recovery_model_profile(env.store, second['run'], work) == 'current-coding'
    assert await env.store.read('run_recovery', first['id']) == first
    history = await env.store.list('work_revision')
    assert any(row['snapshot'] == stopped_work for row in history)


async def test_coding_aggregation_choice_preserves_the_completed_sibling_model(env):
    payload = await model_recovery_fixture(env)
    bad = await patch(env, 'work_item', 'bad', kind='stage_child', parent_stage_id='coding-stage')
    sibling = await patch(env, 'work_item', 'sibling', kind='stage_child', parent_stage_id='coding-stage')
    stage = {k: v for k, v in bad.items() if k not in {'id', 'revision', 'parent_stage_id'}}
    stage.update(key='implementation', kind='aggregation', status='pending', attempt_id=None,
                 dependencies=['bad', 'sibling'], original_dependencies=['upstream'],
                 original_write_paths=['src'], expanded_child_ids=['bad', 'sibling'])
    await patch(env, 'work_item', 'coding-stage', **stage)
    await patch(env, 'work_item', 'after', dependencies=['coding-stage'])
    result = await env.service.recover('run', {**payload, 'work_item_id': 'coding-stage'}, 'coding-stage-choice')
    assert set(result['model_profile_bindings']) == {'bad', 'coding-stage', 'after'}
    assert await env.store.read('work_item', 'sibling') == sibling
    assert await resolve_recovery_model_profile(env.store, result['run'], sibling) is None
    assert await resolve_recovery_model_profile(env.store, result['run'], await env.store.read('work_item', 'bad')) == 'current-coding'


async def test_model_choice_receipt_replays_before_later_config_or_profile_validation(env):
    payload = await model_recovery_fixture(env)
    first = await env.service.recover('run', payload, 'replayed-model')
    await patch(env, 'product_model_binding', 'default', coding_model_profile_id='missing-current-model')
    await patch(env, 'model_profile', 'current-coding', max_output_tokens=8192)
    assert await env.service.recover('run', payload, 'replayed-model') == first
    with pytest.raises(DomainError) as error:
        await env.service.recover('run', {**payload, 'use_current_model_settings': False}, 'replayed-model')
    assert error.value.code == 'idempotency_conflict'


async def test_unchecked_new_retry_uses_original_run_binding_until_owner_selects_otherwise(env):
    payload = await model_recovery_fixture(env)
    result = await env.service.recover('run', {**payload, 'use_current_model_settings': False}, 'original-model')
    work = await env.store.read('work_item', 'bad')
    assert 'model_profile_bindings' not in result
    assert 'recovery_model_binding' not in work['payload']
    assert await resolve_recovery_model_profile(env.store, result['run'], work) is None
    assert result['run']['runtime_bindings']['coding_model_profile_id'] == 'original-coding'


async def test_changed_profile_rejects_frozen_choice_and_unchecked_retry_without_mutation(env):
    payload = await model_recovery_fixture(env)
    first = await env.service.recover('run', payload, 'frozen-choice')
    await patch(env, 'model_profile', 'current-coding', max_output_tokens=8192)
    with pytest.raises(DomainError) as error:
        await resolve_recovery_model_profile(env.store, first['run'], await env.store.read('work_item', 'bad'))
    assert error.value.code == 'stale_model_profile'
    stopped = await env.workflow.control_run('run', {'expected_revision': first['run']['revision'],
        'action': 'cancel', 'reason': 'fixture'}, 'stop-stale')
    before = await env.store.list('work_item')
    with pytest.raises(DomainError) as error:
        await env.service.recover('run', request(revision=stopped['revision'], target='bad'), 'retry-stale')
    assert error.value.code == 'stale_model_profile'
    assert await env.store.list('work_item') == before
    assert len(await env.store.list('run_recovery')) == 1
    await patch(env, 'product_model_binding', 'default', coding_model_profile_id='later-coding')
    refreshed = await env.service.recover('run', {**request(revision=stopped['revision'], target='bad'),
        'use_current_model_settings': True}, 'replace-stale-choice')
    assert await resolve_recovery_model_profile(env.store, refreshed['run'], await env.store.read('work_item', 'bad')) == 'later-coding'


@pytest.mark.parametrize('field,value', [('model_profile_id', 'later-coding'), ('run_id', 'other-run'),
    ('iteration_id', 'other-iteration'), ('generation', 3), ('work_item_id', 'sibling'),
    ('profile_revision', 2), ('recovery_id', 'missing-recovery'), ('untrusted_option', 'injected')])
async def test_arbitrary_payload_cannot_select_a_different_recovery_model(env, field, value):
    payload = await model_recovery_fixture(env)
    result = await env.service.recover('run', payload, 'trusted-choice')
    work = await env.store.read('work_item', 'bad')
    binding = {**work['payload']['recovery_model_binding'], field: value}
    forged = {**work, 'payload': {**work['payload'], 'recovery_model_binding': binding}}
    with pytest.raises(DomainError) as error:
        await resolve_recovery_model_profile(env.store, result['run'], forged)
    assert error.value.code == 'invalid_recovery_model_binding'


@pytest.mark.parametrize('damage', ['digest', 'affected', 'generation'])
async def test_recovery_model_requires_matching_receipt_scope_and_digest(env, damage):
    payload = await model_recovery_fixture(env)
    result = await env.service.recover('run', payload, 'receipt-choice')
    work = await env.store.read('work_item', 'bad')
    if damage == 'affected':
        await patch(env, 'run_recovery', result['id'], affected_work_item_ids=['after'])
    else:
        fields = {'payload_digest': 'sha256:' + '0' * 64} if damage == 'digest' else {'generation': 99}
        bindings = {**result['model_profile_bindings'], 'bad': {**result['model_profile_bindings']['bad'], **fields}}
        await patch(env, 'run_recovery', result['id'], model_profile_bindings=bindings)
    with pytest.raises(DomainError) as error:
        await resolve_recovery_model_profile(env.store, result['run'], work)
    assert error.value.code == 'invalid_recovery_model_binding'


@pytest.mark.parametrize('change', ['binding', 'profile'])
async def test_configuration_race_cannot_commit_a_different_model_choice(env, monkeypatch, change):
    payload = await model_recovery_fixture(env)
    original = env.service._checkpoints
    async def capture(*args, **kwargs):
        points = await original(*args, **kwargs)
        if change == 'binding':
            await patch(env, 'product_model_binding', 'default', coding_model_profile_id='later-coding')
        else:
            await patch(env, 'model_profile', 'current-coding', max_output_tokens=8192)
        return points
    monkeypatch.setattr(env.service, '_checkpoints', capture)
    before = await env.store.list('work_item')
    with pytest.raises(DomainError) as error:
        await env.service.recover('run', payload, 'raced-model')
    assert error.value.code == 'recovery_model_changed'
    assert await env.store.list('work_item') == before
    assert not await env.store.list('run_recovery')


@pytest.mark.parametrize('change', ['missing', 'unaccepted', 'protocol'])
async def test_current_coding_model_must_exist_be_accepted_and_support_responses(env, change):
    payload = await model_recovery_fixture(env)
    if change == 'missing':
        await patch(env, 'product_model_binding', 'default', coding_model_profile_id='missing-profile')
    elif change == 'unaccepted':
        await patch(env, 'model_profile', 'current-coding', acceptance_status='unverified')
    else:
        await patch(env, 'model_profile', 'current-coding', protocols=['chat_completions'])
    with pytest.raises(DomainError) as error:
        await env.service.recover('run', payload, 'unavailable-choice')
    assert error.value.code == 'recovery_model_unavailable'
    assert not await env.store.list('run_recovery')


async def test_current_settings_are_rejected_for_non_coding_or_continue(env):
    with pytest.raises(DomainError) as error:
        await env.service.recover('run', {**request(), 'use_current_model_settings': True}, 'research-model')
    assert error.value.code == 'recovery_model_not_applicable'
    with pytest.raises(DomainError) as error:
        await env.service.recover('run', {**request('continue'), 'use_current_model_settings': True}, 'continue-model')
    assert error.value.code == 'invalid_recovery'
    assert not await env.store.list('run_recovery')


async def test_explicit_false_replays_pre_option_command_without_changing_its_identity(env):
    key = 'legacy-recovery'
    identity = str(uuid5(NAMESPACE_URL, f'run-recovery:run:{key}'))
    legacy_payload = {'run_id': 'run', 'expected_revision': 1, 'mode': 'retry', 'work_item_id': None, 'reason': ''}
    legacy = await env.store.command('run.recover', key, legacy_payload,
        lambda tx: tx.put('run_recovery', identity, {'run_id': 'run', 'mode': 'retry', 'legacy_receipt': True}))
    assert await env.service.recover('run', {**request(), 'use_current_model_settings': False}, key) == legacy
    assert await env.service.recover('run', request(), key) == legacy
    with pytest.raises(DomainError) as error:
        await env.service.recover('run', {**request(), 'use_current_model_settings': True}, key)
    assert error.value.code == 'idempotency_conflict'


async def assert_system_model_inheritance(env, prior_work, generation):
    work = await env.store.read('work_item', prior_work['id'])
    run = await env.store.read('run', work['run_id'])
    binding = work['payload']['recovery_model_binding']
    receipt = await env.store.read('run_recovery', binding['recovery_id'])
    assert binding['generation'] == work['generation'] == generation
    assert binding['model_profile_id'] == prior_work['payload']['recovery_model_binding']['model_profile_id']
    assert receipt['actor'] == 'system' and receipt['mode'] == 'inherit_model_settings'
    assert receipt['execution'] == 'inherit_binding_only'
    assert receipt['source_binding'] == prior_work['payload']['recovery_model_binding']
    assert receipt['source_payload_digest'] == canonical_digest(receipt['source_binding'])
    assert await resolve_recovery_model_profile(env.store, run, work) == binding['model_profile_id']
    return work, receipt


async def test_ordinary_revisions_audit_inheritance_across_generations_without_switching_defaults(env):
    first = await env.service.recover('run', await model_recovery_fixture(env), 'initial-model-choice')
    before = await env.store.read('work_item', 'bad')
    accounts = await env.store.list('budget_account')
    sibling = await env.store.read('work_item', 'sibling')
    await patch(env, 'product_model_binding', 'default', coding_model_profile_id='later-coding')
    payload = {'expected_revision': first['run']['revision'], 'work_item_ids': ['bad'], 'reason': 'Add the missing validation'}
    result = await env.workflow.revise('run', payload, 'ordinary-revision')
    revised, inheritance = await assert_system_model_inheritance(env, before, 3)
    assert inheritance['source_recovery_id'] == first['id']
    assert await env.store.read('run_recovery', first['id']) == first
    receipts = await env.store.list('run_recovery')
    assert await env.workflow.revise('run', payload, 'ordinary-revision') == result
    assert await env.store.list('run_recovery') == receipts
    current = await env.store.read('run', 'run')
    await env.workflow.revise('run', {'expected_revision': current['revision'], 'work_item_ids': ['bad'],
        'reason': 'Correct one additional edge case'}, 'second-ordinary-revision')
    latest, second = await assert_system_model_inheritance(env, revised, 4)
    assert second['source_recovery_id'] == inheritance['id']
    assert await env.store.read('work_item', 'sibling') == sibling
    assert await env.store.list('budget_account') == accounts
    assert (await env.store.read('run', 'run'))['runtime_bindings'] == first['run']['runtime_bindings']
    claim = await env.workflow.claim_next('run', 'fixture', 'after-ordinary-revision')
    assert claim['work_item']['id'] == latest['id']
    assert await resolve_recovery_model_profile(env.store, claim['run'], claim['work_item']) == 'current-coding'


async def test_approval_rejection_inherits_model_for_the_next_coding_attempt(env):
    first = await env.service.recover('run', await model_recovery_fixture(env), 'approval-model-choice')
    claim = await env.workflow.claim_next('run', 'fixture', 'code-awaiting-approval')
    blob = await env.artifacts.put_bytes(b'{"summary":"Fixture code awaiting owner review"}')
    attempt = claim['attempt']
    prior = await env.workflow.finish_attempt(attempt['id'], {'execution_status': 'completed', 'quality_result': 'unknown',
        'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint']}, 'finish-code-for-approval',
        verified_artifacts=[{'digest': blob['id'], 'name': 'code-summary.json', 'media_type': 'application/json'}])
    assert prior['status'] == 'waiting_approval'
    approval = next(a for a in await env.store.list('approval') if a['work_item_id'] == prior['id'] and not a['stale'])
    await env.workflow.decide(approval['id'], {'expected_revision': approval['revision'],
        'expected_fingerprint': approval['fingerprint'], 'decision': 'reject', 'reason': 'Missing validation',
        'change_expectation': 'Validate empty input while keeping existing behavior'}, 'reject-code')
    revised, inherited = await assert_system_model_inheritance(env, prior, 3)
    assert inherited['source_recovery_id'] == first['id']
    assert revised['approval_required'] and revised['status'] == 'pending'
    assert (await env.store.read('approval', approval['id']))['decision'] == 'reject'
    next_claim = await env.workflow.claim_next('run', 'fixture', 'after-rejection')
    assert await resolve_recovery_model_profile(env.store, next_claim['run'], next_claim['work_item']) == 'current-coding'


async def test_automatic_review_repair_inherits_explicit_model_without_owner_recovery_event(env):
    payload = await model_recovery_fixture(env)
    await patch(env, 'work_item', 'after', step='code_review', role='review', write_paths=[])
    first = await env.service.recover('run', payload, 'before-review-repair')
    producer = await patch(env, 'work_item', 'bad', status='completed', quality_result='passed', approval_required=False)
    reviewer = await patch(env, 'work_item', 'after', status='completed', quality_result='failed',
        attempt_id='review-repair-attempt', approval_required=False)
    await patch(env, 'attempt', 'review-repair-attempt', run_id='run', iteration_id='iteration', work_item_id='after',
        status='completed', generation=reviewer['generation'], fencing_token=reviewer['fencing_token'],
        input_fingerprint=reviewer['input_fingerprint'])
    commit = env.project['base_commit']
    await patch(env, 'code_snapshot', 'reviewed-snapshot', run_id='run', work_item_id='bad',
        generation=producer['generation'], commit_oid=commit, stale=False)
    await patch(env, 'review', 'review-repair-attempt', run_id='run', work_item_id='after',
        generation=reviewer['generation'], reviewed_commit=commit,
        blocking_findings=[{'path': 'src/keep.js', 'severity': 'blocking',
                            'description': 'Reject empty input before saving'}])
    await patch(env, 'plan', 'plan', authorized_rework_steps=['implementation'])
    await patch(env, 'product_model_binding', 'default', coding_model_profile_id='later-coding')
    accounts = await env.store.list('budget_account')
    repairs = ReviewRemediation(env.store, env.workflow)
    repair = await repairs.repair('after')
    assert repair and repair['producer_work_item_id'] == 'bad'
    revised, inherited = await assert_system_model_inheritance(env, producer, 3)
    assert revised['payload']['repair_base_snapshot_id'] == 'reviewed-snapshot'
    assert inherited['source_recovery_id'] == first['id']
    events = await env.store.events(0, run_id='run')
    assert sum(e['type'] == 'run.recovered' for e in events) == 1
    assert any(e['type'] == 'model.settings_inherited' for e in events)
    assert len(await env.store.list('review_repair')) == 1
    assert await env.store.list('budget_account') == accounts
    claim = await env.workflow.claim_next('run', 'fixture', 'after-automatic-review-repair')
    assert claim['work_item']['id'] == 'bad'
    assert await resolve_recovery_model_profile(env.store, claim['run'], claim['work_item']) == 'current-coding'


@pytest.mark.parametrize('damage', ['profile_revision', 'receipt_digest'])
async def test_ordinary_rework_rejects_unverified_inheritance_atomically(env, damage):
    first = await env.service.recover('run', await model_recovery_fixture(env), 'verified-selection')
    if damage == 'profile_revision':
        await patch(env, 'model_profile', 'current-coding', max_output_tokens=8192)
    else:
        bindings = {**first['model_profile_bindings'], 'bad': {**first['model_profile_bindings']['bad'], 'payload_digest': 'wrong'}}
        await patch(env, 'run_recovery', first['id'], model_profile_bindings=bindings)
    before = {kind: await env.store.list(kind) for kind in ('run', 'work_item', 'work_revision', 'run_recovery')}
    with pytest.raises(DomainError) as error:
        await env.workflow.revise('run', {'expected_revision': first['run']['revision'],
            'work_item_ids': ['bad'], 'reason': 'ordinary correction'}, 'invalid-inheritance')
    assert error.value.code == ('stale_model_profile' if damage == 'profile_revision' else 'invalid_recovery_model_binding')
    for kind, rows in before.items():
        assert await env.store.list(kind) == rows


async def test_explicit_current_choice_can_replace_stale_system_inherited_profile(env):
    first = await env.service.recover('run', await model_recovery_fixture(env), 'first-owner-choice')
    await env.workflow.revise('run', {'expected_revision': first['run']['revision'],
        'work_item_ids': ['bad'], 'reason': 'ordinary correction'}, 'system-inheritance')
    run = await env.store.read('run', 'run')
    cancelled = await env.workflow.control_run('run', {'expected_revision': run['revision'],
        'action': 'cancel', 'reason': 'fixture pause before selecting a different configured profile'}, 'cancel-inherited')
    await patch(env, 'model_profile', 'current-coding', max_output_tokens=8192)
    await patch(env, 'product_model_binding', 'default', coding_model_profile_id='later-coding')
    result = await env.service.recover('run', {**request(revision=cancelled['revision'], target='bad'),
        'use_current_model_settings': True}, 'replace-inherited-choice')
    work = await env.store.read('work_item', 'bad')
    assert work['generation'] == 4
    assert result['actor'] == 'owner' and result['model_settings_source'] == 'current_configuration'
    assert await resolve_recovery_model_profile(env.store, result['run'], work) == 'later-coding'

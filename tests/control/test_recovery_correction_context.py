"""Retry retains the accepted correction and reviewed source, using isolated stores and Git."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_coding_steps import _stopped_coding_process_proof, execute, task_for
from test_parallel_remediation import parallel_env as parallel_env
from test_recovery import cancel_fixture, patch
from test_recovery import env as recovery_env
from test_remediation import env as review_env

from agentflow.adapters.codex.adapter import _execution_prompt
from agentflow.common import DomainError, canonical_digest
from agentflow.control.failure_remediation import FailureRemediation
from agentflow.control.recovery import RunRecoveryService
from agentflow.control.scheduler import Scheduler
from agentflow.models.profiles import ModelProfile
from agentflow.runtime.service import RuntimeService
from agentflow.runtime.workspace import WorkspaceManager

__all__ = ['recovery_env', 'review_env']


@pytest.mark.parametrize('mode', ['retry', 'continue'])
async def test_reexecution_preserves_user_correction_and_separates_operation_reason(recovery_env, mode):
    env = recovery_env
    correction = 'Preserve root-directory access checks and reject deletion of the root.'
    await patch(env, 'work_item', 'bad', payload={'change_expectation': correction})
    if mode == 'continue':
        await cancel_fixture(env)
    run = await env.store.read('run', 'run')
    await env.service.recover('run', {'expected_revision': run['revision'], 'mode': mode,
        'reason': 'Retry the stopped execution in smaller steps.'}, 'same-work-' + mode)
    work = await env.store.read('work_item', 'bad')
    assert work['payload']['change_expectation'] == correction
    assert work['payload']['recovery_instruction'] == 'Retry the stopped execution in smaller steps.'
    run = await env.store.read('run', 'run')
    await env.workflow.revise('run', {'expected_revision': run['revision'], 'work_item_ids': ['bad'],
        'reason': 'New user requirement replaces the previous scope.'}, 'new-scope')
    work = await env.store.read('work_item', 'bad')
    assert work['payload']['change_expectation'] == 'New user requirement replaces the previous scope.'
    assert 'recovery_instruction' not in work['payload']


async def prepare_review_repair(env, tmp_path, work_id='code'):
    env.root = tmp_path
    env.settings = env.settings.model_copy(update={'max_coding_steps': 8, 'auto_failure_retry_delay_seconds': 0})
    env.workflow.settings = env.settings
    for kind, identity in [('run', 'run'), ('iteration', 'iteration')]:
        row = await env.store.read(kind, identity)
        await patch(env, kind, identity, budget_limit={**row['budget_limit'], 'max_active_seconds': 100,
            'max_tool_calls': 20, 'cost_mode': 'request_limited'})
    await patch(env, 'run', 'run', goal='Correct addition without losing existing implementation.',
        base_commit=env.project['base_commit'], base_ref=env.project['base_ref'])
    for work in await env.store.list('work_item'):
        fingerprint = canonical_digest({'work': work['id']})
        await patch(env, 'work_item', work['id'], input_fingerprint=fingerprint)
        if work.get('attempt_id'):
            await patch(env, 'attempt', work['attempt_id'], input_fingerprint=fingerprint)
    repaired = await env.remediation.repair('review-work')
    assert repaired is not None
    correction = (await env.store.read('work_item', work_id))['payload']['change_expectation']
    env.scheduler = Scheduler(env.workflow, env.store, None, None, env.settings)
    env.recovery = RunRecoveryService(env.store, env.workflow)
    return correction


async def timeout_without_edits(env, work_id='code'):
    if work_id == 'code':
        task = await task_for(env)
    else:
        claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
        work, attempt, run = claim['work_item'], claim['attempt'], claim['run']
        assert work['id'] == work_id
        source, commit = await env.scheduler._source(run, work)
        control = await env.scheduler.coding_steps.prepare(run, work, attempt, commit, 512)
        task = {'attempt_id': attempt['id'], 'work_item_id': work_id, 'run_id': 'run',
            'step': work['step'], 'fencing_token': attempt['fencing_token'],
            'input_fingerprint': attempt['input_fingerprint'], 'workspace': str(source),
            'source_commit': commit, 'allowed_write_paths': work['write_paths'], 'coding_step': control}
    workspace = await WorkspaceManager(env.settings.data_dir).create_clone(
        Path(task['workspace']), task['source_commit'], task['attempt_id'])
    task['workspace'] = str(workspace)
    await _stopped_coding_process_proof(env, task)
    async def timeout(_task):
        return {'execution_status': 'failed', 'runtime_failure_code': 'worker_timeout',
            'summary': 'Stopped timeout fixture, no new edits.', 'artifacts': [],
            'active_seconds': 1, 'observed_tool_calls': 1, 'tool_observation_complete': True}
    env.scheduler.runtime = SimpleNamespace(execute_task=timeout)
    await env.scheduler._execute_existing(task)
    assert not (await env.repository.collect_diff(workspace, task['source_commit']))['has_changes']
    result = await FailureRemediation(env.store, env.workflow, recovery=env.recovery).repair(work_id)
    assert result['status'] == 'repair_scheduled', result
    return task


@pytest.mark.parametrize('checked', ['correction', 'source'])
async def test_review_repair_timeout_keeps_exact_source_and_correction_in_next_task(review_env, tmp_path, checked):
    env = review_env
    correction = await prepare_review_repair(env, tmp_path)
    stopped = await timeout_without_edits(env)
    work = await env.store.read('work_item', 'code')
    run = await env.store.read('run', 'run')
    source, commit = await env.scheduler._source(run, work)
    if checked == 'source':
        assert commit == stopped['source_commit'] == env.snapshot['commit_oid']
        assert (source / 'keep.txt').read_text() == 'valuable prior implementation\n'
    else:
        assert work['payload']['change_expectation'] == correction
        assert '更小' in work['payload']['recovery_instruction']
    next_task = await task_for(env)
    work = await env.store.read('work_item', 'code')
    run = await env.store.read('run', 'run')
    goal = await env.scheduler._prompt(run, work, next_task['source_commit'], coding_step=next_task['coding_step'])
    runtime = RuntimeService.__new__(RuntimeService)
    runtime.data_dir = env.settings.data_dir.resolve()
    profile = ModelProfile(model_profile_id='fixture', provider='openai_compatible', requested_model='fixture-model',
        accepted_api_model='fixture-model', acceptance_status='accepted', protocols=['responses'],
        base_url='https://fixture.invalid/v1', credential_reference='fixture')
    runtime.models = SimpleNamespace(registry=SimpleNamespace(get=AsyncMock(return_value=profile)))
    runtime.workspaces = SimpleNamespace(assert_owned=lambda *_args, **_kwargs: None)
    runtime.codex, runtime.openhands = object(), object()
    envelope, _ = await runtime._envelope({**next_task, 'goal': goal, 'role': 'development',
        'iteration_id': 'iteration', 'profile_id': 'fixture'}, resume=True)
    prompt = _execution_prompt(envelope)
    assert 'Required correction: ' + correction in prompt
    assert 'Recovery execution guidance: ' + work['payload']['recovery_instruction'] in prompt
    assert next_task['source_commit'] == stopped['source_commit']
    # Partial coding steps preserve the same correction while narrowing only the next action.
    result = await execute(env, next_task, {'summary': 'Small correction saved', 'status': 'continue',
        'next_action': 'Check the remaining addition cases'}, 'PARTIAL = True\n', seconds=1, tools=1)
    assert result['payload']['change_expectation'] == correction
    assert result['payload']['recovery_instruction'] == work['payload']['recovery_instruction']
    if checked == 'correction':
        await timeout_without_edits(env)
        work = await env.store.read('work_item', 'code')
        run = await env.store.read('run', 'run')
        source, commit = await env.scheduler._source(run, work)
        assert work['payload']['change_expectation'] == correction
        assert (source / 'feature.py').read_text() == 'PARTIAL = True\n'
        prompt = await env.scheduler._prompt(run, work, commit)
        assert 'Required correction: ' + correction in prompt


@pytest.mark.parametrize('damage', [None, 'source_commit', 'scope', 'alias_generation'])
async def test_parallel_review_timeout_preserves_all_modules_and_checks_source_binding(parallel_env, tmp_path, damage):
    env = parallel_env
    correction = await prepare_review_repair(env, tmp_path, 'module-a')
    before_siblings = {identity: await env.store.read('work_item', identity) for identity in
                       ('module-b', 'module-c', 'module-d')}
    stopped = await timeout_without_edits(env, 'module-a')
    work = await env.store.read('work_item', 'module-a')
    run = await env.store.read('run', 'run')
    checkpoint = await env.store.read('code_snapshot', work['payload']['recovery_checkpoint_id'])
    assert checkpoint['source_attempt_id'] == stopped['attempt_id']
    assert checkpoint['source_commit'] == env.aggregate['commit_oid']
    assert checkpoint['source_write_paths'] == ['src/a.mjs']
    assert checkpoint['source_repair_snapshot_id'].startswith('review-child-checkpoint-')
    if damage:
        if damage == 'source_commit':
            await patch(env, 'dispatch_context', stopped['attempt_id'],
                task={**stopped, 'source_commit': env.project['base_commit']})
        elif damage == 'scope':
            await patch(env, 'work_item', 'module-a', write_paths=['src'])
        else:
            await patch(env, 'code_snapshot', checkpoint['source_repair_snapshot_id'], generation=99)
        with pytest.raises(DomainError) as error:
            await env.scheduler._source(run, await env.store.read('work_item', 'module-a'))
        assert error.value.code == 'invalid_repair_checkpoint'
        return
    source, commit = await env.scheduler._source(run, work)
    assert commit == env.aggregate['commit_oid'] == stopped['source_commit']
    assert work['payload']['change_expectation'] == correction
    assert work['write_paths'] == ['src/a.mjs']
    assert not (await env.repository.collect_diff(source, commit))['has_changes']
    assert all((source / path).read_text() == env.original_text[identity] for identity, path in env.paths.items())
    assert {identity: await env.store.read('work_item', identity) for identity in before_siblings} == before_siblings


async def test_review_repair_blocked_before_dispatch_keeps_its_authorized_aggregate(parallel_env, tmp_path):
    env = parallel_env
    correction = await prepare_review_repair(env, tmp_path, 'module-a')
    work = await patch(env, 'work_item', 'module-a', status='blocked')
    assert work['attempt_id'] is None
    run = await env.store.read('run', 'run')
    await env.recovery.recover('run', {'expected_revision': run['revision'], 'mode': 'retry',
        'work_item_id': 'module-a'}, 'retry-before-dispatch')
    work = await env.store.read('work_item', 'module-a')
    source, commit = await env.scheduler._source(await env.store.read('run', 'run'), work)
    assert commit == env.aggregate['commit_oid']
    assert work['payload']['change_expectation'] == correction
    assert all((source / path).read_text() == env.original_text[identity] for identity, path in env.paths.items())


@pytest.mark.parametrize('damage', ['coherent_initial_baseline', 'receipt_alias', 'source_snapshot',
                                    'review_commit', 'review_attempt'])
async def test_predispatch_review_recovery_rejects_detached_authorization_chain(parallel_env, tmp_path, damage):
    env = parallel_env
    await prepare_review_repair(env, tmp_path, 'module-a')
    work = await env.store.read('work_item', 'module-a')
    alias_id = work['payload']['repair_base_snapshot_id']
    alias = await env.store.read('code_snapshot', alias_id)
    receipt = (await env.store.list('review_repair'))[0]
    if damage == 'coherent_initial_baseline':
        base = env.project['base_commit']
        tree = env.repository._integrity(Path(alias['repository_path']), base)
        await patch(env, 'code_snapshot', alias_id, commit_oid=base, tree_oid=tree, base_oid=base,
            parent_commit_oids=[])
    elif damage == 'receipt_alias':
        await patch(env, 'review_repair', receipt['id'], checkpoint_alias_ids={'module-a': 'another-alias'})
    elif damage == 'source_snapshot':
        await patch(env, 'code_snapshot', alias_id, source_snapshot_id='another-aggregate')
    elif damage == 'review_commit':
        await patch(env, 'review', receipt['review_attempt_id'], reviewed_commit=env.project['base_commit'])
    else:
        await patch(env, 'attempt', receipt['review_attempt_id'], work_item_id='unrelated-review')
    await patch(env, 'work_item', 'module-a', status='blocked')
    run = await env.store.read('run', 'run')
    before = {kind: await env.store.list(kind) for kind in ('work_item', 'run', 'run_recovery', 'code_snapshot')}
    with pytest.raises(DomainError):
        await env.recovery.recover('run', {'expected_revision': run['revision'], 'mode': 'retry',
            'work_item_id': 'module-a'}, 'reject-detached-review-' + damage)
    assert {kind: await env.store.list(kind) for kind in before} == before


async def test_review_recovery_rechecks_receipt_atomically_after_source_validation(parallel_env, tmp_path, monkeypatch):
    env = parallel_env
    await prepare_review_repair(env, tmp_path, 'module-a')
    await patch(env, 'work_item', 'module-a', status='blocked')
    run = await env.store.read('run', 'run')
    receipt = (await env.store.list('review_repair'))[0]
    original = env.recovery._checkpoints
    before_work = await env.store.list('work_item')
    changed = False
    async def race(*args, **kwargs):
        nonlocal changed
        result = await original(*args, **kwargs)
        if kwargs.get('freeze') and not changed:
            changed = True
            await patch(env, 'review_repair', receipt['id'], checkpoint_alias_ids={'module-a': 'changed'})
        return result
    monkeypatch.setattr(env.recovery, '_checkpoints', race)
    with pytest.raises(DomainError) as error:
        await env.recovery.recover('run', {'expected_revision': run['revision'], 'mode': 'retry',
            'work_item_id': 'module-a'}, 'reject-review-receipt-race')
    assert error.value.code == 'revision_conflict'
    assert await env.store.list('work_item') == before_work
    assert not await env.store.list('run_recovery')


@pytest.mark.parametrize('damage', ['receipt_alias', 'review_commit'])
async def test_reused_preserved_review_source_revalidates_original_authorization(parallel_env, tmp_path, damage):
    env = parallel_env
    await prepare_review_repair(env, tmp_path, 'module-a')
    await patch(env, 'work_item', 'module-a', status='blocked')
    run = await env.store.read('run', 'run')
    await env.recovery.recover('run', {'expected_revision': run['revision'], 'mode': 'retry',
        'work_item_id': 'module-a'}, 'preserve-review-before-mutation')
    receipt = (await env.store.list('review_repair'))[0]
    if damage == 'receipt_alias':
        await patch(env, 'review_repair', receipt['id'], checkpoint_alias_ids={'module-a': 'changed'})
    else:
        await patch(env, 'review', receipt['review_attempt_id'], reviewed_commit=env.project['base_commit'])
    with pytest.raises(DomainError) as error:
        await env.scheduler._source(await env.store.read('run', 'run'), await env.store.read('work_item', 'module-a'))
    assert error.value.code == 'invalid_repair_checkpoint'

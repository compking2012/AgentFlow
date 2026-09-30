"""Finite-output recovery preserves code and stops repeated no-progress attempts."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import psutil
import pytest
import pytest_asyncio
from test_recovery import env as env
from test_recovery import patch, stopped_workspace

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.coding_steps import CodingSteps
from agentflow.control.failure_remediation import FailureRemediation, resolve_bounded_coding_recovery
from agentflow.models.budget import account_id
from agentflow.models.profiles import ModelProfile
from agentflow.runtime.launcher import atomic_json
from agentflow.runtime.workspace import WorkspaceManager


async def failed_response(env, work, workspace, source):
    task = {'attempt_id': work['attempt_id'], 'work_item_id': work['id'], 'run_id': 'run', 'iteration_id': 'iteration',
        'fencing_token': work['fencing_token'], 'input_fingerprint': work['input_fingerprint'],
        'workspace': str(workspace), 'allowed_write_paths': work['write_paths'], 'step': work['step'],
        'source_commit': source, 'output_schema': {'type': 'object'}, 'profile_id': 'bounded-model',
        'max_output_tokens': 64, 'reasoning_effort': 'high'}
    await patch(env, 'dispatch_context', work['attempt_id'], task=task)
    await patch(env, 'work_item', work['id'], status='failed', quality_result='unknown')
    await patch(env, 'attempt', work['attempt_id'], status='failed', runtime_failure_code='reasoning_output_limit')
    identity = {'attempt_id': work['attempt_id'], 'operation_id': 'operation-' + work['attempt_id'], 'nonce': str(uuid4()),
        'pid': 1073741824, 'process_started_at': 1.0, 'boot_fingerprint': canonical_digest({'boot_time': psutil.boot_time()}),
        'fencing_token': work['fencing_token']}
    directory = env.settings.data_dir / 'supervisor' / canonical_digest({'attempt_id': work['attempt_id']}).split(':')[1]
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    atomic_json(directory / 'result.json', {**identity, 'execution_status': 'failed', 'exit_code': 1})
    await patch(env, 'supervised_attempt', work['attempt_id'], **identity, state='failed', run_id='run',
                directory=str(directory), input_fingerprint=work['input_fingerprint'])
    call_id = str(uuid4())
    events = [
        {'type': 'response.created', 'response': {'id': 'response-' + call_id, 'status': 'in_progress', 'output': []}},
        {'type': 'response.reasoning_text.delta', 'delta': 'PRIVATE_REASONING_FIXTURE'},
        {'type': 'response.incomplete', 'response': {'id': 'response-' + call_id, 'status': 'incomplete',
            'incomplete_details': {'reason': 'max_output_tokens'}, 'output': [{'type': 'reasoning', 'summary': []}],
            'usage': {'input_tokens': 12, 'output_tokens': 64, 'output_tokens_details': {'reasoning_tokens': 64}}}},
    ]
    raw = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
    folder = env.settings.data_dir / 'model_invocations'
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / (call_id + '.sse')
    path.write_bytes(raw)
    path.chmod(0o600)
    await patch(env, 'model_invocation', call_id, operation_id=call_id, attempt_id=work['attempt_id'], run_id='run',
        iteration_id='iteration', work_item_id=work['id'], fencing_token=work['fencing_token'],
        input_fingerprint=work['input_fingerprint'], profile_id='bounded-model', profile_revision=1,
        protocol='responses', state='completed_unpriced', created_at=utc_now(), usage={'input_tokens': 12, 'output_tokens': 64},
        response_receipt={'path': str(path), 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest(),
                          'media_type': 'text/event-stream', 'status_code': 200})
    for kind, owner in [('run', 'run'), ('iteration', 'iteration')]:
        account = await env.store.read('budget_account', account_id(kind, owner))
        await patch(env, 'budget_account', account['id'], request_count=account['request_count'] + 1)
    return task, path, call_id


@pytest_asyncio.fixture
async def bounded(env):
    env.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0, 'auto_failure_retry_limit': 2})
    env.workflow.settings = env.settings
    await patch(env, 'run', 'run', execution_state='running')
    path, _, _, _ = await stopped_workspace(env)
    (path / 'src/keep.js').unlink()
    (path / 'src').rmdir()
    model = ModelProfile(model_profile_id='bounded-model', provider='openai_compatible', requested_model='fixture',
        accepted_api_model='fixture', acceptance_status='accepted', base_url='https://fixture.invalid/v1',
        protocols=['responses'], credential_reference='env:UNUSED_FIXTURE', max_output_tokens=64, reasoning_effort='high')
    await patch(env, 'model_profile', model.model_profile_id, **model.model_dump(exclude={'revision'}))
    work = await env.store.read('work_item', 'bad')
    env.frozen_task, env.receipt_path, env.call_id = await failed_response(env, work, path, env.project['base_commit'])
    env.workspace = path
    env.automatic = FailureRemediation(env.store, env.workflow, recovery=env.service)
    return env


async def fail_next_bounded_step(env, *, progress=False):
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert claim['work_item']['id'] == 'bad'
    work = claim['work_item']
    checkpoint = await env.store.read('code_snapshot', work['payload']['recovery_checkpoint_id'])
    workspace = await WorkspaceManager(env.settings.data_dir).create_clone(
        Path(checkpoint['repository_path']), checkpoint['commit_oid'], work['attempt_id'])
    if progress:
        (workspace / 'src').mkdir(exist_ok=True)
        (workspace / 'src/step.js').write_text(f'export const step = {work["generation"]};\n')
    await failed_response(env, work, workspace, checkpoint['commit_oid'])
    return workspace


async def test_first_reasoning_only_failure_uses_a_bound_small_step_without_changing_model_or_budget(bounded):
    env = bounded
    budgets = await env.store.list('budget_account')
    profiles = await env.store.list('model_profile')
    result = await env.automatic.repair('bad')
    assert result['status'] == 'repair_scheduled', result
    work = await env.store.read('work_item', 'bad')
    plan = await resolve_bounded_coding_recovery(env.store, await env.store.read('run', 'run'), work)
    assert plan['round'] == 1 and plan['no_progress_streak'] == 1
    assert plan['max_files_per_step'] == 1 and plan['max_changed_lines_per_step'] == 80
    assert plan['step_reduction_factor'] == 0.5 and plan['observed_output_cap'] == 64
    assert plan['enforcement'] == 'planning_targets' and not plan['progress']['has_code_changes']
    assert 'continue' in work['payload']['recovery_instruction'] and '全部原任务' in work['payload']['recovery_instruction']
    checkpoint = await env.store.read('code_snapshot', plan['checkpoint_id'])
    assert checkpoint['tree_oid'] == plan['progress']['tree_oid'] == plan['progress']['source_tree_oid']
    assert await env.store.list('budget_account') == budgets and await env.store.list('model_profile') == profiles
    assert 'PRIVATE_REASONING_FIXTURE' not in json.dumps(result)


async def test_repeated_failure_adapts_small_steps_until_configured_retry_limit(bounded):
    env = bounded
    assert (await env.automatic.repair('bad'))['status'] == 'repair_scheduled'
    await fail_next_bounded_step(env)
    budgets = await env.store.list('budget_account')
    result = await env.automatic.repair('bad')
    assert result['status'] == 'repair_scheduled'
    work = await env.store.read('work_item', 'bad')
    plan = await resolve_bounded_coding_recovery(env.store, await env.store.read('run', 'run'), work)
    assert plan['round'] == 2 and plan['no_progress_streak'] == 2
    assert plan['max_files_per_step'] == 1 and plan['max_actions_per_step'] == 1
    assert await env.store.list('budget_account') == budgets
    await fail_next_bounded_step(env)
    before = await env.store.list('work_item')
    budgets = await env.store.list('budget_account')
    result = await env.automatic.repair('bad')
    assert result['status'] == 'blocked'
    assert 'automatic_repair_limit' in {row['code'] for row in result['blockers']}
    assert await env.store.list('work_item') == before and await env.store.list('budget_account') == budgets
    assert len([row for row in await env.store.list('run_recovery') if row.get('failure_analysis_id')]) == 2


async def test_new_code_progress_is_preserved_and_next_step_shrinks_then_existing_limit_stops(bounded):
    env = bounded
    assert (await env.automatic.repair('bad'))['status'] == 'repair_scheduled'
    workspace = await fail_next_bounded_step(env, progress=True)
    before_text = (workspace / 'src/step.js').read_text()
    result = await env.automatic.repair('bad')
    assert result['status'] == 'repair_scheduled', result
    work = await env.store.read('work_item', 'bad')
    plan = await resolve_bounded_coding_recovery(env.store, await env.store.read('run', 'run'), work)
    assert plan['round'] == 2 and plan['no_progress_streak'] == 0
    assert plan['max_changed_lines_per_step'] == 40 and plan['max_actions_per_step'] == 1
    assert plan['step_reduction_factor'] == 0.25
    assert plan['progress']['changed_paths'] == ['src/step.js']
    checkpoint = await env.store.read('code_snapshot', plan['checkpoint_id'])
    restored = env.tmp_path / 'verified-bounded-code'
    await env.service.repository.clone_snapshot(Path(checkpoint['repository_path']), restored, checkpoint['commit_oid'])
    assert (restored / 'src/step.js').read_text() == before_text
    await fail_next_bounded_step(env, progress=True)
    result = await env.automatic.repair('bad')
    assert result['status'] == 'blocked'
    assert 'automatic_repair_limit' in {row['code'] for row in result['blockers']}


@pytest.mark.parametrize('damage', ['receipt', 'cap', 'scope', 'active_invocation'])
async def test_unverified_receipt_cap_scope_or_live_call_never_authorizes_a_step(bounded, damage):
    env = bounded
    if damage == 'receipt':
        env.receipt_path.write_bytes(b'corrupt')
    elif damage == 'cap':
        await patch(env, 'dispatch_context', 'bad-attempt', task={**env.frozen_task, 'max_output_tokens': 32})
    elif damage == 'scope':
        (env.workspace / 'outside.js').write_text('export const forbidden = true;\n')
    else:
        await patch(env, 'model_invocation', env.call_id, state='dispatching')
    before = await env.store.list('work_item')
    result = await env.automatic.repair('bad')
    assert result['status'] == 'blocked'
    assert await env.store.list('work_item') == before and not await env.store.list('run_recovery')


async def test_bounded_plan_is_generation_bound_and_owner_revision_discards_old_references(bounded):
    env = bounded
    assert (await env.automatic.repair('bad'))['status'] == 'repair_scheduled'
    work = await env.store.read('work_item', 'bad')
    wrong = {**work, 'generation': work['generation'] + 1}
    with pytest.raises(DomainError) as error:
        await resolve_bounded_coding_recovery(env.store, await env.store.read('run', 'run'), wrong)
    assert error.value.code == 'invalid_bounded_coding_recovery'
    await patch(env, 'work_item', 'bad', payload={**work['payload'], 'coding_step_checkpoint_id': 'old-step'})
    run = await env.store.read('run', 'run')
    await env.workflow.revise('run', {'expected_revision': run['revision'], 'work_item_ids': ['bad'],
        'reason': 'Explicitly revise the requested implementation'}, str(uuid4()))
    updated = await env.store.read('work_item', 'bad')
    assert 'bounded_coding_recovery' not in updated['payload'] and 'coding_step_checkpoint_id' not in updated['payload']


@pytest.mark.parametrize('changes', [
    {'uncertain': True}, {'active_seconds': 120}, {'observed_tool_calls': 30}, {'step_count': 32},
])
async def test_shared_coding_step_budget_is_not_reset_to_enable_recovery(bounded, changes):
    env = bounded
    budget = await patch(env, 'coding_work_budget', CodingSteps.budget_id('run', 'bad'), **{
        'run_id': 'run', 'work_item_id': 'bad', 'max_steps': 32, 'max_active_seconds': 120,
        'max_tool_calls': 30, 'active_seconds': 0.0, 'observed_tool_calls': 0, 'step_count': 0,
        'uncertain': False, **changes})
    before = await env.store.list('work_item')
    result = await env.automatic.repair('bad')
    assert result['status'] == 'blocked'
    assert any(row['code'].startswith('coding_budget_') for row in result['blockers'])
    assert await env.store.list('work_item') == before
    assert await env.store.read('coding_work_budget', budget['id']) == budget
    assert not await env.store.list('run_recovery')


async def test_completed_siblings_exhausted_step_allowance_does_not_block_this_target(bounded):
    env = bounded
    prior = await env.store.read('work_item', 'upstream')
    sibling = await patch(env, 'work_item', 'good', **{key: value for key, value in prior.items()
        if key not in {'id', 'revision'}},)
    sibling = await patch(env, 'work_item', 'good', key='good', step='implementation', role='development',
                          write_paths=['other'], attempt_id=None)
    budget = await patch(env, 'coding_work_budget', CodingSteps.budget_id('run', 'good'),
        run_id='run', work_item_id='good', max_steps=1, max_active_seconds=1, max_tool_calls=1,
        active_seconds=1.0, observed_tool_calls=1, step_count=1, uncertain=False)
    assert (await env.automatic.repair('bad'))['status'] == 'repair_scheduled'
    assert await env.store.read('work_item', 'good') == sibling
    assert await env.store.read('coding_work_budget', budget['id']) == budget


@pytest.mark.parametrize('stored_code', [None, 'model_output_limit'])
async def test_generic_or_legacy_missing_code_is_refined_before_choosing_the_bounded_strategy(bounded, stored_code):
    env = bounded
    await patch(env, 'attempt', 'bad-attempt', runtime_failure_code=stored_code)
    directory = env.settings.data_dir / 'supervisor' / canonical_digest({'attempt_id': 'bad-attempt'}).split(':')[1]
    events = directory / 'stdout.jsonl'
    events.write_text(json.dumps({'type': 'error', 'message':
        'Incomplete response returned, reason: max_output_tokens'}) + '\n')
    events.chmod(0o600)
    result = await env.automatic.analyze('bad')
    assert result['failure_code'] == 'reasoning_output_limit'
    assert result['bounded_coding_recovery']['strategy'] == 'bounded_steps'
    assert (await env.store.read('attempt', 'bad-attempt'))['runtime_failure_code'] == stored_code


@pytest.mark.parametrize('ordinal', [6, 100])
@pytest.mark.parametrize('step', ['implementation', 'unit_test_implementation', 'integration_test_implementation'])
async def test_later_bounded_rounds_issue_valid_recovery_without_resetting_history_or_budget(bounded, ordinal, step):
    env = bounded
    env.settings = env.settings.model_copy(update={'auto_failure_retry_limit': 100, 'auto_failure_run_limit': 100})
    env.workflow.settings = env.settings
    work = await patch(env, 'work_item', 'bad', step=step)
    await patch(env, 'dispatch_context', work['attempt_id'], task={**env.frozen_task, 'step': step})
    def seed_history(tx):
        for index in range(1, ordinal):
            tx.put('failure_analysis', f'historical-bounded-{index}', {
                'run_id': 'run', 'work_item_id': 'bad', 'attempt_id': f'historical-attempt-{index}',
                'status': 'repair_scheduled', 'failure_code': 'reasoning_output_limit',
                'bounded_coding_recovery': {'round': index}})
        return {}
    await env.store.command('fixture', 'prior-bounded-history', {}, seed_history)
    history = await env.store.list('failure_analysis')
    accounts = await env.store.list('budget_account')
    profiles = await env.store.list('model_profile')
    result = await env.automatic.repair('bad')
    assert result['status'] == 'repair_scheduled', result
    current = await env.store.read('work_item', 'bad')
    plan = await resolve_bounded_coding_recovery(env.store, await env.store.read('run', 'run'), current)
    assert plan['round'] == ordinal
    assert plan['max_actions_per_step'] == 1 and plan['max_changed_lines_per_step'] == 20
    assert plan['step_reduction_factor'] == 0.03125 and plan['observed_output_cap'] == 64
    assert current['quality_result'] != 'passed'
    assert [await env.store.read('failure_analysis', row['id']) for row in history] == history
    assert await env.store.list('budget_account') == accounts
    assert await env.store.list('model_profile') == profiles
    if ordinal == 100:
        await fail_next_bounded_step(env)
        before = {kind: await env.store.list(kind) for kind in ('work_item', 'budget_account', 'model_invocation', 'run_recovery')}
        stopped = await env.automatic.repair('bad')
        assert stopped['status'] == 'blocked'
        assert 'automatic_repair_limit' in {row['code'] for row in stopped['blockers']}
        assert {kind: await env.store.list(kind) for kind in before} == before


async def test_default_retry_allowance_continues_six_real_no_progress_rounds(bounded):
    env = bounded
    env.settings = type(env.settings).model_validate(env.settings.model_dump(exclude={'auto_failure_retry_limit'}))
    env.workflow.settings = env.settings
    profiles = await env.store.list('model_profile')
    for ordinal in range(1, 7):
        budgets = await env.store.list('budget_account')
        result = await env.automatic.repair('bad')
        assert result['status'] == 'repair_scheduled', result
        work = await env.store.read('work_item', 'bad')
        plan = await resolve_bounded_coding_recovery(env.store, await env.store.read('run', 'run'), work)
        assert plan['round'] == ordinal and plan['no_progress_streak'] == ordinal
        assert work['quality_result'] != 'passed' and work['approval_required']
        assert await env.store.list('budget_account') == budgets
        if ordinal < 6:
            await fail_next_bounded_step(env)
    assert plan['step_reduction_factor'] == 0.03125
    assert len(await env.store.list('run_recovery')) == 6
    assert await env.store.list('model_profile') == profiles

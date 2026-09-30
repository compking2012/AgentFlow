"""Automatic failure analysis and bounded recovery without providers or real Runs."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from test_parallel_remediation import parallel_env as parallel_env
from test_recovery import (
    assert_system_model_inheritance,
    model_recovery_fixture,
    patch,
    stopped_workspace,
)
from test_recovery import env as env

from agentflow.common import DomainError
from agentflow.control.failure_remediation import FailureRemediation
from agentflow.control.recovery import validate_recovery_checkpoint
from agentflow.models.budget import account_id
from agentflow.runtime.trace import ExecutionTrace


@pytest_asyncio.fixture
async def automatic(env):
    env.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0})
    env.workflow.settings = env.settings
    await patch(env, 'run', 'run', execution_state='running')
    await patch(env, 'attempt', 'bad-attempt', runtime_failure_code='invalid_model_output')
    env.automatic = FailureRemediation(env.store, env.workflow, recovery=env.service)
    return env


async def unchanged_state(env):
    return {kind: await env.store.list(kind) for kind in (
        'run', 'work_item', 'attempt', 'artifact', 'approval', 'code_snapshot', 'check',
        'review', 'budget_account', 'model_invocation', 'run_recovery', 'review_repair')}


async def test_stopped_internal_worker_failure_gets_a_fresh_bounded_retry(automatic):
    env = automatic
    await stopped_workspace(env)
    await patch(env, 'attempt', 'bad-attempt', runtime_failure_code='worker_internal_error')
    await patch(env, 'work_item', 'bad', runtime_failure_code='worker_internal_error')
    budgets = await env.store.list('budget_account')
    result = await env.automatic.repair('bad')
    assert result['status'] == 'repair_scheduled'
    assert result['failure_code'] == 'worker_internal_error' and result['action'] == 'retry_current'
    assert (await env.store.read('work_item', 'bad'))['generation'] == 2
    assert await env.store.list('budget_account') == budgets


@pytest.mark.parametrize(('kind', 'identity', 'changes'), [
    ('work_item', 'bad', {'status': 'pending', 'generation': 2, 'attempt_id': None}),
    ('attempt', 'bad-attempt', {'status': 'completed'}),
    ('run', 'run', {'execution_state': 'paused'}),
])
async def test_analysis_state_change_is_a_replayable_noop(automatic, monkeypatch, kind, identity, changes):
    import agentflow.control.failure_remediation as module

    value = automatic
    original_code = value.automatic._code
    changed = {}

    async def change_during_analysis(work, attempt):
        result = await original_code(work, attempt)
        await patch(value, kind, identity, **changes)
        changed.update(await unchanged_state(value))
        return result

    monkeypatch.setattr(value.automatic, '_code', change_during_analysis)
    monkeypatch.setattr(module, 'uuid4', lambda: 'stale-analysis')
    assert await value.automatic.analyze('bad') is None
    assert await value.store.list('failure_analysis') == []
    assert await unchanged_state(value) == changed
    assert not [row for row in await value.store.events(0) if row['type'].startswith('failure.')]
    assert not any(row['title'] == '失败原因分析'
                   for row in (await ExecutionTrace(value.store).page('bad-attempt'))['items'])

    def must_replay(_tx):
        raise AssertionError('A completed no-op command must replay without reapplying its handler')

    assert await value.store.command('failure.analyze', 'stale-analysis', {'work_item_id': 'bad'}, must_replay) == {
        'analyzed': False, 'reason': 'state_changed'}


async def test_reconcile_continues_other_work_after_analysis_becomes_stale(automatic, monkeypatch):
    value = automatic
    bad = await value.store.read('work_item', 'bad')
    fields = {key: field for key, field in bad.items() if key not in {'id', 'revision'}}
    await patch(value, 'work_item', 'other', **{**fields, 'key': 'other', 'attempt_id': 'other-attempt',
        'runtime_failure_code': 'model_authentication_failed'})
    attempt = await value.store.read('attempt', 'bad-attempt')
    await patch(value, 'attempt', 'other-attempt', **{key: field for key, field in attempt.items()
        if key not in {'id', 'revision', 'work_item_id'}}, work_item_id='other')
    other = await value.store.read('work_item', 'other')
    original_code = value.automatic._code

    async def change_during_analysis(work, attempt):
        result = await original_code(work, attempt)
        if work['id'] == 'bad':
            await patch(value, 'work_item', 'bad', status='pending', generation=2, attempt_id=None)
        return result

    monkeypatch.setattr(value.automatic, '_code', change_during_analysis)
    await value.automatic.reconcile()
    analyses = await value.store.list('failure_analysis')
    assert len(analyses) == 1 and analyses[0]['work_item_id'] == 'other'
    assert analyses[0]['failure_code'] == 'model_authentication_failed'
    assert await value.store.read('work_item', 'other') == other
    assert await value.store.list('run_recovery') == []


async def test_analysis_precedes_audited_repair_and_preserves_upstream_budget_and_approval(automatic):
    env = automatic
    upstream = await env.store.read('work_item', 'upstream')
    accounts = await env.store.list('budget_account')
    before = await unchanged_state(env)
    analysis = await env.automatic.analyze('bad')
    assert analysis['phase'] == 'analysis' and analysis['status'] == 'ready'
    assert analysis['failure_code'] == 'invalid_model_output' and analysis['action'] == 'retry_current'
    assert analysis['upstream_work_item_ids'] == ['upstream']
    assert analysis['repair_work_item_ids'] == ['bad']
    assert await unchanged_state(env) == before
    results = await asyncio.gather(*(env.automatic.repair('bad') for _ in range(3)))
    assert any(row and row['status'] == 'repair_scheduled' for row in results)
    records = await env.store.list('failure_analysis')
    assert len(records) == 1 and records[0]['phase'] == 'repair'
    receipts = await env.store.list('run_recovery')
    assert len(receipts) == 1 and receipts[0]['actor'] == 'system'
    assert receipts[0]['failure_analysis_id'] == analysis['id']
    assert receipts[0]['execution'] == 'fresh_attempt'
    assert await env.store.read('work_item', 'upstream') == upstream
    assert await env.store.list('budget_account') == accounts
    current = await env.store.read('work_item', 'bad')
    assert current['generation'] == 2 and current['approval_required']
    assert '既定结果格式' in current['payload']['recovery_instruction']
    assert not await env.store.list('model_invocation')
    events = await env.store.events(0)
    types = [row['type'] for row in events]
    assert types.index('failure.analysis_completed') < types.index('failure.repair_scheduled')
    trace = await ExecutionTrace(env.store).page('bad-attempt')
    assert [row['title'] for row in trace['items']] == ['失败原因分析', '自动修复已安排']
    assert all('原因：' in row['content'] and '重做指令：' in row['content'] for row in trace['items'])
    assert all('authorization_digest' not in row['content'] for row in trace['items'])


@pytest.mark.parametrize('step', ['implementation', 'unit_test_implementation', 'integration_test_implementation'])
async def test_coding_failure_preserves_partial_code_and_exact_checkpoint(automatic, step):
    env = automatic
    path, task, _, _ = await stopped_workspace(env)
    await patch(env, 'work_item', 'bad', step=step)
    await patch(env, 'dispatch_context', 'bad-attempt', task={**task, 'step': step})
    await patch(env, 'attempt', 'bad-attempt', runtime_failure_code='final_schema_invalid')
    repository = env.service.repository
    before = [await asyncio.to_thread(repository._run, path, args) for args in
              [['rev-parse', 'HEAD'], ['ls-files', '--stage'], ['status', '--porcelain']]]
    result = await env.automatic.repair('bad')
    assert result['status'] == 'repair_scheduled', result
    after = [await asyncio.to_thread(repository._run, path, args) for args in
             [['rev-parse', 'HEAD'], ['ls-files', '--stage'], ['status', '--porcelain']]]
    assert after == before
    work = await env.store.read('work_item', 'bad')
    checkpoint = await env.store.read('code_snapshot', work['payload']['recovery_checkpoint_id'])
    await validate_recovery_checkpoint(env.store, await env.store.read('run', 'run'), work, checkpoint, repository)
    fresh = env.tmp_path / ('recovered-' + step)
    await repository.clone_snapshot(path, fresh, checkpoint['commit_oid'])
    assert (fresh / 'src/keep.js').read_text() == 'export const valuable = 42;\n'
    assert work['write_paths'] == ['src'] and work['approval_required']


async def test_failed_parallel_child_preserves_completed_sibling(automatic):
    env = automatic
    bad = await patch(env, 'work_item', 'bad', kind='stage_child', parent_stage_id='stage')
    body = {key: value for key, value in bad.items() if key not in {'id', 'revision'}}
    await patch(env, 'work_item', 'good', **{**body, 'key': 'good', 'status': 'completed',
        'quality_result': 'passed', 'attempt_id': None})
    await patch(env, 'work_item', 'stage', **{**body, 'key': 'stage', 'kind': 'aggregation',
        'parent_stage_id': None, 'dependencies': ['bad', 'good'], 'status': 'pending', 'attempt_id': None,
        'original_dependencies': ['upstream'], 'original_write_paths': [], 'expanded_child_ids': ['bad', 'good']})
    await patch(env, 'work_item', 'after', dependencies=['stage'])
    sibling = await env.store.read('work_item', 'good')
    result = await env.automatic.repair('bad')
    assert result['status'] == 'repair_scheduled'
    assert set(result['affected_work_item_ids']) == {'bad', 'stage', 'after', 'unit'}
    assert await env.store.read('work_item', 'good') == sibling
    assert (await env.store.read('work_item', 'stage'))['kind'] == 'aggregation'


@pytest.mark.parametrize('corrupt', [False, True])
async def test_partial_code_recovery_keeps_only_verified_source_ancestry(automatic, corrupt):
    env = automatic
    path, task, _, _ = await stopped_workspace(env)
    base = task['source_commit']
    await patch(env, 'code_snapshot', 'upstream-source', run_id='run', work_item_id='upstream', generation=1,
        repository_path=str(path), commit_oid=base, tree_oid=env.service.repository._integrity(path, base),
        base_oid=base, parent_commit_oids=['f' * 40] if corrupt else [base], stale=False)
    result = await env.automatic.repair('bad')
    if corrupt:
        assert result['status'] == 'blocked'
        assert (await env.store.read('work_item', 'bad'))['generation'] == 1
        assert not await env.store.list('run_recovery')
    else:
        assert result['status'] == 'repair_scheduled'
        work = await env.store.read('work_item', 'bad')
        checkpoint = await env.store.read('code_snapshot', work['payload']['recovery_checkpoint_id'])
        assert base in checkpoint['parent_commit_oids']
    assert (path / 'src/keep.js').read_text() == 'export const valuable = 42;\n'


@pytest.mark.parametrize('code', ['model_authentication_failed', 'model_credentials_missing',
    'reasoning_output_limit', 'source_snapshot_missing', 'write_scope_violation',
    'execution_unconfirmed', 'execution_receipt_missing', 'terminal_event_missing',
    'response_usage_incompatible', 'model_request_limit_reached'])
async def test_unsafe_or_non_self_healing_failures_are_analyzed_without_retry(automatic, code):
    env = automatic
    await patch(env, 'attempt', 'bad-attempt', runtime_failure_code=code)
    before = await unchanged_state(env)
    analysis = await env.automatic.repair('bad')
    assert analysis['status'] == 'blocked' and analysis['action'] == 'needs_attention'
    assert analysis['failure_code'] == code and analysis['summary']
    assert await unchanged_state(env) == before


@pytest.mark.parametrize('kind,identity,fields', [
    ('attempt', 'bad-attempt', {'status': 'execution_unknown'}),
    ('attempt', 'bad-attempt', {'work_item_id': 'upstream'}),
    ('work_item', 'after', {'status': 'waiting_approval'}),
    ('work_item', 'after', {'status': 'running'}),
    ('run', 'run', {'restore_reconciliation_required': True}),
    ('budget_account', account_id('run', 'run'), {'request_count': 10}),
    ('budget_account', account_id('iteration', 'iteration'), {'restore_uncertain': True}),
    ('model_invocation', 'uncertain', {'run_id': 'run', 'iteration_id': 'iteration', 'state': 'uncertain'}),
    ('delivery_intent', 'publishing', {'run_id': 'run', 'status': 'prepared'}),
])
async def test_recovery_guards_refuse_mutation_after_analysis(automatic, kind, identity, fields):
    env = automatic
    await patch(env, kind, identity, **fields)
    before = await unchanged_state(env)
    analysis = await env.automatic.repair('bad')
    assert analysis['status'] == 'blocked'
    assert await unchanged_state(env) == before


async def fail_next_generation(env):
    claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
    assert claim['work_item']['id'] == 'bad'
    attempt = claim['attempt']
    await env.workflow.finish_attempt(attempt['id'], {'fencing_token': attempt['fencing_token'],
        'input_fingerprint': attempt['input_fingerprint'], 'execution_status': 'failed',
        'quality_result': 'unknown', 'runtime_failure_code': 'invalid_model_output'}, str(uuid4()), verified_artifacts=[])


@pytest.mark.parametrize('per_work,run_limit', [(2, 20), (5, 2), (0, 20), (2, 0)])
async def test_retries_stop_at_task_and_run_limits_without_resetting_budget(automatic, per_work, run_limit):
    env = automatic
    env.settings = env.settings.model_copy(update={'auto_failure_retry_limit': per_work, 'auto_failure_run_limit': run_limit})
    env.workflow.settings = env.settings
    accounts = await env.store.list('budget_account')
    allowed = min(per_work, run_limit)
    for _ in range(allowed):
        assert (await env.automatic.repair('bad'))['status'] == 'repair_scheduled'
        await fail_next_generation(env)
    before = await unchanged_state(env)
    analysis = await env.automatic.repair('bad')
    assert analysis['status'] == 'blocked'
    assert 'automatic_repair_limit' in {row['code'] for row in analysis['blockers']}
    assert await unchanged_state(env) == before
    assert await env.store.list('budget_account') == accounts


async def test_backoff_is_durable_and_wakes_without_an_external_event(automatic, monkeypatch):
    import agentflow.control.failure_remediation as module
    env = automatic
    current = [1000.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: current[0]))
    env.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 30})
    env.workflow.settings = env.settings
    await env.automatic.reconcile()
    analysis = (await env.store.list('failure_analysis'))[0]
    assert analysis['status'] == 'blocked' and analysis['not_before'] == 1030
    await env.automatic.reconcile()
    assert await env.store.list('failure_analysis') == [analysis]
    current[0] = 1031.0
    await env.automatic.reconcile()
    assert (await env.store.read('failure_analysis', analysis['id']))['status'] == 'repair_scheduled'


async def test_stale_analysis_cannot_recover_another_attempt_or_changed_budget(automatic):
    env = automatic
    analysis = await env.automatic.analyze('bad')
    await patch(env, 'budget_account', account_id('run', 'run'), request_count=1)
    before = await unchanged_state(env)
    with pytest.raises(DomainError, match='分析之后'):
        await env.service.recover_automatic('run', analysis['id'])
    assert await unchanged_state(env) == before
    await patch(env, 'work_item', 'bad', generation=2)
    with pytest.raises(DomainError):
        await env.service.recover_automatic('run', analysis['id'])


async def test_corrupt_checkpoint_is_retained_and_never_scheduled(automatic):
    env = automatic
    path, _, directory, _ = await stopped_workspace(env)
    (directory / 'result.json').write_text('{}')
    before = await unchanged_state(env)
    analysis = await env.automatic.repair('bad')
    assert analysis['status'] == 'blocked'
    assert 'recovery_evidence_invalid' in {row['code'] for row in analysis['blockers']}
    assert (path / 'src/keep.js').read_text() == 'export const valuable = 42;\n'
    assert await unchanged_state(env) == before


async def test_existing_owner_model_choice_is_inherited_without_switching_defaults(automatic):
    env = automatic
    first = await env.service.recover('run', await model_recovery_fixture(env), 'owner-model-choice')
    await patch(env, 'product_model_binding', 'default', coding_model_profile_id='later-coding')
    await fail_next_generation(env)
    old = await env.store.read('work_item', 'bad')
    result = await env.automatic.repair('bad')
    assert result['status'] == 'repair_scheduled'
    _, inherited = await assert_system_model_inheritance(env, old, 3)
    assert inherited['source_recovery_id'] == first['id']
    receipts = await env.store.list('run_recovery')
    automatic_receipts = [row for row in receipts if row.get('failure_analysis_id')]
    assert len(automatic_receipts) == 1 and automatic_receipts[0]['actor'] == 'system'


@pytest.mark.parametrize('ready', [False, True])
async def test_source_precondition_failure_requires_successful_source_preflight(automatic, ready):
    env = automatic
    await patch(env, 'attempt', 'bad-attempt', runtime_failure_code=None, status='blocked')
    await patch(env, 'work_item', 'bad', status='blocked',
                blocking_reason='Parallel code branches require reviewed candidate assembly')
    async def preflight(run, work):
        assert run['id'] == 'run' and work['id'] == 'bad'
        return ready
    env.automatic.preflight = preflight
    result = await env.automatic.repair('bad')
    assert (result['status'] == 'repair_scheduled') is ready


async def test_test_assertion_failure_requires_attribution_instead_of_blind_retry(automatic):
    env = automatic
    await patch(env, 'work_item', 'bad', step='unit_test_execution', status='completed', quality_result='failed')
    await patch(env, 'attempt', 'bad-attempt', status='completed', runtime_failure_code=None)
    before = await unchanged_state(env)
    analysis = await env.automatic.repair('bad')
    assert analysis['action'] == 'needs_attention' and analysis['failure_code'] == 'test_failed'
    assert '归属' in analysis['blockers'][0]['message']
    assert await unchanged_state(env) == before


async def test_failed_review_runs_existing_parallel_owner_repair_after_analysis(parallel_env):
    env = parallel_env
    env.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0})
    env.workflow.settings = env.settings
    for kind, owner in [('run', 'run'), ('iteration', 'iteration')]:
        account = await env.store.read('budget_account', account_id(kind, owner))
        await patch(env, kind, owner, budget_limit={'currency': account['currency'],
            'limit_micros': account['limit_micros'], 'max_model_requests': account['max_requests']})
    for work in await env.store.list('work_item'):
        if work['id'] == 'review-work' or not work.get('attempt_id'):
            continue
        await patch(env, 'attempt', work['attempt_id'], run_id='run', work_item_id=work['id'],
            iteration_id='iteration', status='completed', generation=work['generation'],
            fencing_token=work['fencing_token'], input_fingerprint=work['input_fingerprint'])
    sibling = await env.store.read('work_item', 'module-b')
    controller = FailureRemediation(env.store, env.workflow, review=env.remediation)
    result = await controller.repair('review-work')
    assert result['status'] == 'repair_scheduled', result
    assert result['repair_receipt_kind'] == 'review_repair'
    assert result['repair_work_item_ids'] == ['module-a']
    assert 'module-b' in result['preserved_work_item_ids'] and 'module-a' not in result['preserved_work_item_ids']
    assert await env.store.read('work_item', 'module-b') == sibling
    receipt = (await env.store.list('review_repair'))[0]
    assert receipt['base_commit'] == env.aggregate['commit_oid']
    trace = await ExecutionTrace(env.store).page('review-attempt-1')
    assert [row['title'] for row in trace['items']] == ['失败原因分析', '自动修复已安排']


async def test_review_repairs_do_not_consume_execution_failure_allowance(automatic):
    env = automatic
    env.workflow.settings = env.settings.model_copy(update={'auto_failure_retry_limit': 2, 'auto_failure_run_limit': 2})
    for number in range(6):
        await patch(env, 'failure_analysis', f'review-history-{number}', run_id='run', work_item_id='bad',
                    attempt_id=f'old-review-{number}', failure_code='review_failed', action='repair_review_findings',
                    status='repair_scheduled')
    for _ in range(2):
        result = await env.automatic.repair('bad')
        assert result['status'] == 'repair_scheduled', result
        await fail_next_generation(env)
    result = await env.automatic.repair('bad')
    assert result['status'] == 'blocked'
    assert 'automatic_repair_limit' in {row['code'] for row in result['blockers']}
    assert len(await env.store.list('run_recovery')) == 2
    assert len([row for row in await env.store.list('failure_analysis') if row['failure_code'] == 'review_failed']) == 6


async def test_exact_historical_tool_transcript_failure_uses_bounded_fresh_role_retry(automatic):
    import json

    from agentflow.common import canonical_digest
    env = automatic
    env.workflow.settings = env.settings.model_copy(update={'auto_failure_retry_limit': 2})
    await patch(env, 'work_item', 'bad', step='code_review', role='review')
    await patch(env, 'attempt', 'bad-attempt', runtime_failure_code='model_request_failed')
    directory = env.settings.data_dir / 'attempt_artifacts' / canonical_digest('bad-attempt').split(':')[1]
    directory.mkdir(parents=True, mode=0o700)
    error = directory / 'role_error.json'
    error.write_text(json.dumps({'type': 'ConversationRunError', 'message':
        "Conversation run failed: litellm.BadRequestError: An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id' (insufficient tool messages following tool_calls)."}))
    error.chmod(0o600)
    budgets = await env.store.list('budget_account')
    for _ in range(2):
        result = await env.automatic.repair('bad')
        assert result['status'] == 'repair_scheduled', result
        assert result['failure_code'] == 'role_tool_transcript_invalid'
        claim = await env.workflow.claim_next('run', 'fixture', str(uuid4()))
        fresh = claim['attempt']
        assert fresh['id'] != 'bad-attempt' and claim['work_item']['step'] == 'code_review'
        await env.workflow.finish_attempt(fresh['id'], {'fencing_token': fresh['fencing_token'],
            'input_fingerprint': fresh['input_fingerprint'], 'execution_status': 'failed',
            'quality_result': 'unknown', 'runtime_failure_code': 'role_tool_transcript_invalid'}, str(uuid4()), verified_artifacts=[])
    blocked = await env.automatic.repair('bad')
    assert blocked['status'] == 'blocked' and 'automatic_repair_limit' in {row['code'] for row in blocked['blockers']}
    assert await env.store.list('budget_account') == budgets
    assert len(await env.store.list('run_recovery')) == 2


async def test_unrelated_provider_bad_request_does_not_become_automatic_retry(automatic):
    env = automatic
    await patch(env, 'attempt', 'bad-attempt', runtime_failure_code='model_request_failed')
    result = await env.automatic.repair('bad')
    assert result['status'] == 'blocked' and result['action'] == 'needs_attention'
    assert not await env.store.list('run_recovery')


async def test_tool_transcript_retry_is_not_available_to_delivery_system_work(automatic):
    env = automatic
    await patch(env, 'work_item', 'bad', step='delivery', role='system')
    await patch(env, 'attempt', 'bad-attempt', runtime_failure_code='role_tool_transcript_invalid')
    result = await env.automatic.repair('bad')
    assert result['status'] == 'blocked' and result['action'] == 'needs_attention'
    assert not await env.store.list('run_recovery')


async def test_authorization_expiry_does_not_receive_blind_worker_retry_or_extra_budget(automatic):
    env = automatic
    await patch(env, 'attempt', 'bad-attempt', runtime_failure_code='task_authorization_expired')
    before = await unchanged_state(env)
    result = await env.automatic.repair('bad')
    assert result['failure_code'] == 'task_authorization_expired'
    assert result['status'] == 'blocked' and result['action'] == 'needs_attention'
    assert await unchanged_state(env) == before
    assert not await env.store.list('timeout_recovery')


async def test_late_expiry_evidence_invalidates_an_already_ready_worker_retry(automatic):
    from agentflow.common import canonical_digest
    from agentflow.models.profiles import AttemptContext
    from agentflow.runtime.task_authorization import record_expiry
    env = automatic
    fingerprint = canonical_digest('deadline-race-frozen-task')
    await patch(env, 'work_item', 'bad', input_fingerprint=fingerprint)
    await patch(env, 'attempt', 'bad-attempt', runtime_failure_code='worker_exited', input_fingerprint=fingerprint)
    context = AttemptContext(attempt_id='bad-attempt', run_id='run', iteration_id='iteration',
        model_profile_id='fixture', fencing_token=1, input_fingerprint=fingerprint,
        expires_at='2000-01-01T00:00:00+00:00', max_model_requests=10, max_output_tokens=64)
    raw = await patch(env, 'task_authorization', 'expiry-authority', **context.model_dump())
    analysis = await env.automatic.analyze('bad')
    assert analysis['status'] == 'ready' and analysis['failure_code'] == 'worker_exited'
    before = await env.store.list('work_item')
    budgets = await env.store.list('budget_account')
    await record_expiry(env.store, raw, context, await env.store.read('attempt', 'bad-attempt'),
                        await env.store.read('work_item', 'bad'), 'responses')
    with pytest.raises(DomainError) as stale:
        await env.service.recover_automatic('run', analysis['id'])
    assert stale.value.code == 'automatic_repair_stale'
    assert await env.store.list('work_item') == before
    blocked = await env.automatic.repair('bad')
    assert blocked['failure_code'] == 'task_authorization_expired' and blocked['status'] == 'blocked'
    assert not await env.store.list('run_recovery') and not await env.store.list('timeout_recovery')
    assert await env.store.list('budget_account') == budgets


@pytest.mark.parametrize('kind,expected', [
    ('connect_error', 'model_transport_not_sent'),
    ('read_timeout', 'model_transport_read_timeout'),
    (None, 'model_request_outcome_unknown'),
])
async def test_transport_diagnostic_blocks_automatic_retry_without_ack_or_budget_change(automatic, kind, expected):
    env = automatic
    attempt = await patch(env, 'attempt', 'bad-attempt', runtime_failure_code='worker_exited')
    identity = {'attempt_id': attempt['id'], **{field: attempt[field] for field in
                ('run_id', 'iteration_id', 'fencing_token', 'input_fingerprint')}}
    await patch(env, 'dispatch_context', attempt['id'], task={**identity, 'work_item_id': 'bad',
        'profile_id': 'fixture-profile', 'step': 'research'})
    await patch(env, 'task_authorization', 'fixture-authority', **identity,
        model_profile_id='fixture-profile', expected_profile_revision=1, protocols=['chat_completions'])
    await patch(env, 'model_attempt_budget', attempt['id'], attempt_id=attempt['id'], request_count=1,
                uncertain_invocations=0 if kind == 'connect_error' else 1)
    metadata = {'transport_failure': {'version': 1, 'origin': 'model_proxy', 'invocation_id': 'fixture-invocation',
        'phase': 'send', 'kind': kind, 'delivery': 'not_sent' if kind == 'connect_error' else 'unknown',
        'observed_at': '2026-01-01T00:00:01+00:00'}} if kind else {}
    await patch(env, 'model_invocation', 'fixture-invocation', **identity,
        operation_id='fixture-invocation', profile_id='fixture-profile', profile_revision=1,
        protocol='chat_completions', request_ordinal=1, created_at='2026-01-01T00:00:00+00:00',
        state='released' if kind == 'connect_error' else 'uncertain',
        reason='connection_not_established' if kind == 'connect_error' else 'dispatch_result_unknown',
        amount_micros=0, cost_mode='request_limited', **metadata)
    before = await unchanged_state(env)
    result = await env.automatic.repair('bad')
    assert result['failure_code'] == expected and result['status'] == 'blocked'
    assert await unchanged_state(env) == before
    assert not await env.store.list('model_uncertainty_acknowledgment')
    assert not await env.store.list('work_execution_budget_adjustment')

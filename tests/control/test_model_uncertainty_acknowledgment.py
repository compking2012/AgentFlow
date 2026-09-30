"""Exact owner acknowledgments use isolated stopped-process and ledger fixtures."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError
from test_recovery import env as env
from test_recovery import patch, stopped_workspace

from agentflow.common import DomainError, canonical_digest
from agentflow.control.api import create_app
from agentflow.control.coding_steps import CodingSteps
from agentflow.control.failure_remediation import FailureRemediation, guard_automatic
from agentflow.control.model_uncertainty import ModelUncertaintyService
from agentflow.control.request_limits import RequestLimitService
from agentflow.control.work_execution_budget import WorkExecutionBudgetService
from agentflow.models.budget import account_id
from agentflow.models.profiles import AttemptContext
from agentflow.models.uncertainty import ACK_KIND

PROTECTED = ('run', 'iteration', 'work_item', 'attempt', 'supervised_attempt', 'dispatch_context',
             'model_invocation', 'model_attempt_budget', 'budget_account', 'coding_work_budget',
             'coding_step_control', 'coding_step_usage', 'task_authorization', 'approval')


async def snapshot(env):
    return {kind: await env.store.list(kind) for kind in PROTECTED}


@pytest_asyncio.fixture
async def unknown(env, request):
    count = getattr(request, 'param', 1)
    run = await env.store.read('run', 'run')
    run = await patch(env, 'run', 'run', budget_limit={**run['budget_limit'],
                      'max_tool_calls': 100, 'max_active_seconds': 1800})
    fingerprint = canonical_digest({'fixture': 'stopped-model-uncertainty'})
    work = await patch(env, 'work_item', 'bad', step='implementation', key='implementation',
                       role='development', status='running', write_paths=['src'], input_fingerprint=fingerprint)
    attempt = await patch(env, 'attempt', 'bad-attempt', status='running', input_fingerprint=fingerprint)
    coding = CodingSteps(env.store, env.settings, env.service.repository)
    control = await coding.prepare(run, work, attempt, env.project['base_commit'], 65536)
    task = {'run_id': 'run', 'work_item_id': 'bad', 'attempt_id': 'bad-attempt',
            'generation': work['generation'], 'fencing_token': work['fencing_token'],
            'input_fingerprint': fingerprint, 'coding_step': control}
    await coding.account(task, {'active_seconds': 709, 'observed_tool_calls': 120,
                               'tool_observation_complete': True})
    path, task, directory, identity = await stopped_workspace(env)
    task = {**task, 'coding_step': control, 'profile_id': 'fixture-profile', 'cost_mode': 'request_limited'}
    await patch(env, 'dispatch_context', 'bad-attempt', task=task)
    await patch(env, 'task_authorization', 'fixture-authority', run_id='run', work_item_id='bad',
                attempt_id='bad-attempt', fencing_token=1, input_fingerprint=fingerprint,
                scopes=['model:invoke'], max_model_requests=10)
    await patch(env, 'work_item', 'bad', runtime_failure_code='worker_timeout')
    await patch(env, 'attempt', 'bad-attempt', runtime_failure_code='worker_timeout')
    context = AttemptContext(attempt_id='bad-attempt', run_id='run', iteration_id='iteration',
        model_profile_id='fixture-profile', fencing_token=1, input_fingerprint=fingerprint,
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        max_model_requests=10, max_output_tokens=65536, cost_mode='request_limited')
    invocations = []
    # Reserve every concurrent request before any becomes uncertain. Never reset a counter.
    for ordinal in range(count):
        invocation = await env.ledger.reserve(context, protocol='responses',
            request_fingerprint=canonical_digest({'fixture_call': ordinal}), profile_revision=1,
            amount_micros=0, currency='USD', idempotency_key=f'fixture-call-{ordinal}')
        await env.ledger.dispatch(invocation['id'])
        invocations.append(invocation)
    for invocation in invocations:
        await env.ledger.uncertain(invocation['id'], 'consumer_disconnected')
    env.models = SimpleNamespace(_active={}, store=env.store)
    env.acknowledgments = ModelUncertaintyService(env.store, env.workflow, env.models)
    env.execution_budget = WorkExecutionBudgetService(env.store, env.workflow)
    env.invocation_ids = [row['id'] for row in invocations]
    env.invocation_id = env.invocation_ids[0]
    env.context, env.budget_id = context, control['budget_id']
    env.workspace, env.process_directory, env.process_identity = path, directory, identity
    return env


async def item(env, invocation_id=None):
    view = await env.acknowledgments.view('run')
    return next(row for row in view['items'] if row['invocation_id'] == (invocation_id or env.invocation_id))


def acknowledgment(row, **changes):
    revisions = ('run_revision', 'work_revision', 'attempt_revision', 'invocation_revision', 'attempt_budget_revision')
    return {**{'expected_' + field: row[field] for field in revisions},
        'expected_state_digest': row['expected_state_digest'], 'accept_unknown_usage': True,
        'reason': 'Owner accepts the unknown usage of this exact stopped request', **changes}


async def accept(env, invocation_id=None, key='owner-ack'):
    identity = invocation_id or env.invocation_id
    return await env.acknowledgments.acknowledge('run', identity, acknowledgment(await item(env, identity)), key)


async def test_acknowledgment_preserves_original_uncertainty_counters_and_authorizations(unknown):
    env = unknown
    row = await item(env)
    assert row['eligible'] and row['request_counted'] and not row['acknowledged'], row
    assert (env.process_directory / 'result.json').is_file()
    assert (await env.store.read('supervised_attempt', 'bad-attempt'))['state'] == 'completed'
    before = await snapshot(env)
    result = await accept(env)
    assert result['acknowledged'] and result['requires_separate_retry']
    assert await snapshot(env) == before
    assert (await item(env))['acknowledged']
    audits = await env.store.list(ACK_KIND)
    assert len(audits) == 1 and audits[0]['actor'] == 'owner'
    assert audits[0]['invocation_id'] == env.invocation_id and audits[0]['requires_explicit_retry']
    invocation = await env.store.read('model_invocation', env.invocation_id)
    assert invocation['state'] == 'uncertain' and invocation['usage'] is None and invocation['actual_micros'] is None
    attempt_budget = await env.store.read('model_attempt_budget', 'bad-attempt')
    assert attempt_budget['request_count'] == attempt_budget['uncertain_invocations'] == 1
    assert not await env.store.list('run_recovery') and not await env.store.list('work_execution_budget_adjustment')


async def test_acknowledgment_allows_separate_budget_grant_and_explicit_recovery(unknown):
    env = unknown
    assert not (await env.execution_budget.view('run', 'bad'))['can_extend']
    await accept(env)
    view = await env.execution_budget.view('run', 'bad')
    assert view['can_extend'], view
    option = next(row for row in (await env.service.options('run'))['retry_options'] if row['work_item_id'] == 'bad')
    assert not option['eligible']  # The original tool allowance is still exhausted.
    await env.execution_budget.extend('run', 'bad', {
        'expected_run_revision': view['run_revision'], 'expected_work_revision': view['work_revision'],
        'expected_budget_revision': view['budget_revision'], 'additional_tool_calls': 400,
        'reason': 'Owner separately adds the tools needed to finish'}, 'grant-after-ack')
    option = next(row for row in (await env.service.options('run'))['retry_options'] if row['work_item_id'] == 'bad')
    assert option['eligible'], option
    before_calls = await env.store.list('model_invocation')
    run = await env.store.read('run', 'run')
    await env.service.recover('run', {'expected_revision': run['revision'], 'mode': 'retry', 'work_item_id': 'bad'}, 'retry-after-ack')
    assert (await env.store.read('work_item', 'bad'))['generation'] == 2
    assert await env.store.list('model_invocation') == before_calls
    assert (await item(env))['acknowledged'], 'Recovery must not invalidate the precise historical acknowledgment'
    assert not any(row['code'] == 'recovery_budget_uncertain'
                   for row in env.service._common_blockers(await env.service._read('run')))


async def test_acknowledgment_never_reauthorizes_the_same_attempt_but_request_grants_remain_possible(unknown):
    env = unknown
    await accept(env)
    before = await env.store.list('model_invocation')
    with pytest.raises(DomainError) as error:
        await env.ledger.reserve(env.context, protocol='responses', request_fingerprint='new-request',
            profile_revision=1, amount_micros=0, currency='USD', idempotency_key='forbidden-same-attempt')
    assert error.value.code == 'execution_uncertain'
    run = await env.store.read('run', 'run')
    await RequestLimitService(env.store).extend('run', {'expected_revision': run['revision'],
        'max_model_requests': 20, 'reason': 'Independent owner request grant'}, 'future-request-gate')
    assert await env.store.list('model_invocation') == before
    assert (await env.store.read('model_attempt_budget', 'bad-attempt'))['uncertain_invocations'] == 1


@pytest.mark.parametrize('scope', ['run', 'invocation'])
async def test_strict_cost_mode_cannot_be_accepted_as_unknown_usage(unknown, scope):
    env = unknown
    if scope == 'run':
        run = await env.store.read('run', 'run')
        await patch(env, 'run', 'run', budget_limit={**run['budget_limit'], 'cost_mode': 'strict'})
    else:
        await patch(env, 'model_invocation', env.invocation_id, cost_mode='strict')
    row = await item(env)
    assert not row['eligible'] and any(blocker['code'] == 'strict_pricing_requires_reconciliation' for blocker in row['blockers'])
    before = await snapshot(env)
    with pytest.raises(DomainError):
        await accept(env)
    assert await snapshot(env) == before and not await env.store.list(ACK_KIND)


@pytest.mark.parametrize('condition', ['active_work', 'active_process', 'unknown_process',
                                    'missing_receipt', 'identity_mismatch', 'active_transport', 'unknown_transport'])
async def test_execution_or_transport_without_verified_stop_cannot_be_acknowledged(unknown, condition):
    env = unknown
    if condition == 'active_work':
        await patch(env, 'work_item', 'bad', status='running')
        await patch(env, 'attempt', 'bad-attempt', status='running')
    elif condition in {'active_process', 'unknown_process'}:
        await patch(env, 'supervised_attempt', 'bad-attempt',
                    state='running' if condition == 'active_process' else 'execution_unknown')
    elif condition == 'missing_receipt':
        (env.process_directory / 'result.json').unlink()
    elif condition == 'identity_mismatch':
        await patch(env, 'supervised_attempt', 'bad-attempt', nonce='different-process')
    elif condition == 'active_transport':
        env.models._active[env.invocation_id] = object()
    else:
        env.models._active = None
    row = await item(env)
    assert not row['eligible'] and row['blockers'], row
    before = await snapshot(env)
    with pytest.raises(DomainError):
        await accept(env)
    assert await snapshot(env) == before and not await env.store.list(ACK_KIND)


@pytest.mark.parametrize('kind,identity,fields', [
    ('model_attempt_budget', 'bad-attempt', {'request_count': 0}),
    ('model_attempt_budget', 'bad-attempt', {'request_count': 2}),
    ('model_attempt_budget', 'bad-attempt', {'uncertain_invocations': 0}),
    ('model_attempt_budget', 'bad-attempt', {'uncertain_invocations': 2}),
    ('model_attempt_budget', 'bad-attempt', {'restore_uncertain': True}),
    ('budget_account', account_id('run', 'run'), {'request_count': 0}),
    ('budget_account', account_id('iteration', 'iteration'), {'request_count': 2}),
    ('budget_account', account_id('iteration', 'iteration'), {'restore_uncertain': True}),
])
async def test_missing_or_mismatched_request_accounting_is_not_silently_repaired(unknown, kind, identity, fields):
    env = unknown
    await patch(env, kind, identity, **fields)
    row = await item(env)
    assert not row['request_counted'] and not row['eligible'], row
    before = await snapshot(env)
    with pytest.raises(DomainError):
        await accept(env)
    assert await snapshot(env) == before and not await env.store.list(ACK_KIND)


async def test_counter_without_any_invocation_cannot_be_accepted_by_inventing_an_id(env):
    await stopped_workspace(env)
    await patch(env, 'model_attempt_budget', 'bad-attempt', attempt_id='bad-attempt',
                request_count=1, uncertain_invocations=1)
    service = ModelUncertaintyService(env.store, env.workflow, SimpleNamespace(_active={}))
    assert (await service.view('run'))['items'] == []
    payload = {'expected_run_revision': 1, 'expected_work_revision': 2, 'expected_attempt_revision': 2,
        'expected_invocation_revision': 1, 'expected_attempt_budget_revision': 1,
        'expected_state_digest': canonical_digest(await env.service._read('run')),
        'accept_unknown_usage': True, 'reason': 'An absent request cannot be acknowledged'}
    before = await snapshot(env)
    with pytest.raises(DomainError) as error:
        await service.acknowledge('run', 'nonexistent-invocation', payload, 'absent-call')
    assert error.value.code == 'model_uncertainty_not_found'
    assert await snapshot(env) == before and not await env.store.list(ACK_KIND)
    assert any(row['code'] == 'recovery_budget_uncertain'
               for row in env.service._common_blockers(await env.service._read('run')))


@pytest.mark.parametrize('changes', [
    {'accept_unknown_usage': False}, {'accept_unknown_usage': 1}, {'accept_unknown_usage': 'true'},
    {'reason': '   '}, {'uncertain_invocations': 0}, {'expected_invocation_revision': True},
])
async def test_explicit_acceptance_and_strict_request_validation_are_required(unknown, changes):
    env = unknown
    before = await snapshot(env)
    with pytest.raises(ValidationError):
        await env.acknowledgments.acknowledge('run', env.invocation_id,
            acknowledgment(await item(env), **changes), str(uuid4()))
    assert await snapshot(env) == before and not await env.store.list(ACK_KIND)


@pytest.mark.parametrize('field', ['run_revision', 'work_revision', 'attempt_revision',
                                 'invocation_revision', 'attempt_budget_revision'])
async def test_each_expected_revision_is_enforced(unknown, field):
    env = unknown
    row = await item(env)
    before = await snapshot(env)
    with pytest.raises(DomainError) as error:
        await env.acknowledgments.acknowledge('run', env.invocation_id,
            acknowledgment(row, **{'expected_' + field: row[field] + 1}), str(uuid4()))
    assert error.value.code == 'revision_conflict'
    assert await snapshot(env) == before and not await env.store.list(ACK_KIND)


async def test_state_digest_covers_changes_outside_the_five_primary_revisions(unknown):
    env = unknown
    payload = acknowledgment(await item(env))
    await patch(env, 'approval', 'old-approval', note='Concurrent decision metadata changed')
    before = await snapshot(env)
    with pytest.raises(DomainError) as error:
        await env.acknowledgments.acknowledge('run', env.invocation_id, payload, 'stale-digest')
    assert error.value.code == 'revision_conflict'
    assert await snapshot(env) == before and not await env.store.list(ACK_KIND)


async def test_same_key_concurrency_and_lost_response_replay_only_add_one_acknowledgment(unknown):
    env = unknown
    payload = acknowledgment(await item(env))
    before = await snapshot(env)
    results = await asyncio.gather(*(env.acknowledgments.acknowledge('run', env.invocation_id, payload, 'same-key')
                                   for _ in range(4)))
    assert all(result == results[0] for result in results)
    assert len(await env.store.list(ACK_KIND)) == 1 and await snapshot(env) == before
    await patch(env, 'work_item', 'bad', status='running')
    assert await env.acknowledgments.acknowledge('run', env.invocation_id, payload, 'same-key') == results[0]
    with pytest.raises(DomainError) as error:
        await env.acknowledgments.acknowledge('run', env.invocation_id,
            {**payload, 'reason': 'Changed request using the same key'}, 'same-key')
    assert error.value.code == 'idempotency_conflict'
    assert len(await env.store.list(ACK_KIND)) == 1


@pytest.mark.parametrize('unknown', [2], indirect=True)
async def test_acknowledgment_only_exempts_its_exact_invocation(unknown):
    env = unknown
    before = await env.store.list('model_invocation')
    await accept(env, env.invocation_ids[0], 'first-ack')
    assert not (await env.execution_budget.view('run', 'bad'))['can_extend']
    assert any(row['code'] == 'recovery_budget_uncertain'
               for row in env.service._common_blockers(await env.service._read('run')))
    assert not (await item(env, env.invocation_ids[1]))['acknowledged']
    await accept(env, env.invocation_ids[1], 'second-ack')
    assert (await env.execution_budget.view('run', 'bad'))['can_extend']
    assert await env.store.list('model_invocation') == before
    assert (await env.store.read('model_attempt_budget', 'bad-attempt'))['uncertain_invocations'] == 2


@pytest.mark.parametrize('damage', ['counter_mismatch', 'invocation_changed', 'process_changed'])
async def test_acknowledgment_cannot_cover_subsequent_evidence_changes(unknown, damage):
    env = unknown
    await accept(env)
    if damage == 'counter_mismatch':
        await patch(env, 'model_attempt_budget', 'bad-attempt', uncertain_invocations=2)
    elif damage == 'invocation_changed':
        await patch(env, 'model_invocation', env.invocation_id, reason='Different unresolved outcome')
    else:
        await patch(env, 'supervised_attempt', 'bad-attempt', nonce='Different execution identity')
    assert not (await item(env))['acknowledged']
    assert not (await env.execution_budget.view('run', 'bad'))['can_extend']
    assert any(row['code'] == 'recovery_budget_uncertain'
               for row in env.service._common_blockers(await env.service._read('run')))


@pytest.mark.parametrize('race', ['transport', 'receipt'])
async def test_final_writer_rechecks_transport_and_stop_proof_without_database_revision_changes(unknown, monkeypatch, race):
    env = unknown
    payload = acknowledgment(await item(env))
    before = await snapshot(env)
    original = env.store.command
    changed = False
    async def race_at_commit(scope, key, body, handler):
        nonlocal changed
        if scope == 'model.uncertainty.acknowledge' and not changed:
            changed = True
            if race == 'transport':
                env.models._active[env.invocation_id] = object()
            else:
                (env.process_directory / 'result.json').write_text('{incomplete stop evidence')
        return await original(scope, key, body, handler)
    monkeypatch.setattr(env.store, 'command', race_at_commit)
    with pytest.raises(DomainError):
        await env.acknowledgments.acknowledge('run', env.invocation_id, payload, 'race-at-commit')
    assert changed and await snapshot(env) == before and not await env.store.list(ACK_KIND)


async def test_automatic_recovery_requires_a_separate_explicit_action_after_acknowledgment(unknown):
    env = unknown
    await patch(env, 'run', 'run', execution_state='running')
    env.workflow.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0})
    await accept(env)
    automatic = FailureRemediation(env.store, env.workflow)
    before = await env.store.list('work_item')
    analysis = await automatic.analyze('bad')
    assert any(row['code'] == 'manual_retry_after_model_ack' for row in analysis['blockers']), analysis
    def attempt_automatic(tx):
        guard_automatic(tx, env.workflow, analysis['id'], tx.get('work_item', 'bad'), 'retry')
        return {'unexpected': 'automatic authorization'}
    with pytest.raises(DomainError) as error:
        await env.store.command('fixture.automatic-guard', 'automatic-after-ack', {}, attempt_automatic)
    assert error.value.code == 'manual_retry_after_model_ack'
    assert await env.store.list('work_item') == before and not await env.store.list('run_recovery')


async def test_owner_routes_require_origin_and_idempotency_and_never_wake_agents(unknown):
    env = unknown
    scheduler = SimpleNamespace(wake=Mock())
    app = create_app(env.settings, store=env.store, artifacts=env.artifacts, models=env.models, scheduler=scheduler)
    owner = app.state.tokens.exchange(app.state.tokens.bootstrap_code)
    scoped = app.state.tokens.issue('agentflow_attempt', {'model:invoke'}, 'bad-attempt', 60)
    view_path = '/api/v1/runs/run/model_uncertainties'
    command_path = f'/api/v1/runs/run/model_invocations/{env.invocation_id}/acknowledge_unknown'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=env.settings.origin) as client:
        assert (await client.get(view_path)).status_code == 401
        assert (await client.get(view_path, headers={'Authorization': 'Bearer ' + scoped})).status_code in {401, 403}
        headers = {'Authorization': 'Bearer ' + owner, 'Origin': env.settings.origin, 'Idempotency-Key': 'owner-http-ack'}
        view = (await client.get(view_path, headers=headers)).json()
        row = next(row for row in view['items'] if row['invocation_id'] == env.invocation_id)
        payload = acknowledgment(row)
        assert (await client.post(command_path, headers={k: v for k, v in headers.items() if k != 'Origin'}, json=payload)).status_code == 403
        assert (await client.post(command_path, headers={k: v for k, v in headers.items() if k != 'Idempotency-Key'}, json=payload)).status_code == 422
        assert (await client.post(command_path, headers={**headers, 'Authorization': 'Bearer ' + scoped}, json=payload)).status_code in {401, 403}
        result = await client.post(command_path, headers=headers, json=payload)
        assert result.status_code == 200, result.text
        assert result.json()['acknowledged'] and result.json()['invocation_id'] == env.invocation_id
        assert (await client.post(command_path, headers=headers, json=payload)).json() == result.json()
        options = await client.get('/api/v1/runs/run/recovery_options', headers=headers)
        assert options.status_code == 200, options.text
        assert options.json()['model_uncertainties']['items'][0]['acknowledged']
    scheduler.wake.assert_not_called()
    assert not await env.store.list('run_recovery')

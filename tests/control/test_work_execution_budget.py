"""Owner execution-allowance grants conserve usage and never start recovery implicitly."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError
from test_recovery import env as env
from test_recovery import patch

from agentflow.common import DomainError
from agentflow.control.api import create_app
from agentflow.control.coding_steps import CodingSteps
from agentflow.control.failure_remediation import FailureRemediation
from agentflow.control.work_execution_budget import WorkExecutionBudgetService


@pytest_asyncio.fixture
async def exhausted(env):
    run = await env.store.read('run', 'run')
    run = await patch(env, 'run', 'run', execution_state='running',
                      budget_limit={**run['budget_limit'], 'max_tool_calls': 100, 'max_active_seconds': 1800})
    work = await patch(env, 'work_item', 'bad', step='integration_test_implementation', role='development',
                       status='running', quality_result='unknown', write_paths=['src'])
    attempt = await patch(env, 'attempt', 'bad-attempt', status='running')
    coding = CodingSteps(env.store, env.settings, env.service.repository)
    control = await coding.prepare(run, work, attempt, env.project['base_commit'], 65536)
    task = {'run_id': 'run', 'work_item_id': 'bad', 'attempt_id': attempt['id'],
            'generation': work['generation'], 'fencing_token': work['fencing_token'],
            'input_fingerprint': work['input_fingerprint'], 'coding_step': control}
    await coding.account(task, {'active_seconds': 709, 'observed_tool_calls': 120, 'tool_observation_complete': True})
    await patch(env, 'coding_work_budget', control['budget_id'], step_count=6)
    await env.workflow.block_attempt(attempt['id'], 'Tools 120 / 100 exhausted', str(uuid4()), failure_code='coding_budget_exhausted')
    env.budget_id = control['budget_id']
    env.execution_budget = WorkExecutionBudgetService(env.store, env.workflow)
    return env


def grant(view, **changes):
    return {'expected_run_revision': view['run_revision'], 'expected_work_revision': view['work_revision'],
        'expected_budget_revision': view['budget_revision'], 'additional_tool_calls': 400,
        'reason': 'Owner approved additional execution work', **changes}


def dimensions(view):
    return {row['key']: row for row in view['dimensions']}


async def test_known_overrun_is_visible_and_the_recovery_card_has_an_enabled_adjustment(exhausted):
    env = exhausted
    view = await env.execution_budget.view('run', 'bad')
    row = dimensions(view)['max_tool_calls']
    assert view['can_extend'] and view['metering'] == 'known' and view['run_state'] == 'running'
    assert {key: row[key] for key in ('used', 'limit', 'remaining', 'balance', 'overrun')} == {
        'used': 120, 'limit': 100, 'remaining': 0, 'balance': -20, 'overrun': 20}
    assert row['usage_kind'] == 'observed'
    options = await env.service.options('run')
    option = next(row for row in options['retry_options'] if row['work_item_id'] == 'bad')
    assert not option['eligible']
    assert option['execution_budget'] == view


async def test_explicit_grant_preserves_usage_code_approvals_and_models_and_only_enables_separate_retry(exhausted):
    env = exhausted
    view = await env.execution_budget.view('run', 'bad')
    kinds = ('run', 'work_item', 'attempt', 'code_snapshot', 'artifact', 'approval', 'model_profile',
             'budget_account', 'coding_step_control', 'coding_step_usage')
    before = {kind: await env.store.list(kind) for kind in kinds}
    result = await env.execution_budget.extend('run', 'bad', grant(view, additional_active_seconds=1200, additional_steps=8), 'grant')
    rows = dimensions(result)
    assert (rows['max_tool_calls']['used'], rows['max_tool_calls']['limit'], rows['max_tool_calls']['remaining']) == (120, 500, 380)
    assert (rows['max_active_seconds']['used'], rows['max_active_seconds']['limit']) == (709, 3000)
    assert (rows['max_steps']['used'], rows['max_steps']['limit']) == (6, 40)
    assert result['budget_revision'] == view['budget_revision'] + 1
    assert result['run_revision'] == view['run_revision'] and result['work_revision'] == view['work_revision']
    assert {kind: await env.store.list(kind) for kind in kinds} == before
    assert not await env.store.list('model_invocation') and not await env.store.list('run_recovery')
    audit = (await env.store.list('work_execution_budget_adjustment'))[0]
    assert audit['actor'] == 'owner' and audit['used']['observed_tool_calls'] == 120
    assert audit['additions']['additional_tool_calls'] == 400 and audit['requires_explicit_retry']
    option = next(row for row in (await env.service.options('run'))['retry_options'] if row['work_item_id'] == 'bad')
    assert option['eligible']


async def test_an_insufficient_explicit_addition_reports_the_remaining_blocker(exhausted):
    env = exhausted
    view = await env.execution_budget.view('run', 'bad')
    result = await env.execution_budget.extend('run', 'bad', grant(view, additional_tool_calls=10), 'small-grant')
    row = dimensions(result)['max_tool_calls']
    assert row['limit'] == 110 and row['used'] == 120 and row['remaining'] == 0 and row['balance'] == -10
    assert row['exhausted'] and result['execution_blockers']
    assert not next(row for row in (await env.service.options('run'))['retry_options'] if row['work_item_id'] == 'bad')['eligible']


@pytest.mark.parametrize('changes', [{'additional_tool_calls': -1}, {'additional_tool_calls': True},
    {'additional_tool_calls': 1.5}, {'additional_tool_calls': 0}, {'observed_tool_calls': 0}, {'reason': '  '}])
async def test_invalid_or_mass_assignment_requests_never_change_a_budget(exhausted, changes):
    env = exhausted
    view = await env.execution_budget.view('run', 'bad')
    before = await env.store.list('coding_work_budget')
    with pytest.raises(ValidationError):
        await env.execution_budget.extend('run', 'bad', grant(view, **changes), str(uuid4()))
    assert await env.store.list('coding_work_budget') == before


@pytest.mark.parametrize('condition', ['active', 'unknown', 'restore', 'delivery'])
async def test_active_unknown_restored_or_publishing_work_cannot_gain_allowance(exhausted, condition):
    env = exhausted
    if condition == 'active':
        await patch(env, 'work_item', 'bad', status='running')
        await patch(env, 'attempt', 'bad-attempt', status='running')
    elif condition == 'unknown':
        await patch(env, 'coding_work_budget', env.budget_id, uncertain=True)
    elif condition == 'restore':
        await patch(env, 'run', 'run', restore_reconciliation_required=True)
    else:
        await patch(env, 'delivery_intent', 'in-flight', run_id='run', status='prepared')
    view = await env.execution_budget.view('run', 'bad')
    assert not view['can_extend'] and view['adjustment_blockers']
    if condition == 'unknown':
        assert view['metering'] == 'unknown'
        assert all(row['used'] is None and row['remaining'] is None for row in view['dimensions'])
    before = await env.store.list('coding_work_budget')
    with pytest.raises(DomainError):
        await env.execution_budget.extend('run', 'bad', grant(view), str(uuid4()))
    assert await env.store.list('coding_work_budget') == before and not await env.store.list('work_execution_budget_adjustment')


async def test_same_key_concurrency_and_lost_ack_replay_never_add_twice(exhausted):
    env = exhausted
    view = await env.execution_budget.view('run', 'bad')
    payload = grant(view)
    results = await asyncio.gather(*(env.execution_budget.extend('run', 'bad', payload, 'same-key') for _ in range(4)))
    assert all(result == results[0] for result in results)
    assert (await env.store.read('coding_work_budget', env.budget_id))['max_tool_calls'] == 500
    assert len(await env.store.list('work_execution_budget_adjustment')) == 1
    await patch(env, 'work_item', 'bad', status='running')
    assert await env.execution_budget.extend('run', 'bad', payload, 'same-key') == results[0]
    with pytest.raises(DomainError) as error:
        await env.execution_budget.extend('run', 'bad', {**payload, 'additional_tool_calls': 800}, 'same-key')
    assert error.value.code == 'idempotency_conflict'


@pytest.mark.parametrize('kind,identity', [('run', 'run'), ('work_item', 'bad'), ('coding_work_budget', None)])
async def test_all_three_expected_revisions_are_enforced(exhausted, kind, identity):
    env = exhausted
    view = await env.execution_budget.view('run', 'bad')
    await patch(env, kind, identity or env.budget_id, note='concurrent owner change')
    before = await env.store.list('coding_work_budget')
    with pytest.raises(DomainError) as error:
        await env.execution_budget.extend('run', 'bad', grant(view), str(uuid4()))
    assert error.value.code == 'revision_conflict'
    assert await env.store.list('coding_work_budget') == before


async def test_execution_starting_after_read_only_checks_prevents_the_grant(exhausted, monkeypatch):
    env = exhausted
    view = await env.execution_budget.view('run', 'bad')
    original = env.store.command
    changed = False
    async def race(scope, key, payload, handler):
        nonlocal changed
        if scope == 'work.execution_budget.extend' and not changed:
            changed = True
            def start(tx):
                work = tx.get('work_item', 'bad')
                return tx.put('work_item', 'bad', {**work, 'status': 'running'}, work['revision'])
            await original('fixture', 'concurrent-start', {}, start)
        return await original(scope, key, payload, handler)
    monkeypatch.setattr(env.store, 'command', race)
    before = await env.store.list('coding_work_budget')
    with pytest.raises(DomainError) as error:
        await env.execution_budget.extend('run', 'bad', grant(view), 'race')
    assert error.value.code == 'revision_conflict'
    assert await env.store.list('coding_work_budget') == before


async def test_atomic_auto_recovery_guard_rejects_even_a_stale_preliminary_audit_read(exhausted, monkeypatch):
    env = exhausted
    view = await env.execution_budget.view('run', 'bad')
    await env.execution_budget.extend('run', 'bad', grant(view), 'explicit-grant')
    await patch(env, 'work_item', 'bad', runtime_failure_code='worker_timeout')
    await patch(env, 'attempt', 'bad-attempt', runtime_failure_code='worker_timeout')
    env.workflow.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0})
    automatic = FailureRemediation(env.store, env.workflow)
    original = env.store.list
    reads = 0
    async def stale_audit(kind):
        nonlocal reads
        rows = await original(kind)
        if kind == 'work_execution_budget_adjustment':
            reads += 1
            if reads == 2:
                return []
        return rows
    monkeypatch.setattr(env.store, 'list', stale_audit)
    analysis = await automatic.analyze('bad')
    assert analysis['status'] == 'ready', analysis
    before = await env.store.list('work_item')
    with pytest.raises(DomainError) as error:
        await automatic.recovery.recover_automatic('run', analysis['id'])
    assert error.value.code == 'manual_retry_after_budget_change'
    assert await env.store.list('work_item') == before and not await env.store.list('model_invocation')


async def test_owner_api_uses_revision_and_idempotency_without_waking_execution(exhausted):
    env = exhausted
    scheduler = SimpleNamespace(wake=Mock())
    app = create_app(env.settings, store=env.store, artifacts=env.artifacts, scheduler=scheduler)
    owner = app.state.tokens.exchange(app.state.tokens.bootstrap_code)
    scoped = app.state.tokens.issue('agentflow_attempt', {'model:invoke'}, 'bad-attempt', 60)
    path = '/api/v1/runs/run/work_items/bad/execution_budget'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=env.settings.origin) as client:
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers={'Authorization': 'Bearer ' + scoped})).status_code in {401, 403}
        headers = {'Authorization': 'Bearer ' + owner, 'Origin': env.settings.origin, 'Idempotency-Key': 'api-grant'}
        view = (await client.get(path, headers=headers)).json()
        response = await client.post(path + '/extend', headers=headers, json=grant(view))
        assert response.status_code == 200, response.text
        assert response.json()['run_id'] == 'run' and response.json()['work_item_id'] == 'bad'
        assert response.json()['budget_revision'] == view['budget_revision'] + 1
        assert (await client.post(path + '/extend', headers=headers, json=grant(view))).json() == response.json()
    scheduler.wake.assert_not_called()
    assert (await env.store.read('work_item', 'bad'))['status'] == 'blocked'

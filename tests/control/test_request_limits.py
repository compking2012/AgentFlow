import asyncio
import copy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from agentflow.common import DomainError
from agentflow.control.api import create_app
from agentflow.control.request_limits import RequestLimitService
from agentflow.models.budget import BudgetLedger, account_id
from agentflow.models.profiles import AttemptContext
from agentflow.settings import Settings
from agentflow.storage import Store


@pytest_asyncio.fixture
async def limits(tmp_path):
    settings = Settings(data_dir=tmp_path / 'control')
    store = Store(settings.data_dir)
    await store.start()
    run_limit = {'currency': 'USD', 'limit_micros': 50000, 'max_model_requests': 200,
                 'max_tool_calls': 60, 'max_active_seconds': 1800, 'cost_mode': 'request_limited'}
    iteration_limit = {**run_limit, 'limit_micros': 80000}

    def seed(tx):
        tx.put('iteration', 'iteration', {'project_id': 'project', 'budget_limit': iteration_limit})
        tx.put('plan', 'plan', {'budget_limit': run_limit, 'input_fingerprint': 'frozen-plan'})
        tx.put('run', 'run', {'iteration_id': 'iteration', 'project_id': 'project', 'plan_id': 'plan',
            'budget_limit': run_limit, 'execution_state': 'paused', 'input_fingerprint': 'frozen-run',
            'quality_result': 'unknown'})
        tx.put('attempt', 'old-attempt', {'run_id': 'run', 'iteration_id': 'iteration', 'status': 'failed',
            'input_fingerprint': 'frozen-attempt', 'fencing_token': 3})
        tx.put('model_attempt_budget', 'old-attempt', {'request_count': 17, 'uncertain_invocations': 0})
        return {}

    await store.command('test', 'seed', {}, seed)
    ledger = BudgetLedger(store)
    await ledger.setup_accounts('run', 'iteration', 50000, 80000, run_max_requests=200, iteration_max_requests=200)

    def exhausted(tx):
        for kind, owner in [('run', 'run'), ('iteration', 'iteration')]:
            account = tx.get('budget_account', account_id(kind, owner))
            tx.put('budget_account', account['id'], {**account, 'request_count': 200, 'settled_micros': 150,
                'cost_status': 'unknown', 'unpriced_completed_requests': 200, 'preserved_extra': {'fixture': True}},
                account['revision'])
        return {}

    await store.command('test', 'exhausted', {}, exhausted)
    yield store, ledger, RequestLimitService(store), settings
    await store.close()


def request(maximum=300, revision=1, reason='用户明确授权追加 100 次调用'):
    return {'expected_revision': revision, 'max_model_requests': maximum, 'reason': reason}


async def patch(store, kind, identity, fields):
    def update(tx):
        old = tx.get(kind, identity)
        return tx.put(kind, identity, {**(old or {}), **fields}, old['revision'] if old else None)
    return await store.command('test.patch', f'{kind}:{identity}:{len(await store.events(0))}:{fields}', {}, update)


async def budget_state(store):
    return {kind: await store.list(kind) for kind in ('run', 'iteration', 'budget_account', 'request_limit_change')}


async def terminal_failure(store, state):
    await patch(store, 'work_item', 'recoverable', {'run_id': 'run', 'step': 'research',
        'status': 'cancelled' if state == 'cancelled' else 'completed', 'quality_result': 'failed'})
    return await patch(store, 'run', 'run', {'execution_state': state, 'quality_result': 'failed'})


async def test_explicit_extension_preserves_usage_money_frozen_inputs_and_emits_owner_audit(limits):
    store, _, service, _ = limits
    before = await budget_state(store)
    plan = await store.read('plan', 'plan')
    attempt = await store.read('attempt', 'old-attempt')
    attempt_budget = await store.read('model_attempt_budget', 'old-attempt')
    updated = await service.extend('run', request(), 'owner-grant')
    assert updated['execution_state'] == 'paused' and updated['revision'] == 2
    assert updated['budget_limit']['max_model_requests'] == 300
    assert updated['input_fingerprint'] == 'frozen-run'
    assert (await store.read('iteration', 'iteration'))['budget_limit']['max_model_requests'] == 300
    for old in before['budget_account']:
        current = await store.read('budget_account', old['id'])
        assert current == {**old, 'revision': old['revision'] + 1, 'max_requests': 300}
    assert await store.read('plan', 'plan') == plan
    assert await store.read('attempt', 'old-attempt') == attempt
    assert await store.read('model_attempt_budget', 'old-attempt') == attempt_budget
    audit = (await store.list('request_limit_change'))[0]
    assert audit['actor'] == 'owner' and audit['reason'] == request()['reason']
    assert audit['added_requests'] == 100 and audit['run_request_count'] == 200
    assert audit['previous_max_model_requests'] == 200 and audit['max_model_requests'] == 300
    events = [event for event in await store.events(0, 'run') if event['type'] == 'run.request_limit_extended']
    assert len(events) == 1 and events[0]['body']['change_id'] == audit['id']


async def test_replay_after_resume_cannot_grant_twice_and_changed_payload_conflicts(limits):
    store, _, service, _ = limits
    first = await service.extend('run', request(), 'same-owner-request')
    await patch(store, 'run', 'run', {'execution_state': 'running'})
    assert await service.extend('run', request(), 'same-owner-request') == first
    assert len(await store.list('request_limit_change')) == 1
    assert (await store.read('budget_account', account_id('run', 'run')))['max_requests'] == 300
    with pytest.raises(DomainError) as error:
        await service.extend('run', request(400), 'same-owner-request')
    assert error.value.code == 'idempotency_conflict'


async def test_explicit_zero_removes_both_caps_without_resetting_usage_or_frozen_plan(limits):
    store, ledger, service, _ = limits
    before = await budget_state(store)
    plan = await store.read('plan', 'plan')
    updated = await service.extend('run', request(0, reason='用户选择不限次数'), 'unlimited')
    assert updated['budget_limit']['max_model_requests'] == 0 and updated['execution_state'] == 'paused'
    for old in before['budget_account']:
        assert await store.read('budget_account', old['id']) == {**old, 'revision': old['revision'] + 1, 'max_requests': 0}
    assert (await store.read('iteration', 'iteration'))['budget_limit']['max_model_requests'] == 0
    assert await store.read('plan', 'plan') == plan
    audit = (await store.list('request_limit_change'))[0]
    assert audit['unlimited_requests'] is True and audit['added_requests'] is None
    assert audit['run_request_count'] == 200
    fresh = await ledger.setup_accounts('run', 'iteration', 50000, 80000, run_max_requests=0, iteration_max_requests=0)
    assert fresh['run']['request_count'] == 200
    context = AttemptContext(attempt_id='unlimited-attempt', run_id='run', iteration_id='iteration',
        model_profile_id='fixture', fencing_token=1, input_fingerprint='sha256:' + 'a' * 64,
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        max_model_requests=0, max_output_tokens=64, cost_mode='request_limited')
    for index in range(2):
        invocation = await ledger.reserve(context, protocol='responses', request_fingerprint='fixture',
            profile_revision=1, amount_micros=0, currency='USD', idempotency_key=f'after-unlimited-{index}')
        await ledger.release_not_sent(invocation['id'], 'No provider request in this test')
    await patch(store, 'run', 'run', {'execution_state': 'running'})
    assert await service.extend('run', request(0, reason='用户选择不限次数'), 'unlimited') == updated
    assert len(await store.list('request_limit_change')) == 1
    assert (await store.read('budget_account', account_id('run', 'run')))['request_count'] == 202
    with pytest.raises(DomainError, match='explicit budget revision'):
        await ledger.setup_accounts('run', 'iteration', 50000, 80000, run_max_requests=200, iteration_max_requests=200)


@pytest.mark.parametrize('run_maximum,iteration_maximum,new_maximum,expected_iteration', [
    (200, 0, 300, 0), (0, 200, 0, 0), (0, 0, 0, None), (0, 0, 300, None),
])
async def test_zero_is_unlimited_when_extending_existing_or_shared_limits(
        limits, run_maximum, iteration_maximum, new_maximum, expected_iteration):
    store, _, service, _ = limits
    run = await store.read('run', 'run')
    run = await patch(store, 'run', 'run', {'budget_limit': {**run['budget_limit'], 'max_model_requests': run_maximum}})
    iteration = await store.read('iteration', 'iteration')
    await patch(store, 'iteration', 'iteration', {'budget_limit': {
        **iteration['budget_limit'], 'max_model_requests': iteration_maximum}})
    for kind, maximum in [('run', run_maximum), ('iteration', iteration_maximum)]:
        await patch(store, 'budget_account', account_id(kind, kind), {
            'max_requests': maximum, 'request_count': 5000 if maximum == 0 else 200})
    before = await budget_state(store)
    if expected_iteration is None:
        with pytest.raises(DomainError) as error:
            await service.extend('run', request(new_maximum, run['revision']), 'not-extension')
        assert error.value.code == 'request_limit_not_increased'
        assert await budget_state(store) == before
    else:
        result = await service.extend('run', request(new_maximum, run['revision']), 'extend-shared')
        assert result['budget_limit']['max_model_requests'] == new_maximum
        assert (await store.read('budget_account', account_id('iteration', 'iteration')))['max_requests'] == expected_iteration
        for old in before['budget_account']:
            current = await store.read('budget_account', old['id'])
            assert current['request_count'] == old['request_count']
            assert current['settled_micros'] == old['settled_micros']


async def test_competing_grants_use_run_revision_and_commit_one_atomic_change(limits):
    store, _, service, _ = limits
    results = await asyncio.gather(service.extend('run', request(300), 'grant-a'),
                                   service.extend('run', request(400), 'grant-b'), return_exceptions=True)
    assert sum(isinstance(result, DomainError) for result in results) == 1
    assert next(result for result in results if isinstance(result, DomainError)).code == 'revision_conflict'
    run = await store.read('run', 'run')
    assert len(await store.list('request_limit_change')) == 1
    for kind, owner in [('run', 'run'), ('iteration', 'iteration')]:
        account = await store.read('budget_account', account_id(kind, owner))
        assert account['max_requests'] == run['budget_limit']['max_model_requests']
        assert account['request_count'] == 200


@pytest.mark.parametrize('change', [
    {'max_model_requests': '300'}, {'max_model_requests': True}, {'max_model_requests': 300.0},
    {'max_model_requests': 2001}, {'max_model_requests': -1}, {'expected_revision': True},
    {'expected_revision': 0}, {'reason': '   '}, {'reason': 'x' * 2001}, {'reset_count': True},
])
async def test_request_schema_rejects_coercion_invalid_limits_and_extra_fields(limits, change):
    store, _, service, _ = limits
    before = await budget_state(store)
    with pytest.raises(ValidationError):
        await service.extend('run', {**request(), **change}, 'invalid')
    assert await budget_state(store) == before


@pytest.mark.parametrize('state', ['running', 'blocked', 'failed', 'completed', 'cancelled', 'publishing', 'execution_unknown'])
async def test_unpaused_or_terminal_without_recoverable_work_rejects_a_grant(limits, state):
    store, _, service, _ = limits
    current = await patch(store, 'run', 'run', {'execution_state': state})
    before = await budget_state(store)
    with pytest.raises(DomainError) as error:
        await service.extend('run', request(revision=current['revision']), 'not-paused')
    assert error.value.code == ('run_not_recoverable' if state in {'completed', 'cancelled'} else 'run_not_paused')
    assert await budget_state(store) == before


@pytest.mark.parametrize('maximum', [199, 200])
async def test_limits_must_strictly_increase(limits, maximum):
    store, _, service, _ = limits
    before = await budget_state(store)
    with pytest.raises(DomainError) as error:
        await service.extend('run', request(maximum), 'decrease')
    assert error.value.code == 'request_limit_not_increased'
    assert await budget_state(store) == before


@pytest.mark.parametrize(('kind', 'fields', 'code'), [
    ('attempt', {'status': 'running'}, 'active_attempts'),
    ('attempt', {'status': 'waiting_execution'}, 'active_attempts'),
    ('attempt', {'status': 'execution_unknown'}, 'active_attempts'),
    ('work_item', {'status': 'running'}, 'active_attempts'),
    ('supervised_attempt', {'state': 'launch_intent'}, 'active_attempts'),
    ('supervised_attempt', {'state': 'running'}, 'active_attempts'),
    ('supervised_attempt', {'state': 'execution_unknown'}, 'active_attempts'),
    ('model_invocation', {'state': 'reserved', 'amount_micros': 0}, 'model_calls_unsettled'),
    ('model_invocation', {'state': 'dispatching', 'amount_micros': 0}, 'model_calls_unsettled'),
    ('model_invocation', {'state': 'uncertain', 'amount_micros': 0}, 'model_calls_unsettled'),
    ('node_job', {'state': 'queued'}, 'node_jobs_in_flight'),
    ('node_job', {'state': 'leased'}, 'node_jobs_in_flight'),
    ('node_job', {'state': 'running'}, 'node_jobs_in_flight'),
    ('node_job', {'state': 'stopping'}, 'node_jobs_in_flight'),
    ('node_job', {'state': 'execution_unknown'}, 'node_jobs_in_flight'),
])
@pytest.mark.parametrize('run_state', ['paused', 'completed', 'cancelled'])
async def test_unfinished_or_uncertain_activity_rejects_without_partial_budget_updates(limits, kind, fields, code, run_state):
    store, _, service, _ = limits
    run = await terminal_failure(store, run_state) if run_state != 'paused' else await store.read('run', 'run')
    await patch(store, kind, 'busy', {'run_id': 'run', 'iteration_id': 'iteration', **fields})
    before = await budget_state(store)
    with pytest.raises(DomainError) as error:
        await service.extend('run', request(revision=run['revision']), 'busy')
    assert error.value.code == code
    assert await budget_state(store) == before


async def test_shared_iteration_activity_is_checked_and_unrelated_runs_do_not_block(limits):
    store, _, service, _ = limits
    await patch(store, 'run', 'peer', {'iteration_id': 'iteration', 'project_id': 'project', 'execution_state': 'paused'})
    await patch(store, 'attempt', 'peer-attempt', {'run_id': 'peer', 'status': 'running'})
    with pytest.raises(DomainError) as error:
        await service.extend('run', request(), 'shared-active')
    assert error.value.code == 'active_attempts'
    await patch(store, 'run', 'peer', {'iteration_id': 'other-iteration'})
    assert (await service.extend('run', request(), 'unrelated-active'))['budget_limit']['max_model_requests'] == 300


@pytest.mark.parametrize('fields', [
    {'reserved_micros': 1}, {'uncertain_micros': 1}, {'restore_uncertain': True},
])
@pytest.mark.parametrize('run_state', ['paused', 'completed', 'cancelled'])
async def test_reservations_and_restored_uncertainty_cannot_be_bypassed(limits, fields, run_state):
    store, _, service, _ = limits
    run = await terminal_failure(store, run_state) if run_state != 'paused' else await store.read('run', 'run')
    await patch(store, 'budget_account', account_id('iteration', 'iteration'), fields)
    before = await budget_state(store)
    with pytest.raises(DomainError) as error:
        await service.extend('run', request(revision=run['revision']), 'unsettled-budget')
    assert error.value.code == 'budget_requires_reconciliation'
    assert await budget_state(store) == before


async def test_orphan_attempt_uncertainty_also_blocks_zero_cost_extension(limits):
    store, _, service, _ = limits
    await patch(store, 'model_attempt_budget', 'old-attempt', {'uncertain_invocations': 1})
    with pytest.raises(DomainError) as error:
        await service.extend('run', request(), 'orphan-uncertain')
    assert error.value.code == 'model_calls_unsettled'


async def test_setup_accounts_cannot_change_limits_and_can_resume_after_explicit_grant(limits):
    store, ledger, service, _ = limits
    with pytest.raises(DomainError) as error:
        await ledger.setup_accounts('run', 'iteration', 50000, 80000, run_max_requests=300, iteration_max_requests=300)
    assert error.value.code == 'budget_configuration_conflict'
    await service.extend('run', request(), 'explicit')
    fresh = await ledger.setup_accounts('run', 'iteration', 50000, 80000,
        run_max_requests=300, iteration_max_requests=300)
    assert fresh['run']['max_requests'] == fresh['iteration']['max_requests'] == 300
    assert fresh['run']['request_count'] == fresh['iteration']['request_count'] == 200
    assert await ledger.setup_accounts('run', 'iteration', 50000, 80000,
        run_max_requests=300, iteration_max_requests=300) == fresh
    # The old fingerprint exists in the command journal. It must not replay stale
    # settings as an authorization after an explicit owner grant.
    with pytest.raises(DomainError) as error:
        await ledger.setup_accounts('run', 'iteration', 50000, 80000,
            run_max_requests=200, iteration_max_requests=200)
    assert error.value.code == 'budget_configuration_conflict'
    assert (await store.read('budget_account', account_id('run', 'run')))['request_count'] == 200


async def test_existing_account_mismatch_is_not_silently_repaired(limits):
    store, _, service, _ = limits
    await patch(store, 'budget_account', account_id('run', 'run'), {'max_requests': 201})
    before = await budget_state(store)
    with pytest.raises(DomainError) as error:
        await service.extend('run', request(), 'mismatch')
    assert error.value.code == 'budget_configuration_conflict'
    assert await budget_state(store) == before


async def test_grant_allows_exactly_the_added_requests_without_resetting_prior_usage(limits):
    store, ledger, service, _ = limits
    context = AttemptContext(attempt_id='new-attempt', run_id='run', iteration_id='iteration',
        model_profile_id='fixture', fencing_token=1, input_fingerprint='sha256:' + 'a' * 64,
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        max_model_requests=300, max_output_tokens=64, cost_mode='request_limited')
    arguments = {'protocol': 'responses', 'request_fingerprint': 'local-ledger-only',
                 'profile_revision': 1, 'amount_micros': 0, 'currency': 'USD'}
    with pytest.raises(DomainError) as error:
        await ledger.reserve(context, **arguments)
    assert error.value.code == 'request_limit_exceeded'
    await service.extend('run', request(), 'grant')
    for index in range(100):
        invocation = await ledger.reserve(context, **arguments, idempotency_key=f'local-{index}')
        await ledger.release_not_sent(invocation['id'], 'Fixture makes no provider calls')
    with pytest.raises(DomainError) as error:
        await ledger.reserve(context, **arguments, idempotency_key='over-new-limit')
    assert error.value.code == 'request_limit_exceeded'
    for kind, owner in [('run', 'run'), ('iteration', 'iteration')]:
        account = await store.read('budget_account', account_id(kind, owner))
        assert account['request_count'] == account['max_requests'] == 300
        assert account['settled_micros'] == 150


async def test_missing_run_account_cannot_be_created_by_extension(limits):
    store, _, service, _ = limits
    original = await store.read('run', 'run')
    peer = {key: value for key, value in original.items() if key not in {'id', 'revision'}}
    await patch(store, 'run', 'without-account', peer)
    with pytest.raises(DomainError) as error:
        await service.extend('without-account', request(), 'missing')
    assert error.value.code == 'budget_account_missing'
    assert await store.read('budget_account', account_id('run', 'without-account')) is None


@pytest.mark.parametrize(('iteration_maximum', 'expected'), [(400, 500), (1950, None)])
async def test_shared_iteration_gains_the_same_explicit_increment_with_a_hard_cap(limits, iteration_maximum, expected):
    store, _, service, _ = limits
    iteration = await store.read('iteration', 'iteration')
    await patch(store, 'iteration', 'iteration', {'budget_limit': {
        **iteration['budget_limit'], 'max_model_requests': iteration_maximum}})
    await patch(store, 'budget_account', account_id('iteration', 'iteration'), {'max_requests': iteration_maximum})
    if expected is None:
        before = await budget_state(store)
        with pytest.raises(DomainError) as error:
            await service.extend('run', request(), 'shared-overflow')
        assert error.value.code == 'request_limit_too_large'
        assert await budget_state(store) == before
    else:
        await service.extend('run', request(), 'shared-grant')
        assert (await store.read('iteration', 'iteration'))['budget_limit']['max_model_requests'] == expected
        assert (await store.read('budget_account', account_id('iteration', 'iteration')))['max_requests'] == expected


async def test_owner_api_requires_auth_origin_idempotency_and_valid_body(limits):
    store, _, _, settings = limits
    app = create_app(settings, store=store)
    token = app.state.tokens.issue('agentflow_owner', {'owner:*'}, 'owner', 60)
    headers = {'Authorization': 'Bearer ' + token, 'Origin': settings.origin, 'Idempotency-Key': 'owner-http'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.origin) as client:
        for missing, status in [('Authorization', 401), ('Origin', 403), ('Idempotency-Key', 422)]:
            assert (await client.post('/api/v1/runs/run/request_limit', json=request(),
                headers={key: value for key, value in headers.items() if key != missing})).status_code == status
        invalid = copy.deepcopy(request())
        invalid['max_model_requests'] = '300'
        assert (await client.post('/api/v1/runs/run/request_limit', json=invalid, headers=headers)).status_code == 422
        response = await client.post('/api/v1/runs/run/request_limit', json=request(), headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()['budget_limit']['max_model_requests'] == 300
        assert response.json()['execution_state'] == 'paused'


@pytest.mark.parametrize('state', ['completed', 'cancelled'])
@pytest.mark.parametrize('maximum', [0, 300])
async def test_terminal_failure_can_raise_limit_without_resetting_usage_or_resuming(limits, state, maximum):
    store, _, service, _ = limits
    run = await terminal_failure(store, state)
    accounts = await store.list('budget_account')
    preserved = {kind: await store.list(kind) for kind in ('plan', 'work_item', 'attempt',
        'model_attempt_budget', 'model_invocation', 'artifact', 'approval', 'check', 'code_snapshot', 'run_recovery')}
    updated = await service.extend('run', request(maximum, run['revision']), 'terminal-grant')
    assert updated == {**run, 'revision': run['revision'] + 1,
                       'budget_limit': {**run['budget_limit'], 'max_model_requests': maximum}}
    assert updated['execution_state'] == state
    for old in accounts:
        current = await store.read('budget_account', old['id'])
        assert current == {**old, 'revision': old['revision'] + 1, 'max_requests': maximum}
    for kind, before in preserved.items():
        assert await store.list(kind) == before
    assert (await store.list('request_limit_change'))[0]['run_request_count'] == 200
    assert [event['type'] for event in await store.events(0, 'run')] == ['run.request_limit_extended']


@pytest.mark.parametrize('status,quality,step,archived,accepted', [
    ('failed', 'unknown', 'research', False, True),
    ('blocked', 'unknown', 'implementation', False, True),
    ('cancelled', 'unknown', 'implementation', False, True),
    ('completed', 'failed', 'prd', False, True),
    ('completed', 'inconclusive', 'research', False, True),
    ('completed', 'unknown', 'code_review', False, True),
    ('completed', 'unknown', 'unit_test_execution', False, True),
    ('completed', 'passed', 'integration_test_execution', False, False),
    ('completed', 'unknown', 'research', False, False),
    ('failed', 'failed', 'research', True, False),
])
async def test_terminal_extension_uses_recovery_work_eligibility(limits, status, quality, step, archived, accepted):
    store, _, service, _ = limits
    run = await terminal_failure(store, 'completed')
    await patch(store, 'work_item', 'recoverable', {'status': status, 'quality_result': quality, 'step': step, 'archived': archived})
    before = await budget_state(store)
    if accepted:
        updated = await service.extend('run', request(0, run['revision']), 'eligible')
        assert updated['execution_state'] == 'completed' and updated['budget_limit']['max_model_requests'] == 0
    else:
        with pytest.raises(DomainError) as error:
            await service.extend('run', request(0, run['revision']), 'not-recoverable')
        assert error.value.code == 'run_not_recoverable'
        assert await budget_state(store) == before


@pytest.mark.parametrize('run_state', ['paused', 'completed', 'cancelled'])
@pytest.mark.parametrize('evidence', ['run_delivery_ids', 'confirmed_delivery', 'confirmed_intent', 'prepared_intent'])
async def test_delivery_confirmation_or_inflight_publication_rejects_any_grant(limits, run_state, evidence):
    store, _, service, _ = limits
    await terminal_failure(store, run_state)
    if evidence == 'run_delivery_ids':
        await patch(store, 'run', 'run', {'delivery_ids': ['delivered']})
    elif evidence == 'confirmed_delivery':
        await patch(store, 'delivery', 'delivered', {'run_id': 'run', 'confirmed_at': '2026-09-22T00:00:00Z'})
    else:
        await patch(store, 'delivery_intent', 'publication', {'run_id': 'run',
            'status': 'confirmed' if evidence == 'confirmed_intent' else 'prepared'})
    run = await store.read('run', 'run')
    before = await budget_state(store)
    with pytest.raises(DomainError) as error:
        await service.extend('run', request(0, run['revision']), 'delivered')
    assert error.value.code == ('delivery_in_progress' if evidence == 'prepared_intent' else 'delivered_run')
    assert await budget_state(store) == before


@pytest.mark.parametrize('run_state', ['paused', 'completed', 'cancelled'])
@pytest.mark.parametrize('change,code', [({'deleted_at': 'now'}, 'product_deleted'),
    ({'needs_restart': True}, 'product_restart_required'), ({'config_revision': 2}, 'product_restart_required'),
    ({'run_id': 'replacement'}, 'historical_product_run')])
async def test_product_guards_apply_to_paused_and_terminal_budget_grants(limits, run_state, change, code):
    store, _, service, _ = limits
    run = await terminal_failure(store, run_state)
    await patch(store, 'plan', 'plan', {'product_contract': {'product_id': 'product', 'config_revision': 1}})
    await patch(store, 'product', 'product', {'project_id': 'project', 'run_id': 'run', **change})
    before = await budget_state(store)
    with pytest.raises(DomainError) as error:
        await service.extend('run', request(0, run['revision']), 'product-protected')
    assert error.value.code == code
    assert await budget_state(store) == before


async def test_terminal_grant_replay_remains_idempotent_after_product_and_run_change(limits):
    store, _, service, _ = limits
    run = await terminal_failure(store, 'cancelled')
    original = request(0, run['revision'])
    first = await service.extend('run', original, 'terminal-once')
    await patch(store, 'run', 'run', {'execution_state': 'running'})
    await patch(store, 'product', 'legacy-product', {'run_id': 'run', 'deleted_at': 'now'})
    assert await service.extend('run', original, 'terminal-once') == first
    assert len(await store.list('request_limit_change')) == 1
    for account in await store.list('budget_account'):
        assert account['max_requests'] == 0 and account['request_count'] == 200
    with pytest.raises(DomainError) as error:
        await service.extend('run', {**original, 'reason': 'changed command'}, 'terminal-once')
    assert error.value.code == 'idempotency_conflict'
    current = await store.read('run', 'run')
    with pytest.raises(DomainError) as error:
        await service.extend('run', request(300, current['revision']), 'new-request')
    assert error.value.code == 'product_deleted'


async def test_terminal_grant_still_checks_shared_iteration_and_account_identity(limits):
    store, _, service, _ = limits
    run = await terminal_failure(store, 'cancelled')
    await patch(store, 'run', 'peer', {'iteration_id': 'iteration', 'execution_state': 'running'})
    await patch(store, 'attempt', 'peer-attempt', {'run_id': 'peer', 'status': 'running'})
    before = await budget_state(store)
    with pytest.raises(DomainError) as error:
        await service.extend('run', request(0, run['revision']), 'shared-active')
    assert error.value.code == 'active_attempts'
    assert await budget_state(store) == before
    await patch(store, 'attempt', 'peer-attempt', {'status': 'failed'})
    await patch(store, 'budget_account', account_id('iteration', 'iteration'), {'max_requests': 250})
    before = await budget_state(store)
    with pytest.raises(DomainError) as error:
        await service.extend('run', request(0, run['revision']), 'mismatched-account')
    assert error.value.code == 'budget_configuration_conflict'
    assert await budget_state(store) == before


async def test_terminal_owner_api_returns_same_state_and_does_not_wake_scheduler(limits):
    from types import SimpleNamespace
    store, _, _, settings = limits
    run = await terminal_failure(store, 'cancelled')
    wakeups = []
    app = create_app(settings, store=store, scheduler=SimpleNamespace(wake=lambda: wakeups.append(True)))
    token = app.state.tokens.issue('agentflow_owner', {'owner:*'}, 'owner', 60)
    headers = {'Authorization': 'Bearer ' + token, 'Origin': settings.origin, 'Idempotency-Key': 'terminal-http'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.origin) as client:
        response = await client.post('/api/v1/runs/run/request_limit', json=request(0, run['revision']), headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()['execution_state'] == 'cancelled'
        assert response.json()['budget_limit']['max_model_requests'] == 0
    assert wakeups == []
    assert not await store.list('run_recovery')

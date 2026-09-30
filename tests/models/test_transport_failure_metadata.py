"""Proxy-caught transport errors retain exact safe metadata and original accounting."""
import hashlib
import json

import httpx
import pytest

from agentflow.common import DomainError
from agentflow.models.service import ModelService


async def scoped_service(store, tmp_path, context, profile_factory, transport, *, protocol='responses'):
    async def authorize(token, requested_protocol):
        assert token == 'task-only-token' and requested_protocol == protocol
        return context
    client = httpx.AsyncClient(transport=transport, trust_env=False)
    service = ModelService(store, tmp_path, authorize, lambda _: 'PRIVATE_UPSTREAM_KEY', client)
    profile = await service.registry.register(profile_factory('http://127.0.0.1:1'), 'profile')
    await service.ledger.setup_accounts(context.run_id, context.iteration_id, 10000, 10000)
    def seed(tx):
        tx.put('attempt', context.attempt_id, {'run_id': context.run_id, 'iteration_id': context.iteration_id,
            'work_item_id': 'work', 'generation': 1, 'status': 'running', 'fencing_token': context.fencing_token,
            'input_fingerprint': context.input_fingerprint})
        tx.put('dispatch_context', context.attempt_id, {'task': {'attempt_id': context.attempt_id,
            'run_id': context.run_id, 'iteration_id': context.iteration_id, 'work_item_id': 'work',
            'profile_id': context.model_profile_id, 'fencing_token': context.fencing_token,
            'input_fingerprint': context.input_fingerprint,
            'step': 'implementation' if protocol == 'responses' else 'code_review'}})
        return tx.put('task_authorization', hashlib.sha256(b'task-only-token').hexdigest(),
                      {**context.model_dump(), 'expected_profile_revision': profile['revision']})
    await store.command('fixture', 'transport-binding', {}, seed)
    return service, client


@pytest.mark.parametrize('exception,kind,state,code', [
    (httpx.ConnectError, 'connect_error', 'released', 'model_transport_not_sent'),
    (httpx.ConnectTimeout, 'connect_timeout', 'uncertain', 'model_transport_connect_timeout'),
    (httpx.ReadTimeout, 'read_timeout', 'uncertain', 'model_transport_read_timeout'),
    (httpx.WriteTimeout, 'write_timeout', 'uncertain', 'model_transport_write_timeout'),
    (httpx.PoolTimeout, 'pool_timeout', 'uncertain', 'model_transport_pool_timeout'),
    (httpx.RemoteProtocolError, 'remote_protocol_error', 'uncertain', 'model_transport_protocol_error'),
    (httpx.LocalProtocolError, 'local_protocol_error', 'uncertain', 'model_transport_protocol_error'),
    (httpx.ReadError, 'read_error', 'uncertain', 'model_transport_connection_error'),
    (httpx.WriteError, 'write_error', 'uncertain', 'model_transport_connection_error'),
    (httpx.CloseError, 'close_error', 'uncertain', 'model_transport_connection_error'),
    (RuntimeError, 'other_error', 'uncertain', 'model_request_outcome_unknown'),
])
async def test_proxy_records_exact_closed_failure_at_send_without_replay(
        store, tmp_path, context, profile_factory, exception, kind, state, code):
    seen = []
    async def fail(request):
        seen.append(request)
        raise exception('PRIVATE_EXCEPTION https://private.example/path?secret=PRIVATE_UPSTREAM_KEY')
    service, client = await scoped_service(store, tmp_path, context, profile_factory, httpx.MockTransport(fail))
    try:
        with pytest.raises(DomainError):
            await service.forward('responses', {'model': 'fixture-model', 'input': 'fixture'}, 'task-only-token', 'one-request')
        invocation = (await store.list('model_invocation'))[0]
        assert invocation['state'] == state
        metadata = invocation.get('transport_failure')
        assert metadata is not None
        assert metadata['kind'] == kind and metadata['phase'] == 'send'
        assert metadata['delivery'] == ('not_sent' if state == 'released' else 'unknown')
        assert metadata['invocation_id'] == invocation['id']
        assert 'PRIVATE_' not in json.dumps(metadata) and 'private.example' not in json.dumps(metadata)
        from agentflow.models.transport_failures import transport_failure_for_attempt
        assert await transport_failure_for_attempt(store, context.attempt_id,
            fencing_token=context.fencing_token, input_fingerprint=context.input_fingerprint) == code
        before = {name: await store.list(name) for name in ('budget_account', 'model_attempt_budget', 'model_invocation')}
        with pytest.raises(DomainError):
            await service.forward('responses', {'model': 'fixture-model', 'input': 'fixture'}, 'task-only-token', 'one-request')
        assert {name: await store.list(name) for name in before} == before
        assert len(seen) == 1
        accounts = await store.list('budget_account')
        assert all(row['request_count'] == 1 and row['settled_micros'] == 0 for row in accounts)
        assert all(row['uncertain_micros'] == (invocation['amount_micros'] if state == 'uncertain' else 0) for row in accounts)
        assert invocation['actual_micros'] is None if state == 'uncertain' else invocation['actual_micros'] == 0
        assert not await store.list('model_uncertainty_acknowledgment')
    finally:
        await service.close()
        await client.aclose()


async def test_later_success_suppresses_a_prior_transport_failure(store, tmp_path, context, profile_factory):
    calls = []
    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError('PRIVATE_ERROR')
        return httpx.Response(200, json={'id': 'response', 'status': 'completed', 'model': 'fixture-model',
            'output': [], 'usage': {'input_tokens': 1, 'output_tokens': 1}})
    service, client = await scoped_service(store, tmp_path, context, profile_factory, httpx.MockTransport(handler))
    try:
        with pytest.raises(DomainError):
            await service.forward('responses', {'model': 'fixture-model', 'input': 'first'}, 'task-only-token', 'first')
        await service.forward('responses', {'model': 'fixture-model', 'input': 'later'}, 'task-only-token', 'later')
        from agentflow.models.transport_failures import transport_failure_for_attempt
        assert await transport_failure_for_attempt(store, context.attempt_id,
            fencing_token=context.fencing_token, input_fingerprint=context.input_fingerprint) is None
        assert len(calls) == 2 and sorted(row['request_ordinal'] for row in await store.list('model_invocation')) == [1, 2]
    finally:
        await service.close()
        await client.aclose()


@pytest.mark.parametrize('record,field,value', [
    ('model_invocation', 'run_id', 'other-run'),
    ('model_invocation', 'iteration_id', 'other-iteration'),
    ('model_invocation', 'fencing_token', 2),
    ('model_invocation', 'fencing_token', True),
    ('model_invocation', 'input_fingerprint', 'sha256:' + 'b' * 64),
    ('model_invocation', 'profile_id', 'other-profile'),
    ('model_invocation', 'profile_revision', 2),
    ('model_invocation', 'profile_revision', True),
    ('model_invocation', 'protocol', 'chat_completions'),
    ('model_invocation', 'operation_id', 'other-invocation'),
    ('model_invocation', 'request_ordinal', 2),
    ('model_invocation', 'request_ordinal', True),
    ('task_authorization', 'fencing_token', 2),
    ('task_authorization', 'fencing_token', True),
    ('task_authorization', 'input_fingerprint', 'sha256:' + 'b' * 64),
    ('task_authorization', 'expected_profile_revision', 2),
    ('task_authorization', 'protocols', ['chat_completions']),
    ('dispatch_context', 'run_id', 'other-run'),
    ('dispatch_context', 'fencing_token', True),
    ('dispatch_context', 'iteration_id', 'other-iteration'),
    ('dispatch_context', 'profile_id', 'other-profile'),
    ('dispatch_context', 'work_item_id', 'other-work'),
    ('dispatch_context', 'step', 'unknown-step'),
    ('attempt', 'status', 'completed'),
    ('attempt', 'fencing_token', 2),
    ('attempt', 'fencing_token', True),
    ('attempt', 'input_fingerprint', 'sha256:' + 'b' * 64),
    ('model_attempt_budget', 'request_count', 2),
    ('model_attempt_budget', 'attempt_id', 'other-attempt'),
])
async def test_reader_rejects_changed_frozen_binding(store, tmp_path, context, profile_factory, record, field, value):
    async def fail(request):
        raise httpx.ReadTimeout('PRIVATE_TIMEOUT')
    service, client = await scoped_service(store, tmp_path, context, profile_factory, httpx.MockTransport(fail))
    try:
        with pytest.raises(DomainError):
            await service.forward('responses', {'model': 'fixture-model', 'input': 'fixture'}, 'task-only-token')
        row = (await store.list(record))[0]
        def alter(tx):
            changed = {**row, field: value} if record != 'dispatch_context' else {
                **row, 'task': {**row['task'], field: value}}
            return tx.put(record, row['id'], changed, row['revision'])
        await store.command('fixture', 'changed-binding', {}, alter)
        from agentflow.models.transport_failures import transport_failure_for_attempt
        before = {kind: await store.list(kind) for kind in ('model_invocation', 'model_attempt_budget', 'budget_account')}
        assert await transport_failure_for_attempt(store, context.attempt_id,
            fencing_token=context.fencing_token, input_fingerprint=context.input_fingerprint) is None
        assert {kind: await store.list(kind) for kind in before} == before
    finally:
        await service.close()
        await client.aclose()


@pytest.mark.parametrize('field,value', [
    ('kind', 'ReadTimeout: PRIVATE_ERROR'), ('kind', []), ('phase', 'response_body'),
    ('delivery', 'not_sent'), ('invocation_id', 'other'), ('version', True),
    ('origin', 'worker'), ('observed_at', 'PRIVATE_ERROR'), ('observed_at', '2026-01-01T00:00:00'),
    ('raw_exception', 'PRIVATE_ERROR'),
])
async def test_reader_rejects_malformed_or_unbound_exception_metadata(store, tmp_path, context, profile_factory, field, value):
    async def fail(request):
        raise httpx.ReadTimeout('PRIVATE_TIMEOUT')
    service, client = await scoped_service(store, tmp_path, context, profile_factory, httpx.MockTransport(fail))
    try:
        with pytest.raises(DomainError):
            await service.forward('responses', {'model': 'fixture-model', 'input': 'fixture'}, 'task-only-token')
        row = (await store.list('model_invocation'))[0]
        await store.command('fixture', 'bad-metadata', {}, lambda tx: tx.put('model_invocation', row['id'], {
            **row, 'transport_failure': {**row['transport_failure'], field: value}}, row['revision']))
        from agentflow.models.transport_failures import transport_failure_for_attempt
        assert await transport_failure_for_attempt(store, context.attempt_id,
            fencing_token=context.fencing_token, input_fingerprint=context.input_fingerprint) is None
    finally:
        await service.close()
        await client.aclose()


@pytest.mark.parametrize('reason,count,expected', [
    ('dispatch_result_unknown', 1, 'model_request_outcome_unknown'),
    ('dispatch_result_unknown', 2, None),
    ('controller_restarted_during_dispatch', 1, None),
    ('response_incomplete_or_invalid', 1, None),
])
async def test_legacy_unknown_outcome_never_infers_timeout_from_elapsed_time(store, tmp_path, context, profile_factory, reason, count, expected):
    async def fail(request):
        raise httpx.ReadTimeout('PRIVATE_TIMEOUT')
    service, client = await scoped_service(store, tmp_path, context, profile_factory, httpx.MockTransport(fail))
    try:
        with pytest.raises(DomainError):
            await service.forward('responses', {'model': 'fixture-model', 'input': 'fixture'}, 'task-only-token')
        row = (await store.list('model_invocation'))[0]
        legacy = {key: value for key, value in row.items() if key not in {'transport_failure', 'request_ordinal'}}
        legacy.update(reason=reason, created_at='2026-01-01T00:00:00+00:00', updated_at='2026-01-01T00:03:05+00:00')
        await store.command('fixture', 'legacy', {}, lambda tx: tx.put('model_invocation', row['id'], legacy, row['revision']))
        budget = await store.read('model_attempt_budget', context.attempt_id)
        await store.command('fixture', 'legacy-count', {}, lambda tx: tx.put('model_attempt_budget', context.attempt_id,
            {**budget, 'request_count': count}, budget['revision']))
        from agentflow.models.transport_failures import transport_failure_for_attempt
        assert await transport_failure_for_attempt(store, context.attempt_id,
            fencing_token=context.fencing_token, input_fingerprint=context.input_fingerprint) == expected
    finally:
        await service.close()
        await client.aclose()


@pytest.mark.parametrize('ordering,expected', [
    ('ordinal', 'model_transport_not_sent'), ('duplicate_ordinal', None),
    ('legacy', 'model_transport_not_sent'), ('ambiguous_legacy', None),
    ('later_unknown_ordinal', None),
])
async def test_only_uniquely_last_invocation_can_refine_failure(store, tmp_path, context, profile_factory, ordering, expected):
    async def fail(request):
        raise httpx.ConnectError('PRIVATE_ERROR')
    service, client = await scoped_service(store, tmp_path, context, profile_factory, httpx.MockTransport(fail))
    try:
        for key in ('first', 'second'):
            with pytest.raises(DomainError):
                await service.forward('responses', {'model': 'fixture-model', 'input': key}, 'task-only-token', key)
        rows = sorted(await store.list('model_invocation'), key=lambda row: row['request_ordinal'])
        def alter(tx):
            for index, row in enumerate(rows):
                changed = {**row, 'created_at': f'2026-01-01T00:00:0{index}+00:00'}
                if ordering == 'duplicate_ordinal':
                    changed['request_ordinal'] = 2
                if ordering in {'legacy', 'ambiguous_legacy'}:
                    changed.pop('request_ordinal')
                if ordering == 'ambiguous_legacy':
                    changed['created_at'] = '2026-01-01T00:00:00+00:00'
                if ordering == 'later_unknown_ordinal' and index == 1:
                    changed.pop('request_ordinal')
                tx.put('model_invocation', row['id'], changed, row['revision'])
            return {}
        await store.command('fixture', 'ordering', {}, alter)
        from agentflow.models.transport_failures import transport_failure_for_attempt
        assert await transport_failure_for_attempt(store, context.attempt_id,
            fencing_token=context.fencing_token, input_fingerprint=context.input_fingerprint) == expected
    finally:
        await service.close()
        await client.aclose()


async def test_settlement_rejects_untrusted_metadata_and_preserves_first_failure(store, context):
    from agentflow.models.budget import BudgetLedger
    ledger = BudgetLedger(store)
    await ledger.setup_accounts(context.run_id, context.iteration_id, 10000, 10000)
    invocation = await ledger.reserve(context, protocol='responses', request_fingerprint='sha256:' + 'b' * 64,
        profile_revision=1, amount_micros=10, currency='USD')
    await ledger.dispatch(invocation['id'])
    before = {kind: await store.list(kind) for kind in ('budget_account', 'model_invocation', 'model_attempt_budget')}
    metadata = {'version': 1, 'origin': 'model_proxy', 'invocation_id': invocation['id'],
        'phase': 'send', 'kind': 'read_timeout', 'delivery': 'unknown', 'observed_at': '2026-01-01T00:00:01+00:00'}
    with pytest.raises(DomainError) as invalid:
        await ledger.uncertain(invocation['id'], 'dispatch_result_unknown',
                               transport_failure={**metadata, 'exception': 'PRIVATE_ERROR'})
    assert invalid.value.code == 'invalid_transport_failure'
    assert {kind: await store.list(kind) for kind in before} == before
    await ledger.uncertain(invocation['id'], 'dispatch_result_unknown', transport_failure=metadata)
    first = {kind: await store.list(kind) for kind in before}
    await ledger.uncertain(invocation['id'], 'dispatch_result_unknown',
                           transport_failure={**metadata, 'kind': 'write_timeout'})
    assert {kind: await store.list(kind) for kind in before} == first
    assert first['model_invocation'][0]['transport_failure']['kind'] == 'read_timeout'
    assert all(row['request_count'] == 1 and row['reserved_micros'] == row['uncertain_micros'] == 10
               for row in first['budget_account'])

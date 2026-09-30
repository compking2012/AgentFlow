"""Automatic timeout grants preserve stopped code and every historical meter."""
import asyncio
import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from test_model_uncertainty_acknowledgment import PROTECTED
from test_model_uncertainty_acknowledgment import unknown as unknown
from test_recovery import env as env
from test_recovery import patch

from agentflow.models.uncertainty import ACK_KIND, acknowledged_invocation_ids
from agentflow.runtime.launcher import atomic_json


@pytest_asyncio.fixture
async def timed_out(unknown):
    value = unknown
    await patch(value, 'run', 'run', execution_state='running')
    await patch(value, 'work_item', 'bad', status='failed')
    await patch(value, 'attempt', 'bad-attempt', status='failed', execution_status='failed')
    await patch(value, 'supervised_attempt', 'bad-attempt', state='failed', reason='timeout')
    atomic_json(value.process_directory / 'result.json', {**value.process_identity,
        'execution_status': 'failed', 'reason': 'timeout', 'finished_at': datetime.now(UTC).isoformat()})
    value.settings = value.settings.model_copy(update={"auto_timeout_retry_limit": 2})
    value.workflow.settings = value.settings
    return value


def service(value, models=True):
    from agentflow.control.timeout_recovery import TimeoutRecovery
    return TimeoutRecovery(value.store, value.workflow, value.service, value.models if models else None)


async def state(value):
    kinds = set(PROTECTED) | {'timeout_recovery', ACK_KIND}
    return {kind: await value.store.list(kind) for kind in kinds}


async def ack_state(value):
    result = await value.service._read('run')
    result['timeout_recovery'] = await value.store.list('timeout_recovery')
    return result


async def test_timeout_replenishes_one_attempt_without_erasing_usage_or_unknown_cost(timed_out):
    value = timed_out
    before = await state(value)
    worker = service(value)
    assert await worker.prepare('run', 'bad'), worker.last_blocker
    after = await state(value)
    budget = after['coding_work_budget'][0]
    assert budget['active_seconds'] == 709 and budget['observed_tool_calls'] == 120 and budget['step_count'] == 1
    assert budget['max_active_seconds'] == 2509 and budget['max_tool_calls'] == 220
    assert budget['max_steps'] == before['coding_work_budget'][0]['max_steps']
    for kind in set(PROTECTED) - {'coding_work_budget'}:
        assert after[kind] == before[kind], kind
    assert value.workspace.joinpath('src/keep.js').read_text() == 'export const valuable = 42;\n'
    audit = after['timeout_recovery'][0]
    assert audit['attempt_id'] == 'bad-attempt' and audit['retry_ordinal'] == 1
    assert audit['old_limits'] == {'max_active_seconds': 1800, 'max_tool_calls': 100}
    assert audit['new_limits'] == {'max_active_seconds': 2509, 'max_tool_calls': 220}
    assert audit['policy']['timeout_limit'] == 2
    ack = after[ACK_KIND][0]
    assert ack['actor'] == 'system' and ack['authorization_kind'] == 'timeout_retry_policy'
    assert ack['requires_explicit_retry'] is False and ack['timeout_recovery_id'] == audit['id']
    assert acknowledged_invocation_ids(await ack_state(value)) == {value.invocation_id}
    assert after['model_invocation'][0]['actual_micros'] is None and after['model_invocation'][0]['usage'] is None
    assert not await value.store.list('run_recovery')


async def test_timeout_prepare_is_concurrent_idempotent_and_survives_store_restart(timed_out):
    value = timed_out
    assert all(await asyncio.gather(*(service(value).prepare('run', 'bad') for _ in range(3))))
    before = await state(value)
    await value.store.close()
    await value.store.start()
    assert await service(value).prepare('run', 'bad')
    assert await state(value) == before
    assert len(before['timeout_recovery']) == len(before[ACK_KIND]) == 1


@pytest.mark.parametrize('condition', ['disabled', 'work_cap', 'run_cap', 'paused', 'cancelled',
    'wrong_reason', 'receipt_reason', 'missing_receipt', 'active_process', 'live_process',
    'active_transport', 'unknown_transport', 'missing_checkpoint', 'write_scope', 'fence',
    'usage_unknown', 'steps_exhausted', 'strict', 'other_unknown', 'uncounted', 'old_disconnect'])
async def test_unsafe_or_unauthorized_timeout_changes_nothing(timed_out, condition):
    value = timed_out
    if condition == 'disabled':
        value.workflow.settings = value.settings.model_copy(update={"auto_timeout_retry_limit": 0})
    elif condition in {'work_cap', 'run_cap'}:
        value.workflow.settings = value.settings.model_copy(update={
            'auto_failure_retry_limit' if condition == 'work_cap' else 'auto_failure_run_limit': 1})
        await patch(value, 'failure_analysis', 'prior', run_id='run', work_item_id='bad' if condition == 'work_cap' else 'sibling',
                    attempt_id='previous-attempt', status='repair_scheduled')
    elif condition in {'paused', 'cancelled'}:
        await patch(value, 'run', 'run', execution_state=condition)
    elif condition == 'wrong_reason':
        await patch(value, 'supervised_attempt', 'bad-attempt', reason='log_limit')
    elif condition == 'receipt_reason':
        atomic_json(value.process_directory / 'result.json', {**value.process_identity,
            'execution_status': 'failed', 'reason': 'log_limit'})
    elif condition == 'missing_receipt':
        value.process_directory.joinpath('result.json').unlink()
    elif condition == 'active_process':
        await patch(value, 'supervised_attempt', 'bad-attempt', state='execution_unknown')
    elif condition == 'live_process':
        import os

        import psutil
        identity = {**value.process_identity, 'pid': os.getpid(), 'process_started_at': psutil.Process().create_time()}
        atomic_json(value.process_directory / 'result.json', {**identity, 'execution_status': 'failed', 'reason': 'timeout'})
        await patch(value, 'supervised_attempt', 'bad-attempt', **identity)
    elif condition == 'active_transport':
        value.models._active[value.invocation_id] = object()
    elif condition == 'unknown_transport':
        value.models = None
    elif condition == 'missing_checkpoint':
        value.workspace.rename(value.workspace.with_name('gone'))
    elif condition == 'write_scope':
        context = await value.store.read('dispatch_context', 'bad-attempt')
        await patch(value, 'dispatch_context', 'bad-attempt', task={**context['task'], 'allowed_write_paths': ['elsewhere']})
    elif condition == 'fence':
        await patch(value, 'work_item', 'bad', fencing_token=77)
    elif condition == 'usage_unknown':
        await patch(value, 'coding_step_usage', 'bad-attempt', known=False)
    elif condition == 'steps_exhausted':
        await patch(value, 'coding_work_budget', value.budget_id, step_count=value.settings.max_coding_steps)
    elif condition == 'strict':
        run = await value.store.read('run', 'run')
        await patch(value, 'run', 'run', budget_limit={**run['budget_limit'], 'cost_mode': 'strict'})
    elif condition == 'other_unknown':
        await patch(value, 'model_invocation', value.invocation_id, reason='malformed_response')
    elif condition == 'uncounted':
        await patch(value, 'model_attempt_budget', 'bad-attempt', request_count=0)
    elif condition == 'old_disconnect':
        await patch(value, 'model_invocation', value.invocation_id, updated_at='2020-01-01T00:00:00+00:00')
    before = await state(value)
    worker = service(value)
    assert not await worker.prepare('run', 'bad')
    assert worker.last_blocker
    assert await state(value) == before


async def test_mutations_during_checkpoint_validation_never_partially_authorize(timed_out, monkeypatch):
    value = timed_out
    original = value.service._checkpoints
    async def race(*args, **kwargs):
        result = await original(*args, **kwargs)
        await patch(value, 'work_item', 'bad', generation=2)
        return result
    monkeypatch.setattr(value.service, '_checkpoints', race)
    before = await value.store.read('coding_work_budget', value.budget_id)
    assert not await service(value).prepare('run', 'bad')
    assert await value.store.read('coding_work_budget', value.budget_id) == before
    assert not await value.store.list(ACK_KIND) and not await value.store.list('timeout_recovery')


@pytest.mark.parametrize('damage', ['actor_only', 'missing_record', 'wrong_basis', 'wrong_policy', 'wrong_attempt'])
async def test_system_actor_alone_or_tampered_timeout_binding_cannot_accept_unknown(timed_out, damage):
    value = timed_out
    assert await service(value).prepare('run', 'bad')
    current = await ack_state(value)
    if damage in {'actor_only', 'missing_record'}:
        current['timeout_recovery'] = []
    elif damage == 'wrong_basis':
        current[ACK_KIND][0]['basis']['fencing_token'] = 9
    elif damage == 'wrong_policy':
        current['timeout_recovery'][0]['policy']['timeout_limit'] = 0
    else:
        current['timeout_recovery'][0]['attempt_id'] = 'different-attempt'
    assert acknowledged_invocation_ids(current) == set()


@pytest.mark.parametrize(('field', 'replacement'), [
    ('process_birth_source', None), ('process_birth_fingerprint', None),
    ('process_birth_source', 'linux_proc_start_ticks'),
    ('process_birth_fingerprint', 'sha256:' + 'f' * 64),
])
async def test_timeout_acknowledgment_cannot_downgrade_or_change_birth_identity(timed_out, field, replacement):
    from agentflow.common import canonical_digest
    from agentflow.models.uncertainty import timeout_authorization_digest
    value = timed_out
    birth = {'process_birth_source': 'macos_proc_bsdinfo',
             'process_birth_fingerprint': canonical_digest('original process birth')}
    await patch(value, 'supervised_attempt', 'bad-attempt', **birth)
    path = value.process_directory / 'result.json'
    atomic_json(path, {**json.loads(path.read_text()), **birth})
    worker = service(value)
    assert await worker.prepare('run', 'bad'), worker.last_blocker
    current = await ack_state(value)
    assert acknowledged_invocation_ids(current) == {value.invocation_id}
    grant = current['timeout_recovery'][0]
    if replacement is None:
        grant['stop_receipt'].pop(field)
    else:
        grant['stop_receipt'][field] = replacement
    # Rehash the changed receipt to exercise semantic process identity binding,
    # independently of the grant's ordinary content-digest checks.
    grant['stop_receipt_digest'] = canonical_digest(grant['stop_receipt'])
    grant['authorization_digest'] = timeout_authorization_digest(grant)
    current[ACK_KIND][0]['authorization_digest'] = grant['authorization_digest']
    assert acknowledged_invocation_ids(current) == set()


async def test_retry_count_combines_reservations_and_scheduled_retries_without_double_counting(timed_out):
    from agentflow.control.timeout_recovery import retry_counts
    scheduled = [{'id': 'a', 'run_id': 'run', 'work_item_id': 'bad', 'attempt_id': 'old', 'status': 'repair_scheduled'},
                 {'id': 'b', 'run_id': 'run', 'work_item_id': 'sibling', 'attempt_id': 'sibling-old', 'status': 'repair_scheduled'}]
    grants = [{'id': 't', 'run_id': 'run', 'work_item_id': 'bad', 'attempt_id': 'old'},
              {'id': 'u', 'run_id': 'run', 'work_item_id': 'bad', 'attempt_id': 'pending'}]
    assert retry_counts(scheduled, grants, 'run', 'bad') == {'work': 2, 'run': 3, 'timeouts_work': 2}
    assert retry_counts(scheduled, grants, 'run', 'bad', exclude_attempt_id='pending') == {'work': 1, 'run': 2, 'timeouts_work': 1}


@pytest.mark.parametrize('manual_kind', [ACK_KIND, 'work_execution_budget_adjustment'])
async def test_explicit_manual_retry_boundary_does_not_receive_partial_automatic_grant(timed_out, manual_kind):
    value = timed_out
    await patch(value, manual_kind, 'manual-decision', run_id='run', work_item_id='bad',
                work_generation=1, requires_explicit_retry=True)
    before = await state(value)
    assert not await service(value).prepare('run', 'bad')
    assert await state(value) == before


async def test_existing_grant_cannot_authorize_a_different_original_attempt_identity(timed_out):
    value = timed_out
    assert await service(value).prepare('run', 'bad')
    await patch(value, 'work_item', 'bad', fencing_token=2)
    await patch(value, 'attempt', 'bad-attempt', fencing_token=2)
    before = await state(value)
    assert not await service(value).prepare('run', 'bad')
    assert await state(value) == before


async def test_timeout_specific_cap_counts_unconsumed_grants(timed_out):
    value = timed_out
    value.workflow.settings = value.settings.model_copy(update={'auto_timeout_retry_limit': 1})
    await patch(value, 'timeout_recovery', 'prior-timeout', run_id='run', work_item_id='bad', attempt_id='prior')
    before = await state(value)
    assert not await service(value).prepare('run', 'bad')
    assert await state(value) == before


async def test_no_unknown_call_needs_no_model_service_or_new_acknowledgment(timed_out):
    value = timed_out
    await value.ledger.complete_unpriced(value.invocation_id,
        {'input_tokens': 10, 'output_tokens': 2}, {'response_id': 'fixture'})
    assert await service(value, models=False).prepare('run', 'bad')
    assert not await value.store.list(ACK_KIND)
    assert (await value.store.read('coding_work_budget', value.budget_id))['max_tool_calls'] == 220


async def test_larger_existing_allowance_is_not_replaced_or_charged_again(timed_out):
    value = timed_out
    before = await patch(value, 'coding_work_budget', value.budget_id, max_active_seconds=5000, max_tool_calls=500)
    assert await service(value).prepare('run', 'bad')
    assert await value.store.read('coding_work_budget', value.budget_id) == before


@pytest.mark.parametrize('unknown', [2], indirect=True)
async def test_every_timeout_uncertain_request_remains_counted_and_individually_bound(timed_out):
    value = timed_out
    before = await value.store.list('model_invocation')
    assert await service(value).prepare('run', 'bad')
    assert acknowledged_invocation_ids(await ack_state(value)) == set(value.invocation_ids)
    assert len(await value.store.list(ACK_KIND)) == 2
    assert await value.store.list('model_invocation') == before
    budget = await value.store.read('model_attempt_budget', 'bad-attempt')
    assert budget['request_count'] == budget['uncertain_invocations'] == 2


@pytest.mark.parametrize('damage', ['process_run', 'dispatch_control', 'missing_dispatch_control'])
async def test_grant_requires_same_run_and_original_frozen_coding_control(timed_out, damage):
    value = timed_out
    if damage == 'process_run':
        await patch(value, 'supervised_attempt', 'bad-attempt', run_id='different-run')
    else:
        context = await value.store.read('dispatch_context', 'bad-attempt')
        task = dict(context['task'])
        if damage == 'dispatch_control':
            task['coding_step'] = {**task['coding_step'], 'max_tool_calls': 9999}
        else:
            task.pop('coding_step')
        await patch(value, 'dispatch_context', 'bad-attempt', task=task)
    before = await state(value)
    assert not await service(value).prepare('run', 'bad')
    assert await state(value) == before


async def test_fresh_recovery_retains_system_acknowledgment_and_original_code(timed_out):
    value = timed_out
    assert await service(value).prepare('run', 'bad')
    before = await value.store.list('model_invocation')
    run = await value.store.read('run', 'run')
    receipt = await value.service.recover('run', {'expected_revision': run['revision'], 'mode': 'retry',
        'work_item_id': 'bad'}, 'fresh-after-timeout')
    assert receipt['execution'] == 'fresh_attempt'
    assert (await value.store.read('work_item', 'bad'))['generation'] == 2
    assert acknowledged_invocation_ids(await ack_state(value)) == set(value.invocation_ids)
    assert await value.store.list('model_invocation') == before
    assert not any(row['code'] == 'recovery_budget_uncertain'
                   for row in value.service._common_blockers(await value.service._read('run')))
    assert value.workspace.joinpath('src/keep.js').read_text() == 'export const valuable = 42;\n'


async def test_historical_work_without_failure_code_uses_verified_attempt_and_receipt(timed_out):
    value = timed_out
    await patch(value, 'work_item', 'bad', runtime_failure_code=None)
    worker = service(value)
    assert await worker.prepare('run', 'bad'), worker.last_blocker
    assert (await value.store.read('coding_work_budget', value.budget_id))['max_tool_calls'] == 220

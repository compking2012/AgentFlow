"""Parallel stopped timeouts reserve their retries together without forgiving usage."""
import asyncio
import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from test_recovery import patch
from test_timeout_recovery import ack_state, service, state
from test_timeout_recovery import env as env
from test_timeout_recovery import timed_out as timed_out
from test_timeout_recovery import unknown as unknown

from agentflow.common import canonical_digest
from agentflow.control.coding_steps import CodingSteps
from agentflow.control.failure_remediation import FailureRemediation
from agentflow.models.uncertainty import ACK_KIND, acknowledged_invocation_ids
from agentflow.runtime.launcher import atomic_json
from agentflow.runtime.workspace import WorkspaceManager


@pytest_asyncio.fixture
async def parallel_timeouts(timed_out):
    value = timed_out
    run = await value.store.read('run', 'run')
    original = await value.store.read('work_item', 'bad')
    work = await patch(value, 'work_item', 'peer',
        **{key: item for key, item in original.items() if key not in {'id', 'revision', 'attempt_id', 'status'}},
        attempt_id='peer-attempt', status='running')
    original_attempt = await value.store.read('attempt', 'bad-attempt')
    attempt = await patch(value, 'attempt', 'peer-attempt',
        **{key: item for key, item in original_attempt.items() if key not in {'id', 'revision', 'work_item_id', 'status'}},
        work_item_id='peer', status='running')
    coding = CodingSteps(value.store, value.settings, value.service.repository)
    control = await coding.prepare(run, work, attempt, value.project['base_commit'], 65536)
    workspace = await WorkspaceManager(value.settings.data_dir).create_clone(
        value.tmp_path / 'project', value.project['base_commit'], 'peer-attempt')
    (workspace / 'src').mkdir()
    (workspace / 'src/peer.js').write_text('export const peer = 15;\n')
    original_task = (await value.store.read('dispatch_context', 'bad-attempt'))['task']
    task = {**original_task, 'work_item_id': 'peer', 'attempt_id': 'peer-attempt',
            'workspace': str(workspace), 'coding_step': control}
    await patch(value, 'dispatch_context', 'peer-attempt', task=task)
    await coding.account(task, {'active_seconds': 1800, 'observed_tool_calls': 60,
                               'tool_observation_complete': True})
    await patch(value, 'work_item', 'peer', status='failed')
    await patch(value, 'attempt', 'peer-attempt', status='failed')
    context = value.context.model_copy(update={'attempt_id': 'peer-attempt'})
    invocation = await value.ledger.reserve(context, protocol='responses', request_fingerprint='peer-call',
        profile_revision=1, amount_micros=0, currency='USD', idempotency_key='peer-call')
    await value.ledger.dispatch(invocation['id'])
    await value.ledger.uncertain(invocation['id'], 'consumer_disconnected')
    directory = value.settings.data_dir / 'supervisor' / canonical_digest({'attempt_id': 'peer-attempt'}).split(':')[1]
    directory.mkdir(parents=True, mode=0o700)
    identity = {**value.process_identity, 'attempt_id': 'peer-attempt', 'nonce': 'peer-nonce', 'pid': 1073741823}
    atomic_json(directory / 'result.json', {**identity, 'execution_status': 'failed', 'reason': 'timeout',
                                           'finished_at': datetime.now(UTC).isoformat()})
    await patch(value, 'supervised_attempt', 'peer-attempt', **identity, state='failed', reason='timeout',
                run_id='run', directory=str(directory), input_fingerprint=work['input_fingerprint'])
    value.peer = {'invocation_id': invocation['id'], 'budget_id': control['budget_id'],
                  'workspace': workspace, 'directory': directory}
    return value


async def test_parallel_prepare_is_atomic_concurrent_and_durable_without_rewriting_history(parallel_timeouts):
    value = parallel_timeouts
    before = await state(value)
    workers = [service(value) for _ in range(4)]
    results = await asyncio.gather(*(worker.prepare('run', work_id)
        for worker, work_id in zip(workers, ['bad', 'peer', 'peer', 'bad'], strict=True)))
    assert all(results), [worker.last_blocker for worker in workers]
    after = await state(value)
    assert len(after['timeout_recovery']) == len(after[ACK_KIND]) == 2
    assert acknowledged_invocation_ids(await ack_state(value)) == {value.invocation_id, value.peer['invocation_id']}
    for kind in before.keys() - {'coding_work_budget', 'timeout_recovery', ACK_KIND}:
        assert after[kind] == before[kind], kind
    budgets = {row['work_item_id']: row for row in after['coding_work_budget']}
    assert (budgets['bad']['max_active_seconds'], budgets['bad']['max_tool_calls']) == (2509, 220)
    assert (budgets['peer']['max_active_seconds'], budgets['peer']['max_tool_calls']) == (3600, 160)
    for work_id in ('bad', 'peer'):
        original = next(row for row in before['coding_work_budget'] if row['work_item_id'] == work_id)
        for field in ('active_seconds', 'observed_tool_calls', 'step_count', 'max_steps'):
            assert budgets[work_id][field] == original[field]
    await value.store.close()
    await value.store.start()
    assert await service(value).prepare('run', 'peer')
    assert await service(value).prepare('run', 'bad')
    assert await state(value) == after


async def test_both_reserved_retries_survive_first_normal_recovery_at_exact_run_cap(parallel_timeouts):
    value = parallel_timeouts
    value.workflow.settings = value.settings.model_copy(update={
        'auto_failure_retry_delay_seconds': 0, 'auto_failure_run_limit': 2})
    controller = FailureRemediation(value.store, value.workflow, recovery=value.service, models=value.models)
    original_calls = await value.store.list('model_invocation')
    first = await controller.repair('bad')
    assert first['status'] == 'repair_scheduled', first['blockers']
    assert await service(value).prepare('run', 'peer')
    second = await controller.repair('peer')
    assert second['status'] == 'repair_scheduled', second['blockers']
    assert len(await value.store.list('run_recovery')) == 2
    assert len(await value.store.list('timeout_recovery')) == 2
    for work_id in ('bad', 'peer'):
        work = await value.store.read('work_item', work_id)
        assert work['generation'] == 2 and work['status'] == 'pending' and work['approval_required']
        assert work['payload']['recovery_checkpoint_id']
    assert await value.store.list('model_invocation') == original_calls
    assert acknowledged_invocation_ids(await ack_state(value)) == {value.invocation_id, value.peer['invocation_id']}
    assert value.workspace.joinpath('src/keep.js').read_text() == 'export const valuable = 42;\n'
    assert value.peer['workspace'].joinpath('src/peer.js').read_text() == 'export const peer = 15;\n'


@pytest.mark.parametrize('damage', ['run_capacity', 'peer_work_cap', 'peer_timeout_cap', 'non_timeout',
    'non_timeout_unknown', 'active_consumer', 'active_work', 'wrong_attempt', 'strict', 'uncounted',
    'missing_receipt', 'old_disconnect', 'missing_workspace', 'unknown_usage', 'manual_retry',
    'approval', 'dependent_peer'])
async def test_invalid_peer_never_partially_acknowledges_or_extends_either_task(parallel_timeouts, damage):
    value = parallel_timeouts
    if damage == 'run_capacity':
        value.workflow.settings = value.settings.model_copy(update={'auto_failure_run_limit': 1})
    elif damage in {'peer_work_cap', 'peer_timeout_cap'}:
        value.workflow.settings = value.settings.model_copy(update={
            'auto_failure_retry_limit' if damage == 'peer_work_cap' else 'auto_timeout_retry_limit': 1})
        await patch(value, 'failure_analysis', 'prior-peer', run_id='run', work_item_id='peer',
                    attempt_id='prior-peer-attempt', status='repair_scheduled', failure_code='worker_timeout')
    elif damage == 'non_timeout':
        await patch(value, 'attempt', 'peer-attempt', runtime_failure_code='worker_exited')
    elif damage == 'non_timeout_unknown':
        await patch(value, 'model_invocation', value.peer['invocation_id'], reason='malformed_response')
    elif damage == 'active_consumer':
        value.models._active[value.peer['invocation_id']] = object()
    elif damage == 'active_work':
        await patch(value, 'work_item', 'peer', status='running')
    elif damage == 'wrong_attempt':
        await patch(value, 'work_item', 'peer', attempt_id=None)
    elif damage == 'strict':
        await patch(value, 'model_invocation', value.peer['invocation_id'], cost_mode='strict')
    elif damage == 'uncounted':
        await patch(value, 'model_attempt_budget', 'peer-attempt', request_count=0)
    elif damage == 'missing_receipt':
        value.peer['directory'].joinpath('result.json').unlink()
    elif damage == 'old_disconnect':
        await patch(value, 'model_invocation', value.peer['invocation_id'], updated_at='2020-01-01T00:00:00+00:00')
    elif damage == 'missing_workspace':
        value.peer['workspace'].rename(value.peer['workspace'].with_name('gone'))
    elif damage == 'unknown_usage':
        await patch(value, 'coding_step_usage', 'peer-attempt', known=False)
    elif damage == 'manual_retry':
        await patch(value, ACK_KIND, 'manual-peer', run_id='run', work_item_id='peer',
                    work_generation=1, requires_explicit_retry=True)
    elif damage == 'approval':
        await patch(value, 'work_item', 'after', dependencies=['peer'], status='waiting_approval')
    elif damage == 'dependent_peer':
        await patch(value, 'work_item', 'peer', dependencies=['bad'])
    before = await state(value)
    worker = service(value)
    assert not await worker.prepare('run', 'bad')
    assert worker.last_blocker
    assert await state(value) == before


@pytest.mark.parametrize('race', ['receipt', 'source', 'consumer'])
async def test_group_writer_rechecks_every_peer_before_any_grant(parallel_timeouts, monkeypatch, race):
    value = parallel_timeouts
    original = value.store.command
    changed = False
    async def mutate(scope, key, body, handler):
        nonlocal changed
        if scope == 'timeout.retry.prepare' and not changed:
            changed = True
            if race == 'receipt':
                path = value.peer['directory'] / 'result.json'
                atomic_json(path, {**json.loads(path.read_text()), 'reason': 'log_limit'})
            elif race == 'source':
                value.peer['workspace'].joinpath('src/peer.js').write_text('changed during validation\n')
            else:
                value.models._active[value.peer['invocation_id']] = object()
        return await original(scope, key, body, handler)
    monkeypatch.setattr(value.store, 'command', mutate)
    before = await state(value)
    assert not await service(value).prepare('run', 'bad')
    assert changed
    assert await state(value) == before


@pytest.mark.parametrize('field', ['finished_at', 'updated_at', 'dispatch_started_at'])
async def test_malformed_peer_timestamp_blocks_group_without_escaping_prepare(parallel_timeouts, field):
    value = parallel_timeouts
    if field == 'finished_at':
        path = value.peer['directory'] / 'result.json'
        atomic_json(path, {**json.loads(path.read_text()), field: 123})
    else:
        await patch(value, 'model_invocation', value.peer['invocation_id'], **{field: 123})
    before = await state(value)
    worker = service(value)
    assert not await worker.prepare('run', 'bad')
    assert worker.last_blocker['code'] == 'timeout_evidence_invalid'
    assert await state(value) == before


async def test_automatic_repair_explains_peer_that_prevents_timeout_authorization(parallel_timeouts):
    value = parallel_timeouts
    value.workflow.settings = value.settings.model_copy(update={
        'auto_failure_retry_delay_seconds': 0, 'auto_failure_run_limit': 1})
    controller = FailureRemediation(value.store, value.workflow, recovery=value.service, models=value.models)
    result = await controller.repair('bad')
    assert result['status'] == 'blocked'
    assert any(row['code'] == 'automatic_timeout_retry_limit' for row in result['blockers'])
    assert not await value.store.list('timeout_recovery')

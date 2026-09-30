"""Existing timeout policy extends a delegated allowance and its owner atomically."""

import pytest
from test_model_uncertainty_acknowledgment import unknown as unknown
from test_recovery import env as env
from test_recovery import patch
from test_timeout_recovery import service, state
from test_timeout_recovery import timed_out as timed_out

from agentflow.common import DomainError, canonical_digest
from agentflow.control.coding_steps import CodingSteps


async def delegate_timeout(value):
    def seed(tx):
        work = tx.get('work_item', 'bad')
        owner = tx.put('work_item', 'original-owner', {**{k: v for k, v in work.items() if k not in {'id', 'revision'}},
            'key': 'original-owner', 'status': 'completed',
            'attempt_id': None, 'payload': {}, 'quality_result': 'passed'})
        source = tx.put('code_snapshot', 'review-original', {'run_id': 'run', 'work_item_id': owner['id'],
            'repository_path': value.project['local_path'], 'commit_oid': value.project['base_commit'],
            'base_oid': value.project['base_commit'], 'generation': 1, 'parent_commit_oids': [],
            'tree_oid': value.service.repository._integrity(value.workspace, value.project['base_commit']), 'stale': False})
        context = {'source_snapshot_id': source['id'], 'snapshot': source, 'accepted_documents': [],
            'owners': [{'work_item_id': owner['id'], 'step': owner['step'], 'write_paths': owner['write_paths'],
                'planned_write_paths': owner['write_paths'], 'work_version': owner}]}
        work = tx.put('work_item', work['id'], {**work, 'payload': {'review_contract_task': 'timeout-batch',
            'review_contract_kind': 'production_fix', 'review_contract_owner': owner['id'], 'review_contract_actions': []}}, work['revision'])
        tx.put('review_contract_repair', 'timeout-batch', {'actor': 'controller', 'run_id': 'run',
            'state': 'repairing', 'context': context, 'context_digest': canonical_digest(context),
            'work_specs': {work['id']: work}})
        budget = tx.get('coding_work_budget', value.budget_id)
        pool_id = CodingSteps.budget_id('run', owner['id'])
        pool = tx.put('coding_work_budget', pool_id, {**{k: v for k, v in budget.items() if k not in {'id', 'revision'}},
            'work_item_id': owner['id']})
        tx.put('coding_work_budget', budget['id'], {**budget, 'review_contract_owner_budget': pool_id,
            'review_contract_batch': 'timeout-batch'}, budget['revision'])
        tx.put('review_contract_budget_charge', work['attempt_id'], {'run_id': 'run', 'batch_id': 'timeout-batch',
            'work_item_id': work['id'], 'owner_work_item_id': owner['id'], 'owner_budget_id': pool_id,
            'known': True, 'active_seconds': 709, 'observed_tool_calls': 120})
        return pool
    return await value.store.command('fixture.delegated-timeout', 'seed', {}, seed)


async def test_timeout_extends_child_and_original_owner_by_same_increment_without_reset(timed_out):
    value = timed_out
    original = await delegate_timeout(value)
    before = await state(value)
    worker = service(value)
    assert await worker.prepare('run', 'bad'), worker.last_blocker
    pool = await value.store.read('coding_work_budget', original['id'])
    child = await value.store.read('coding_work_budget', value.budget_id)
    assert pool['max_active_seconds'] == child['max_active_seconds'] == 2509
    assert pool['max_tool_calls'] == child['max_tool_calls'] == 220
    for key in ('active_seconds', 'observed_tool_calls', 'step_count', 'max_steps'):
        assert pool[key] == original[key]
    audit = (await value.store.list('timeout_recovery'))[0]
    binding = audit['owner_budget']
    assert binding['budget_id'] == pool['id'] and binding['work_item_id'] == 'original-owner'
    assert binding['old_limits'] == {'max_active_seconds': 1800, 'max_tool_calls': 100}
    assert binding['new_limits'] == {'max_active_seconds': 2509, 'max_tool_calls': 220}
    assert binding['preserved_usage']['active_seconds'] == 709
    after = await state(value)
    for kind in ('model_invocation', 'model_attempt_budget', 'budget_account', 'work_item', 'attempt', 'approval'):
        assert after[kind] == before[kind]
    assert await worker.prepare('run', 'bad'), worker.last_blocker
    assert await state(value) == after


@pytest.mark.parametrize('damage', ['uncertain', 'foreign_pool', 'owner_scope', 'steps_exhausted'])
async def test_timeout_refuses_invalid_original_owner_without_partial_budget_or_ack(timed_out, damage):
    value = timed_out
    pool = await delegate_timeout(value)
    if damage == 'uncertain':
        await patch(value, 'coding_work_budget', pool['id'], uncertain=True)
    elif damage == 'foreign_pool':
        await patch(value, 'coding_work_budget', pool['id'], work_item_id='different-owner')
    elif damage == 'owner_scope':
        await patch(value, 'work_item', 'original-owner', write_paths=['elsewhere'])
    else:
        await patch(value, 'coding_work_budget', pool['id'], step_count=pool['max_steps'])
    before = await state(value)
    assert not await service(value).prepare('run', 'bad')
    assert await state(value) == before


async def test_timeout_owner_write_failure_rolls_back_child_limit_and_uncertainty_ack(timed_out, monkeypatch):
    from agentflow.storage.store import Transaction
    value = timed_out
    pool = await delegate_timeout(value)
    before = await state(value)
    original = Transaction.put
    def fail(tx, kind, identity, body, *args, **kwargs):
        if kind == 'coding_work_budget' and identity == pool['id']:
            raise DomainError('fixture_owner_write_failure', 'Original owner allowance write failed')
        return original(tx, kind, identity, body, *args, **kwargs)
    monkeypatch.setattr(Transaction, 'put', fail)
    worker = service(value)
    assert not await worker.prepare('run', 'bad')
    assert worker.last_blocker['code'] == 'fixture_owner_write_failure'
    assert await state(value) == before

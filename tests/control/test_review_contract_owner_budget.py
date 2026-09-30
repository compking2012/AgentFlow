"""Delegated repairs reserve and replenish the original author's shared allowance."""
import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from test_review_contract_repair import schedule, setup_batch
from test_workflow import flow as flow

from agentflow.common import DomainError, canonical_digest
from agentflow.control.coding_steps import CodingSteps


async def mixed_owner_batch(flow):
    _, result = await setup_batch(flow)
    def evidence(tx):
        batch = tx.get('review_contract_repair', 'batch')
        context = deepcopy(batch['context'])
        owner = tx.get('work_item', 'api-tests')
        tx.put('work_item', owner['id'], {**owner, 'write_paths': ['tests/api.spec.mjs', 'tests/status.test.mjs']}, owner['revision'])
        context['owners'][0]['write_paths'] = ['tests/api.spec.mjs', 'tests/status.test.mjs']
        context['test_paths'] = ['tests/api.spec.mjs', 'tests/web.spec.mjs', 'tests/status.test.mjs']
        artifact = tx.put('artifact', 'test-plan', {'run_id': 'run', 'digest': 'plan-digest', 'stale': False})
        context['accepted_documents'] = [{'artifact_id': artifact['id'], 'digest': artifact['digest'],
            'revision': artifact['revision'], 'step': 'integration_test_strategy', 'text': 'Assert status.'}]
        context['accepted_requirements'].append({'artifact_id': 'test-plan', 'requirement_id': 'IT1',
            'step': 'integration_test_strategy', 'text': 'Assert status.'})
        context['findings'].append({**context['findings'][0], 'finding_id': 'status-finding'})
        result['actions'].append({'finding_id': 'status-finding', 'classification': 'test_coverage_extension',
            'owner_work_item_id': 'api-tests', 'repair_paths': ['tests/status.test.mjs'],
            'evidence_paths': ['tests/api.spec.mjs'], 'migrations': [], 'reason': 'Missing status coverage.',
            'requirement_refs': [{'artifact_id': 'test-plan', 'requirement_id': 'IT1', 'quote': 'Assert status.'}]})
        return tx.put('review_contract_repair', batch['id'], {**batch, 'context': context,
            'context_digest': canonical_digest(context)}, batch['revision'])
    await flow[1].command('fixture.owner-evidence', 'mixed', {}, evidence)
    await schedule(flow, result)
    batch = await flow[1].read('review_contract_repair', 'batch')
    work = [await flow[1].read('work_item', key) for key in batch['repair_work_item_ids']]
    return batch, work


async def running_action(flow, work):
    identity = str(uuid4())
    def claim(tx):
        current = tx.get('work_item', work['id'])
        current = tx.put('work_item', work['id'], {**current, 'status': 'running',
            'fencing_token': current['fencing_token'] + 1, 'attempt_id': identity}, current['revision'])
        attempt = tx.put('attempt', identity, {key: current[key] for key in
            ('run_id', 'generation', 'fencing_token', 'input_fingerprint')} | {
            'work_item_id': current['id'], 'status': 'running'})
        return {'run': tx.get('run', 'run'), 'work_item': current, 'attempt': attempt}
    return await flow[1].command('fixture.owner-claim', identity, {}, claim)


async def owner_pool(flow, **fields):
    identity = CodingSteps.budget_id('run', 'api-tests')
    def update(tx):
        budget = tx.get('coding_work_budget', identity)
        return tx.put('coding_work_budget', identity, {**budget, **fields}, budget['revision'])
    return await flow[1].command('fixture.owner-pool', str(uuid4()), fields, update)


async def test_same_owner_kinds_execute_in_order_while_independent_owners_remain_parallel(flow):
    _, work = await mixed_owner_batch(flow)
    own = sorted((w for w in work if w['payload']['review_contract_owner'] == 'api-tests'),
                 key=lambda w: w['payload']['review_contract_kind'])
    assert own[0]['dependencies'] == ['triage']
    assert own[1]['dependencies'] == ['triage', own[0]['id']]
    assert all(w['dependencies'] == ['triage'] for w in work if w not in own)


@pytest.mark.parametrize('change, code', [({'uncertain': True}, 'coding_budget_uncertain'),
    ({'max_active_seconds': 0}, 'coding_budget_exhausted'),
    ({'max_tool_calls': 0}, 'coding_budget_exhausted'),
    ({'max_steps': 0}, 'coding_budget_exhausted')])
async def test_delegated_prepare_refuses_exhausted_or_unknown_owner_pool(flow, change, code):
    _, work = await mixed_owner_batch(flow)
    action = next(w for w in work if w['payload']['review_contract_owner'] == 'api-tests')
    await owner_pool(flow, **change)
    claim = await running_action(flow, action)
    coding = CodingSteps(flow[1], flow[0].settings, None)
    with pytest.raises(DomainError) as caught:
        await coding.prepare(claim['run'], claim['work_item'], claim['attempt'], 'commit', 1024)
    assert caught.value.code == code
    assert not await flow[1].list('coding_step_control')


async def test_parallel_preparations_cannot_spend_the_same_owner_allowance_twice(flow):
    _, work = await mixed_owner_batch(flow)
    own = [w for w in work if w['payload']['review_contract_owner'] == 'api-tests']
    await owner_pool(flow, max_active_seconds=40, max_tool_calls=6, max_steps=2)
    claims = [await running_action(flow, item) for item in own]
    coding = CodingSteps(flow[1], flow[0].settings, None)
    results = await asyncio.gather(*(coding.prepare(c['run'], c['work_item'], c['attempt'], 'commit', 1024)
        for c in claims), return_exceptions=True)
    controls = [r for r in results if isinstance(r, dict)]
    assert len(controls) == 1
    assert controls[0]['max_active_seconds'] == 40 and controls[0]['max_tool_calls'] == 6
    assert any(isinstance(r, DomainError) for r in results)
    control = controls[0]
    claim = next(c for c in claims if c['attempt']['id'] == control['attempt_id'])
    await coding.account({'run_id': 'run', 'work_item_id': claim['work_item']['id'],
        'attempt_id': control['attempt_id'], 'coding_step': control},
        {'active_seconds': 9, 'observed_tool_calls': 2, 'tool_observation_complete': True})
    remaining = next(c for c in claims if c != claim)
    next_control = await coding.prepare(remaining['run'], remaining['work_item'], remaining['attempt'], 'commit', 1024)
    assert (next_control['max_active_seconds'], next_control['max_tool_calls']) == (31, 4)
    pool = await flow[1].read('coding_work_budget', CodingSteps.budget_id('run', 'api-tests'))
    assert (pool['active_seconds'], pool['observed_tool_calls'], pool['step_count']) == (9, 2, 1)


async def test_direct_owner_inflight_control_reserves_the_same_pool(flow):
    _, work = await mixed_owner_batch(flow)
    action = next(w for w in work if w['payload']['review_contract_owner'] == 'api-tests')
    await owner_pool(flow, max_active_seconds=40, max_tool_calls=6, max_steps=2)
    def direct(tx):
        tx.put('attempt', 'direct-owner-attempt', {'run_id': 'run', 'work_item_id': 'api-tests', 'status': 'running'})
        return tx.put('coding_step_control', 'direct-owner-attempt', {'run_id': 'run', 'work_item_id': 'api-tests',
            'attempt_id': 'direct-owner-attempt', 'budget_id': CodingSteps.budget_id('run', 'api-tests'),
            'max_active_seconds': 30, 'max_tool_calls': 5})
    await flow[1].command('fixture.owner-direct', 'running', {}, direct)
    claim = await running_action(flow, action)
    control = await CodingSteps(flow[1], flow[0].settings, None).prepare(
        claim['run'], claim['work_item'], claim['attempt'], 'commit', 1024)
    assert (control['max_active_seconds'], control['max_tool_calls']) == (10, 1)


async def test_completed_child_cannot_hide_runtime_overrun_behind_larger_child_limit(flow):
    _, work = await mixed_owner_batch(flow)
    action = next(w for w in work if w['payload']['review_contract_owner'] == 'api-tests')
    await owner_pool(flow, max_active_seconds=40, max_tool_calls=6, max_steps=2)
    claim = await running_action(flow, action)
    coding = CodingSteps(flow[1], flow[0].settings, None)
    control = await coding.prepare(claim['run'], claim['work_item'], claim['attempt'], 'commit', 1024)
    task = {'run_id': 'run', 'work_item_id': action['id'], 'attempt_id': control['attempt_id'], 'coding_step': control}
    await coding.account(task, {'active_seconds': 41, 'observed_tool_calls': 2, 'tool_observation_complete': True})
    with pytest.raises(DomainError) as caught:
        await coding.collect(task, {'status': 'complete', 'summary': 'Changes ready', 'next_action': ''}, {'commit_oid': 'commit'})
    assert caught.value.code == 'coding_budget_exhausted'
    pool = await flow[1].read('coding_work_budget', CodingSteps.budget_id('run', 'api-tests'))
    assert pool['active_seconds'] == 41


async def test_proved_never_started_control_releases_owner_reservation(flow):
    _, work = await mixed_owner_batch(flow)
    action = next(w for w in work if w['payload']['review_contract_owner'] == 'api-tests')
    await owner_pool(flow, max_active_seconds=40, max_tool_calls=6, max_steps=2)
    coding = CodingSteps(flow[1], flow[0].settings, None)
    claim = await running_action(flow, action)
    control = await coding.prepare(claim['run'], claim['work_item'], claim['attempt'], 'commit', 1024)
    def stopped(tx):
        attempt = tx.get('attempt', control['attempt_id'])
        return tx.put('attempt', attempt['id'], {**attempt, 'status': 'blocked',
            'summary': 'Preparation stopped before dispatch context, authorization or worker launch.'}, attempt['revision'])
    await flow[1].command('review.never-started', 'stopped', {}, stopped)
    retry = await running_action(flow, action)
    retry_control = await coding.prepare(retry['run'], retry['work_item'], retry['attempt'], 'commit', 1024)
    assert (retry_control['max_active_seconds'], retry_control['max_tool_calls']) == (40, 6)


async def test_unknown_usage_does_not_release_owner_reservation(flow):
    _, work = await mixed_owner_batch(flow)
    own = [w for w in work if w['payload']['review_contract_owner'] == 'api-tests']
    coding = CodingSteps(flow[1], flow[0].settings, None)
    first = await running_action(flow, own[0])
    control = await coding.prepare(first['run'], first['work_item'], first['attempt'], 'commit', 1024)
    await coding.account({'run_id': 'run', 'work_item_id': own[0]['id'], 'attempt_id': control['attempt_id'],
                          'coding_step': control}, {})
    next_claim = await running_action(flow, own[1])
    with pytest.raises(DomainError) as caught:
        await coding.prepare(next_claim['run'], next_claim['work_item'], next_claim['attempt'], 'commit', 1024)
    assert caught.value.code == 'coding_budget_uncertain'


async def test_known_direct_and_delegated_settlement_clears_inflight_only_uncertainty(flow):
    _, work = await mixed_owner_batch(flow)
    action = next(w for w in work if w['payload']['review_contract_owner'] == 'api-tests')
    await owner_pool(flow, max_active_seconds=40, max_tool_calls=6, max_steps=4)
    def smaller_child(tx):
        budget_id = CodingSteps.budget_id('run', action['id'])
        budget = tx.get('coding_work_budget', budget_id)
        return tx.put('coding_work_budget', budget_id, {**budget, 'max_active_seconds': 10,
            'max_tool_calls': 2}, budget['revision'])
    await flow[1].command('review.smaller-child', 'cap', {}, smaller_child)
    coding = CodingSteps(flow[1], flow[0].settings, None)
    child = await running_action(flow, action)
    child_control = await coding.prepare(child['run'], child['work_item'], child['attempt'], 'commit', 1024)
    owner = await flow[1].read('work_item', 'api-tests')
    direct = await running_action(flow, owner)
    direct_control = await coding.prepare(direct['run'], direct['work_item'], direct['attempt'], 'commit', 1024)
    assert (child_control['max_active_seconds'], direct_control['max_active_seconds']) == (10, 30)
    for claim, control in [(child, child_control), (direct, direct_control)]:
        await coding.account({'run_id': 'run', 'work_item_id': claim['work_item']['id'],
            'attempt_id': control['attempt_id'], 'coding_step': control},
            {'active_seconds': 5, 'observed_tool_calls': 1, 'tool_observation_complete': True})
    pool = await flow[1].read('coding_work_budget', CodingSteps.budget_id('run', 'api-tests'))
    assert (pool['active_seconds'], pool['observed_tool_calls'], pool['step_count']) == (10, 2, 2)
    assert pool['uncertain'] is False


async def test_unproved_stopped_direct_control_keeps_unknown_usage_blocking(flow):
    _, work = await mixed_owner_batch(flow)
    action = next(w for w in work if w['payload']['review_contract_owner'] == 'api-tests')
    await owner_pool(flow, max_active_seconds=40, max_tool_calls=6, max_steps=4)
    coding = CodingSteps(flow[1], flow[0].settings, None)
    direct = await running_action(flow, await flow[1].read('work_item', 'api-tests'))
    control = await coding.prepare(direct['run'], direct['work_item'], direct['attempt'], 'commit', 1024)
    def unknown(tx):
        attempt = tx.get('attempt', control['attempt_id'])
        return tx.put('attempt', attempt['id'], {**attempt, 'status': 'failed'}, attempt['revision'])
    await flow[1].command('fixture.owner-unknown', 'stopped', {}, unknown)
    await owner_pool(flow, max_active_seconds=80, max_tool_calls=12)
    child = await running_action(flow, action)
    with pytest.raises(DomainError) as caught:
        await coding.prepare(child['run'], child['work_item'], child['attempt'], 'commit', 1024)
    assert caught.value.code == 'coding_budget_uncertain'


async def test_proved_unstarted_direct_control_does_not_reappear_as_unknown_after_child_settlement(flow):
    _, work = await mixed_owner_batch(flow)
    own = [w for w in work if w['payload']['review_contract_owner'] == 'api-tests']
    await owner_pool(flow, max_active_seconds=40, max_tool_calls=6, max_steps=4)
    coding = CodingSteps(flow[1], flow[0].settings, None)
    direct = await running_action(flow, await flow[1].read('work_item', 'api-tests'))
    control = await coding.prepare(direct['run'], direct['work_item'], direct['attempt'], 'commit', 1024)
    def stopped(tx):
        attempt = tx.get('attempt', control['attempt_id'])
        tx.put('attempt', attempt['id'], {**attempt, 'status': 'blocked',
            'summary': 'Preparation stopped before any dispatch or launch.'}, attempt['revision'])
        work = tx.get('work_item', 'api-tests')
        return tx.put('work_item', work['id'], {**work, 'status': 'blocked'}, work['revision'])
    await flow[1].command('review.direct-never-started', 'stopped', {}, stopped)
    child = await running_action(flow, own[0])
    child_control = await coding.prepare(child['run'], child['work_item'], child['attempt'], 'commit', 1024)
    await coding.account({'run_id': 'run', 'work_item_id': own[0]['id'], 'attempt_id': child_control['attempt_id'],
        'coding_step': child_control}, {'active_seconds': 5, 'observed_tool_calls': 1, 'tool_observation_complete': True})
    pool = await flow[1].read('coding_work_budget', CodingSteps.budget_id('run', 'api-tests'))
    assert pool['uncertain'] is False

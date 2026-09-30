"""Whole planning results commit their graph and completion together."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_workflow import flow as flow

from agentflow.common import DomainError
from agentflow.domain.planning_contract import build_planning_contract


async def prepared(flow, *, invalid=False):
    service, store, artifacts, project, _ = flow
    fingerprint = 'sha256:' + '1' * 64
    stages = [('architecture', 'architecture_planning'), ('development_plan', 'architecture_planning'),
              ('implementation', 'development'), ('code_review', 'review')]
    # Use the domain-owned role; implementation must never trust a model role.
    from agentflow.domain.planning import ROLES
    def seed(tx):
        tx.put('plan', 'plan', {'project_id': project['id'], 'actual_steps': [s for s, _ in stages],
            'work_specs': [{'key': s, 'step': s, 'role': ROLES[s]} for s, _ in stages]})
        tx.put('run', 'run', {'project_id': project['id'], 'iteration_id': 'iteration', 'plan_id': 'plan',
            'execution_state': 'running', 'input_fingerprint': fingerprint, 'blocking_reasons': []})
        for index, (stage, _) in enumerate(stages):
            tx.put('work_item', stage, {'project_id': project['id'], 'run_id': 'run', 'step': stage, 'key': stage,
                'role': ROLES[stage], 'generation': 1, 'fencing_token': 1 if stage == 'development_plan' else 0,
                'status': 'completed' if stage == 'architecture' else 'running' if stage == 'development_plan' else 'pending',
                'attempt_id': 'attempt' if stage == 'development_plan' else None,
                'quality_result': 'unknown', 'approval_required': False, 'required': True,
                'artifact_ids': [], 'dependencies': [stages[index - 1][0]] if index else [],
                'write_paths': ['src'] if stage == 'implementation' else [], 'payload': {},
                'input_fingerprint': fingerprint, 'policy_fingerprint': fingerprint})
        return tx.put('attempt', 'attempt', {'work_item_id': 'development_plan', 'run_id': 'run',
            'iteration_id': 'iteration', 'generation': 1, 'fencing_token': 1,
            'input_fingerprint': fingerprint, 'status': 'running', 'execution_status': None})
    attempt = await store.command('fixture', 'seed-planning', {}, seed)
    proposals = [
        {'stage_key': 'implementation', 'children': [
            {'key': 'one', 'goal': 'Implement first module', 'write_paths': ['src/one.py']},
            {'key': 'two', 'goal': 'Implement second module', 'write_paths': ['src/two.py']}]},
        {'stage_key': 'code_review', 'children': [
            {'key': 'one', 'goal': 'Review first module', 'review_focus': 'current_code',
             'write_paths': ['src/one.py'] if invalid else [], 'inspection_paths': ['src/one.py']},
            {'key': 'two', 'goal': 'Review second module', 'review_focus': 'existing_test_regressions',
             'write_paths': [], 'inspection_paths': ['src/two.py']}]}]
    contract = build_planning_contract(await store.read('run', 'run'), await store.read('work_item', 'development_plan'),
        await store.read('plan', 'plan'), await store.list('work_item'), max_children=32, max_work_items=512, max_edges=4096)
    result = {'title': 'Plan', 'summary': 'Implement and review two modules', 'content': 'Concrete plan',
              'sources': [], 'unknowns': [], 'parallel_work': proposals}
    blob = await artifacts.put_bytes(json.dumps(result).encode())
    payload = {'execution_status': 'completed', 'quality_result': 'unknown',
               'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint']}
    metadata = [{'digest': blob['id'], 'name': 'openhands_final.json', 'media_type': 'application/json'}]
    batch = {'proposals': proposals, 'contract': contract, 'result_digest': blob['id']}
    return payload, metadata, batch


async def state(store):
    return {kind: await store.list(kind) for kind in ('run', 'work_item', 'attempt', 'artifact',
                                                     'stage_expansion', 'planning_commit', 'approval')}


async def test_invalid_later_proposal_cannot_partially_commit_or_finish(flow):
    payload, metadata, batch = await prepared(flow, invalid=True)
    before = await state(flow[1])
    with pytest.raises(DomainError) as caught:
        await flow[0].finish_attempt('attempt', payload, 'finish', verified_artifacts=metadata, planning_batch=batch)
    assert caught.value.code == 'planning_validation_failed'
    assert any(issue['code'] == 'role_write_scope' for issue in caught.value.details['issues'])
    assert await state(flow[1]) == before


async def test_valid_batch_and_completion_are_atomic_idempotent_and_readonly_reviews(flow):
    payload, metadata, batch = await prepared(flow)
    async def finish():
        return await flow[0].finish_attempt('attempt', payload, 'finish', verified_artifacts=metadata, planning_batch=batch)
    results = await asyncio.gather(finish(), finish())
    assert results[0] == results[1] and results[0]['status'] == 'completed'
    assert len(await flow[1].list('stage_expansion')) == 2
    assert len(await flow[1].list('planning_commit')) == 1
    items = await flow[1].list('work_item')
    assert len(items) == 8
    reviews = [item for item in items if item.get('parent_stage_id') == 'code_review']
    assert len(reviews) == 2 and all(not item['write_paths'] for item in reviews)
    assert {tuple(item['payload']['inspection_paths']) for item in reviews} == {('src/one.py',), ('src/two.py',)}
    original = await state(flow[1])
    await finish()
    assert await state(flow[1]) == original


async def test_completion_write_failure_rolls_back_all_expansions_and_retries_once(flow, monkeypatch):
    from agentflow.storage.store import Transaction
    payload, metadata, batch = await prepared(flow)
    before = await state(flow[1])
    original = Transaction.put
    def fail(self, kind, *args, **kwargs):
        if kind == 'artifact':
            raise RuntimeError('Injected completion write failure')
        return original(self, kind, *args, **kwargs)
    monkeypatch.setattr(Transaction, 'put', fail)
    with pytest.raises(RuntimeError, match='Injected'):
        await flow[0].finish_attempt('attempt', payload, 'finish', verified_artifacts=metadata, planning_batch=batch)
    assert await state(flow[1]) == before
    monkeypatch.setattr(Transaction, 'put', original)
    await flow[0].finish_attempt('attempt', payload, 'finish', verified_artifacts=metadata, planning_batch=batch)
    assert len(await flow[1].list('stage_expansion')) == 2


async def test_changed_target_revision_rejects_frozen_plan_before_any_commit(flow):
    payload, metadata, batch = await prepared(flow)
    def change(tx):
        row = tx.get('work_item', 'implementation')
        return tx.put('work_item', row['id'], {**row, 'write_paths': ['src/other']}, row['revision'])
    await flow[1].command('fixture', 'change', {}, change)
    before = await state(flow[1])
    with pytest.raises(DomainError) as caught:
        await flow[0].finish_attempt('attempt', payload, 'finish', verified_artifacts=metadata, planning_batch=batch)
    assert caught.value.code == 'stale_planning_contract'
    assert await state(flow[1]) == before


async def test_finished_plan_cannot_replay_different_proposals_with_same_key(flow):
    payload, metadata, batch = await prepared(flow)
    await flow[0].finish_attempt('attempt', payload, 'finish', verified_artifacts=metadata, planning_batch=batch)
    changed = copy.deepcopy(batch)
    changed['proposals'][0]['children'][0]['goal'] = 'Changed request'
    with pytest.raises(DomainError) as caught:
        await flow[0].finish_attempt('attempt', payload, 'finish', verified_artifacts=metadata, planning_batch=changed)
    assert caught.value.code in {'idempotency_conflict', 'planning_result_mismatch'}


async def test_blocked_attempt_preserves_structured_controller_diagnostic(flow):
    await prepared(flow)
    diagnostic = {'code': 'unexpected_plan_failure', 'message': 'Exact controller failure',
                  'details': {'phase': 'collection', 'stage_key': 'code_review'}}
    await flow[0].block_attempt('attempt', diagnostic['message'], 'block',
        failure_code='controller_validation_failed', failure_diagnostic=diagnostic)
    assert (await flow[1].read('work_item', 'development_plan'))['failure_diagnostic'] == diagnostic
    assert (await flow[1].read('attempt', 'attempt'))['failure_diagnostic'] == diagnostic


@pytest.mark.parametrize('invalid', [True, False])
async def test_scheduler_collects_the_whole_plan_without_partial_expansion(flow, invalid):
    from agentflow.control.coding_steps import CodingSteps
    from agentflow.control.scheduler import Scheduler
    from agentflow.repository import RepositoryAdapter
    from agentflow.runtime.trace import ExecutionTrace
    payload, metadata, batch = await prepared(flow, invalid=invalid)
    service, store, artifacts, project, _ = flow
    raw = await artifacts.read(metadata[0]['digest'])
    output = service.settings.data_dir / 'attempt_artifacts/openhands_final.json'
    output.parent.mkdir(parents=True)
    output.write_bytes(raw)
    scheduler = Scheduler.__new__(Scheduler)
    scheduler._wake = asyncio.Event()
    scheduler.store, scheduler.settings, scheduler.workflow = store, service.settings, service
    scheduler.repository = RepositoryAdapter()
    scheduler.coding_steps = CodingSteps(store, service.settings, scheduler.repository)
    scheduler.traces = ExecutionTrace(store)
    scheduler.runtime = SimpleNamespace(execute_task=AsyncMock(return_value={'execution_status': 'completed',
        'result': json.loads(raw), 'artifacts': [str(output)]}))
    await scheduler._execute_existing({'attempt_id': 'attempt', 'work_item_id': 'development_plan', 'run_id': 'run',
        'step': 'development_plan', 'fencing_token': payload['fencing_token'],
        'input_fingerprint': payload['input_fingerprint'], 'workspace': str(Path(project['local_path'])),
        'source_commit': project['base_commit'], 'planning_contract': batch['contract']})
    work = await store.read('work_item', 'development_plan')
    if invalid:
        assert work['status'] == 'blocked' and work['runtime_failure_code'] == 'planning_validation_failed'
        assert work['failure_diagnostic']['details']['issues'][0]['code'] == 'role_write_scope'
        assert not await store.list('stage_expansion') and not await store.list('planning_commit')
    else:
        assert work['status'] == 'completed', work
        assert len(await store.list('stage_expansion')) == 2
        assert len(await store.list('planning_commit')) == 1


async def test_legacy_goal_recovery_archives_partially_created_children(flow):
    from agentflow.domain.expansion import StageExpander
    from agentflow.domain.planning import ROLES
    _, _, batch = await prepared(flow)
    def goal(tx):
        row = tx.get('work_item', 'development_plan')
        return tx.put('work_item', row['id'], {**row, 'step': 'goal', 'key': 'goal', 'role': ROLES['goal']}, row['revision'])
    await flow[1].command('fixture', 'goal', {}, goal)
    before = await flow[1].read('work_item', 'implementation')
    expanded = await StageExpander(flow[1]).expand('run', 'implementation', batch['proposals'][0]['children'],
        'legacy-partial', before['revision'])
    def recover(tx):
        affected = flow[0]._invalidate(tx, tx.list('work_item'), {'development_plan'}, 'Retry original planning',
                                     expand_roots=False)
        return {'affected': sorted(affected)}
    await flow[1].command('fixture', 'recover-goal', {}, recover)
    assert (await flow[1].read('work_item', 'implementation'))['kind'] == 'stage'
    for identity in expanded['work_item_ids']:
        assert (await flow[1].read('work_item', identity))['archived']


@pytest.mark.parametrize('corrected', [False, True])
async def test_original_webcalendar_seven_groups_reject_all_five_errors_or_commit_together(flow, corrected):
    from jsonschema import Draft202012Validator

    from agentflow.control.scheduler import DEVELOPMENT_PLAN_SCHEMA
    from agentflow.control.workflow_stages import STAGE_ORDER
    from agentflow.domain.planning import CODING_STEPS, ROLES, STEPS
    payload, _, _ = await prepared(flow)
    service, store, artifacts, project, _ = flow
    def graph(tx):
        current = tx.get('plan', 'plan')
        specs = [{'key': key, 'step': 'code_review' if key.endswith(':review') else key,
                  'role': ROLES['code_review' if key.endswith(':review') else key]} for key in STAGE_ORDER]
        tx.put('plan', 'plan', {**current, 'actual_steps': list(STEPS), 'work_specs': specs}, current['revision'])
        for i, spec in enumerate(specs):
            old = tx.get('work_item', spec['key'])
            planning = spec['key'] == 'development_plan'
            tx.put('work_item', spec['key'], {**(old or {}), **spec, 'run_id': 'run', 'project_id': project['id'],
                'generation': 1, 'fencing_token': int(planning), 'attempt_id': 'attempt' if planning else None,
                'dependencies': [specs[i - 1]['key']] if i else [], 'artifact_ids': [], 'payload': {},
                'status': 'completed' if i < STAGE_ORDER.index('development_plan') else 'running' if planning else 'pending',
                'write_paths': ['.'] if spec['step'] in CODING_STEPS else [], 'quality_result': 'unknown',
                'required': True, 'approval_required': False, 'input_fingerprint': payload['input_fingerprint'],
                'policy_fingerprint': payload['input_fingerprint']}, old['revision'] if old else None)
        return {}
    await store.command('fixture', 'full-graph', {}, graph)
    proposals = json.loads((Path(__file__).parents[1] / 'fixtures/webcalendar_parallel_work.json').read_text())
    if corrected:
        for group in proposals:
            if group['stage_key'] not in CODING_STEPS:
                for child in group['children']:
                    if child['write_paths']:
                        child['inspection_paths'], child['write_paths'] = child['write_paths'], []
    result = {'title': 'WebCalendar plan', 'summary': 'Original seven task groups', 'content': 'Fixture document',
              'sources': [], 'unknowns': [], 'parallel_work': proposals}
    Draft202012Validator(DEVELOPMENT_PLAN_SCHEMA).validate(result)
    blob = await artifacts.put_bytes(json.dumps(result).encode())
    contract = build_planning_contract(await store.read('run', 'run'), await store.read('work_item', 'development_plan'),
        await store.read('plan', 'plan'), await store.list('work_item'), max_children=32, max_work_items=512, max_edges=4096)
    before = await state(store)
    async def finish():
        return await service.finish_attempt('attempt', payload, 'whole-real-plan',
            verified_artifacts=[{'digest': blob['id'], 'name': 'openhands_final.json', 'media_type': 'application/json'}],
            planning_batch={'proposals': proposals, 'contract': contract, 'result_digest': blob['id']})
    if not corrected:
        with pytest.raises(DomainError) as caught:
            await finish()
        assert caught.value.code == 'planning_validation_failed'
        assert len(caught.value.details['issues']) == 5
        assert all(issue['code'] == 'role_write_scope' for issue in caught.value.details['issues'])
        assert await state(store) == before
    else:
        assert (await finish())['status'] == 'completed'
        assert len(await store.list('stage_expansion')) == 7
        children = [item for item in await store.list('work_item') if item.get('parent_stage_id')]
        assert len(children) == sum(len(group['children']) for group in proposals)
        assert all(not child['write_paths'] for child in children if child['step'] == 'code_review')

"""Explain the current automatic stop without replacing the original failure."""
from uuid import uuid4

import pytest
from test_workflow import flow as flow
from test_workflow import start

from agentflow.control.failure_remediation import failure_signature
from agentflow.control.presentation import RunPresentationService
from agentflow.runtime.failures import runtime_failure_message

STOP = '本任务已自动重试 2/2 次，达到上限，保留现场等待处理。'


async def failed_task(flow):
    workflow, store, artifacts, _, _ = flow
    run = await start(flow)
    claim = await workflow.claim_next(run['id'], 'fixture', str(uuid4()))
    await workflow.block_attempt(claim['attempt']['id'], 'No source changes', str(uuid4()), failure_code='no_code_changes')
    work = await store.read('work_item', claim['work_item']['id'])
    attempt = await store.read('attempt', claim['attempt']['id'])
    return RunPresentationService(store, artifacts, workflow.settings), run, work, attempt


async def analysis(store, run, work, attempt, identity='current-analysis', **changes):
    fields = {'run_id': run['id'], 'work_item_id': work['id'], 'attempt_id': attempt['id'],
        'generation': work['generation'], 'actor': 'controller', 'phase': 'analysis', 'status': 'blocked',
        'failure_signature': failure_signature(work, attempt), 'analyzed_at': '2026-09-27T10:00:00Z',
        'blockers': [{'code': 'automatic_repair_limit', 'message': STOP}], **changes}
    return await store.command('fixture.analysis', str(uuid4()), {}, lambda tx: tx.put('failure_analysis', identity, fields))


async def task_view(presenter, run, work):
    view = await presenter.workflow(run['id'])
    return next(task for stage in view['stages'] for task in stage['tasks'] if task['id'] == work['id']), view


async def test_current_retry_limit_explains_why_original_failure_is_stopped(flow):
    presenter, run, work, attempt = await failed_task(flow)
    store = flow[1]
    await analysis(store, run, work, attempt)
    before = {kind: await store.list(kind) for kind in ('run', 'work_item', 'attempt', 'failure_analysis', 'budget_account')}
    task, _ = await task_view(presenter, run, work)
    assert runtime_failure_message('no_code_changes') in task['blocking_reason']
    assert STOP in task['blocking_reason']
    assert task['status'] == 'blocked' and task['quality_result'] == work['quality_result']
    assert {kind: await store.list(kind) for kind in before} == before


@pytest.mark.parametrize('changes', [
    {'run_id': 'other-run'}, {'work_item_id': 'other-work'}, {'generation': 0},
    {'attempt_id': 'old-attempt'}, {'failure_signature': 'mismatched'}, {'actor': 'model'},
])
async def test_unrelated_or_stale_analysis_cannot_supply_a_stop_reason(flow, changes):
    presenter, run, work, attempt = await failed_task(flow)
    await analysis(flow[1], run, work, attempt, **changes)
    task, _ = await task_view(presenter, run, work)
    assert task['blocking_reason'] == runtime_failure_message('no_code_changes')


@pytest.mark.parametrize('latest_status', ['ready', 'repair_scheduled'])
async def test_latest_nonblocked_analysis_hides_an_older_stop_message(flow, latest_status):
    presenter, run, work, attempt = await failed_task(flow)
    await analysis(flow[1], run, work, attempt, identity='old-blocked')
    await analysis(flow[1], run, work, attempt, identity='latest', status=latest_status,
                   analyzed_at='2026-09-27T10:01:00Z', blockers=[])
    task, _ = await task_view(presenter, run, work)
    assert task['blocking_reason'] == runtime_failure_message('no_code_changes')


async def test_latest_analysis_keeps_one_necessary_limit_reason_not_all_warnings(flow):
    presenter, run, work, attempt = await failed_task(flow)
    await analysis(flow[1], run, work, attempt, identity='old-blocked', blockers=[{
        'code': 'retry_backoff', 'message': 'OLD BACKOFF'}])
    await analysis(flow[1], run, work, attempt, identity='latest', analyzed_at='2026-09-27T10:01:00Z', blockers=[
        {'code': 'recovery_budget_uncertain', 'message': 'REDUNDANT USAGE WARNING'},
        {'code': 'automatic_repair_limit', 'message': STOP},
        {'code': 'human_approval_pending', 'message': 'ADDITIONAL HISTORY'},
    ])
    task, _ = await task_view(presenter, run, work)
    assert STOP in task['blocking_reason']
    assert not any(text in task['blocking_reason'] for text in ('OLD BACKOFF', 'REDUNDANT USAGE WARNING', 'ADDITIONAL HISTORY'))


async def test_active_current_attempt_is_not_relabelled_by_blocked_analysis(flow):
    presenter, run, work, attempt = await failed_task(flow)
    store = flow[1]
    def running(tx):
        current_work, current_attempt = tx.get('work_item', work['id']), tx.get('attempt', attempt['id'])
        tx.put('work_item', work['id'], {**current_work, 'status': 'running'}, current_work['revision'])
        tx.put('attempt', attempt['id'], {**current_attempt, 'status': 'running'}, current_attempt['revision'])
        return {}
    await store.command('fixture.running', str(uuid4()), {}, running)
    work, attempt = await store.read('work_item', work['id']), await store.read('attempt', attempt['id'])
    await analysis(store, run, work, attempt)
    task, view = await task_view(presenter, run, work)
    assert task['blocking_reason'] is None
    assert task['status'] == view['stages'][0]['status'] == 'running'


@pytest.mark.parametrize('blockers', [None, 42])
async def test_malformed_optional_blockers_cannot_break_workflow_projection(flow, blockers):
    presenter, run, work, attempt = await failed_task(flow)
    await analysis(flow[1], run, work, attempt, blockers=blockers)
    task, _ = await task_view(presenter, run, work)
    assert task['blocking_reason'] == runtime_failure_message('no_code_changes')


async def test_stop_explanation_is_bounded_even_if_controller_message_is_long(flow):
    presenter, run, work, attempt = await failed_task(flow)
    await analysis(flow[1], run, work, attempt, blockers=[{
        'code': 'automatic_repair_limit', 'message': STOP + '\n' + 'additional detail ' * 1000}])
    task, _ = await task_view(presenter, run, work)
    assert STOP in task['blocking_reason']
    assert runtime_failure_message('no_code_changes') in task['blocking_reason']
    assert len(task['blocking_reason']) < 500

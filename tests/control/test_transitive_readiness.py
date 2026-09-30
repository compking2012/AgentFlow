"""A retained output does not make its reopened ancestors ready for dispatch."""
from uuid import uuid4

import pytest
from test_parallel_remediation import update
from test_workflow import flow as flow
from test_workflow import start


async def graph(flow, ancestor_status='running', ancestor_quality='unknown', ancestor_step='implementation'):
    service, store, _, project, _ = flow
    run = await start(flow)
    initial = (await store.list('work_item'))[0]
    await update(store, 'work_item', initial['id'], status='completed', quality_result='passed')
    common = {k: v for k, v in initial.items() if k not in {'id', 'revision'}}
    for identity, dependencies, status in [('a', [], ancestor_status), ('b', ['a'], 'completed'), ('c', ['b'], 'pending')]:
        await update(store, 'work_item', identity, **{**common, 'key': identity, 'dependencies': dependencies,
            'status': status, 'step': ancestor_step if identity == 'a' else 'code_review' if identity == 'c' else 'implementation',
            'role': 'review' if identity == 'c' else 'development', 'write_paths': [],
            'quality_result': ancestor_quality if identity == 'a' else 'passed'})
    return service, store, run


@pytest.mark.parametrize(('status', 'quality', 'step'), [
    ('running', 'unknown', 'implementation'), ('waiting_approval', 'passed', 'implementation'),
    ('blocked', 'unknown', 'implementation'), ('execution_unknown', 'unknown', 'implementation'),
    ('completed', 'failed', 'implementation'), ('completed', 'inconclusive', 'implementation'),
    ('completed', 'unknown', 'code_review'), ('completed', 'unknown', 'unit_test_execution'),
])
async def test_pending_review_waits_for_all_transitive_prerequisites(flow, status, quality, step):
    service, store, run = await graph(flow, status, quality, step)
    retained = await store.read('work_item', 'b')
    assert (await service.claim_next(run['id'], 'fixture', str(uuid4())))['attempt'] is None
    assert await store.read('work_item', 'b') == retained
    assert (await store.read('work_item', 'c'))['status'] == 'pending'
    await update(store, 'work_item', 'a', status='completed', quality_result='passed')
    assert (await service.claim_next(run['id'], 'fixture', str(uuid4())))['work_item']['id'] == 'c'


async def test_unrelated_ready_branches_keep_running_in_parallel(flow):
    service, store, run = await graph(flow)
    template = await store.read('work_item', 'c')
    for identity in ('independent-1', 'independent-2'):
        await update(store, 'work_item', identity, **{k: v for k, v in template.items() if k not in {'id', 'revision', 'dependencies'}},
            dependencies=[])
    claimed = [await service.claim_next(run['id'], 'fixture', str(uuid4())) for _ in range(2)]
    assert {c['work_item']['id'] for c in claimed} == {'independent-1', 'independent-2'}
    assert (await store.read('work_item', 'c'))['status'] == 'pending'


@pytest.mark.parametrize('damage', ['missing', 'cycle', 'foreign_run', 'archived'])
async def test_invalid_transitive_graph_does_not_claim_downstream(flow, damage):
    service, store, run = await graph(flow, 'completed', 'passed')
    fields = ({'dependencies': ['missing']} if damage == 'missing' else {'dependencies': ['b']}
        if damage == 'cycle' else {'run_id': 'foreign-run'} if damage == 'foreign_run' else {'archived': True})
    await update(store, 'work_item', 'a', **fields)
    assert (await service.claim_next(run['id'], 'fixture', str(uuid4())))['attempt'] is None


async def test_completed_code_without_separate_quality_result_remains_usable(flow):
    service, _, run = await graph(flow, 'completed', 'unknown')
    assert (await service.claim_next(run['id'], 'fixture', str(uuid4())))['work_item']['id'] == 'c'

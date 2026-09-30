import pytest
from test_workflow import flow as flow
from test_workflow import start

from agentflow.control.presentation import RunPresentationService
from agentflow.domain.planning import WorkSpec


@pytest.mark.asyncio
@pytest.mark.parametrize('unfinished',['pending','cancelled','blocked'])
async def test_unfinished_independent_root_is_not_hidden_by_completed_peer(flow,unfinished):
    workflow,store,artifacts,_,_=flow
    run=await start(flow)
    await workflow.add_work_items(run['id'],[
        WorkSpec('market','research','research'),WorkSpec('customers','research','research')
    ],run['revision'],'independent-research')
    works={w['key']:w for w in await store.list('work_item')}
    def state(tx):
        pending=tx.get('work_item',works['market']['id'])
        complete=tx.get('work_item',works['customers']['id'])
        tx.put('work_item',pending['id'],{**pending,'status':unfinished,'quality_result':'unknown'},pending['revision'])
        tx.put('work_item',complete['id'],{**complete,'status':'completed','quality_result':'passed','generation':2},complete['revision'])
        return {}
    await store.command('fixture','states',{},state)
    view=await RunPresentationService(store,artifacts,workflow.settings).workflow(run['id'])
    stage=next(s for s in view['stages'] if s['key']=='research')
    assert all(not t['is_history'] for t in stage['tasks'])
    assert stage['status']!='completed'
    assert stage['work_item_id']==works['market']['id']

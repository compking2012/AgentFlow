"""A project map is a read model, never synthetic scheduler work."""
from uuid import uuid4

import pytest
from test_workflow import flow as flow
from test_workflow import plan_payload

from agentflow.control import project_workflow as views
from agentflow.control.workflow_stages import STAGE_ORDER


async def put(store, kind, identity, value):
    current = await store.read(kind, identity)
    return await store.command('test.project-view', str(uuid4()), {}, lambda tx: tx.put(
        kind, identity, {key: item for key, item in value.items() if key not in {'id', 'revision'}}, current['revision'] if current else None))


async def seed_version(flow, product, name, steps, *, base=None, refs=None):
    service, store, _, project, _ = flow
    payload = plan_payload(project)
    payload.update(goal=name, selection={'mode': 'selected', 'selected_steps': steps},
        stage_input_versions={steps[0]: refs} if refs else {},
        product_contract={'product_id': product['id'], 'change_id': name if base else None, 'base_run_id': base})
    plan = await service.create_plan(payload, str(uuid4()))
    run = await service.start_run({'plan_id': plan['id'], 'expected_revision': plan['revision']}, str(uuid4()))
    if base:
        await put(store, 'product_change', name, {'product_id': product['id'], 'title': name,
            'description': name, 'run_id': run['id'], 'plan_id': plan['id'], 'base_run_id': base,
            'kind': 'change', 'start_stage': 'prd', 'state': 'running', 'created_at': name})
    actual = [{'id': item['id'], 'work_item_id': item['id'], 'key': item['key'], 'step': item['step'],
        'status': item['status'], 'quality_result': item['quality_result'], 'tasks': [], 'dependencies': item['dependencies']}
        for item in await store.list('work_item') if item['run_id'] == run['id']]
    return run, plan, {'run_id': run['id'], 'input_fingerprint': run['input_fingerprint'], 'stages': actual}


async def test_initial_and_two_changes_share_template_with_frozen_prefix_and_real_task_ids(flow):
    service, store, _, project, _ = flow
    product = await put(store, 'product', 'product', {'name': '阅读清单', 'output_directory': project['local_path'], 'project_id': project['id'], 'run_ids': []})
    initial, initial_plan, original = await seed_version(flow, product, '初始开发', ['goal', 'research'])
    refs = []
    for step in ('goal', 'research'):
        claim = await service.claim_next(initial['id'], 'worker', str(uuid4()))
        from test_workflow import finish
        work = await finish(flow, claim)
        source = await store.read('artifact', work['artifact_ids'][0])
        refs.append({'artifact_version_id': source['id'], 'fingerprint': source['digest'], 'revision': source['revision']})
    first, first_plan, first_actual = await seed_version(flow, product, '筛选收藏', ['prd'], base=initial['id'], refs=refs)
    second, second_plan, second_actual = await seed_version(flow, product, '导出列表', ['prd'], base=first['id'], refs=refs)
    await put(store, 'product', product['id'], {**product, 'run_id': second['id'], 'initial_run_id': initial['id'],
        'run_ids': [initial['id'], first['id'], second['id']], 'current_change_id': '导出列表'})
    before = {kind: await store.list(kind) for kind in ('run', 'work_item', 'attempt', 'budget_account')}
    maps = [await views.compose_product_workflow(store, run, plan, actual) for run, plan, actual in (
        (initial, initial_plan, original), (first, first_plan, first_actual), (second, second_plan, second_actual))]
    expected = list(STAGE_ORDER[:STAGE_ORDER.index('delivery') + 1])
    assert [[s['key'] for s in view['stages']] for view in maps] == [expected] * 3
    assert len({tuple(s['id'] for s in view['stages']) for view in maps}) == 1
    for view, actual in zip(maps[1:], (first_actual, second_actual)):
        assert [s['status'] for s in view['stages'][:2]] == ['inherited', 'inherited']
        assert all(s['quality_result'] == 'not_applicable' and not s['tasks'] and not s.get('work_item_id') for s in view['stages'][:2])
        assert view['stages'][2]['work_item_id'] == actual['stages'][0]['work_item_id']
        assert view['stages'][0]['provenance']['source_run_id'] == initial['id']
        assert view['stages'][0]['output']['media_type'] == 'text/markdown'
        assert view['stages'][0]['output']['artifact_id'] in {row['id'] for row in await store.list('readable_artifact') if row.get('run_id') == initial['id']}
    assert {kind: await store.list(kind) for kind in before} == before


@pytest.mark.parametrize('damage', ['digest', 'revision', 'foreign', 'stale', 'absent'])
async def test_frozen_prefix_never_uses_missing_stale_or_foreign_evidence(flow, damage):
    _, store, _, project, _ = flow
    product = await put(store, 'product', 'product', {'name': '项目', 'project_id': project['id'], 'run_ids': []})
    source = await put(store, 'artifact', 'baseline', {'step': 'research', 'product_id': product['id'],
        'project_id': project['id'], 'digest': 'a' * 64, 'source_kind': 'static_diagnosis', 'stale': False, 'run_id': None, 'work_item_id': None, 'generation': 0, 'quality_result': 'not_applicable'})
    ref = {'artifact_version_id': source['id'], 'fingerprint': source['digest'], 'revision': source['revision']}
    run, plan, actual = await seed_version(flow, product, '调整', ['prd'], base='frozen-base', refs=[ref])
    if damage == 'absent':
        plan['stage_reused_inputs'] = {'prd': ['absent']}
    else:
        damaged = {**source, {'digest': 'digest', 'revision': 'extra', 'foreign': 'product_id', 'stale': 'stale'}[damage]:
                   {'digest': 'b' * 64, 'revision': True, 'foreign': 'other', 'stale': True}[damage]}
        await put(store, 'artifact', source['id'], damaged)
    view = await views.compose_product_workflow(store, run, plan, actual)
    assert view['stages'][1]['status'] == 'missing_baseline'
    assert not view['stages'][1]['tasks'] and not view['stages'][1].get('output')


async def test_import_registration_and_preparing_change_have_readonly_full_map(flow):
    _, store, _, project, _ = flow
    product = await put(store, 'product', 'imported', {'name': '现有工具', 'project_id': project['id'],
        'creation_mode': 'import', 'diagnosis': {'diagnosis_kind': 'static'}, 'state': 'registered', 'run_ids': []})
    view = await views.unstarted_product_workflow(store, product['id'])
    assert [s['status'] for s in view['stages'][:3]] == ['inherited', 'inherited', 'pending']
    assert view['stages'][1]['provenance']['kind'] == 'static_baseline'
    assert '未开展市场' in view['stages'][1]['provenance']['description']
    assert view['run_id'] is None and all(not s.get('work_item_id') and not s['tasks'] for s in view['stages'])
    change = await put(store, 'product_change', 'waiting', {'product_id': product['id'], 'start_stage': 'prd',
        'state': 'preparing', 'title': '修改筛选', 'base_run_id': None})
    view = await views.unstarted_product_workflow(store, product['id'], change['id'])
    assert view['version']['change_id'] == change['id']
    assert not await store.list('work_item')


async def test_index_groups_restarts_old_project_ids_and_generic_runs_without_guid_labels(flow):
    _, store, _, project, _ = flow
    product = await put(store, 'product', 'product', {'name': '阅读清单', 'output_directory': project['local_path'], 'project_id': project['id'], 'run_ids': []})
    first, _, _ = await seed_version(flow, product, '初始开发', ['goal'])
    restart, _, _ = await seed_version(flow, product, '重建', ['goal'], base=first['id'])
    change = await store.read('product_change', '重建')
    await put(store, 'product_change', change['id'], {**change, 'kind': 'restart'})
    await put(store, 'product', product['id'], {**product, 'run_id': restart['id'], 'initial_run_id': first['id'],
        'run_ids': [first['id'], restart['id']], 'project_id': 'different-project'})
    result = await views.project_workflow_index(store)
    assert len(result['items']) == 1
    row = result['items'][0]
    assert row['name'] == '阅读清单'
    assert [v['kind'] for v in row['versions']] == ['initial', 'restart']
    assert [v['run_id'] for v in row['versions']] == [first['id'], restart['id']]
    assert row['default_version_id'] == row['versions'][1]['id']
    assert all(first['id'] not in v['label'] and restart['id'] not in v['label'] for v in row['versions'])
    orphan = await put(store, 'project', 'orphan', {'name': '旧工作区'})
    await put(store, 'run', 'legacy', {'project_id': orphan['id'], 'goal': '检查功能', 'execution_state': 'paused'})
    assert any(p['name'] == '旧工作区' for p in (await views.project_workflow_index(store))['items'])

async def test_inherited_document_is_only_frozen_snapshot_not_current_product_head(flow):
    _, store, _, project, _ = flow
    product = await put(store, 'product', 'product', {'name': '项目', 'project_id': project['id'], 'run_ids': []})
    original = await put(store, 'readable_artifact', 'frozen-document', {'project_document': True,
        'product_id': product['id'], 'logical_stage_key': 'research', 'run_id': 'base',
        'digest': 'a' * 64, 'name': '市场调研.md', 'work_item_id': 'historic-work', 'generation': 1})
    baseline = {'product_id': product['id'], 'run_id': 'base', 'documents': {'research': {
        'artifact_id': original['id'], 'digest': original['digest'], 'name': original['name'],
        'run_id': 'base', 'work_item_id': 'historic-work', 'generation': 1}}}
    run, plan, actual = await seed_version(flow, product, '变更', ['prd'], base='base')
    plan['product_contract']['document_baseline'] = baseline
    await put(store, 'readable_artifact', 'new-head', {**original, 'id': 'new-head', 'digest': 'b' * 64, 'run_id': run['id']})
    view = await views.compose_product_workflow(store, run, plan, actual)
    inherited = view['stages'][1]
    assert inherited['status'] == 'inherited'
    assert inherited['output']['artifact_id'] == original['id']
    assert inherited['output']['digest'] == original['digest']
    assert not inherited['tasks']
    plan['product_contract']['document_baseline']['documents']['research']['digest'] = 'b' * 64
    bad = await views.compose_product_workflow(store, run, plan, actual)
    assert bad['stages'][1]['status'] == 'missing_baseline'
    assert not bad['stages'][1]['output']

async def test_static_import_baseline_is_named_as_static_evidence_not_market_research(flow):
    _, store, _, project, _ = flow
    product = await put(store, 'product', 'imported-static', {'name': '已有代码', 'project_id': project['id'],
        'creation_mode': 'import', 'diagnosis': {'diagnosis_kind': 'static'}, 'state': 'registered'})
    stage = (await views.unstarted_product_workflow(store, product['id']))['stages'][1]
    assert stage['expected_artifact']['name'] == '现有工程静态基线'
    assert '未开展市场' in stage['expected_artifact']['description']

import json
from pathlib import Path
from uuid import uuid4

import pytest
from test_workflow import flow as flow
from test_workflow import plan_payload, start

from agentflow.common import DomainError
from agentflow.control.presentation import RunPresentationService
from agentflow.domain.expansion import StageExpander


async def complete_document(flow, text='# 产品目标\n\n给读者可理解的范围与验收标准。'):
    service, _, artifacts, _, _ = flow
    run = await start(flow)
    claim = await service.claim_next(run['id'], 'test-worker', str(uuid4()))
    blob = await artifacts.put_bytes(json.dumps({'title': '用户目标重构', 'summary': '明确范围', 'content': text,
        'sources': [], 'unknowns': []}, ensure_ascii=False).encode())
    request = {'execution_status': 'completed', 'quality_result': 'unknown',
        'fencing_token': claim['attempt']['fencing_token'], 'input_fingerprint': claim['attempt']['input_fingerprint']}
    inputs = [{'digest': blob['id'], 'name': 'openhands_final.json', 'media_type': 'application/json'}]
    key = str(uuid4())
    completed = await service.finish_attempt(claim['attempt']['id'], request, key, verified_artifacts=inputs)
    return run, claim, completed, request, inputs, key


async def test_stage_has_named_markdown_saved_to_workspace_and_stale_versions_are_rejected(flow):
    service, store, artifacts, _, _ = flow
    run, claim, work, request, inputs, key = await complete_document(flow)
    presenter = RunPresentationService(store, artifacts, service.settings)
    view = await presenter.workflow(run['id'])
    assert len(view['stages']) == 1
    output = view['stages'][0]['output']
    assert output['name'] == '产品目标说明.md' and output['media_type'] == 'text/markdown'
    assert '给读者可理解' in Path(output['path']).read_text()
    assert not Path(output['path']).read_text().lstrip().startswith('{')
    human = [await store.read('artifact', identity) for identity in work['artifact_ids']]
    assert sum(bool(item.get('readable')) for item in human) == 1
    replay = await service.finish_attempt(claim['attempt']['id'], request, key, verified_artifacts=inputs)
    assert replay == work
    current = await service.run_detail(run['id'])
    await service.revise(run['id'], {'expected_revision': current['revision'], 'work_item_ids': [work['id']],
        'reason': '修改目标'}, 'new-version')
    with pytest.raises(DomainError, match='旧的工作版本'):
        await presenter.readable(output['artifact_id'])
    assert Path(output['path']).is_file(), 'Historical readable files must be retained'
    assert await service.finish_attempt(claim['attempt']['id'], request, key, verified_artifacts=inputs) == work


async def test_frozen_project_language_controls_finished_document_and_filename(flow):
    service, store, artifacts, project, _ = flow
    plan = await service.create_plan(plan_payload(project), 'english-plan')
    # This is the same contract frozen by product preparation before run start.
    plan = await store.command('test.language', 'freeze-language', {}, lambda tx: tx.put('plan', plan['id'],
        {**plan, 'product_contract': {'language': 'en'}}, plan['revision']))
    run = await service.start_run({'plan_id': plan['id'], 'expected_revision': plan['revision']}, 'english-start')
    claim = await service.claim_next(run['id'], 'test-worker', 'english-claim')
    blob = await artifacts.put_bytes(json.dumps({'title': 'Product Goal', 'summary': 'A compact task list',
        'content': 'Help one person organize daily tasks.', 'sources': [], 'unknowns': []}).encode())
    await service.finish_attempt(claim['attempt']['id'], {'execution_status': 'completed', 'quality_result': 'unknown',
        'fencing_token': claim['attempt']['fencing_token'], 'input_fingerprint': claim['attempt']['input_fingerprint']},
        'english-finish', verified_artifacts=[{'digest': blob['id'], 'name': 'openhands_final.json', 'media_type': 'application/json'}])
    presenter = RunPresentationService(store, artifacts, service.settings)
    output = (await presenter.workflow(run['id']))['stages'][0]['output']
    assert output['name'] == 'Product Goal.md'
    assert 'Help one person' in Path(output['path']).read_text()
    assert '产品目标' not in Path(output['path']).read_text()


async def test_workspace_document_edits_and_symlinks_are_never_overwritten(flow, tmp_path):
    service, store, artifacts, _, _ = flow
    run, _, work, *_ = await complete_document(flow)
    presenter = RunPresentationService(store, artifacts, service.settings)
    output = (await presenter.workflow(run['id']))['stages'][0]['output']
    path = Path(output['path'])
    path.write_text('用户修改，保留。')
    again = await presenter.document(work)
    assert again['path'] is None and again['storage_error']
    assert path.read_text() == '用户修改，保留。'
    path.unlink()
    private = tmp_path / 'private.txt'
    private.write_text('do not change')
    path.symlink_to(private)
    assert (await presenter.document(work))['storage_error']
    assert private.read_text() == 'do not change'


async def test_parallel_stage_is_one_outer_node_with_children_and_named_summary(flow):
    service, store, artifacts, project, _ = flow
    payload = plan_payload(project)
    payload['selection'] = {'mode': 'selected', 'selected_steps': ['research']}
    plan = await service.create_plan(payload, 'research-plan')
    run = await service.start_run({'plan_id': plan['id'], 'expected_revision': plan['revision']}, 'research-run')
    stage = next(w for w in await store.list('work_item') if w['key'] == 'research')
    await StageExpander(store).expand(run['id'], stage['id'], [
        {'key': 'market', 'goal': '市场研究', 'write_paths': []},
        {'key': 'technology', 'goal': '技术调研', 'write_paths': []},
    ], 'split', stage['revision'])
    for i in range(2):
        await service.claim_next(run['id'], f'worker-{i}', f'claim-{i}')
    view = await RunPresentationService(store, artifacts, service.settings).workflow(run['id'])
    assert len(view['stages']) == 1
    grouped = next(s for s in view['stages'] if s['step'] == 'research')
    assert len(grouped['tasks']) == 3 and grouped['tasks'][-1]['name'] == '调研分析汇总'
    assert grouped['tasks'][-1]['is_aggregation'] and grouped['status'] == 'running'
    assert grouped['output'] is None


async def test_preview_is_bounded_and_download_remains_available(flow):
    service, store, artifacts, _, _ = flow
    run, *_ = await complete_document(flow, '大文档' * 200000)
    presenter = RunPresentationService(store, artifacts, service.settings)
    output = (await presenter.workflow(run['id']))['stages'][0]['output']
    with pytest.raises(DomainError) as error:
        await presenter.readable(output['artifact_id'])
    assert error.value.code == 'preview_too_large'
    _, data = await presenter.readable(output['artifact_id'], preview=False)
    assert data['size'] > 1024 * 1024


async def test_projection_failure_cannot_rewrite_completed_work(flow, monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(RunPresentationService, 'document', AsyncMock(side_effect=ValueError('copy unavailable')))
    _, store, _, _, _ = flow
    run, _, work, *_ = await complete_document(flow)
    assert (await store.read('work_item', work['id']))['status'] == 'completed'
    assert (await store.read('run', run['id']))['execution_state'] == 'completed'
    outputs = [await store.read('artifact', identity) for identity in work['artifact_ids']]
    assert any(item.get('readable') for item in outputs)


async def test_stage_order_obeys_test_code_review_dependencies(flow):
    from agentflow.domain.planning import WorkSpec
    service, store, artifacts, _, _ = flow
    run = await start(flow)
    await service.add_work_items(run['id'], [
        WorkSpec('unit_test_implementation:review', 'code_review', 'review', dependencies=('unit-code',)),
        WorkSpec('unit-code', 'unit_test_implementation', 'unit_test', write_paths=('tests',)),
    ], run['revision'], 'code-and-review')
    stages = (await RunPresentationService(store, artifacts, service.settings).workflow(run['id']))['stages']
    names = [stage['name'] for stage in stages]
    assert names.index('单元测试编写') < names.index('单元测试代码审查')

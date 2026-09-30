import json
from uuid import uuid4

import pytest
from test_workflow import flow as flow
from test_workflow import plan_payload

from agentflow.common import DomainError


async def baseline(flow, project_id=None):
    _, store, artifacts, project, _ = flow
    content = await artifacts.put_bytes(json.dumps({'title': '产品背景', 'summary': '来自用户',
        'content': '保留长期产品目标', 'sources': [], 'unknowns': []}).encode())
    identity = str(uuid4())
    return await store.command('test.baseline', identity, {}, lambda tx: tx.put('artifact', identity, {
        'project_id': project_id or project['id'], 'run_id': None, 'work_item_id': None, 'generation': 0,
        'step': 'goal', 'digest': content['id'], 'source_kind': 'owner_input', 'quality_result': 'not_applicable',
        'stale': False, 'name': '用户产品目标.json', 'media_type': 'application/json'}))


async def test_stage_inputs_keep_same_project_frozen_references_and_recheck_at_start(flow):
    service, store, _, project, _ = flow
    artifact = await baseline(flow)
    ref = {'object_id': artifact['id'], 'fingerprint': artifact['digest'], 'revision': artifact['revision']}
    payload = {**plan_payload(project), 'selection': {'mode': 'selected', 'selected_steps': ['prd']},
        'stage_input_versions': {'prd': [ref]}}
    plan = await service.create_plan(payload, 'incremental-plan')
    assert plan['state'] == 'ready' and plan['actual_steps'] == ['prd']
    assert plan['stage_reused_inputs']['prd'] == [artifact['id']]
    await store.command('test.change-input', 'edit', {}, lambda tx: tx.put('artifact', artifact['id'],
        {**artifact, 'name': '修改后的名称'}, artifact['revision']))
    with pytest.raises(DomainError) as error:
        await service.start_run({'plan_id': plan['id'], 'expected_revision': plan['revision']}, 'start')
    assert error.value.code == 'stale_input'


async def test_cross_project_inputs_are_rejected_even_when_their_digest_is_real(flow):
    service, _, _, project, _ = flow
    artifact = await baseline(flow, 'another-project')
    plan = await service.create_plan({**plan_payload(project), 'input_versions': [
        {'object_id': artifact['id'], 'fingerprint': artifact['digest']}]}, 'foreign')
    assert plan['state'] == 'missing_inputs'
    assert any(item['code'] == 'invalid_input' for item in plan['missing_inputs'])


async def test_iteration_uses_confirmed_delivery_branch_instead_of_old_head(flow):
    service, store, _, project, _ = flow
    from pathlib import Path
    repo = Path(project['local_path'])
    original = await service._git(repo, 'rev-parse', 'HEAD')
    ref = 'refs/heads/codex/agentflow/test-delivery'
    await service._git(repo, 'checkout', '-b', 'codex/agentflow/test-delivery')
    (repo / 'delivered.txt').write_text('previous delivery')
    await service._git(repo, 'add', 'delivered.txt')
    await service._git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-m', 'delivery fixture')
    commit = await service._git(repo, 'rev-parse', 'HEAD')
    await service._git(repo, 'checkout', 'main')
    assert await service._git(repo, 'rev-parse', 'HEAD') == original
    def records(tx):
        tx.put('run', 'prior-run', {'project_id': project['id']})
        return tx.put('delivery', 'prior-delivery', {'run_id': 'prior-run', 'commit_oid': commit,
            'delivery_ref': ref, 'confirmed_at': '2026-09-21T00:00:00Z'})
    await store.command('test.delivery-record', 'delivery-record', {}, records)
    plan = await service.create_plan({**plan_payload(project), 'source_commit': commit, 'source_ref': ref}, 'delivery-base')
    assert plan['base_commit'] == commit and plan['base_ref'] == ref
    await service._git(repo, 'update-ref', ref, original)
    with pytest.raises(DomainError) as error:
        await service.start_run({'plan_id': plan['id'], 'expected_revision': plan['revision']}, 'changed-ref')
    assert error.value.code == 'stale_source_baseline'

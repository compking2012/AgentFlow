"""Current documents use stable paths while immutable evidence keeps all revisions."""
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from test_readable_workflow import complete_document
from test_workflow import flow as flow

from agentflow.common import DomainError
from agentflow.control.presentation import RunPresentationService


async def revise_and_complete(flow, run, work, text='# 当前目标\n\n已补充验收边界。'):
    service, store, artifacts, _, _ = flow
    current = await service.run_detail(run['id'])
    await service.revise(run['id'], {'expected_revision': current['revision'], 'work_item_ids': [work['id']],
        'reason': '补充验收边界'}, str(uuid4()))
    claim = await service.claim_next(run['id'], 'worker', str(uuid4()))
    blob = await artifacts.put_bytes(json.dumps({'content': text, 'summary': '已补充验收边界',
        'sources': [], 'unknowns': []}, ensure_ascii=False).encode())
    return await service.finish_attempt(claim['attempt']['id'], {'execution_status': 'completed',
        'quality_result': 'unknown', 'fencing_token': claim['attempt']['fencing_token'],
        'input_fingerprint': claim['attempt']['input_fingerprint']}, str(uuid4()),
        verified_artifacts=[{'digest': blob['id'], 'name': 'openhands_final.json', 'media_type': 'application/json'}])


async def test_rework_replaces_current_markdown_and_retains_issue_history_and_cas(flow):
    service, store, artifacts, _, _ = flow
    run, _, work, *_ = await complete_document(flow)
    presenter = RunPresentationService(store, artifacts, service.settings)
    first = await presenter.document(work)
    original_blob = await artifacts.read(first['digest'])
    revised = await revise_and_complete(flow, run, work)
    second = await presenter.document(revised)
    assert second['path'] == first['path'], 'Rework must update the same logical-stage document'
    paths = list((service.settings.data_dir / 'workspace_documents').rglob('*.md'))
    assert paths == [Path(second['path'])]
    content = Path(second['path']).read_text()
    assert '已补充验收边界' in content and '历史问题与处理' in content
    assert await artifacts.read(first['digest']) == original_blob
    with pytest.raises(DomainError, match='旧的工作版本|来源已更新'):
        await presenter.readable(first['artifact_id'])


async def test_rework_cannot_replace_user_modified_current_document(flow):
    service, store, artifacts, _, _ = flow
    run, _, work, *_ = await complete_document(flow)
    presenter = RunPresentationService(store, artifacts, service.settings)
    first = await presenter.document(work)
    path = Path(first['path'])
    path.write_text('用户的补充，不要覆盖。')
    revised = await revise_and_complete(flow, run, work)
    second = await presenter.document(revised)
    assert second['path'] is None and second['storage_error']
    assert path.read_text() == '用户的补充，不要覆盖。'
    assert list(path.parents[1].rglob('*.md')) == [path]


async def test_old_generation_cannot_publish_after_new_generation_has_finished(flow):
    service, store, artifacts, _, _ = flow
    run, _, work, *_ = await complete_document(flow)
    presenter = RunPresentationService(store, artifacts, service.settings)
    old = await presenter.document(work)
    revised = await revise_and_complete(flow, run, work)
    current = await presenter.document(revised)
    try:
        await presenter.document(work)
    except DomainError as error:
        assert error.code == 'stale_artifact'
    assert Path(current['path']).read_text().count('已补充验收边界') >= 1
    assert old['path'] == current['path']
    await asyncio.gather(*(presenter.document(revised) for _ in range(4)))
    assert len(list(Path(current['path']).parents[1].rglob('*.md'))) == 1


async def test_pending_rework_keeps_latest_body_with_explicit_unfinished_status(flow):
    service, store, artifacts, _, _ = flow
    run, _, work, *_ = await complete_document(flow)
    presenter = RunPresentationService(store, artifacts, service.settings)
    before = await presenter.document(work)
    current = await service.run_detail(run['id'])
    await service.revise(run['id'], {'expected_revision': current['revision'], 'work_item_ids': [work['id']],
        'reason': '补充验收边界'}, str(uuid4()))
    pending = await presenter.document(await store.read('work_item', work['id']))
    assert pending and pending['path'] == before['path']
    content = Path(pending['path']).read_text()
    assert '给读者可理解' in content and '当前阶段尚未完成' in content
    assert '补充验收边界 — 待处理' in content


async def test_registered_legacy_child_copies_are_removed_but_user_files_and_edits_survive(flow):
    service, store, artifacts, _, _ = flow
    run, _, work, *_ = await complete_document(flow)
    presenter = RunPresentationService(store, artifacts, service.settings)
    current = await presenter.document(work)
    record = await store.read('readable_artifact', current['artifact_id'])
    child_id = str(uuid4())
    old_parent = service.settings.data_dir / 'workspace_documents' / run['id'] / ('目标整理-' + work['id'][:12] + '-v1')
    old_child = old_parent.parent / ('目标整理-' + child_id[:12] + '-v1')
    old_parent.mkdir()
    old_child.mkdir()
    modified = old_parent / current['name']
    modified.write_text('用户修改过的旧副本')
    retained = old_child / '用户笔记.md'
    retained.write_text('私人笔记')
    generated = old_child / '产品目标说明 - child.md'
    generated.write_bytes(await artifacts.read(current['digest']))

    def legacy(tx):
        tx.put('work_item', child_id, {**{k: v for k, v in work.items() if k not in {'id', 'revision'}},
            'parent_stage_id': work['id'], 'artifact_ids': [], 'key': 'goal:child'})
        for identity, work_id, name in [('legacy-parent', work['id'], current['name']),
                                        ('legacy-child', child_id, generated.name)]:
            tx.put('readable_artifact', identity, {k: v for k, v in {**record, 'work_item_id': work_id,
                'name': name}.items() if k not in {'id', 'revision', 'projection_key', 'projection_state', 'logical_stage_key'}})
        return {}
    await store.command('fixture.legacy', 'seed', {}, legacy)
    await presenter.document(await store.read('work_item', work['id']))
    assert not generated.exists(), 'Registered child copies must be folded into the stage projection'
    assert modified.read_text() == '用户修改过的旧副本'
    assert retained.read_text() == '私人笔记'
    assert await artifacts.read(current['digest'])


async def test_child_preview_never_materializes_an_extra_document(flow):
    service, store, artifacts, _, _ = flow
    run, _, work, *_ = await complete_document(flow)
    child_id = str(uuid4())
    blob = await artifacts.put_bytes(b'{"content":"Child body"}')
    def seed(tx):
        artifact = tx.put('artifact', 'child-source', {'work_item_id': child_id, 'run_id': run['id'],
            'generation': 1, 'digest': blob['id'], 'name': 'openhands_final.json', 'media_type': 'application/json'})
        return tx.put('work_item', child_id, {**{k: v for k, v in work.items() if k not in {'id', 'revision'}},
            'parent_stage_id': work['id'], 'artifact_ids': [artifact['id']], 'key': 'goal:child'})
    child = await store.command('fixture.child', 'seed', {}, seed)
    presenter = RunPresentationService(store, artifacts, service.settings)
    output = await presenter.document(child)
    assert output['path'] is None and output['preview_url']
    assert len(list((service.settings.data_dir / 'workspace_documents').rglob('*.md'))) == 1


async def test_current_link_invalidates_when_child_state_changes(flow):
    service, store, artifacts, _, _ = flow
    run, _, work, *_ = await complete_document(flow)
    presenter = RunPresentationService(store, artifacts, service.settings)
    before = await presenter.document(work)
    def seed(tx):
        tx.put('work_item', 'child-pending', {**{k: v for k, v in work.items() if k not in {'id', 'revision'}},
            'parent_stage_id': work['id'], 'artifact_ids': [], 'key': 'goal:child', 'status': 'pending'})
        return {}
    await store.command('fixture.child', 'pending', {}, seed)
    with pytest.raises(DomainError, match='来源已更新'):
        await presenter.readable(before['artifact_id'])
    after = await presenter.document(work)
    assert '当前阶段尚未完成' in Path(after['path']).read_text()


async def test_product_ownership_is_rechecked_before_current_copy_is_written(flow, tmp_path, monkeypatch):
    service, store, artifacts, project, _ = flow
    _, _, work, *_ = await complete_document(flow)
    output = tmp_path / 'product-output'
    output.mkdir()
    marker = output / '.agentflow-product.json'
    marker.write_text(json.dumps({'product_id': 'owned-product'}))
    await store.command('fixture.product', 'seed', {}, lambda tx: tx.put('product', 'owned-product', {
        'project_id': project['id'], 'output_directory': str(output)}))
    presenter = RunPresentationService(store, artifacts, service.settings)
    original = presenter._directory
    async def change_owner(item):
        directory = await original(item)
        marker.write_text(json.dumps({'product_id': 'someone-else'}))
        return directory
    monkeypatch.setattr(presenter, '_directory', change_owner)
    result = await presenter.document(work)
    assert result['path'] is None and result['storage_error']
    assert not list(output.rglob('*.md'))


async def test_registered_child_copy_is_cleaned_even_before_aggregation_exists(flow):
    from test_workflow import start
    service, store, artifacts, _, _ = flow
    run = await start(flow)
    root = next(work for work in await store.list('work_item') if work['run_id'] == run['id'])
    child_id = 'old-child-before-summary'
    blob = await artifacts.put_bytes(b'# Child report\n')
    directory = service.settings.data_dir / 'workspace_documents' / run['id'] / ('目标整理-' + child_id[:12] + '-v1')
    directory.mkdir(parents=True)
    old = directory / '产品目标说明 - child.md'
    old.write_bytes(b'# Child report\n')
    def seed(tx):
        tx.put('work_item', child_id, {**{key: value for key, value in root.items() if key not in {'id', 'revision'}},
            'key': 'goal:child', 'parent_stage_id': root['id'], 'status': 'completed'})
        tx.put('readable_artifact', 'old-child-readable', {'run_id': run['id'], 'work_item_id': child_id,
            'generation': 1, 'digest': blob['id'], 'name': old.name, 'source_versions': []})
        return {}
    await store.command('fixture.child', 'before-summary', {}, seed)
    assert await RunPresentationService(store, artifacts, service.settings).document(root) is None
    assert not old.exists()
    assert await artifacts.read(blob['id']) == b'# Child report\n'


async def test_registered_archived_root_copy_is_folded_into_current_stage(flow):
    service, store, artifacts, _, _ = flow
    run, _, work, *_ = await complete_document(flow)
    presenter = RunPresentationService(store, artifacts, service.settings)
    current = await presenter.document(work)
    archived_id = 'archived-root-version'
    directory = service.settings.data_dir / 'workspace_documents' / run['id'] / ('目标整理-' + archived_id[:12] + '-v1')
    directory.mkdir(parents=True)
    old = directory / current['name']
    old.write_bytes(await artifacts.read(current['digest']))
    def seed(tx):
        tx.put('work_item', archived_id, {**{key: value for key, value in work.items() if key not in {'id', 'revision'}},
            'key': 'prior-goal', 'archived': True})
        tx.put('readable_artifact', 'archived-readable', {'run_id': run['id'], 'work_item_id': archived_id,
            'generation': 1, 'digest': current['digest'], 'name': old.name, 'source_versions': []})
        return {}
    await store.command('fixture.archived', 'root', {}, seed)
    await presenter.document(work)
    assert not old.exists()
    assert Path(current['path']).is_file()


async def test_repeated_workflow_reads_do_not_append_commands_or_readable_records(flow):
    import sqlite3
    service, store, artifacts, _, _ = flow
    run, _, work, *_ = await complete_document(flow)
    presenter = RunPresentationService(store, artifacts, service.settings)
    await presenter.workflow(run['id'])
    def commands_count():
        with sqlite3.connect('file:' + str(store.database_path) + '?mode=ro', uri=True) as connection:
            return connection.execute('SELECT count(*) FROM commands').fetchone()[0]
    before = commands_count()
    records = await store.list('readable_artifact')
    for _ in range(3):
        await presenter.workflow(run['id'])
        await presenter.document(work)
    assert commands_count() == before
    assert await store.list('readable_artifact') == records


async def test_passing_review_with_new_warning_preserves_old_blocker_outcome(flow):
    from agentflow.control.documents_projection import render_group_document
    from agentflow.control.workflow_stages import group_workflow_stages
    _, _, artifacts, _, _ = flow
    works, records = [], []
    for identity, dependencies, quality, findings in [
        ('initial', [], 'failed', [{'severity': 'blocking', 'description': 'Missing owner check'}]),
        ('current', ['initial'], 'passed', [{'severity': 'warning', 'description': 'Could simplify naming'}]),
    ]:
        blob = await artifacts.put_bytes(json.dumps({'findings': findings}).encode())
        records.append({'id': identity + '-source', 'work_item_id': identity, 'run_id': 'run',
                        'generation': 1, 'digest': blob['id'], 'name': 'openhands_final.json'})
        works.append({'id': identity, 'run_id': 'run', 'generation': 1, 'step': 'code_review',
            'key': 'code_review' if identity == 'initial' else 'repair:review', 'dependencies': dependencies,
            'status': 'completed', 'quality_result': quality, 'artifact_ids': [identity + '-source']})
    group = group_workflow_stages(works)[0]
    result = await render_group_document(group, works, artifacts, records)
    assert 'Missing owner check — 当前审查已无阻塞，未再报告此问题' in result['content'].decode()
    assert 'Could simplify naming' in result['content'].decode()


async def test_delayed_old_publication_cannot_overwrite_new_generation(flow, monkeypatch):
    service, store, artifacts, _, _ = flow
    run, _, work, *_ = await complete_document(flow)
    presenter = RunPresentationService(store, artifacts, service.settings)
    initial = await presenter.document(work)
    reached, resume = asyncio.Event(), asyncio.Event()
    original = presenter._directory
    async def delayed(item):
        directory = await original(item)
        reached.set()
        await resume.wait()
        return directory
    monkeypatch.setattr(presenter, '_directory', delayed)
    in_flight = asyncio.create_task(presenter.document(work))
    await asyncio.wait_for(reached.wait(), 2)
    revised = await revise_and_complete(flow, run, work)
    current = await RunPresentationService(store, artifacts, service.settings).document(revised)
    resume.set()
    late = await asyncio.wait_for(in_flight, 2)
    assert late['path'] is None and late['storage_error']
    assert current['path'] == initial['path']
    assert '已补充验收边界' in Path(current['path']).read_text()
    with pytest.raises(DomainError):
        await presenter.readable(late['artifact_id'])


async def test_new_repair_root_records_requested_fix_in_current_code_document(flow):
    from agentflow.control.documents_projection import render_group_document
    from agentflow.control.workflow_stages import group_workflow_stages
    _, _, artifacts, _, _ = flow
    works, records = [], []
    for identity, dependencies, body in [('initial', [], 'Initial implementation'),
                                       ('repair', ['initial'], 'Repaired implementation')]:
        blob = await artifacts.put_bytes(json.dumps({'summary': body, 'commit_oid': identity}).encode())
        records.append({'id': identity + '-source', 'work_item_id': identity, 'run_id': 'run',
                        'generation': 1, 'digest': blob['id'], 'name': 'codex_final.json'})
        works.append({'id': identity, 'run_id': 'run', 'generation': 1, 'step': 'implementation', 'key': identity,
            'dependencies': dependencies, 'status': 'completed', 'quality_result': 'passed',
            'artifact_ids': [identity + '-source'], 'payload': {'change_expectation': '补齐权限检查'} if identity == 'repair' else {}})
    result = await render_group_document(group_workflow_stages(works)[0], works, artifacts, records)
    assert 'Repaired implementation' in result['content'].decode()
    assert '补齐权限检查 — 已在当前版本重新生成' in result['content'].decode()


async def test_current_readable_link_rejects_source_invalidation_without_generation_change(flow):
    service, store, artifacts, _, _ = flow
    _, _, work, *_ = await complete_document(flow)
    presenter = RunPresentationService(store, artifacts, service.settings)
    document = await presenter.document(work)
    def invalidate(tx):
        source = tx.get('artifact', work['artifact_ids'][0])
        return tx.put('artifact', source['id'], {**source, 'stale': True}, source['revision'])
    await store.command('fixture.source', 'invalidate', {}, invalidate)
    with pytest.raises(DomainError, match='来源已更新'):
        await presenter.readable(document['artifact_id'])

"""Project documents accumulate change history while run snapshots remain immutable."""
import asyncio
import json
from pathlib import Path

import pytest
from test_workflow import flow as flow

from agentflow.common import DomainError
from agentflow.control.presentation import RunPresentationService


async def seed_run(flow, tmp_path, *, run_id='r1', product_id='p1', change=None, body='# 范围\n\n原有功能。',
                   language='zh-CN', baseline=None, stage='prd', pending=False):
    service, store, artifacts, project, _ = flow
    output = tmp_path / product_id
    output.mkdir(exist_ok=True)
    (output / '.agentflow-product.json').write_text(json.dumps({'product_id': product_id}))
    blob = await artifacts.put_bytes(body.encode())
    def seed(tx):
        product = tx.get('product', product_id)
        tx.put('product', product_id, {**(product or {}), 'project_id': project['id'], 'name': '测试产品',
            'output_directory': str(output), 'run_id': run_id, 'current_change_id': change,
            'run_ids': list(dict.fromkeys([*(product or {}).get('run_ids', []), run_id]))},
            product['revision'] if product else None)
        contract = {'product_id': product_id, 'language': language, 'change_id': change}
        if baseline is not None:
            contract['document_baseline'] = baseline
        tx.put('plan', 'plan-' + run_id, {'project_id': project['id'], 'product_contract': contract})
        tx.put('run', run_id, {'project_id': project['id'], 'plan_id': 'plan-' + run_id, 'created_at': '2026-09-29T01:00:00Z'})
        if change:
            tx.put('product_change', change, {'product_id': product_id, 'run_id': run_id,
                'title': change, 'description': '补充' + change, 'created_at': '2026-09-29T02:00:00Z',
                'document_baseline': baseline})
        tx.put('artifact', 'a-' + run_id + stage, {'run_id': run_id, 'work_item_id': 'w-' + run_id + stage,
            'generation': 1, 'digest': blob['id'], 'name': stage + '.md', 'readable': True, 'media_type': 'text/markdown'})
        return tx.put('work_item', 'w-' + run_id + stage, {'run_id': run_id, 'key': stage, 'step': stage,
            'generation': 1, 'status': 'pending' if pending else 'completed', 'quality_result': 'passed',
            'dependencies': [], 'artifact_ids': [] if pending else ['a-' + run_id + stage]})
    work = await store.command('fixture.project-document', run_id + stage, {}, seed)
    return RunPresentationService(store, artifacts, service.settings), work, output


async def test_changes_share_stage_path_and_history_precedes_cumulative_body(flow, tmp_path):
    presenter, work, output = await seed_run(flow, tmp_path)
    first = await presenter.document(work)
    assert Path(first['path']) == output / 'documents/prd/产品需求文档 PRD.md'
    from agentflow.control.project_documents import ProjectDocumentService
    docs = ProjectDocumentService(flow[1], flow[2], flow[0].settings)
    baseline = await docs.freeze('p1')
    presenter, work, _ = await seed_run(flow, tmp_path, run_id='r2', change='筛选功能', baseline=baseline,
        body='# 范围\n\n原有功能。\n\n## 筛选\n\n支持筛选。')
    second = await presenter.document(work)
    baseline = await docs.freeze('p1')
    presenter, work, _ = await seed_run(flow, tmp_path, run_id='r3', change='导出功能', baseline=baseline,
        body='# 范围\n\n原有功能。\n\n## 筛选\n\n支持筛选。\n\n## 导出\n\n支持导出。')
    third = await presenter.document(work)
    assert first['path'] == second['path'] == third['path']
    text = Path(third['path']).read_text()
    assert text.count('## 变更记录') == 1 and text.index('## 变更记录') < text.index('# 范围')
    assert all(value in text for value in ('初始版本', '筛选功能', '导出功能', '原有功能。', '支持筛选。', '支持导出。'))
    assert len(list((output / 'documents').rglob('*.md'))) == 1
    assert (await presenter.readable(first['artifact_id']))[0]['digest'] == first['digest']


async def test_freezing_documents_is_independent_of_project_code_checkout_errors(flow, tmp_path):
    from agentflow.control.project_documents import ProjectDocumentService
    _, work, output = await seed_run(flow, tmp_path, stage='implementation')
    def code_error(tx):
        current = tx.get('work_item', work['id'])
        tx.put('work_item', work['id'], {**current, 'attempt_id': 'old-code'}, current['revision'])
        tx.put('code_snapshot', 'old-code', {'work_item_id': work['id'], 'generation': 1,
            'repository_path': str(output / 'repository'), 'commit_oid': 'a' * 40})
        return tx.put('project_code', flow[3]['id'], {'run_id': work['run_id'], 'snapshot_id': 'old-code',
            'state': 'blocked', 'error': 'Owner checked out another branch; do not change it.'})
    await flow[1].command('fixture.code-error', 'once', {}, code_error)
    service = ProjectDocumentService(flow[1], flow[2], flow[0].settings)
    result = await service.freeze('p1')
    assert result['documents']['implementation']['artifact_id']
    assert len(list((output / 'documents/implementation').glob('*.md'))) == 1
    assert (await flow[1].read('project_code', flow[3]['id']))['state'] == 'blocked'


async def test_historical_read_does_not_roll_back_current_and_language_keeps_one_file(flow, tmp_path):
    presenter, work, output = await seed_run(flow, tmp_path)
    first = await presenter.document(work)
    presenter, current, _ = await seed_run(flow, tmp_path, run_id='r2', change='English update', language='en',
        body='# Scope\n\nUpdated scope.')
    second = await presenter.document(current)
    after = Path(second['path']).read_bytes()
    historical = await presenter.document(work)
    assert historical['digest'] == first['digest']
    assert historical['path'] is None
    assert Path(second['path']).read_bytes() == after
    assert first['path'] == second['path']
    assert len(list((output / 'documents').rglob('*.md'))) == 1


async def test_pending_change_preserves_existing_stage_until_replacement(flow, tmp_path):
    presenter, work, _ = await seed_run(flow, tmp_path)
    first = await presenter.document(work)
    from agentflow.control.project_documents import ProjectDocumentService
    docs = ProjectDocumentService(flow[1], flow[2], flow[0].settings)
    baseline = await docs.freeze('p1')
    before = Path(first['path']).read_bytes()
    presenter, pending, _ = await seed_run(flow, tmp_path, run_id='r2', change='待处理变更', baseline=baseline, pending=True)
    inherited = await presenter.document(pending)
    assert inherited['digest'] == first['digest']
    assert Path(first['path']).read_bytes() == before


async def test_products_with_same_project_never_crosswrite(flow, tmp_path):
    presenter, first_work, first_output = await seed_run(flow, tmp_path)
    first = await presenter.document(first_work)
    presenter, second_work, second_output = await seed_run(flow, tmp_path, product_id='p2', run_id='other', body='# 独立产品')
    second = await presenter.document(second_work)
    assert Path(first['path']).is_relative_to(first_output)
    assert Path(second['path']).is_relative_to(second_output)
    assert '独立产品' not in Path(first['path']).read_text()


async def test_manual_content_blocks_publication_and_freeze(flow, tmp_path):
    presenter, work, _ = await seed_run(flow, tmp_path)
    first = await presenter.document(work)
    path = Path(first['path'])
    path.write_text('手工修改必须保留')
    presenter, work, _ = await seed_run(flow, tmp_path, run_id='r2', change='新功能', body='# 新文档')
    blocked = await presenter.document(work)
    assert blocked['path'] is None and blocked['storage_error']
    assert path.read_text() == '手工修改必须保留'
    from agentflow.control.project_documents import ProjectDocumentService
    with pytest.raises(DomainError):
        await ProjectDocumentService(flow[1], flow[2], flow[0].settings).freeze('p1')


async def test_delayed_old_run_cannot_publish_after_current_change_moves(flow, tmp_path, monkeypatch):
    presenter, work, _ = await seed_run(flow, tmp_path)
    first = await presenter.document(work)
    from agentflow.control.project_documents import ProjectDocumentService
    original = ProjectDocumentService._directory
    reached, resume = asyncio.Event(), asyncio.Event()
    async def delayed(self, *args):
        result = await original(self, *args)
        reached.set()
        await resume.wait()
        return result
    monkeypatch.setattr(ProjectDocumentService, '_directory', delayed)
    in_flight = asyncio.create_task(presenter.document(work))
    await asyncio.wait_for(reached.wait(), 2)
    presenter, current, _ = await seed_run(flow, tmp_path, run_id='r2', change='新版本', body='# 新版本正文')
    monkeypatch.setattr(ProjectDocumentService, '_directory', original)
    second = await presenter.document(current)
    resume.set()
    await in_flight
    assert first['path'] == second['path']
    assert '新版本正文' in Path(second['path']).read_text()


async def legacy_projection(flow, tmp_path, *, run_id='r1', change=None, body='# 原始范围\n\n已有功能。'):
    presenter, work, output = await seed_run(flow, tmp_path, run_id=run_id, change=change, body=body)
    source = await flow[1].read('artifact', work['artifact_ids'][0])
    old = output / 'documents' / run_id / 'prd' / '产品需求文档 PRD.md'
    old.parent.mkdir(parents=True)
    old.write_bytes(await flow[2].read(source['digest']))
    await flow[1].command('fixture.legacy', run_id, {}, lambda tx: tx.put('readable_artifact', 'legacy-' + run_id, {
        'run_id': run_id, 'work_item_id': work['id'], 'generation': 1, 'digest': source['digest'],
        'name': old.name, 'projection_key': run_id + '-prd', 'logical_stage_key': 'prd', 'projection_state': 'old',
        'source_versions': [{'id': source['id'], 'digest': source['digest'], 'generation': 1, 'stale': False}]}))
    return presenter, work, output, old


async def test_migration_flattens_registered_runs_in_order_and_keeps_history_immutable(flow, tmp_path):
    from agentflow.control.project_documents import ProjectDocumentService
    presenter, initial, output, old1 = await legacy_projection(flow, tmp_path)
    _, current, _, old2 = await legacy_projection(flow, tmp_path, run_id='r2', change='增加导出',
        body='# 原始范围\n\n已有功能。\n\n## 导出\n\n导出功能。')
    docs = ProjectDocumentService(flow[1], flow[2], flow[0].settings)
    result = await docs.migrate('p1')
    assert result['conflicts'] == []
    assert not old1.exists() and not old2.exists()
    flat = output / 'documents/prd/产品需求文档 PRD.md'
    text = flat.read_text()
    assert '增加导出' in text and '初始版本' in text and '导出功能。' in text
    before = flat.read_bytes()
    historical = await presenter.document(initial)
    assert '导出功能。' not in (await flow[2].read(historical['digest'])).decode()
    assert flat.read_bytes() == before
    again = await docs.migrate('p1')
    assert again['conflicts'] == [] and flat.read_bytes() == before
    assert (await presenter.document(current))['path'] == str(flat)


@pytest.mark.parametrize('conflict', ['manual', 'unknown', 'symlink', 'stale'])
async def test_migration_preflight_preserves_unproven_or_changed_files(flow, tmp_path, conflict):
    from agentflow.control.project_documents import ProjectDocumentService
    _, work, output, old = await legacy_projection(flow, tmp_path)
    if conflict == 'manual':
        old.write_text('手工修订')
    elif conflict == 'unknown':
        (old.parent / '笔记.md').write_text('不明来源')
    elif conflict == 'symlink':
        old.unlink()
        target = tmp_path / 'private.md'
        target.write_text('私有内容')
        old.symlink_to(target)
    else:
        def stale(tx):
            row = tx.get('artifact', work['artifact_ids'][0])
            return tx.put('artifact', row['id'], {**row, 'stale': True}, row['revision'])
        await flow[1].command('fixture.stale', 'one', {}, stale)
    before = old.read_bytes()
    result = await ProjectDocumentService(flow[1], flow[2], flow[0].settings).migrate('p1')
    assert result['conflicts']
    assert old.read_bytes() == before
    assert not (output / 'documents/prd/产品需求文档 PRD.md').exists()


async def test_file_mutation_during_publication_rolls_back_head(flow, tmp_path, monkeypatch):
    import agentflow.control.project_documents as module
    presenter, work, _ = await seed_run(flow, tmp_path)
    first = await presenter.document(work)
    head = (await flow[1].list('project_document_head'))[0]
    presenter, new, _ = await seed_run(flow, tmp_path, run_id='r2', change='更新', body='# 新正文')
    original = module.write_current_copy
    def changed(path, content, known_digests=()):
        path.write_text('写入前的手工修改')
        return original(path, content, known_digests)
    monkeypatch.setattr(module, 'write_current_copy', changed)
    result = await presenter.document(new)
    assert result['storage_error'] and not result['path']
    assert Path(first['path']).read_text() == '写入前的手工修改'
    assert (await flow[1].list('project_document_head'))[0] == head


async def test_frozen_old_export_includes_unchanged_stages_without_future_content(flow, tmp_path):
    from agentflow.control.project_documents import ProjectDocumentService
    presenter, work, _ = await seed_run(flow, tmp_path, stage='goal', body='# 原始目标')
    await presenter.document(work)
    docs = ProjectDocumentService(flow[1], flow[2], flow[0].settings)
    baseline = await docs.freeze('p1')
    presenter, work, _ = await seed_run(flow, tmp_path, run_id='r2', change='需求整理', baseline=baseline)
    second = await presenter.document(work)
    frozen = await docs.freeze('p1', 'r2')
    assert set(frozen['documents']) == {'goal', 'prd'}
    presenter, work, _ = await seed_run(flow, tmp_path, run_id='r3', change='未来需求', baseline=frozen, body='# 未来内容')
    third = await presenter.document(work)
    old = await docs.freeze('p1', 'r2')
    assert old == frozen
    assert old['documents']['prd']['digest'] == second['digest'] != third['digest']


async def test_legacy_change_freeze_inherits_missing_stages_without_explicit_baseline(flow, tmp_path):
    from agentflow.control.project_documents import ProjectDocumentService
    presenter, initial, output = await seed_run(flow, tmp_path, stage='goal', body='# 保留原始目标')
    first = await presenter.document(initial)
    presenter, current, _ = await seed_run(flow, tmp_path, run_id='r2', change='旧版需求', body='# 新增需求')
    await presenter.document(current)
    result = await ProjectDocumentService(flow[1], flow[2], flow[0].settings).freeze('p1')
    assert set(result['documents']) == {'goal', 'prd'}
    assert result['documents']['goal']['digest'] == first['digest']
    assert (output / 'documents/goal/产品目标说明.md').is_file()


async def test_freeze_checks_manual_edits_in_unchanged_inherited_stage(flow, tmp_path):
    from agentflow.control.project_documents import ProjectDocumentService
    presenter, initial, _ = await seed_run(flow, tmp_path, stage='goal', body='# 原始目标')
    first = await presenter.document(initial)
    docs = ProjectDocumentService(flow[1], flow[2], flow[0].settings)
    baseline = await docs.freeze('p1')
    presenter, current, _ = await seed_run(flow, tmp_path, run_id='r2', change='新需求', baseline=baseline)
    await presenter.document(current)
    Path(first['path']).write_text('原始目标手工修改')
    with pytest.raises(DomainError):
        await docs.freeze('p1')
    historical = await docs.freeze('p1', 'r1', materialize=False)
    assert historical['documents']['goal']['digest'] == first['digest']
    assert Path(first['path']).read_text() == '原始目标手工修改'


async def test_export_documents_use_selected_run_snapshots_and_stage_directories(flow, tmp_path):
    from agentflow.control.product_exports import ProductExporter
    from agentflow.control.project_documents import ProjectDocumentService
    presenter, work, _ = await seed_run(flow, tmp_path, stage='goal', body='# 原始目标')
    await presenter.document(work)
    docs = ProjectDocumentService(flow[1], flow[2], flow[0].settings)
    baseline = await docs.freeze('p1')
    presenter, work, _ = await seed_run(flow, tmp_path, run_id='r2', change='需求二', baseline=baseline, body='# 第二版需求')
    await presenter.document(work)
    exporter = ProductExporter(flow[1], flow[2], None, flow[0].settings.data_dir)
    destination = tmp_path / 'export-before'
    destination.mkdir()
    run = await flow[1].read('run', 'r2')
    plan = await flow[1].read('plan', run['plan_id'])
    product = await flow[1].read('product', 'p1')
    await exporter._write_documents(destination, product, run, plan)
    before = {str(path.relative_to(destination)): path.read_bytes() for path in destination.rglob('*.md')}
    assert set(before) == {'goal/产品目标说明.md', 'prd/产品需求文档 PRD.md'}
    presenter, work, _ = await seed_run(flow, tmp_path, run_id='r3', change='未来版本', body='# 不得泄漏的未来内容')
    await presenter.document(work)
    after_directory = tmp_path / 'export-after'
    after_directory.mkdir()
    await exporter._write_documents(after_directory, await flow[1].read('product', 'p1'), run, plan)
    assert {str(path.relative_to(after_directory)): path.read_bytes() for path in after_directory.rglob('*.md')} == before


async def test_regeneration_issue_history_is_at_document_front(flow, tmp_path):
    from test_current_documents import revise_and_complete
    from test_readable_workflow import complete_document
    service, store, artifacts, _, _ = flow
    run, _, work, *_ = await complete_document(flow)
    presenter = RunPresentationService(store, artifacts, service.settings)
    await presenter.document(work)
    revised = await revise_and_complete(flow, run, work)
    descriptor = await presenter.document(revised)
    content = Path(descriptor['path']).read_text()
    assert content.index('## 历史问题与处理') < content.index('# 当前目标')


async def test_migration_retains_previous_body_for_pending_current_stage(flow, tmp_path):
    from agentflow.control.project_documents import ProjectDocumentService
    _, _, output, old = await legacy_projection(flow, tmp_path)
    await seed_run(flow, tmp_path, run_id='r2', change='正在生成', pending=True)
    result = await ProjectDocumentService(flow[1], flow[2], flow[0].settings).migrate('p1')
    assert result['conflicts'] == []
    flat = output / 'documents/prd/产品需求文档 PRD.md'
    assert flat.is_file() and '已有功能。' in flat.read_text()
    assert not old.exists()


async def test_migration_keeps_legacy_copy_until_current_identity_can_publish(flow, tmp_path):
    from agentflow.control.project_documents import ProjectDocumentService
    _, _, output, old = await legacy_projection(flow, tmp_path)
    def preparing(tx):
        product = tx.get('product', 'p1')
        return tx.put('product', 'p1', {**product, 'current_change_id': 'not-attached'}, product['revision'])
    await flow[1].command('fixture.preparing', 'once', {}, preparing)
    result = await ProjectDocumentService(flow[1], flow[2], flow[0].settings).migrate('p1')
    assert result['conflicts']
    assert old.is_file()
    assert not (output / 'documents/prd/产品需求文档 PRD.md').exists()


async def test_migration_reports_file_changed_during_safe_cleanup(flow, tmp_path, monkeypatch):
    import agentflow.control.documents_projection as projection
    from agentflow.control.project_documents import ProjectDocumentService
    _, _, _, old = await legacy_projection(flow, tmp_path)
    original = projection.prune_legacy_copies
    def changed(candidates):
        old.write_text('清理前手工修改')
        return original(candidates)
    monkeypatch.setattr(projection, 'prune_legacy_copies', changed)
    result = await ProjectDocumentService(flow[1], flow[2], flow[0].settings).migrate('p1')
    assert result['conflicts']
    assert old.read_text() == '清理前手工修改'


async def test_migration_removes_empty_legacy_stage_scaffolding(flow, tmp_path):
    from agentflow.control.project_documents import ProjectDocumentService
    _, _, output, _ = await legacy_projection(flow, tmp_path)
    (output / 'documents/r1/unit_test_execution').mkdir()
    (output / 'documents/r1/integration_test_execution').mkdir()
    result = await ProjectDocumentService(flow[1], flow[2], flow[0].settings).migrate('p1')
    assert result['conflicts'] == []
    assert not (output / 'documents/r1').exists()


async def test_migration_accepts_frozen_historical_stale_sources(flow, tmp_path):
    from agentflow.control.project_documents import ProjectDocumentService
    _, work, output, old = await legacy_projection(flow, tmp_path)
    store = flow[1]
    def history(tx):
        source = tx.get('artifact', work['artifact_ids'][0])
        tx.put('artifact', source['id'], {**source, 'stale': True}, source['revision'])
        readable = tx.get('readable_artifact', 'legacy-r1')
        return tx.put('readable_artifact', readable['id'], {**readable,
            'source_versions': [{**row, 'stale': True} for row in readable['source_versions']]}, readable['revision'])
    await store.command('fixture.historical-stale', 'one', {}, history)
    result = await ProjectDocumentService(store, flow[2], flow[0].settings).migrate('p1')
    assert result['conflicts'] == []
    assert not old.exists()
    assert '已有功能。' in (output / 'documents/prd/产品需求文档 PRD.md').read_text()


def finder_metadata():
    import struct
    return struct.pack('>I4sIII', 1, b'Bud1', 32, 32, 32) + bytes(48)


async def test_migration_removes_only_verified_regular_finder_metadata(flow, tmp_path):
    from agentflow.control.project_documents import ProjectDocumentService
    _, _, output, old = await legacy_projection(flow, tmp_path)
    metadata = old.parent.parent / '.DS_Store'
    metadata.write_bytes(finder_metadata())
    result = await ProjectDocumentService(flow[1], flow[2], flow[0].settings).migrate('p1')
    assert result['conflicts'] == []
    assert not metadata.exists() and not old.exists()
    assert (output / 'documents/prd/产品需求文档 PRD.md').is_file()


@pytest.mark.parametrize('kind', ['fake', 'truncated', 'symlink', 'hardlink'])
async def test_migration_preserves_unverified_finder_metadata(flow, tmp_path, kind):
    from agentflow.control.project_documents import ProjectDocumentService
    _, _, _, old = await legacy_projection(flow, tmp_path)
    metadata = old.parent.parent / '.DS_Store'
    if kind in {'symlink', 'hardlink'}:
        target = tmp_path / 'original-data'
        target.write_bytes(finder_metadata())
        metadata.symlink_to(target) if kind == 'symlink' else metadata.hardlink_to(target)
    else:
        metadata.write_bytes(b'private notes' if kind == 'fake' else finder_metadata()[:8])
    before = metadata.read_bytes()
    result = await ProjectDocumentService(flow[1], flow[2], flow[0].settings).migrate('p1')
    assert result['conflicts']
    assert metadata.read_bytes() == before and old.exists()


async def test_current_history_repairs_after_earlier_run_is_first_materialized(flow, tmp_path):
    presenter, initial, output = await seed_run(flow, tmp_path)
    presenter, current, _ = await seed_run(flow, tmp_path, run_id='r2', change='先查看当前需求', body='# 新增范围')
    early = await presenter.document(current)
    assert '初始版本' not in Path(early['path']).read_text()
    old = await presenter.document(initial)
    assert old['path'] is None
    latest = await presenter.document(current)
    text = Path(latest['path']).read_text()
    assert '初始版本' in text and '先查看当前需求' in text
    assert early['digest'] != latest['digest']
    history_row = next(line for line in text.splitlines() if line.startswith('| ') and '先查看当前需求' in line)
    assert '新增范围' in history_row
    assert '初始版本' not in (await flow[2].read(early['digest'])).decode()
    assert text.count('## 变更记录') == 1
    assert len(list((output / 'documents').rglob('*.md'))) == 1


async def test_current_history_repairs_when_migration_later_adds_baseline(flow, tmp_path):
    from agentflow.control.project_documents import ProjectDocumentService
    presenter, initial, output, old = await legacy_projection(flow, tmp_path)
    presenter, current, _ = await seed_run(flow, tmp_path, run_id='r2', change='先查看当前需求', body='# 新增范围')
    early = await presenter.document(current)
    assert '初始版本' not in Path(early['path']).read_text()
    result = await ProjectDocumentService(flow[1], flow[2], flow[0].settings).migrate('p1')
    assert result['conflicts'] == [] and not old.exists()
    latest = await presenter.document(current)
    assert '初始版本' in Path(latest['path']).read_text()
    assert '先查看当前需求' in Path(latest['path']).read_text()
    historical = await presenter.document(initial)
    assert historical['path'] is None
    assert len(list((output / 'documents').rglob('*.md'))) == 1


async def test_late_current_read_cannot_remove_newly_discovered_history(flow, tmp_path, monkeypatch):
    from agentflow.control.project_documents import ProjectDocumentService
    presenter, initial, output = await seed_run(flow, tmp_path)
    presenter, current, _ = await seed_run(flow, tmp_path, run_id='r2', change='当前变更', body='# 新增范围')
    original = ProjectDocumentService._directory
    reached, resume = asyncio.Event(), asyncio.Event()
    async def delayed(self, *args):
        directory = await original(self, *args)
        reached.set()
        await resume.wait()
        return directory
    monkeypatch.setattr(ProjectDocumentService, '_directory', delayed)
    in_flight = asyncio.create_task(presenter.document(current))
    await asyncio.wait_for(reached.wait(), 2)
    monkeypatch.setattr(ProjectDocumentService, '_directory', original)
    await presenter.document(initial)
    completed = await presenter.document(current)
    flat = output / 'documents/prd/产品需求文档 PRD.md'
    before = flat.read_bytes()
    assert '初始版本' in before.decode()
    resume.set()
    late = await in_flight
    assert late['path'] is None and late['storage_error']
    assert flat.read_bytes() == before
    assert (await flow[1].list('project_document_head'))[0]['digest'] == completed['digest']


@pytest.mark.parametrize('proof', ['valid', 'missing', 'wrong_run', 'explicit_stale'])
async def test_old_nonprojection_schema_requires_proven_work_revision_before_migration(flow, tmp_path, proof):
    from agentflow.control.project_documents import ProjectDocumentService
    presenter, original_work, output, path = await legacy_projection(flow, tmp_path)
    store, artifacts = flow[1], flow[2]
    old = path.parent.parent / ('产品需求-' + original_work['id'][:12] + '-v1') / path.name
    old.parent.mkdir()
    path.rename(old)
    path.parent.rmdir()
    latest = await artifacts.put_bytes('# 当前完成需求\n\n使用本轮完成内容。'.encode())
    def rework(tx):
        source = tx.get('artifact', original_work['artifact_ids'][0])
        tx.put('artifact', source['id'], {**source, 'stale': True}, source['revision'])
        record = tx.get('readable_artifact', 'legacy-r1')
        historical = {key: value for key, value in record.items()
                      if key not in {'projection_key', 'projection_state', 'logical_stage_key'}}
        historical['source_versions'] = [{key: value for key, value in ref.items()
            if key != 'stale' or proof == 'explicit_stale'} for ref in record['source_versions']]
        tx.put('readable_artifact', record['id'], historical, record['revision'])
        if proof != 'missing':
            tx.put('work_revision', 'completed-old-work', {'work_item_id': original_work['id'],
                'snapshot': {**original_work, 'run_id': 'other-run' if proof == 'wrong_run' else 'r1'}})
        tx.put('artifact', 'current-source', {'run_id': 'r1', 'work_item_id': original_work['id'],
            'generation': 2, 'digest': latest['id'], 'name': 'prd.md', 'readable': True, 'media_type': 'text/markdown'})
        current = tx.get('work_item', original_work['id'])
        return tx.put('work_item', current['id'], {**current, 'generation': 2,
            'artifact_ids': ['current-source']}, current['revision'])
    current = await store.command('fixture.earliest-readable', proof, {}, rework)
    result = await ProjectDocumentService(store, artifacts, flow[0].settings).migrate('p1')
    if proof != 'valid':
        assert result['conflicts'] and old.exists()
        return
    assert result['conflicts'] == [] and not old.exists()
    records = await store.list('readable_artifact')
    imported = next(row for row in records if row.get('legacy_artifact_id') == 'legacy-r1')
    assert imported['projection_state'].startswith('legacy:')
    assert '已有功能。' in (await artifacts.read(imported['digest'])).decode()
    flat = output / 'documents/prd/产品需求文档 PRD.md'
    assert '使用本轮完成内容。' in flat.read_text() and '已有功能。' not in flat.read_text()
    assert (await presenter.document(current))['path'] == str(flat)

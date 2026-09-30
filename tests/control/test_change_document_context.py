"""Requirement changes consume immutable project documents, never mutable paths."""
import pytest
from test_stage_context import context_system as context_system
from test_stage_context import work

from agentflow.common import DomainError
from agentflow.control.stage_context import StageContext


async def baseline_document(store, artifacts, stage='prd', **overrides):
    raw = await artifacts.put_bytes(b'# Reading list\n\nKeep saved books and reading status.\n')
    record = await store.command('fixture', 'baseline-' + stage, {}, lambda tx: tx.put('readable_artifact', 'doc-' + stage, {
        'product_id': 'product', 'project_document': True, 'logical_stage_key': stage,
        'digest': raw['id'], 'name': stage + '.md', 'run_id': 'previous-run',
        'work_item_id': 'previous-work', 'generation': 1, **overrides}))
    return {'artifact_id': record['id'], 'digest': record['digest'], 'name': record['name'],
            'run_id': record['run_id'], 'work_item_id': record['work_item_id'], 'generation': 1}


def baseline_plan(stage, descriptor):
    return {'product_contract': {'product_id': 'product', 'change_id': 'change',
        'document_baseline': {'product_id': 'product', 'run_id': 'previous-run',
                              'documents': {stage: descriptor}}}}


@pytest.mark.parametrize('stage', ['prd', 'requirements', 'architecture', 'development_plan',
    'unit_test_plan', 'integration_test_strategy'])
async def test_each_document_stage_receives_its_frozen_previous_document(context_system, stage):
    store, artifacts, settings = context_system
    descriptor = await baseline_document(store, artifacts, stage)
    item = work('current', stage, artifact_ids=[])
    result = await StageContext(store, artifacts, settings.data_dir).build(
        {'id': 'run'}, item, baseline_plan(stage, descriptor), {'current': item})
    assert len(result['documents']) == 1
    entry = result['documents'][0]
    assert entry['evidence_kind'] == 'project_document_baseline'
    assert entry['logical_stage_key'] == stage and entry['source_digest'] == descriptor['digest']
    assert 'Keep saved books and reading status.' in result['text']
    assert 'Preserve unaffected' in result['text']
    assert 'complete updated document' in result['text']
    assert f'[project_document_baseline:{stage}]' in result['text']


@pytest.mark.parametrize('override', [{'product_id': 'different-product'}, {'digest': 'sha256:' + '0' * 64},
    {'project_document': False}, {'logical_stage_key': 'architecture'}])
async def test_foreign_or_replaced_document_snapshot_is_rejected(context_system, override):
    store, artifacts, settings = context_system
    descriptor = await baseline_document(store, artifacts)
    record = await store.read('readable_artifact', descriptor['artifact_id'])
    await store.command('fixture', 'tamper', {}, lambda tx: tx.put('readable_artifact', record['id'],
        {**record, **override}, record['revision']))
    item = work('current', 'prd', artifact_ids=[])
    with pytest.raises(DomainError, match='baseline'):
        await StageContext(store, artifacts, settings.data_dir).build(
            {'id': 'run'}, item, baseline_plan('prd', descriptor), {'current': item})


async def test_review_baseline_uses_parent_logical_phase_without_reusing_quality(context_system):
    store, artifacts, settings = context_system
    stage = 'unit_test_implementation:review'
    descriptor = await baseline_document(store, artifacts, stage)
    parent = work('parent', 'code_review', key=stage, artifact_ids=[])
    child = work('child', 'code_review', parent_stage_id='parent', kind='stage_child', artifact_ids=[])
    result = await StageContext(store, artifacts, settings.data_dir).build(
        {'id': 'run'}, child, baseline_plan(stage, descriptor), {'parent': parent, 'child': child})
    assert result['documents'][0]['logical_stage_key'] == stage
    assert 'never carry forward a prior pass' in result['text']
    assert result['review_phase_contract']['required_test_phases'] == []


async def test_frozen_snapshot_survives_source_rework_without_reading_current_copy(context_system):
    store, artifacts, settings = context_system
    descriptor = await baseline_document(store, artifacts, stale=True)
    item = work('current', 'prd', artifact_ids=[])
    result = await StageContext(store, artifacts, settings.data_dir).build(
        {'id': 'run'}, item, baseline_plan('prd', descriptor), {'current': item})
    assert len(result['documents']) == 1 and 'Keep saved books' in result['text']

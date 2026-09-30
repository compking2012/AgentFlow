"""The owner sees product stages, not the scheduler's repair graph."""
from copy import deepcopy

import pytest
from test_workflow import flow as flow
from test_workflow import start

from agentflow.control.workflow_stages import group_workflow_stages


def contextual_items():
    items = [work('implementation', 'implementation'), work('code_review', 'code_review', ['implementation']),
        work('unit_test_plan', 'unit_test_plan', ['code_review']),
        work('integration_test_strategy', 'integration_test_strategy', ['unit_test_plan']),
        work('unit_test_implementation', 'unit_test_implementation', ['integration_test_strategy']),
        work('repair', 'implementation', ['unit_test_implementation'], payload={'product_frozen_repair': True}),
        work('repair-review', 'code_review', ['repair'])]
    for item in items:
        item.update(run_id='run', artifact_ids=[item['id'] + '-artifact'])
    items[-1].update(quality_result='failed', generation=81)
    artifacts = [dict(id=item['artifact_ids'][0], work_item_id=item['id'], run_id='run',
                      generation=item['generation'], created_at='2026-09-01T00:00:00+00:00') for item in items]
    repairs = [dict(id='repair', repair_kind='product_test_repair', run_id='run', repair_work_item_id='repair',
                    review_work_item_id='repair-review', affected_work_item_ids=[], created_at='2026-09-02T00:00:00+00:00')]
    return items, artifacts, repairs


def contexts(items, artifacts, repairs, revisions=()):
    from agentflow.control.workflow_stages import stage_contexts
    runtime = [row for row in repairs if row.get('repair_kind') == 'product_test_runtime_repair']
    review_repairs = [row for row in repairs if row.get('repair_kind') == 'review_repair']
    return stage_contexts(group_workflow_stages(items, runtime_repairs=runtime, review_repairs=review_repairs),
                          items, repairs, artifacts, revisions)


def test_review_context_preserves_initial_pass_and_counts_repair_events_not_generation():
    items, artifacts, repairs = contextual_items()
    result = contexts(items, artifacts, repairs)
    assert result['code_review']['review'] == {
        'initial_work_item_id': 'code_review', 'initial_status': 'completed', 'initial_quality_result': 'passed',
        'current_work_item_id': 'repair-review', 'repair_round': 1}
    items[-1].update(status='running', quality_result='unknown', generation=82)
    repairs.append(dict(id='review-attempt', repair_kind='review_repair', run_id='run',
                        producer_work_item_id='repair', review_work_item_id='repair-review',
                        affected_work_item_ids=['repair', 'repair-review'], created_at='2026-09-03T00:00:00+00:00'))
    assert contexts(items, artifacts, repairs)['code_review']['review']['repair_round'] == 2


def test_parallel_review_aliases_count_once_per_batch_and_legacy_receipts_stay_separate():
    items, artifacts, _ = contextual_items()
    for suffix in ('a', 'b', 'c'):
        items.append(work('review-' + suffix, 'code_review', ['repair'],
                          parent_stage_id='repair-review', run_id='run'))
    receipts = []
    for batch, expected_round in [('first-batch', 1), ('second-batch', 2)]:
        receipts.extend(dict(id=batch + '-' + suffix, batch_id=batch, repair_kind='review_repair',
            run_id='run', producer_work_item_id='repair', review_work_item_id='review-' + suffix)
            for suffix in ('a', 'b', 'c'))
        assert contexts(items, artifacts, receipts)['code_review']['review']['repair_round'] == expected_round
    receipts.extend(dict(id=identity, repair_kind='review_repair', run_id='run',
        producer_work_item_id='repair', review_work_item_id='repair-review') for identity in ('legacy-one', 'legacy-two'))
    assert contexts(items, artifacts, receipts)['code_review']['review']['repair_round'] == 4


def test_batch_round_deduplication_preserves_individual_retained_plan_evidence():
    items, artifacts, _ = contextual_items()
    receipts = [dict(id='alias-' + str(index), batch_id='same-batch', repair_kind='review_repair',
        run_id='run', producer_work_item_id='repair', review_work_item_id='repair-review',
        created_at='2026-09-02T00:00:00+00:00',
        affected_work_item_ids=[] if index == 0 else ['unit_test_plan']) for index in range(3)]
    result = contexts(items, artifacts, receipts)
    assert result['code_review']['review']['repair_round'] == 1
    assert result['unit_test_plan']['retained_plan'] == {'after_repair_work_item_id': 'repair'}


def test_review_context_does_not_invent_an_initial_pass_or_unreceipted_repair():
    items, artifacts, repairs = contextual_items()
    items[1]['quality_result'] = 'failed'
    assert contexts(items, artifacts, repairs)['code_review']['review']['initial_quality_result'] == 'failed'
    assert 'code_review' not in contexts(items, artifacts, [])
    repairs[0]['run_id'] = 'other-run'
    assert 'code_review' not in contexts(items, artifacts, repairs)


def test_in_place_review_repair_uses_historical_first_outcome():
    items, artifacts, _ = contextual_items()
    items = items[:2]
    original = deepcopy(items[1])
    items[1].update(status='pending', quality_result='unknown', generation=3)
    repairs = [dict(id='attempt', repair_kind='review_repair', run_id='run', producer_work_item_id='implementation',
                    review_work_item_id='code_review', affected_work_item_ids=['implementation', 'code_review'])]
    result = contexts(items, artifacts, repairs, [dict(work_item_id='code_review', snapshot=original)])
    assert result['code_review']['review']['initial_quality_result'] == 'passed'
    assert result['code_review']['review']['repair_round'] == 1


def test_only_valid_completed_plans_predating_descendant_repair_are_retained():
    items, artifacts, repairs = contextual_items()
    result = contexts(items, artifacts, repairs)
    for key in ('unit_test_plan', 'integration_test_strategy'):
        assert result[key]['retained_plan'] == {'after_repair_work_item_id': 'repair'}
    for change in ({'status': 'pending'}, {'status': 'failed'}, {'quality_result': 'failed'}, {'generation': 2}):
        changed = deepcopy(items)
        changed[2].update(change)
        assert 'unit_test_plan' not in contexts(changed, artifacts, repairs)
    for change in ({'stale': True}, {'generation': 2}, {'run_id': 'other'},
                   {'created_at': '2026-09-03T00:00:00+00:00'}, {'created_at': None}):
        changed = deepcopy(artifacts)
        changed[2].update(change)
        assert 'unit_test_plan' not in contexts(items, changed, repairs)
    repairs[0]['affected_work_item_ids'] = ['unit_test_plan']
    assert 'unit_test_plan' not in contexts(items, artifacts, repairs)


def test_unrelated_repair_and_pending_plan_child_do_not_claim_retained_documents():
    items, artifacts, repairs = contextual_items()
    items[5]['dependencies'] = ['code_review']
    assert 'unit_test_plan' not in contexts(items, artifacts, repairs)
    items[5]['dependencies'] = ['unit_test_implementation']
    child = work('plan-child', 'unit_test_plan', parent_stage_id='unit_test_plan', run_id='run')
    child['status'] = 'pending'
    items.append(child)
    assert 'unit_test_plan' not in contexts(items, artifacts, repairs)


@pytest.mark.parametrize('phase', ['unit', 'integration'])
def test_test_code_review_context_stays_with_its_phase(phase):
    key = phase + '_test_implementation'
    items = [work(key, key), work(key + ':review', 'code_review', [key]),
        work('runtime', key, [key + ':review'], payload={'test_runtime_repair_id': 'runtime'}),
        work('runtime-review', 'code_review', ['runtime'])]
    for item in items:
        item['run_id'] = 'run'
    items[-1]['quality_result'] = 'failed'
    receipts = [dict(id='runtime', run_id='run', repair_kind='product_test_runtime_repair', phase=phase,
                     repair_work_item_id='runtime', review_work_item_id='runtime-review')]
    result = contexts(items, [], receipts)
    assert result[key + ':review']['review']['initial_work_item_id'] == key + ':review'
    assert result[key + ':review']['review']['initial_quality_result'] == 'passed'
    assert result[key + ':review']['review']['repair_round'] == 1
    assert 'code_review' not in result


def test_late_test_owner_repair_moves_current_gate_to_test_review_and_retains_plans():
    items, artifacts, _ = contextual_items()
    original_review = work('unit_test_implementation:review', 'code_review', ['unit_test_implementation'], run_id='run')
    items.append(original_review)
    history = [dict(work_item_id=original_review['id'], snapshot=deepcopy(original_review))]
    original_review.update(status='pending', quality_result='unknown', generation=999)
    items[5]['archived'] = True
    items[6].update(dependencies=['unit_test_implementation'], status='pending', quality_result='unknown')
    receipt = dict(id='late-attempt', run_id='run', repair_kind='review_repair', mode='late_test_owner',
        review_work_item_id='repair-review', producer_work_item_id='repair',
        repair_work_item_ids=['unit_test_implementation'], owner_stage_work_item_ids=['unit_test_implementation'],
        affected_work_item_ids=['unit_test_implementation', original_review['id'], 'repair-review'],
        preserved_plan_work_item_ids=['unit_test_plan', 'integration_test_strategy'],
        review_bindings={'repair-review': {'dependencies': ['unit_test_implementation'], 'minimum_generation': 1}},
        created_at='2026-09-04T00:00:00+00:00')
    groups = {group['key']: group for group in group_workflow_stages(items, review_repairs=[receipt])}
    assert groups['code_review']['work']['id'] == 'code_review'
    assert groups['unit_test_implementation:review']['work']['id'] == 'repair-review'
    result = contexts(items, artifacts, [receipt], history)
    assert 'code_review' not in result
    review = result['unit_test_implementation:review']['review']
    assert review['initial_quality_result'] == 'passed'
    assert review['current_work_item_id'] == 'repair-review'
    assert review['repair_round'] == 1
    assert result['unit_test_plan']['retained_plan']['after_repair_work_item_id'] == 'unit_test_implementation'
    assert result['integration_test_strategy']['retained_plan']['after_repair_work_item_id'] == 'unit_test_implementation'


def own_phase_items():
    items, artifacts, _ = contextual_items()
    items[5]['archived'] = True
    gate = items[6]
    gate.update(dependencies=['integration_test_implementation'], generation=3, status='pending', quality_result='unknown')
    items.extend([work('unit_test_implementation:review', 'code_review', ['unit_test_implementation'], run_id='run'),
        work('integration_test_implementation', 'integration_test_implementation', ['unit_test_implementation:review'], run_id='run')])
    children = ['integration-review-' + str(index) for index in range(4)]
    items.extend(work(identity, 'code_review', ['integration_test_implementation'], run_id='run',
                      parent_stage_id='integration_test_implementation:review') for identity in children)
    reviewer = work('integration_test_implementation:review', 'code_review', children, run_id='run',
                    original_dependencies=['integration_test_implementation'], kind='aggregation')
    history = [dict(work_item_id=reviewer['id'], snapshot=deepcopy(reviewer))]
    reviewer.update(status='pending', quality_result='unknown', generation=2)
    items.append(reviewer)
    receipts = [dict(id='unit-first', run_id='run', repair_kind='review_repair', mode='late_test_owner',
        review_work_item_id=gate['id'], owner_stage_work_item_ids=['unit_test_implementation'],
        review_bindings={gate['id']: {'dependencies': ['unit_test_implementation'], 'minimum_generation': 2}},
        created_at='2026-09-03T00:00:00+00:00'),
        dict(id='integration-next', run_id='run', repair_kind='review_repair', mode='late_test_owner',
        routing_kind='original_test_phase', review_work_item_id=reviewer['id'], full_source_review_work_item_id=gate['id'],
        owner_stage_work_item_ids=['integration_test_implementation'],
        review_bindings={reviewer['id']: {'dependencies': children, 'minimum_generation': 2},
                         gate['id']: {'dependencies': ['integration_test_implementation'], 'minimum_generation': 3}},
        created_at='2026-09-04T00:00:00+00:00')]
    return items, artifacts, receipts, history


def test_own_phase_aggregate_and_explicit_full_gate_share_one_current_integration_review():
    items, artifacts, receipts, history = own_phase_items()
    before = deepcopy(items)
    groups = {group['key']: group for group in group_workflow_stages(items, review_repairs=receipts)}
    assert groups['code_review']['work']['id'] == 'code_review'
    assert groups['code_review']['work']['quality_result'] == 'passed'
    assert groups['unit_test_implementation:review']['work']['quality_result'] == 'passed'
    integration = groups['integration_test_implementation:review']
    assert integration['work']['id'] == 'repair-review'
    assert {member['id'] for member in integration['members']} == {'integration_test_implementation:review', 'repair-review'}
    result = contexts(items, artifacts, receipts, history)
    assert 'code_review' not in result and 'unit_test_implementation:review' not in result
    assert result['integration_test_implementation:review']['review'] == {
        'initial_work_item_id': 'integration_test_implementation:review', 'initial_status': 'completed',
        'initial_quality_result': 'passed', 'current_work_item_id': 'repair-review', 'repair_round': 1}
    assert items == before


@pytest.mark.parametrize('invalid', ['unlisted_gate', 'missing_binding', 'wrong_dependencies', 'future_generation',
                                    'foreign_run', 'unrelated_owner'])
def test_full_gate_reclassification_requires_explicit_current_receipt_binding(invalid):
    from agentflow.control.workflow_stages import logical_stage_key
    items, _, receipts, _ = own_phase_items()
    receipt = receipts[-1]
    binding = receipt['review_bindings']['repair-review']
    if invalid == 'unlisted_gate':
        receipt['full_source_review_work_item_id'] = 'some-other-review'
    elif invalid == 'missing_binding':
        receipt['review_bindings'].pop('repair-review')
    elif invalid == 'wrong_dependencies':
        binding['dependencies'] = ['unit_test_implementation']
    elif invalid == 'future_generation':
        binding['minimum_generation'] = 4
    elif invalid == 'foreign_run':
        receipt['run_id'] = 'other-run'
    else:
        items.append(work('unrelated-owner', 'integration_test_implementation', run_id='run'))
        receipt['owner_stage_work_item_ids'] = ['unrelated-owner']
    assert logical_stage_key(items[6], work_items={work['id']: work for work in items}, review_repairs=receipts) == 'code_review'


def work(identity, step, dependencies=(), **fields):
    return dict(id=identity, key=identity, step=step, role='review' if step == 'code_review' else 'development',
                dependencies=list(dependencies), generation=1, status='completed', quality_result='passed', **fields)


def repaired_items():
    return [work('implementation', 'implementation'), work('code_review', 'code_review', ['implementation']),
        work('integration_test_implementation', 'integration_test_implementation', ['code_review']),
        work('integration_test_implementation:review', 'code_review', ['integration_test_implementation']),
        work('repair', 'implementation', ['integration_test_implementation:review']),
        work('repair-review', 'code_review', ['repair']),
        work('runtime', 'integration_test_implementation', ['repair-review']),
        work('runtime-review', 'code_review', ['runtime'])]


def test_repairs_fold_into_stable_stages_without_back_edges_or_mutating_work():
    items = repaired_items()
    before = deepcopy(items)
    groups = group_workflow_stages(items, runtime_repairs=[{'review_work_item_id': 'runtime-review', 'phase': 'integration'}])
    assert [g['key'] for g in groups] == ['implementation', 'code_review', 'integration_test_implementation',
                                         'integration_test_implementation:review']
    assert [g['id'] for g in groups] == [g['key'] for g in groups]
    assert [g['work']['id'] for g in groups] == ['repair', 'repair-review', 'runtime', 'runtime-review']
    positions = {g['id']: i for i, g in enumerate(groups)}
    assert all(positions[parent] < positions[g['id']] for g in groups for parent in g['dependencies'])
    assert groups[-1]['dependencies'] == ['integration_test_implementation']
    assert items == before


def test_review_phases_remain_distinct_and_children_stay_inside_their_root():
    items = [work('unit_test_implementation', 'unit_test_implementation'),
             work('unit_test_implementation:review', 'code_review', ['unit_test_implementation']),
             work('review-part', 'code_review', parent_stage_id='unit_test_implementation:review')]
    groups = group_workflow_stages(items)
    assert [g['key'] for g in groups] == ['unit_test_implementation', 'unit_test_implementation:review']
    assert [x['id'] for x in groups[-1]['members']] == ['unit_test_implementation:review']


def test_later_pending_repair_is_current_without_losing_original_identity():
    items = repaired_items()
    items[4].update(status='pending', quality_result='unknown')
    group = group_workflow_stages(items)[0]
    assert group['id'] == 'implementation'
    assert group['work']['status'] == 'pending'
    assert group['current_ids'] == ['repair']


def test_runtime_review_rebound_to_owner_source_repair_becomes_latest_production_review():
    items = repaired_items()
    items.append(work('owner-source', 'implementation', ['runtime'],
                      payload={'owner_review_source_repair_id': 'owner-source'}))
    items[-2]['dependencies'] = ['owner-source']
    groups = group_workflow_stages(items, runtime_repairs=[{'review_work_item_id': 'runtime-review', 'phase': 'integration'}])
    stages = {g['key']: g for g in groups}
    assert stages['code_review']['work']['id'] == 'runtime-review'
    assert stages['integration_test_implementation:review']['work']['id'] == 'integration_test_implementation:review'


def test_independent_members_do_not_hide_a_failure_behind_a_completed_peer():
    items = [work('market', 'research'), work('customers', 'research')]
    items[0]['status'] = 'failed'
    group = group_workflow_stages(items)[0]
    assert set(group['current_ids']) == {'market', 'customers'}


def test_arbitrary_plan_keys_keep_the_same_stage_id_after_retry():
    items = [work('a-market', 'research'), work('b-customers', 'research')]
    original = group_workflow_stages(items)[0]['id']
    items[0]['generation'] += 1
    assert group_workflow_stages(items)[0]['id'] == original


def test_plan_forward_dependencies_are_used_instead_of_late_repair_dependencies():
    items = repaired_items()
    items[0]['dependencies'] = ['runtime-review']  # Original execution node was rebound by rework.
    # Pure stage projection must also reject an invalid execution DAG rather than hide a cycle.
    import pytest

    from agentflow.common import DomainError
    with pytest.raises(DomainError):
        group_workflow_stages(items)


async def test_workflow_folds_repair_tasks_and_uses_latest_status_and_retry_identity(flow):
    from agentflow.control.presentation import RunPresentationService
    from agentflow.domain.planning import WorkSpec
    service, store, artifacts, _, _ = flow
    run = await start(flow)
    await service.add_work_items(run['id'], [
        WorkSpec('implementation', 'implementation', 'development'),
        WorkSpec('code_review', 'code_review', 'review', dependencies=('implementation',)),
        WorkSpec('integration_test_implementation', 'integration_test_implementation', 'integration_test',
                 dependencies=('code_review',)),
        WorkSpec('repair', 'implementation', 'development', dependencies=('integration_test_implementation',)),
        WorkSpec('repair-review', 'code_review', 'review', dependencies=('repair',)),
    ], run['revision'], 'repair-graph')
    works = {w['key']: w for w in await store.list('work_item')}
    def states(tx):
        for key in ('implementation', 'code_review', 'integration_test_implementation', 'repair', 'repair-review'):
            item = tx.get('work_item', works[key]['id'])
            tx.put('work_item', item['id'], {**item, 'status': 'completed', 'quality_result': 'passed'}, item['revision'])
        old = tx.get('work_item', works['code_review']['id'])
        tx.put('work_item', old['id'], {**old, 'quality_result': 'failed'}, old['revision'])
        return {}
    await store.command('test.complete', 'complete-repairs', {}, states)
    before = await store.list('work_item')
    stages = (await RunPresentationService(store, artifacts, service.settings).workflow(run['id']))['stages']
    implementation = [s for s in stages if s['step'] == 'implementation']
    reviews = [s for s in stages if s['name'] == '代码审查']
    assert len(implementation) == len(reviews) == 1
    assert implementation[0]['id'] == works['implementation']['id']
    assert implementation[0]['work_item_id'] == works['repair']['id']
    assert reviews[0]['quality_result'] == 'passed'
    assert next(t for t in reviews[0]['tasks'] if t['id'] == works['code_review']['id'])['is_history'] is True
    assert reviews[0]['dependencies'] == [implementation[0]['id']]
    assert await store.list('work_item') == before


async def test_workflow_exposes_review_context_without_replacing_latest_output_or_quality(flow):
    import json

    from agentflow.control.presentation import RunPresentationService
    from agentflow.domain.planning import WorkSpec

    service, store, artifacts, _, _ = flow
    run = await start(flow)
    items, _, repairs = contextual_items()
    await service.add_work_items(run['id'], [WorkSpec(w['key'], w['step'], w['role'],
        dependencies=tuple(w['dependencies'])) for w in items], run['revision'], 'context-graph')
    saved = {w['key']: w for w in await store.list('work_item')}
    blob = await artifacts.put_bytes(json.dumps({'summary': '当前复审问题', 'content': '当前复审仍需修复。'}).encode())

    def seed(tx):
        for item in items:
            current = tx.get('work_item', saved[item['key']]['id'])
            artifact_id = item['id'] + '-source'
            tx.put('artifact', artifact_id, {'work_item_id': current['id'], 'run_id': run['id'],
                'generation': current['generation'], 'created_at': '2026-09-01T00:00:00+00:00',
                'digest': blob['id'], 'name': 'openhands_final.json', 'media_type': 'application/json'})
            tx.put('work_item', current['id'], {**current, 'status': item['status'],
                'quality_result': item['quality_result'], 'payload': item.get('payload', {}),
                'artifact_ids': [artifact_id]}, current['revision'])
        receipt = repairs[0]
        tx.put('product_test_repair', 'receipt', {**receipt, 'id': 'receipt', 'run_id': run['id'],
            'repair_work_item_id': saved['repair']['id'], 'review_work_item_id': saved['repair-review']['id']})
        return {}

    await store.command('test.context', 'seed-context', {}, seed)
    before = await store.list('work_item')
    presenter = RunPresentationService(store, artifacts, service.settings)
    stages = {stage['key']: stage for stage in (await presenter.workflow(run['id']))['stages']}
    review = stages['code_review']
    assert review['context']['review']['initial_quality_result'] == 'passed'
    assert review['quality_result'] == 'failed'
    assert review['output']['artifact_id']
    record = await store.read('readable_artifact', review['output']['artifact_id'])
    assert record['work_item_id'] == saved['repair-review']['id']
    assert any(task['is_history'] and task['id'] == saved['code_review']['id'] for task in review['tasks'])
    assert stages['unit_test_plan']['context']['retained_plan']
    assert stages['integration_test_strategy']['context']['retained_plan']
    assert await store.list('work_item') == before

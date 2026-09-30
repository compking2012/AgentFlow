"""Read-only logical stages over the scheduler's detailed execution graph."""
from __future__ import annotations

from datetime import datetime
from typing import NotRequired, TypedDict

from agentflow.common import DomainError
from agentflow.domain.planning import STEPS

STAGE_ORDER = tuple(key for step in STEPS for key in (
    (step, step + ':review') if step in {'unit_test_implementation', 'integration_test_implementation'} else (step,)))


def _late_review_producer(work, work_items, review_repairs):
    dependencies = work.get('dependencies', [])
    if work.get('step') != 'code_review' or not isinstance(dependencies, list):
        return None
    by_id = work_items or {}
    ancestors, pending = set(), list(dependencies)
    while pending:
        identity = pending.pop()
        if identity in ancestors:
            continue
        ancestor = by_id.get(identity)
        if not ancestor or ancestor.get('archived') or ancestor.get('run_id') != work.get('run_id'):
            continue
        ancestors.add(identity)
        pending.extend(ancestor.get('dependencies', []))
    for receipt in review_repairs:
        if (receipt.get('mode') != 'late_test_owner' or receipt.get('run_id') != work.get('run_id')
                or work.get('id') not in {receipt.get('review_work_item_id'), receipt.get('full_source_review_work_item_id')}):
            continue
        binding = receipt.get('review_bindings', {}).get(work.get('id'), {})
        minimum = binding.get('minimum_generation')
        if (binding.get('dependencies') != dependencies or type(minimum) is not int or minimum < 1
                or type(work.get('generation')) is not int or work['generation'] < minimum):
            continue
        owners = [by_id[identity] for identity in receipt.get('owner_stage_work_item_ids', [])
                  if identity in ancestors and not by_id[identity].get('parent_stage_id')
                  and by_id[identity].get('step') in {'unit_test_implementation', 'integration_test_implementation'}]
        if len(owners) == 1:
            return owners[0]
    return None


def logical_stage_key(work, runtime_repairs=(), work_items=None, review_repairs=()):
    if work.get('step') == 'code_review':
        if work.get('key') in {'unit_test_implementation:review', 'integration_test_implementation:review'}:
            return work['key']
        producer = _late_review_producer(work, work_items, review_repairs)
        if producer:
            return producer['step'] + ':review'
        receipt = next((r for r in runtime_repairs if r.get('review_work_item_id') == work.get('id')), None)
        if receipt and receipt.get('phase') in {'unit', 'integration'}:
            dependencies = work.get('dependencies', [])
            producer = (work_items or {}).get(dependencies[0]) if len(dependencies) == 1 else None
            # The controller can explicitly rebind a runtime review to a new
            # production repair. Reflect its current assignment, not its old label.
            if (producer and producer.get('step') == 'implementation'
                    and producer.get('payload', {}).get('owner_review_source_repair_id') == producer['id']):
                return 'code_review'
            return receipt['phase'] + '_test_implementation:review'
    return work.get('step', work.get('key', 'unknown'))


def _lineage(by_id):
    ancestors, visiting = {}, set()

    def lineage(identity):
        if identity in ancestors:
            return ancestors[identity]
        if identity in visiting:
            raise DomainError('workflow_graph_invalid', '阶段依赖存在循环，无法展示执行顺序。')
        visiting.add(identity)
        found = set()
        for parent in by_id[identity].get('dependencies', []):
            if parent in by_id:
                found.add(parent)
                found.update(lineage(parent))
        visiting.remove(identity)
        ancestors[identity] = found
        return found

    for identity in by_id:
        lineage(identity)
    return ancestors


def group_workflow_stages(items, plan=None, runtime_repairs=(), review_repairs=()):
    """Fold repairs into stable stage identities without changing execution state.

    The execution DAG determines which revision supersedes another. The product
    process uses original forward dependencies; repair back-edges stay in the
    task history and must not create cycles in this separate presentation graph.
    """
    by_id = {w['id']: w for w in items if not w.get('archived')}
    ancestors = _lineage(by_id)
    roots = [w for w in by_id.values() if not w.get('parent_stage_id')
             and not w.get('payload', {}).get('review_contract_task')]
    root_key = {w['id']: logical_stage_key(w, runtime_repairs, by_id, review_repairs) for w in roots}
    key_for = {w['id']: root_key.get(w.get('parent_stage_id', w['id'])) for w in by_id.values()}
    grouped = {}
    for root in roots:
        grouped.setdefault(root_key[root['id']], []).append(root)
    order = {key: index for index, key in enumerate(STAGE_ORDER)}
    keys = sorted(grouped, key=lambda key: (order.get(key, len(order)), key))
    specs = {s['key']: s for s in (plan or {}).get('work_specs', [])}
    spec_keys = {key: logical_stage_key(spec) for key, spec in specs.items()}
    groups = []
    for key in keys:
        members = sorted(grouped[key], key=lambda w: (len(ancestors[w['id']]), w.get('generation', 1), w['id']))
        original = next((w for w in members if w.get('key') == key),
                        min(members, key=lambda w: (len(ancestors[w['id']]), w['id'])))
        current = [w for w in members if not any(w['id'] in ancestors[other['id']] for other in members)]
        representative = members[-1]
        rebound = [work for work in current if _late_review_producer(work, by_id, review_repairs)]
        if rebound:
            # Rebound final gates can run beside the original phase review. A
            # retry generation on that peer does not make it the latest gate.
            representative = max(rebound, key=lambda work: max(
                (str(row.get('created_at', '')), row['id'], row.get('full_source_review_work_item_id') == work['id'])
                for row in review_repairs if _late_review_producer(work, by_id, [row])))
            members = [work for work in members if work['id'] != representative['id']] + [representative]
        dependencies = []
        spec = specs.get(original.get('key'))
        if key in {'unit_test_implementation:review', 'integration_test_implementation:review'}:
            dependencies = [key.removesuffix(':review')]
        elif spec:
            dependencies = [spec_keys.get(d) for d in spec.get('dependencies', [])]
        else:
            dependencies = [key_for.get(d) for d in original.get('original_dependencies', original.get('dependencies', []))]
        groups.append({'id': original['id'], 'key': key, 'work': {**representative, 'logical_stage_key': key},
            'members': members, 'current_ids': [w['id'] for w in current], 'dependency_keys': dependencies})
    ids = {g['key']: g['id'] for g in groups}
    positions = {key: index for index, key in enumerate(keys)}
    for group in groups:
        group['dependencies'] = list(dict.fromkeys(ids[key] for key in group.pop('dependency_keys')
            if key in positions and positions[key] < positions[group['key']]))
    return groups


class ReviewContext(TypedDict):
    initial_work_item_id: str
    initial_status: str
    initial_quality_result: str
    current_work_item_id: str
    repair_round: int


class RetainedPlanContext(TypedDict):
    after_repair_work_item_id: str


class StageContext(TypedDict):
    review: NotRequired[ReviewContext]
    retained_plan: NotRequired[RetainedPlanContext]


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def stage_contexts(groups, items, repairs=(), artifacts=(), revisions=()) -> dict[str, StageContext]:
    """Explain repair history without changing a stage's status, output or edges.

    Repair receipts establish business rounds; retry generations do not. A plan
    is retained only when its current completed evidence predates a descendant
    repair and that repair did not invalidate any part of the plan.
    """
    by_id = {work['id']: work for work in items if not work.get('archived')}
    ancestors = _lineage(by_id)
    events = {}
    coding = {'implementation', 'unit_test_implementation', 'integration_test_implementation'}
    for receipt in repairs:
        kind = receipt.get('repair_kind')
        producer = by_id.get(receipt.get('repair_work_item_id') or receipt.get('producer_work_item_id'))
        review = by_id.get(receipt.get('review_work_item_id'))
        if kind == 'review_repair' and receipt.get('mode') == 'late_test_owner':
            producer = _late_review_producer(review or {}, by_id, [receipt])
        if (not producer or not review or producer.get('step') not in coding
                or review.get('step') != 'code_review' or receipt.get('superseded_by')
                or any(work.get('run_id') != receipt.get('run_id') for work in (producer, review))):
            continue
        payload = producer.get('payload', {})
        if kind == 'product_test_repair':
            known = payload.get('product_frozen_repair') is True
        elif kind == 'product_test_runtime_repair':
            known = payload.get('test_runtime_repair_id') == receipt.get('id')
        elif kind == 'review_source_repair':
            known = (receipt.get('actor') == 'owner'
                     and payload.get('owner_review_source_repair_id') == receipt.get('id'))
        else:
            known = kind == 'review_repair'
        if not known or producer['id'] not in ancestors[review['id']]:
            continue
        events[(kind, receipt['id'])] = {**receipt, 'producer_id': producer['id'],
                                      'review_root_id': review.get('parent_stage_id') or review['id']}
    result = {}
    for group in groups:
        context: StageContext = {}
        current = group['work']
        current_ids = set(group['current_ids'])
        current_ancestors = set().union(*(ancestors[identity] | {identity} for identity in current_ids))
        if current.get('step') == 'code_review':
            members = {work['id'] for work in group['members']}
            # A parallel cohort emits one receipt per alias for the same round.
            # Keep every event above for retention evidence; dedupe only this count.
            rounds = {(event['repair_kind'], event.get('batch_id') or event['id']) for event in events.values()
                      if event['review_root_id'] in members & current_ancestors}
            if rounds:
                original = by_id[group['id']]
                history = [row['snapshot'] for row in revisions
                           if row.get('work_item_id') == original['id']
                           and row.get('snapshot', {}).get('run_id') == original.get('run_id')]
                history.sort(key=lambda work: (work.get('generation', 0), work.get('revision', 0)))
                initial = next((work for work in [*history, original] if work.get('status') == 'completed'), original)
                context['review'] = {'initial_work_item_id': original['id'],
                    'initial_status': initial['status'], 'initial_quality_result': initial.get('quality_result', 'unknown'),
                    'current_work_item_id': current['id'], 'repair_round': len(rounds)}
        if group['key'] in {'unit_test_plan', 'integration_test_strategy'}:
            active = [work for work in by_id.values()
                      if work['id'] in current_ids or work.get('parent_stage_id') in current_ids]
            complete = all(work.get('status') == 'completed'
                           and work.get('quality_result') not in {'failed', 'inconclusive'} for work in active)
            for event in sorted(events.values(), key=lambda event: str(event.get('created_at', '')), reverse=True):
                created = _timestamp(event.get('created_at'))
                if (not complete or created is None or not current_ids <= ancestors[event['producer_id']]
                        or any(work['id'] in event.get('affected_work_item_ids', []) for work in active)):
                    continue
                def valid_evidence(work):
                    sources = [artifact for artifact in artifacts if artifact['id'] in work.get('artifact_ids', [])
                        and artifact.get('work_item_id') == work['id'] and artifact.get('run_id') == work.get('run_id')
                        and artifact.get('generation') == work.get('generation') and not artifact.get('stale')]
                    return bool(sources) and all((when := _timestamp(source.get('created_at'))) is not None
                                                and when <= created for source in sources)
                if all(valid_evidence(work) for work in active):
                    context['retained_plan'] = {'after_repair_work_item_id': event['producer_id']}
                    break
        if context:
            result[group['key']] = context
    return result

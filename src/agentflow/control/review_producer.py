"""Resolve review producers from the sealed stage graph, never by ancestor guessing."""
from __future__ import annotations

from agentflow.domain.planning import CODING_STEPS


def _current_attempt(tx, work):
    attempt = tx.get('attempt', work.get('attempt_id')) if work.get('attempt_id') else None
    if (not attempt or attempt.get('status') != 'completed'
            or attempt.get('run_id') != work.get('run_id') or attempt.get('work_item_id') != work.get('id')
            or any(attempt.get(field) != work.get(field)
                   for field in ('generation', 'fencing_token', 'input_fingerprint'))):
        return None
    return attempt


def _completed_review(tx, work, quality):
    if (work.get('step') != 'code_review' or work.get('role') != 'review'
            or work.get('status') != 'completed' or work.get('quality_result') != quality
            or work.get('write_paths') != []):
        return None
    attempt = _current_attempt(tx, work)
    review = tx.get('review', work.get('attempt_id')) if attempt else None
    if (not review or review.get('stale') or attempt.get('quality_result') != quality
            or review.get('run_id') != work.get('run_id') or review.get('work_item_id') != work.get('id')
            or review.get('generation') != work.get('generation') or review.get('quality_result') != quality
            or review.get('reviewer_id', attempt['id']) != attempt['id']
            or not isinstance(review.get('blocking_findings'), list)
            or bool(review['blocking_findings']) != (quality == 'failed')):
        return None
    context = tx.get('dispatch_context', attempt['id'])
    if context:
        task = context.get('task', {})
        if (task.get('attempt_id') != attempt['id'] or task.get('step') != 'code_review'
                or task.get('source_commit') != review.get('reviewed_commit')
                or any(task.get(field) != attempt.get(field)
                       for field in ('run_id', 'work_item_id', 'fencing_token', 'input_fingerprint'))):
            return None
    return review


def _owner_group_binding(tx, run, stage, expansion, identities, original):
    """Explain a changed producer using Owner receipts; the expansion stays immutable."""
    if stage.get('payload', {}).get('review_contract_binding'):
        from agentflow.control.review_contract_binding import valid_group_binding
        return valid_group_binding(tx, run, stage, expansion, identities, original)
    identity = stage.get('payload', {}).get('owner_review_producer_binding')
    if not identity:
        return expansion['original_stage'].get('dependencies') == original
    producer_id, visited = original[0], set()
    while identity:
        if identity in visited:
            return False
        visited.add(identity)
        receipt = tx.get('review_source_repair', identity)
        binding = (receipt or {}).get('review_group_binding', {})
        repair = tx.get('work_item', producer_id)
        if (not receipt or receipt.get('actor') != 'owner' or receipt.get('id') != producer_id
                or receipt.get('repair_work_item_id') != producer_id or receipt.get('run_id') != run['id']
                or receipt.get('iteration_id') != run['iteration_id'] or receipt.get('review_work_item_id') != stage['id']
                or not repair or repair.get('key') != 'owner-review-repair-' + producer_id
                or repair.get('run_id') != run['id'] or repair.get('project_id') != run['project_id']
                or repair.get('step') != 'implementation' or repair.get('role') != 'development'
                or repair.get('kind') in {'aggregation', 'stage_child'} or repair.get('parent_stage_id')
                or repair.get('payload', {}).get('owner_review_source_repair_id') != identity
                or repair.get('dependencies') != [receipt.get('producer_work_item_id')]
                or repair.get('write_paths') != receipt.get('write_paths')
                or binding.get('stage_expansion_id') != expansion['id']
                or binding.get('expansion_fingerprint') != expansion['input_fingerprint']
                or binding.get('previous_producer_work_item_id') != receipt.get('producer_work_item_id')
                or binding.get('child_ids') != sorted(identities)
                or type(binding.get('minimum_stage_generation')) is not int
                or stage['generation'] < binding['minimum_stage_generation']
                or set(binding.get('minimum_child_generations', {})) != set(identities)
                or set(binding.get('accepted_child_review_ids', {})) != set(identities)):
            return False
        source = tx.get('code_snapshot', receipt.get('source_snapshot_id'))
        alias = tx.get('code_snapshot', receipt.get('snapshot_id'))
        if (not source or source.get('run_id') != run['id']
                or source.get('work_item_id') != receipt.get('producer_work_item_id')
                or source.get('commit_oid') != receipt.get('source_commit')
                or not alias or alias.get('run_id') != run['id'] or alias.get('work_item_id') != producer_id
                or alias.get('owner_repair_id') != identity or alias.get('source_snapshot_id') != source['id']
                or alias.get('base_oid') != source['commit_oid'] or alias.get('source_review_attempt_id') != receipt.get('review_attempt_id')
                or any(alias.get(field) != source.get(field) for field in ('commit_oid', 'tree_oid'))):
            return False
        historical = [(stage['id'], receipt.get('review_attempt_id'), 'failed', binding['minimum_stage_generation'])]
        for child_id in identities:
            child = tx.get('work_item', child_id)
            generation = binding['minimum_child_generations'][child_id]
            if not child or type(generation) is not int or child['generation'] < generation:
                return False
            historical.append((child_id, binding['accepted_child_review_ids'][child_id], 'passed', generation))
        for work_id, attempt_id, quality, next_generation in historical:
            review, attempt = tx.get('review', attempt_id), tx.get('attempt', attempt_id)
            if (not review or not attempt or review.get('run_id') != run['id'] or review.get('work_item_id') != work_id
                    or review.get('quality_result') != quality or review.get('reviewed_commit') != source['commit_oid']
                    or not isinstance(review.get('blocking_findings'), list)
                    or bool(review['blocking_findings']) != (quality == 'failed')
                    or review.get('generation', 0) + 1 != next_generation or attempt.get('status') != 'completed'
                    or attempt.get('quality_result') != quality
                    or any(attempt.get(field) != review.get(field) for field in ('run_id', 'work_item_id', 'generation'))):
                return False
        producer_id = receipt['producer_work_item_id']
        identity = binding.get('previous_binding_receipt_id')
    return expansion['original_stage'].get('dependencies') == [producer_id]


def sealed_review_group(tx, run, reviewer):
    """Resolve one frozen review stage and its exact current membership."""
    dependencies = reviewer.get('dependencies', [])
    try:
        original = reviewer.get('original_dependencies')
        identities = reviewer.get('expanded_child_ids')
        if (reviewer.get('kind') != 'aggregation' or reviewer.get('step') != 'code_review'
                or reviewer.get('role') != 'review' or reviewer.get('write_paths') != []
                or reviewer.get('archived') or reviewer.get('parent_stage_id')
                or reviewer.get('run_id') != run['id'] or reviewer.get('project_id') != run.get('project_id')
                or reviewer.get('original_write_paths') != []
                or not isinstance(original, list) or len(original) != 1
                or not isinstance(identities, list) or not 2 <= len(identities) <= 32
                or any(not isinstance(identity, str) for identity in identities)
                or len(set(identities)) != len(identities) or not isinstance(dependencies, list)
                or len(dependencies) != len(identities) or set(dependencies) != set(identities)):
            return None
        expansions = [row for row in tx.list('stage_expansion') if row.get('run_id') == run['id']
            and row.get('stage_work_item_id') == reviewer['id']
            and row.get('input_fingerprint') == reviewer.get('expansion_fingerprint')]
        if len(expansions) != 1:
            return None
        expansion = expansions[0]
        frozen = expansion.get('original_stage', {})
        if (set(expansion.get('child_ids', [])) != set(identities)
                or not _owner_group_binding(tx, run, reviewer, expansion, identities, original) or frozen.get('write_paths') != []
                or any(frozen.get(field) != reviewer.get(field)
                       for field in ('id', 'run_id', 'project_id', 'key', 'step', 'role'))):
            return None
        if reviewer.get('payload', {}).get('review_contract_binding'):
            from agentflow.control.review_contract_binding import bound_producer
            producer = bound_producer(tx, run, reviewer)
        else:
            producer = tx.get('work_item', original[0])
        if (not producer or producer.get('archived')
                or producer.get('run_id') != run['id'] or producer.get('project_id') != run.get('project_id')
                or producer.get('step') not in CODING_STEPS or producer.get('status') != 'completed'
                or producer.get('kind') == 'stage_child' or producer.get('parent_stage_id')
                or not _current_attempt(tx, producer)):
            return None
        snapshot = tx.get('code_snapshot', producer['attempt_id'])
        if (not snapshot or snapshot.get('stale') or snapshot.get('run_id') != run['id']
                or snapshot.get('work_item_id') != producer['id'] or snapshot.get('generation') != producer['generation']):
            return None
        children = [row for row in tx.list('work_item') if row.get('parent_stage_id') == reviewer['id']
                    and not row.get('archived')]
        if {child['id'] for child in children} != set(identities):
            return None
        for child in children:
            if (child.get('kind') != 'stage_child' or child.get('step') != 'code_review'
                    or child.get('role') != 'review' or child.get('write_paths') != []
                    or child.get('run_id') != run['id']
                    or child.get('project_id') != run.get('project_id') or child.get('dependencies') != original):
                return None
        return {'producer': producer, 'snapshot': snapshot, 'children': children, 'stage': reviewer, 'expansion': expansion}
    except (KeyError, TypeError, AttributeError):
        return None


def review_producer(tx, run, reviewer):
    """Aggregators retain one original coding dependency; children are evidence gates."""
    if reviewer.get('payload', {}).get('review_contract_binding') and reviewer.get('kind') != 'aggregation':
        from agentflow.control.review_contract_binding import bound_producer
        return bound_producer(tx, run, reviewer)
    if reviewer.get('payload', {}).get('late_test_review_binding'):
        from agentflow.control.late_test_review import bound_review_producer
        return bound_review_producer(tx, run, reviewer)
    dependencies = reviewer.get('dependencies', [])
    if reviewer.get('kind') != 'aggregation':
        return tx.get('work_item', dependencies[0]) if isinstance(dependencies, list) and len(dependencies) == 1 else None
    group = sealed_review_group(tx, run, reviewer)
    review = _completed_review(tx, reviewer, 'failed')
    if not group or not review or review.get('reviewed_commit') != group['snapshot']['commit_oid']:
        return None
    for child in group['children']:
        child_review = _completed_review(tx, child, 'passed')
        if not child_review or child_review.get('reviewed_commit') != group['snapshot']['commit_oid']:
            return None
    return group['producer']


def review_cohort(tx, run, reviewer):
    """All current sealed peers must be accepted before any failed facet is repaired."""
    producer = review_producer(tx, run, reviewer)
    if not producer:
        return None
    if reviewer.get('kind') != 'stage_child':
        review = tx.get('review', reviewer.get('attempt_id'))
        return {'producer': producer, 'members': [(reviewer, review)]} if review else None
    parent = tx.get('work_item', reviewer.get('parent_stage_id'))
    group = sealed_review_group(tx, run, parent) if parent else None
    if not group or group['producer']['id'] != producer['id']:
        return None
    members = []
    for child in sorted(group['children'], key=lambda row: row['id']):
        quality = child.get('quality_result')
        if quality not in {'passed', 'failed'}:
            return None
        review = _completed_review(tx, child, quality)
        if not review or review.get('reviewed_commit') != group['snapshot']['commit_oid']:
            return None
        if quality == 'failed':
            members.append((child, review))
    if not any(child['id'] == reviewer['id'] for child, _ in members):
        return None
    return {'producer': producer, 'members': members}


def pending_review_peers(tx, run, reviewer):
    if reviewer.get('kind') != 'stage_child':
        return []
    parent = tx.get('work_item', reviewer.get('parent_stage_id'))
    group = sealed_review_group(tx, run, parent) if parent else None
    if not group or not any(child['id'] == reviewer['id'] for child in group['children']):
        return []
    return [child['id'] for child in group['children'] if child['id'] != reviewer['id']
            and (child.get('status') != 'completed' or child.get('quality_result') not in {'passed', 'failed'})]

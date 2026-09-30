"""Verify a historical review-repair source against its durable authorization chain."""
from __future__ import annotations

import asyncio

from agentflow.common import DomainError
from agentflow.domain.planning import CODING_STEPS

REVIEW_CHECKPOINT_KINDS = {'reviewed_aggregate_child_repair', 'reviewed_source_repair', 'late_test_owner_repair'}

REVIEW_SOURCE_KINDS = ('review_repair', 'review', 'attempt', 'code_snapshot', 'work_item', 'plan', 'stage_expansion', 'dispatch_context')


def review_repair_source(state: dict, run: dict, work: dict, snapshot: dict) -> dict:
    """Allow stale historical evidence only while every original identity still agrees.

    Git integrity is checked by the caller; an intact but unrelated commit is not
    authorization. This pure form is also used inside an atomically rechecked
    recovery-state snapshot.
    """
    try:
        if (not snapshot or snapshot.get('run_id') != run['id']
                or snapshot.get('work_item_id') != work['id'] or work.get('run_id') != run['id']
                or work.get('archived') or work.get('step') not in CODING_STEPS
                or type(snapshot.get('generation')) is not int
                or not 1 <= snapshot['generation'] < work['generation']):
            raise ValueError('review_source_owner_mismatch')
        receipts = [row for row in state.get('review_repair', []) if row.get('run_id') == run['id']
            and ((row.get('mode') in {'parallel_aggregate', 'single_source', 'late_test_owner'}
                  and row.get('checkpoint_alias_ids', {}).get(work['id']) == snapshot['id'])
                 or (not row.get('checkpoint_alias_ids') and row.get('mode') != 'parallel_aggregate'
                     and row.get('producer_work_item_id') == work['id']
                     and row.get('base_snapshot_id') == snapshot['id']))]
        if len(receipts) != 1:
            raise ValueError('review_repair_receipt_missing_or_ambiguous')
        receipt = receipts[0]
        snapshots = {row['id']: row for row in state.get('code_snapshot', [])}
        attempts = {row['id']: row for row in state.get('attempt', [])}
        reviews = {row['id']: row for row in state.get('review', [])}
        source = snapshots.get(receipt.get('base_snapshot_id'))
        review = reviews.get(receipt.get('review_attempt_id'))
        reviewer = attempts.get(receipt.get('review_attempt_id'))
        if (not source or not review or not reviewer
                or work['id'] not in receipt.get('affected_work_item_ids', [])
                or review.get('run_id') != run['id'] or reviewer.get('run_id') != run['id']
                or source.get('run_id') != run['id']
                or source.get('work_item_id') != receipt.get('producer_work_item_id')
                or review.get('work_item_id') != receipt.get('review_work_item_id')
                or reviewer.get('work_item_id') != receipt.get('review_work_item_id')
                or reviewer.get('status') != 'completed' or reviewer.get('generation') != review.get('generation')
                or review.get('quality_result') != 'failed' or not review.get('blocking_findings')
                or receipt.get('base_commit') != review.get('reviewed_commit')
                or receipt.get('base_commit') != source.get('commit_oid')
                or any(snapshot.get(field) != source.get(field)
                       for field in ('commit_oid', 'tree_oid', 'base_oid', 'repository_path'))):
            raise ValueError('review_source_authorization_mismatch')
        if receipt.get('mode') in {'parallel_aggregate', 'single_source', 'late_test_owner'}:
            single = receipt['mode'] == 'single_source'
            late = receipt['mode'] == 'late_test_owner'
            source_author = attempts.get(source['id'])
            contribution = snapshots.get(snapshot.get('source_child_snapshot_id'))
            author = attempts.get(snapshot.get('source_child_snapshot_id'))
            if (snapshot.get('checkpoint_kind') != ('late_test_owner_repair' if late else 'reviewed_source_repair' if single else 'reviewed_aggregate_child_repair')
                    or work['id'] not in receipt.get('repair_work_item_ids', [])
                    or snapshot.get('source_snapshot_id') != source['id']
                    or snapshot.get('source_work_item_id') != source['work_item_id']
                    or snapshot.get('source_generation') != source['generation']
                    or snapshot.get('source_review_id') != review['id']
                    or snapshot.get('source_review_attempt_id') != reviewer['id']
                    or snapshot.get('source_review_work_item_id') != review['work_item_id']
                    or snapshot.get('source_review_generation') != review['generation']
                    or snapshot.get('child_scope') != work.get('write_paths')
                    or (not late and (work['id'] != source['work_item_id'] or snapshot.get('source_child_snapshot_id') != source['id']
                        if single else work.get('parent_stage_id') != source['work_item_id']))
                    or not source_author or source_author.get('status') != 'completed'
                    or source_author.get('run_id') != run['id']
                    or source_author.get('work_item_id') != source['work_item_id']
                    or source_author.get('generation') != source['generation']
                    or not contribution or contribution.get('run_id') != run['id']
                    or contribution.get('work_item_id') != work['id']
                    or contribution.get('generation') != snapshot['generation']
                    or not author or author.get('status') != 'completed' or author.get('run_id') != run['id']
                    or author.get('work_item_id') != work['id'] or author.get('generation') != snapshot['generation']):
                raise ValueError('review_alias_authorization_mismatch')
            if late:
                evidence = receipt.get('owner_evidence', {}).get(work['id'], {})
                original = evidence.get('work', {})
                if (evidence.get('snapshot_id') != contribution['id'] or original.get('id') != work['id']
                        or original.get('attempt_id') != author['id'] or original.get('generation') != snapshot['generation']
                        or original.get('write_paths') != work.get('write_paths')
                        or original.get('step') != work.get('step') or original.get('dependencies') != work.get('dependencies')
                        or original.get('parent_stage_id') != work.get('parent_stage_id')
                        or snapshot.get('finding_paths') != sorted({f['path'] for f in receipt.get('findings_by_work_item', {}).get(work['id'], [])})):
                    raise ValueError('late_test_owner_authorization_mismatch')
                if receipt.get('routing_kind') == 'original_test_phase':
                    from agentflow.control.late_test_review import _phase_receipt_valid
                    from agentflow.control.recovery import _ReadState
                    _phase_receipt_valid(_ReadState(state), run, receipt)
        return {'receipt': receipt, 'review': review, 'reviewer': reviewer, 'source_snapshot': source}
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise DomainError('invalid_review_checkpoint',
            '审查返工来源与原审查、返工授权或代码快照不一致，不能选择替代基线。') from error


async def validate_review_repair_source(store, run: dict, work: dict, snapshot: dict) -> dict:
    """Read-only convenience wrapper; mutating callers must also fence these rows."""
    values = await asyncio.gather(*(store.list(kind) for kind in REVIEW_SOURCE_KINDS))
    state = dict(zip(REVIEW_SOURCE_KINDS, values, strict=True))
    proof = review_repair_source(state, run, work, snapshot)
    if proof['receipt'].get('mode') == 'late_test_owner':
        from agentflow.control.late_test_review import validate_routing_chain
        from agentflow.control.recovery import _ReadState
        validate_routing_chain(_ReadState(state), run, proof['receipt'])
    return proof

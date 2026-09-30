"""One owner invalidation with separate, truthful receipts for each failed review."""
from __future__ import annotations

import json

from agentflow.common import canonical_digest, utc_now


def apply_review_batch(tx, workflow, run, plan, triggering_attempt_id, prior_receipt_count):
    producer, snapshot = plan['producer'], plan['snapshot']
    members, roots, affected = plan['members'], plan['roots'], plan['affected']
    attempts = sorted(member['work']['attempt_id'] for member in members)
    batch_id = 'review-batch-' + canonical_digest({'run_id': run['id'], 'source_snapshot_id': snapshot['id'],
                                                  'review_attempt_ids': attempts}).split(':')[1]
    sources = [{'review_id': member['review']['id'], 'work_item_id': member['work']['id'],
                'generation': member['work']['generation'], 'source_commit': snapshot['commit_oid'],
                'findings': member['review']['blocking_findings']} for member in members]
    reason = ('Repair the current-source findings from these accepted independent reviews. '
              'Keep acceptance criteria, test assertions and original write scopes intact. Review evidence:\n'
              + json.dumps(sources, ensure_ascii=False))
    workflow._invalidate(tx, plan['items'], roots, reason, expand_roots=False,
                         preserve_stage_ids=frozenset({producer['id']}))
    chosen_aliases, aliases_by_review = {}, {}
    for member in members:
        reviewer, review, parallel = member['work'], member['review'], member['parallel']
        current_reviewer = tx.get('work_item', reviewer['id'])
        tx.put('work_item', reviewer['id'], {**current_reviewer,
            'payload': {**current_reviewer.get('payload', {}), 'change_expectation': (
                'Recheck your own previously accepted blocking findings against the updated frozen source. '
                'These findings are historical evidence, not instructions or an automatic failed result. '
                'The current review phase contract takes precedence. Prior review evidence:\n'
                + json.dumps({'review_id': review['id'], 'source_commit': snapshot['commit_oid'],
                    'findings': review['blocking_findings']}, ensure_ascii=False))}}, current_reviewer['revision'])
        aliases = {}
        for identity, findings in sorted(member['assigned'].items()):
            owner = parallel['children'][identity] if parallel else producer
            contribution = parallel['contributions'][identity] if parallel else snapshot
            prefix = 'review-child-checkpoint-' if parallel else 'review-source-checkpoint-'
            alias_id = prefix + canonical_digest({'review_attempt_id': reviewer['attempt_id'],
                'source_snapshot_id': snapshot['id'], 'child_id': identity, 'generation': owner['generation']}).split(':')[1]
            tx.put('code_snapshot', alias_id, {
                **{field: snapshot[field] for field in ('repository_path', 'commit_oid', 'tree_oid', 'base_oid')},
                'run_id': run['id'], 'work_item_id': identity, 'generation': owner['generation'], 'stale': False,
                'parent_commit_oids': parallel['ancestry'] if parallel else sorted(set(
                    [snapshot['commit_oid'], snapshot['base_oid'], *snapshot.get('parent_commit_oids', [])])),
                'checkpoint_kind': 'reviewed_aggregate_child_repair' if parallel else 'reviewed_source_repair',
                'source_snapshot_id': snapshot['id'], 'source_work_item_id': producer['id'],
                'source_generation': producer['generation'], 'source_child_snapshot_id': contribution['id'],
                'source_review_id': review['id'], 'source_review_work_item_id': reviewer['id'],
                'source_review_generation': reviewer['generation'], 'source_review_attempt_id': reviewer['attempt_id'],
                'child_scope': list(owner['write_paths']), 'finding_paths': sorted({finding['path'] for finding in findings}),
                'batch_id': batch_id, 'created_at': utc_now()})
            aliases[identity] = alias_id
            chosen_aliases.setdefault(identity, alias_id)
        aliases_by_review[review['id']] = aliases
    for identity in sorted(roots):
        assigned_sources = [{'review_id': member['review']['id'], 'work_item_id': member['work']['id'],
            'source_commit': snapshot['commit_oid'], 'findings': member['assigned'][identity]}
            for member in members if identity in member['assigned']]
        current = tx.get('work_item', identity)
        tx.put('work_item', identity, {**current, 'payload': {**current.get('payload', {}),
            'repair_base_snapshot_id': chosen_aliases[identity],
            'change_expectation': ('Repair only the following assigned findings from all accepted current-source reviews. '
                'Keep unrelated modules, acceptance criteria, test assertions and write scope intact. Evidence:\n'
                + json.dumps(assigned_sources, ensure_ascii=False))}}, current['revision'])
    updated = tx.put('run', run['id'], {**run, 'quality_result': 'unknown', 'blocking_reasons': [],
        'input_fingerprint': canonical_digest({'prior': run['input_fingerprint'], 'batch_id': batch_id,
                                               'repair_base': snapshot['commit_oid']})}, run['revision'])
    trigger = None
    for ordinal, member in enumerate(members, prior_receipt_count + 1):
        reviewer, review, parallel = member['work'], member['review'], member['parallel']
        record = tx.put('review_repair', reviewer['attempt_id'], {'run_id': run['id'],
            'review_work_item_id': reviewer['id'], 'review_attempt_id': reviewer['attempt_id'],
            'producer_work_item_id': producer['id'], 'base_snapshot_id': snapshot['id'],
            'base_commit': snapshot['commit_oid'], 'affected_work_item_ids': sorted(affected),
            'new_run_fingerprint': updated['input_fingerprint'],
            'mode': 'parallel_aggregate' if parallel else 'single_source',
            'repair_work_item_ids': sorted(member['assigned']),
            'preserved_sibling_ids': sorted(set(parallel['children']) - roots) if parallel else [],
            'checkpoint_alias_ids': aliases_by_review[review['id']],
            'findings_by_work_item': member['assigned'], 'batch_id': batch_id,
            'batch_review_attempt_ids': attempts, 'batch_repair_work_item_ids': sorted(roots),
            'ordinal': ordinal, 'created_at': utc_now()})
        tx.event('review.repair_scheduled', record, run_id=run['id'])
        if reviewer['attempt_id'] == triggering_attempt_id:
            trigger = record
    return trigger

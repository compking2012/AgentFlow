"""Repair known, pre-freeze review failures within the original execution authority."""
from __future__ import annotations

import json
import re
import time
from uuid import uuid4

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.review_producer import pending_review_peers, review_cohort
from agentflow.domain.expansion import _path as normalize_scope
from agentflow.domain.planning import CODING_STEPS, descendants
from agentflow.models.budget import account_id


def _within(path, scopes):
    return any(scope == '.' or path == scope or path.startswith(scope + '/') for scope in scopes)


def _known_fingerprint(value):
    return isinstance(value, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', value) is not None


def review_repair_allowed(limit, count):
    """-1 continues until passed; zero disables; positive values cap quality repair."""
    return type(limit) is int and (limit == -1 or limit > 0 and count < limit)


def review_repair_count(records, attempts, work_id):
    count = 0
    for record in records:
        owner = record.get('review_work_item_id') or attempts.get(record['review_attempt_id'], {}).get('work_item_id')
        # Unknown legacy ownership must not silently reset its limit.
        count += owner is None or owner == work_id
    return count


def _review_child_context(tx, run, reviewer, producer):
    """A review facet still reviews the containing stage's exact coding producer."""
    parent = tx.get('work_item', reviewer.get('parent_stage_id')) if reviewer.get('parent_stage_id') else None
    if (not parent or parent.get('archived') or reviewer.get('archived')
            or parent.get('kind') != 'aggregation' or parent.get('step') != 'code_review'
            or parent.get('role') != 'review' or parent.get('run_id') != run['id']
            or parent.get('project_id') != run.get('project_id')
            or parent.get('original_dependencies') != [producer['id']]
            or parent.get('original_write_paths') != [] or parent.get('write_paths') != []):
        return False
    identities, dependencies = parent.get('expanded_child_ids'), parent.get('dependencies')
    if (not isinstance(identities, list) or not 2 <= len(identities) <= 32
            or any(not isinstance(identity, str) for identity in identities)
            or len(set(identities)) != len(identities) or reviewer['id'] not in identities
            or not isinstance(dependencies, list) or len(dependencies) != len(identities)
            or any(not isinstance(identity, str) for identity in dependencies) or set(dependencies) != set(identities)):
        return False
    children = [item for item in tx.list('work_item') if item.get('parent_stage_id') == parent['id']
                and not item.get('archived')]
    return (set(item['id'] for item in children) == set(identities)
            and all(item.get('kind') == 'stage_child' and item.get('step') == 'code_review'
                and item.get('role') == 'review' and item.get('run_id') == run['id']
                and item.get('project_id') == run.get('project_id')
                and item.get('dependencies') == parent['original_dependencies'] and item.get('write_paths') == []
                for item in children))


def _parallel_repair(tx, run, producer, snapshot, review, items):
    """Resolve every finding to one current child without widening its authorization."""
    identities = producer.get('expanded_child_ids')
    if (not isinstance(identities, list) or not 2 <= len(identities) <= 32
            or any(not isinstance(identity, str) for identity in identities)
            or len(set(identities)) != len(identities) or set(identities) != set(producer['dependencies'])
            or snapshot['id'] != producer.get('attempt_id') or snapshot.get('run_id') != run['id']
            or review.get('run_id') != run['id']
            or not all(isinstance(snapshot.get(field), str) and snapshot[field]
                       for field in ('repository_path', 'commit_oid', 'tree_oid', 'base_oid'))
            or not isinstance(producer.get('original_dependencies'), list)):
        return None
    children = {item['id']: item for item in items if item.get('parent_stage_id') == producer['id']
                and not item.get('archived')}
    if set(children) != set(identities):
        return None
    contributions, scopes = {}, {}
    try:
        original = [normalize_scope(path) for path in producer.get('original_write_paths', [])]
        if not original:
            return None
        for identity, child in children.items():
            if (child.get('kind') != 'stage_child' or child.get('step') != producer['step']
                    or child.get('role') != producer['role'] or child.get('status') != 'completed'
                    or child.get('dependencies') != producer['original_dependencies']
                    or not child.get('attempt_id')):
                return None
            scopes[identity] = [normalize_scope(path) for path in child.get('write_paths', [])]
            if not scopes[identity] or any(not _within(path, original) for path in scopes[identity]):
                return None
            contribution = tx.get('code_snapshot', child['attempt_id'])
            if (not contribution or contribution.get('stale') or contribution.get('run_id') != run['id']
                    or contribution.get('work_item_id') != identity or contribution.get('generation') != child['generation']
                    or not all(isinstance(contribution.get(field), str) and contribution[field]
                               for field in ('repository_path', 'commit_oid', 'tree_oid', 'base_oid'))):
                return None
            contributions[identity] = contribution
        assigned = {}
        for finding in review['blocking_findings']:
            if not isinstance(finding, dict) or finding.get('severity') != 'blocking':
                return None
            path = normalize_scope(finding.get('path'))
            if path == '.':
                return None
            owners = [identity for identity, paths in scopes.items() if _within(path, paths)]
            if len(owners) != 1:
                return None
            assigned.setdefault(owners[0], []).append({**finding, 'path': path})
    except (DomainError, TypeError, ValueError):
        return None
    if not assigned:
        return None
    # Later aggregation receipts contain only their direct tips. Retain the
    # current contributions and their ancestry so another repair round still
    # recognizes unchanged siblings as already included in the reviewed base.
    ancestry = set()
    for source in [snapshot, *contributions.values()]:
        parents = source.get('parent_commit_oids', [])
        if (not isinstance(parents, list) or any(not isinstance(value, str) or not value for value in parents)
                or not isinstance(source.get('base_oid'), str) or not source['base_oid']):
            return None
        ancestry.update([source['commit_oid'], source['base_oid'], *parents])
    return {'children': children, 'contributions': contributions, 'scopes': scopes,
            'assigned': assigned, 'ancestry': sorted(ancestry)}


def _review_repair_plan(tx, run, reviewer):
    cohort = review_cohort(tx, run, reviewer)
    if not cohort:
        return None
    producer = cohort['producer']
    if producer.get('step') not in CODING_STEPS or producer.get('archived') or producer.get('status') != 'completed':
        return None
    items = [item for item in tx.list('work_item') if item.get('run_id') == run['id'] and not item.get('archived')]
    members, roots, snapshot = [], set(), None
    for member, review in cohort['members']:
        if not review or not review.get('blocking_findings'):
            return None
        sources = [row for row in tx.list('code_snapshot') if row.get('work_item_id') == producer['id']
            and row.get('generation') == producer['generation'] and not row.get('stale')
            and row.get('commit_oid') == review.get('reviewed_commit')]
        if len(sources) != 1 or snapshot is not None and sources[0]['id'] != snapshot['id']:
            return None
        snapshot = sources[0]
        parallel = _parallel_repair(tx, run, producer, snapshot, review, items) if producer.get('kind') == 'aggregation' else None
        if producer.get('kind') == 'aggregation' and parallel is None:
            return None
        assigned = parallel['assigned'] if parallel else {producer['id']: review['blocking_findings']}
        if not parallel:
            try:
                scopes = [normalize_scope(path) for path in producer.get('write_paths', [])]
                normalized = []
                for finding in review['blocking_findings']:
                    path = normalize_scope(finding.get('path'))
                    if finding.get('severity') != 'blocking' or path == '.' or not _within(path, scopes):
                        return None
                    normalized.append({**finding, 'path': path})
                assigned = {producer['id']: normalized}
            except (DomainError, TypeError, ValueError, AttributeError):
                return None
        roots.update(assigned)
        members.append({'work': member, 'review': review, 'parallel': parallel, 'assigned': assigned})
    return {'producer': producer, 'snapshot': snapshot, 'members': members, 'roots': roots,
            'items': items, 'affected': descendants(items, roots)}


def review_repair_target(tx, run, reviewer):
    """Resolve every current review's coding owners before checking their shared budgets."""
    from agentflow.control.late_test_review import late_test_target
    late = late_test_target(tx, run, reviewer)
    if late:
        return late
    if reviewer.get('payload', {}).get('late_test_review_binding'):
        return None  # Bound full-source authorship is not fallback scope authority.
    plan = _review_repair_plan(tx, run, reviewer)
    if not plan:
        return None
    return {'work_item_id': reviewer['id'], 'root_work_item_ids': sorted(plan['roots']),
            'review_work_item_ids': [member['work']['id'] for member in plan['members']],
            'affected_work_item_ids': sorted(plan['affected'])}


def review_repair_manual_blockers(tx, run, target):
    """A review repair cannot consume an owner's still-pending explicit retry."""
    if not target:
        return []
    work_ids = set(target['affected_work_item_ids']) | {target['work_item_id']}
    work = {item['id']: item for item in tx.list('work_item') if item['id'] in work_ids}
    blockers = []
    for kind, code, message in (
        ('work_execution_budget_adjustment', 'manual_retry_after_budget_change',
         '返工任务的执行额度已追加，请明确选择重试；保存额度本身不会启动 Agent。'),
        ('model_uncertainty_acknowledgment', 'manual_retry_after_model_ack',
         '返工任务的未知模型调用已确认保留，请明确选择重试；确认本身不会启动 Agent。'),
    ):
        if any(row.get('run_id') == run['id'] and row.get('work_item_id') in work
               and row.get('work_generation') == work[row['work_item_id']]['generation']
               and row.get('requires_explicit_retry') for row in tx.list(kind)):
            blockers.append({'code': code, 'message': message})
    return blockers


class ReviewRemediation:
    def __init__(self, store, workflow):
        self.store, self.workflow = store, workflow
        self._event_cursor = 0
        self._initialized = False
        self._deferred = {}

    async def reconcile(self):
        events = await self.store.events(self._event_cursor, limit=10000)
        changed_runs = {e['run_id'] for e in events}
        if events:
            self._event_cursor = events[-1]['id']
        due = {identity for identity, deadline in self._deferred.items() if deadline <= time.time()}
        if self._initialized and not changed_runs and not due:
            return
        for item in await self.store.list('work_item'):
            if self._initialized and item.get('run_id') not in changed_runs and item['id'] not in due:
                continue
            if item.get('step') == 'code_review' and item.get('quality_result') == 'failed' and item.get('status') == 'completed':
                await self.repair(item['id'])
        self._initialized = True

    async def repair(self, review_work_id: str, *, analysis_id: str | None = None):
        """Legacy callers receive only actual scheduled-repair receipts."""
        outcome = await self.repair_outcome(review_work_id, analysis_id=analysis_id)
        return outcome['receipt'] if outcome['outcome'] == 'scheduled' else None

    async def repair_outcome(self, review_work_id: str, *, analysis_id: str | None = None):
        from agentflow.control.review_contract_view import review_contract_outcome
        try:
            result = await self._repair(review_work_id, analysis_id=analysis_id)
        except DomainError as error:
            self._deferred.pop(review_work_id, None)
            return {'outcome': 'blocked', 'receipt': None, 'retryable': False, 'blockers': [
                {'code': error.code, 'message': error.message, **({'details': error.details} if error.details is not None else {})}]}
        if isinstance(result, dict) and 'outcome' in result:
            return result
        if result is None:
            return {'outcome': 'not_applicable', 'receipt': None, 'retryable': False, 'blockers': [
                {'code': 'review_repair_state_changed', 'message': '当前审查状态或返工来源已变化，需要重新核验。'}]}
        if 'state' in result and 'stage_id' in result:
            self._deferred.pop(review_work_id, None)
            return review_contract_outcome(result)
        return {'outcome': 'scheduled', 'receipt': result, 'blockers': [], 'retryable': False}

    async def _repair(self, review_work_id: str, *, analysis_id: str | None = None):
        # Check the actual coding owners' cumulative allowance, including review
        # facets and expanded coding stages. Freeze that read across async I/O.
        from agentflow.control.recovery import KINDS, RunRecoveryService, _ReadState, _related
        reviewer = await self.store.read('work_item', review_work_id)
        if not reviewer or reviewer.get('status') != 'completed' or reviewer.get('quality_result') != 'failed':
            self._deferred.pop(review_work_id, None)
            return None
        recovery = RunRecoveryService(self.store, self.workflow)
        state = await recovery._read(reviewer['run_id'])
        reviewer = _ReadState(state).get('work_item', review_work_id)
        if not reviewer or reviewer.get('status') != 'completed' or reviewer.get('quality_result') != 'failed':
            return None
        from agentflow.control.late_test_review import LateTestReviewRepair, late_test_target
        if late_test_target(_ReadState(state), state['run'][0], reviewer):
            return await LateTestReviewRepair(self.store, self.workflow).repair(review_work_id, analysis_id=analysis_id)
        if reviewer.get('payload', {}).get('late_test_review_binding'):
            return None
        def refusal(reasons, *, retryable=False):
            return {'outcome': 'blocked', 'receipt': None, 'blockers': reasons, 'retryable': retryable}

        def defer(reasons=None):
            self._deferred[review_work_id] = time.time() + max(1, self.workflow.settings.auto_failure_retry_delay_seconds)
            return refusal(reasons or [{'code': 'review_repair_state_changed',
                'message': '核验期间运行证据发生变化，等待重新核验当前状态。'}], retryable=True)

        target = review_repair_target(_ReadState(state), state['run'][0], reviewer)
        blockers = recovery._common_blockers(state)
        blockers += await recovery._process_blockers(state)
        blockers += await recovery._target_blockers(state, target)
        blockers += review_repair_manual_blockers(_ReadState(state), state['run'][0], target)
        if target is None and not blockers:
            from agentflow.control.review_contract_repair import ReviewContractRepair
            resolution = await ReviewContractRepair(self.store, self.workflow).ensure_triage(
                review_work_id, analysis_id=analysis_id)
            if resolution is not None:
                return resolution
            raise DomainError('review_repair_target_unresolved', '原作者范围与审查问题不匹配，且尚无可核验的归属分析目标。')
        if state['run'][0].get('restore_reconciliation_required'):
            blockers.append({'code': 'recovery_restore_uncertain', 'message': '恢复数据仍待核对，尚未安排审查返工。'})
        if target is None or blockers:
            if pending_review_peers(_ReadState(state), state['run'][0], reviewer):
                return defer([*blockers, {'code': 'review_peers_incomplete',
                    'message': '等待全部当前并行审查结束，尚未安排返工。'}])
            if any(row['code'] in {'active_work', 'execution_unknown', 'recovery_budget_uncertain'} for row in blockers):
                return defer(blockers)
            return refusal(blockers or [{'code': 'review_repair_target_unresolved',
                'message': '当前审查没有可核验的原作者修复目标。'}])
        state_digest = canonical_digest(state)

        # A refused proposal is not cached: pending human decisions and sibling
        # work can resolve later. Accepted proposals are fenced and durable.
        def apply(tx):
            reviewer = tx.get('work_item', review_work_id)
            if not reviewer or reviewer['status'] != 'completed' or reviewer.get('quality_result') != 'failed':
                return None
            if canonical_digest(_related({kind: tx.list(kind) for kind in KINDS}, reviewer['run_id'])) != state_digest:
                return defer()
            if analysis_id:
                from agentflow.control.failure_remediation import guard_automatic
                guard_automatic(tx, self.workflow, analysis_id, reviewer, 'repair_review_findings')
            run = tx.get('run', reviewer['run_id'])
            if not run or run['execution_state'] != 'running' or run.get('delivery_ids'):
                return None
            limit = self.workflow.settings.auto_review_repair_limit
            repairs = [r for r in tx.list('review_repair') if r['run_id'] == run['id']]
            attempts = {r['review_attempt_id']: tx.get('attempt', r['review_attempt_id']) or {} for r in repairs}
            if (not review_repair_allowed(limit, review_repair_count(repairs, attempts, reviewer['id']))
                    or any(r['review_attempt_id'] == reviewer['attempt_id'] for r in repairs)):
                return None
            # An authorized repair/revision starts a new input version. Older
            # frozen candidates remain audit history; unknown identity stays blocked.
            if any(c.get('run_id') == run['id'] and (
                    not _known_fingerprint(run.get('input_fingerprint'))
                    or not _known_fingerprint(c.get('run_input_fingerprint'))
                    or c['run_input_fingerprint'] == run['input_fingerprint']) for c in tx.list('candidate')) or any(
                    i['run_id'] == run['id'] for i in tx.list('delivery_intent')):
                return None
            repair_plan = _review_repair_plan(tx, run, reviewer)
            if not repair_plan:
                return None
            for member in repair_plan['members']:
                owner = member['work']
                if (not review_repair_allowed(limit, review_repair_count(repairs, attempts, owner['id']))
                        or any(record['review_attempt_id'] == owner['attempt_id'] for record in repairs)):
                    return None
            plan = tx.get('plan', run['plan_id'])
            producer = repair_plan['producer']
            if (not producer or producer.get('archived') or producer['step'] not in CODING_STEPS or producer['status'] != 'completed'
                    or producer['step'] not in plan.get('authorized_rework_steps', [])
                    or producer.get('kind') == 'stage_child' or producer.get('parent_stage_id')):
                return None
            review_child = reviewer.get('kind') == 'stage_child'
            review_aggregate = reviewer.get('kind') == 'aggregation'
            if review_child and not _review_child_context(tx, run, reviewer, producer):
                return None
            review = tx.get('review', reviewer['attempt_id'])
            attempt = tx.get('attempt', reviewer['attempt_id'])
            if (not review or not attempt or attempt['status'] != 'completed'
                    or attempt['fencing_token'] != reviewer['fencing_token']
                    or attempt['generation'] != reviewer['generation']
                    or attempt['input_fingerprint'] != reviewer['input_fingerprint']
                    or review['generation'] != reviewer['generation'] or not review.get('blocking_findings')):
                return None
            if producer.get('kind') == 'aggregation' or review_child or review_aggregate:
                project_id = run.get('project_id')
                if (not isinstance(project_id, str) or not project_id or reviewer.get('step') != 'code_review'
                        or review.get('stale') or review.get('run_id') != run['id']
                        or review.get('quality_result') != 'failed'
                        or review.get('work_item_id') != reviewer['id']
                        or attempt.get('run_id') != run['id'] or attempt.get('work_item_id') != reviewer['id']
                        or producer.get('run_id') != run['id'] or producer.get('project_id') != project_id
                        or reviewer.get('project_id') != project_id):
                    return None
            snapshots = [s for s in tx.list('code_snapshot') if s.get('work_item_id') == producer['id']
                         and s.get('generation') == producer['generation'] and not s.get('stale')
                         and s.get('commit_oid') == review.get('reviewed_commit')]
            if len(snapshots) != 1:
                return None
            items = [w for w in tx.list('work_item') if w['run_id'] == run['id'] and not w.get('archived')]
            snapshot = snapshots[0]
            if review_child or review_aggregate:
                if (snapshot.get('id') != producer.get('attempt_id') or snapshot.get('run_id') != run['id']
                        or not all(isinstance(snapshot.get(field), str) and snapshot[field]
                                   for field in ('repository_path', 'commit_oid', 'tree_oid', 'base_oid'))):
                    return None
            if producer.get('kind') != 'aggregation':
                try:
                    scopes = [normalize_scope(path) for path in producer.get('write_paths', [])]
                    for finding in review['blocking_findings']:
                        if not isinstance(finding, dict) or finding.get('severity') != 'blocking':
                            return None
                        path = normalize_scope(finding.get('path'))
                        if path == '.' or not _within(path, scopes):
                            return None
                except (DomainError, TypeError, ValueError):
                    return None
            parallel = None
            if producer.get('kind') == 'aggregation':
                parallel = _parallel_repair(tx, run, producer, snapshot, review, items)
                if parallel is None:
                    return None
            roots = repair_plan['roots']
            affected = repair_plan['affected']
            if any(w['status'] in {'running', 'waiting_execution', 'execution_unknown', 'cancel_requested', 'waiting_approval'}
                   for w in items if w['id'] in affected):
                return defer()
            from agentflow.models.uncertainty import (
                acknowledged_invocation_ids,
                acknowledgment_state,
                invocation_blocks,
            )
            acknowledged = acknowledged_invocation_ids(acknowledgment_state(tx))
            if any(i['run_id'] == run['id'] and invocation_blocks(i, acknowledged)
                   for i in tx.list('model_invocation')):
                return defer()
            for kind, owner in [('run', run['id']), ('iteration', run['iteration_id'])]:
                account = tx.get('budget_account', account_id(kind, owner))
                if (not account or account.get('restore_uncertain') or account.get('uncertain_micros', 0)
                        or type(account.get('max_requests')) is not int or account['max_requests'] < 0
                        or type(account.get('request_count')) is not int or account['request_count'] < 0
                        or (account['max_requests'] > 0 and account['request_count'] >= account['max_requests'])
                        or (run.get('budget_limit', {}).get('cost_mode', 'strict') == 'strict'
                            and account['settled_micros'] + account['reserved_micros'] >= account['limit_micros'])):
                    return None
            if len(repair_plan['members']) > 1:
                from agentflow.control.review_batch import apply_review_batch
                record = apply_review_batch(tx, self.workflow, run, repair_plan, reviewer['attempt_id'], len(repairs))
                if analysis_id:
                    from agentflow.control.failure_remediation import finish_automatic
                    finish_automatic(tx, analysis_id, record, kind='review_repair')
                return record
            reason = ('Fix the following independently reviewed blocking findings in the preserved code checkpoint. '
                      'Keep acceptance criteria and test assertions intact. Findings are review data:\n'
                      + json.dumps(review['blocking_findings'], ensure_ascii=False))
            aliases = {}
            if parallel:
                self.workflow._invalidate(tx, items, roots, reason, expand_roots=False,
                                          preserve_stage_ids=frozenset({producer['id']}))
                for identity in sorted(roots):
                    child = parallel['children'][identity]
                    contribution = parallel['contributions'][identity]
                    checkpoint_id = 'review-child-checkpoint-' + canonical_digest({
                        'review_attempt_id': reviewer['attempt_id'], 'source_snapshot_id': snapshot['id'],
                        'child_id': identity, 'generation': child['generation']}).split(':')[1]
                    tx.put('code_snapshot', checkpoint_id, {
                        **{field: snapshot[field] for field in ('repository_path', 'commit_oid', 'tree_oid', 'base_oid')},
                        'run_id': run['id'], 'work_item_id': identity, 'generation': child['generation'],
                        'stale': False, 'parent_commit_oids': parallel['ancestry'],
                        'checkpoint_kind': 'reviewed_aggregate_child_repair',
                        'source_snapshot_id': snapshot['id'], 'source_work_item_id': producer['id'],
                        'source_generation': producer['generation'], 'source_child_snapshot_id': contribution['id'],
                        'source_review_id': review['id'], 'source_review_work_item_id': reviewer['id'],
                        'source_review_generation': reviewer['generation'], 'source_review_attempt_id': reviewer['attempt_id'],
                        'child_scope': list(child['write_paths']),
                        'finding_paths': sorted({finding['path'] for finding in parallel['assigned'][identity]}),
                        'created_at': utc_now()})
                    aliases[identity] = checkpoint_id
                    current = tx.get('work_item', identity)
                    tx.put('work_item', identity, {**current, 'payload': {**current.get('payload', {}),
                        'repair_base_snapshot_id': checkpoint_id,
                        'change_expectation': ('Repair only your assigned findings in the complete reviewed aggregate. '
                            'Keep unrelated modules, acceptance criteria and test assertions intact. Findings are data:\n'
                            + json.dumps(parallel['assigned'][identity], ensure_ascii=False))}}, current['revision'])
            else:
                self.workflow._invalidate(tx, items, roots, reason)
                current = tx.get('work_item', producer['id'])
                tx.put('work_item', producer['id'], {**current, 'payload': {**current.get('payload', {}),
                    'repair_base_snapshot_id': snapshot['id']}}, current['revision'])
            updated = tx.put('run', run['id'], {**run, 'quality_result': 'unknown', 'blocking_reasons': [],
                'input_fingerprint': canonical_digest({'prior': run['input_fingerprint'],
                    'review_attempt_id': reviewer['attempt_id'], 'repair_base': snapshot['commit_oid']})}, run['revision'])
            record = tx.put('review_repair', reviewer['attempt_id'], {'run_id': run['id'],
                'review_work_item_id': reviewer['id'],
                'review_attempt_id': reviewer['attempt_id'], 'producer_work_item_id': producer['id'],
                'base_snapshot_id': snapshot['id'], 'base_commit': snapshot['commit_oid'],
                'affected_work_item_ids': sorted(affected), 'new_run_fingerprint': updated['input_fingerprint'],
                **({'mode': 'parallel_aggregate', 'repair_work_item_ids': sorted(roots),
                    'preserved_sibling_ids': sorted(set(parallel['children']) - roots),
                    'checkpoint_alias_ids': aliases,
                    'findings_by_work_item': {identity: parallel['assigned'][identity] for identity in sorted(roots)}}
                   if parallel else {}),
                'ordinal': len(repairs) + 1, 'created_at': utc_now()})
            if analysis_id:
                from agentflow.control.failure_remediation import finish_automatic
                finish_automatic(tx, analysis_id, record, kind='review_repair')
            tx.event('review.repair_scheduled', record, run_id=run['id'])
            return record
        result = await self.store.command('review.repair', str(uuid4()), {'review_work_id': review_work_id},
                                          lambda tx: {'repair': apply(tx)})
        if result['repair'] is not None and result['repair'].get('outcome', 'scheduled') == 'scheduled':
            self._deferred.pop(review_work_id, None)
        return result['repair']

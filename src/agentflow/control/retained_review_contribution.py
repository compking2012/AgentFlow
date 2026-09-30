"""Prove an unchanged review-repair child still contains its accepted contribution.

This is collection evidence, never a quality pass or permission for an empty initial
implementation. The execution/diff base remains the complete reviewed aggregate.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.recovery import (
    KINDS,
    STOPPED,
    RunRecoveryService,
    _ReadState,
    _related,
    coding_usage_blockers,
)
from agentflow.control.review_checkpoint import REVIEW_CHECKPOINT_KINDS, review_repair_source
from agentflow.domain.expansion import _path, _within
from agentflow.domain.planning import CODING_STEPS
from agentflow.models.budget import account_id
from agentflow.models.uncertainty import (
    acknowledged_invocation_ids,
    attempt_uncertainty_blocks,
    invocation_blocks,
)

_KINDS = tuple(dict.fromkeys((*KINDS, 'run_recovery', 'work_revision')))


def _invalid():
    return DomainError('invalid_review_checkpoint',
        '未改动的审查返工无法核验原已完成贡献、原范围、完整源码或已停止执行；未接受空实现。')


class RetainedReviewContribution:
    def __init__(self, store, workflow, repository, coding_steps):
        self.store, self.workflow, self.repository, self.coding_steps = store, workflow, repository, coding_steps
        self.recovery = RunRecoveryService(store, workflow)

    @staticmethod
    def _review_origin(state, work):
        """Ordinary coding recovery is not authority to reuse a review contribution."""
        declared = set()
        for receipt in state['review_repair']:
            declared.add(receipt.get('checkpoint_alias_ids', {}).get(work['id']))
            if receipt.get('producer_work_item_id') == work['id']:
                declared.add(receipt.get('base_snapshot_id'))
        declared.discard(None)
        snapshots = {row['id']: row for row in state['code_snapshot']}
        identity, visited = work.get('payload', {}).get('repair_base_snapshot_id'), set()
        while identity:
            if identity in declared:
                return True
            if identity in visited:
                raise _invalid()
            visited.add(identity)
            snapshot = snapshots.get(identity)
            if not snapshot:
                return False
            if (snapshot.get('checkpoint_kind') in REVIEW_CHECKPOINT_KINDS
                    or any(snapshot.get(key) for key in ('source_review_snapshot_id', 'source_review_id',
                                                        'source_review_attempt_id', 'source_review_work_item_id'))):
                return True
            if snapshot.get('purpose') != 'recovery_checkpoint':
                return False
            identity = snapshot.get('source_repair_snapshot_id')
        return False

    @staticmethod
    def _evidence_state(state, binding):
        """Fence the evidence used by this work, not independent sibling progress."""
        work_id = binding['work']['id']
        own_attempts = {row['id'] for row in state['attempt'] if row.get('work_item_id') == work_id}
        authorization = binding['authorized']
        attempt_ids = own_attempts | {authorization['reviewer']['id'], authorization['source_snapshot']['id']}
        snapshot_ids = set(binding['chain']) | {binding['contribution']['id'], authorization['source_snapshot']['id']}
        recovery_ids = {row.get('recovery_id') for row in state['code_snapshot'] if row['id'] in snapshot_ids}
        selected = {}
        for kind, rows in state.items():
            if kind == 'run':
                selected[kind] = [{key: binding['run'].get(key) for key in
                                  ('id', 'project_id', 'iteration_id', 'plan_id', 'input_fingerprint')}]
            elif kind == 'work_item':
                selected[kind] = [row for row in rows if row['id'] == work_id]
            elif kind == 'attempt':
                selected[kind] = [row for row in rows if row['id'] in attempt_ids]
            elif kind == 'code_snapshot':
                selected[kind] = [row for row in rows if row['id'] in snapshot_ids]
            elif kind == 'review_repair':
                selected[kind] = [row for row in rows if row['id'] == authorization['receipt']['id']]
            elif kind == 'review':
                selected[kind] = [row for row in rows if row['id'] == authorization['review']['id']]
            elif kind == 'run_recovery':
                selected[kind] = [row for row in rows if row['id'] in recovery_ids]
            elif kind in {'dispatch_context', 'supervised_attempt', 'model_attempt_budget'}:
                selected[kind] = [row for row in rows if row['id'] in own_attempts]
            elif kind in {'coding_step_control', 'coding_step_usage', 'coding_work_budget', 'work_revision',
                          'approval', 'task_authorization', 'model_invocation', 'model_uncertainty_acknowledgment', 'timeout_recovery'}:
                selected[kind] = [row for row in rows if row.get('work_item_id') == work_id or row.get('attempt_id') in own_attempts]
        return selected

    def _binding(self, state, task, snapshot):
        tx = _ReadState(state)
        run, work = state['run'][0], tx.get('work_item', task['work_item_id'])
        attempt = tx.get('attempt', task['attempt_id'])
        control = tx.get('coding_step_control', task['attempt_id'])
        context = tx.get('dispatch_context', task['attempt_id'])
        durable_task = {key: value for key, value in task.items() if key != 'task_token'}
        if (run.get('execution_state') not in {'running', 'paused'} or run.get('delivery_ids')
                or not work or work.get('step') not in CODING_STEPS or work.get('kind') == 'aggregation' or work.get('status') != 'running'
                or work.get('archived') or not work.get('write_paths')
                or work.get('attempt_id') != task['attempt_id'] or not attempt or attempt.get('status') != 'running'
                or not control or control != task.get('coding_step') or not context or context.get('task') != durable_task
                or attempt.get('work_item_id') != work['id'] or work.get('run_id') != run['id']
                or any(attempt.get(key) != work.get(key) or control.get(key) != work.get(key)
                       for key in ('run_id', 'generation', 'fencing_token', 'input_fingerprint'))
                or control.get('work_item_id') != work['id'] or control.get('attempt_id') != attempt['id']
                or task.get('allowed_write_paths') != work['write_paths'] or task.get('step') != work['step']
                or task.get('source_commit') != control.get('source_commit')
                or control.get('base_commit') != task.get('source_commit')
                or any(row.get(flag) for row in (run, work, attempt, control) for flag in
                       ('restore_uncertain', 'restore_reconciliation_required', 'restore_revalidation_required'))):
            raise _invalid()
        scope = [_path(path) for path in work['write_paths']]
        current = tx.get('code_snapshot', work.get('payload', {}).get('repair_base_snapshot_id'))
        if not current or current.get('generation') != work['generation'] - 1:
            raise _invalid()
        chain, historical_attempts = [], {attempt['id']}
        while current.get('purpose') == 'recovery_checkpoint':
            if current['id'] in chain:
                raise _invalid()
            chain.append(current['id'])
            recovery = tx.get('run_recovery', current.get('recovery_id'))
            source_attempt = tx.get('attempt', current.get('source_attempt_id'))
            source_context = tx.get('dispatch_context', current.get('source_attempt_id'))
            source_task = (source_context or {}).get('task', {})
            checkpoint = (recovery or {}).get('checkpoint', {})
            points = checkpoint.get('items', [checkpoint])
            if (not recovery or recovery.get('run_id') != run['id'] or work['id'] not in recovery.get('affected_work_item_ids', [])
                    or current.get('run_id') != run['id'] or current.get('work_item_id') != work['id']
                    or not source_attempt or source_attempt.get('status') not in STOPPED
                    or source_attempt.get('work_item_id') != work['id'] or source_attempt.get('run_id') != run['id']
                    or any(source_attempt.get(field) != current.get('source_' + field)
                           for field in ('generation', 'fencing_token', 'input_fingerprint'))
                    or not any(point.get('snapshot_id') == current['id'] and point.get('commit_oid') == current.get('commit_oid') for point in points)
                    or current.get('commit_oid') != task['source_commit'] or current.get('tree_oid') != snapshot['tree_oid']):
                raise _invalid()
            historical_attempts.add(source_attempt['id'])
            prior_id = current.get('source_repair_snapshot_id') or current.get('source_review_snapshot_id')
            prior = tx.get('code_snapshot', prior_id)
            if not prior or prior.get('commit_oid') != current['commit_oid'] or prior.get('tree_oid') != current['tree_oid']:
                raise _invalid()
            if current.get('source_repair_snapshot_id'):
                if (current.get('source_write_paths') != scope or current.get('source_commit') != prior['commit_oid']
                        or prior.get('generation') != current['source_generation'] - 1
                        or source_task.get('attempt_id') != source_attempt['id'] or source_task.get('work_item_id') != work['id']
                        or source_task.get('source_commit') != prior['commit_oid'] or source_task.get('allowed_write_paths') != scope
                        or any(source_task.get(key) != source_attempt.get(key) for key in ('run_id', 'fencing_token', 'input_fingerprint'))):
                    raise _invalid()
            elif (current.get('source_review_write_paths') != scope
                    or prior.get('source_child_snapshot_id') != source_attempt['id']):
                raise _invalid()
            current = prior
        if current['id'] in chain or current.get('checkpoint_kind') not in REVIEW_CHECKPOINT_KINDS:
            raise _invalid()
        chain.append(current['id'])
        authorized = review_repair_source(state, run, work, current)
        contribution = tx.get('code_snapshot', current['source_child_snapshot_id'])
        author = tx.get('attempt', contribution['id'])
        author_context = tx.get('dispatch_context', contribution['id'])
        author_task = (author_context or {}).get('task', {})
        originals = [row['snapshot'] for row in state['work_revision'] if row.get('work_item_id') == work['id']
                     and row.get('snapshot', {}).get('attempt_id') == contribution['id']]
        if len(originals) != 1:
            raise _invalid()
        original = originals[0]
        original_control = tx.get('coding_step_control', author['id'])
        original_base = original_control.get('base_commit') if original_control else author_task.get('source_commit')
        if (original.get('status') != 'completed' or original.get('quality_result') in {'failed', 'inconclusive'}
                or original.get('write_paths') != scope or original.get('parent_stage_id') != work.get('parent_stage_id')
                or any(original.get(key) != author.get(key) for key in ('run_id', 'generation', 'fencing_token', 'input_fingerprint'))
                or author_task.get('attempt_id') != author['id'] or author_task.get('work_item_id') != work['id']
                or author_task.get('step') != work['step'] or author_task.get('allowed_write_paths') != scope
                or author_task.get('workspace') != contribution.get('repository_path') or original_base != contribution.get('base_oid')
                or author_task.get('coding_step') != original_control
                or any(author_task.get(key) != author.get(key) for key in ('run_id', 'fencing_token', 'input_fingerprint'))
                or current['commit_oid'] != task['source_commit'] or current['tree_oid'] != snapshot['tree_oid']):
            raise _invalid()
        if original.get('approval_required') and not any(row.get('work_item_id') == work['id'] and row.get('decision') == 'approve'
                and row.get('fingerprint') == original.get('approved_fingerprint') for row in state['approval']):
            raise _invalid()
        historical_attempts.add(author['id'])
        acknowledged = acknowledged_invocation_ids(state)
        calls = [row for row in state['model_invocation'] if row.get('attempt_id') in historical_attempts]
        if (any(invocation_blocks(row, acknowledged) for row in calls)
                or any(attempt_uncertainty_blocks(row, state['model_invocation'], acknowledged)
                       for row in state['model_attempt_budget'] if row['id'] in historical_attempts)):
            raise _invalid()
        for kind, owner in [('run', run['id']), ('iteration', run['iteration_id'])]:
            account = tx.get('budget_account', account_id(kind, owner))
            declared = (tx.get(kind, owner) or {}).get('budget_limit', {})
            if (not account or account.get('restore_uncertain') or account.get('uncertain_micros')
                    or account.get('owner_kind') != kind or account.get('owner_id') != owner
                    or account.get('max_requests') != declared.get('max_model_requests')
                    or account.get('limit_micros') != declared.get('limit_micros') or account.get('currency') != declared.get('currency')
                    or any(type(account.get(key)) is not int or account[key] < 0 for key in
                           ('request_count', 'max_requests', 'limit_micros', 'settled_micros', 'reserved_micros'))
                    or account['max_requests'] and account['request_count'] > account['max_requests']
                    or run.get('budget_limit', {}).get('cost_mode') != 'request_limited'
                    and account['settled_micros'] + account['reserved_micros'] > account['limit_micros']):
                raise _invalid()
        process = tx.get('supervised_attempt', attempt['id'])
        if not process or process.get('state') != 'completed':
            raise _invalid()
        return {'run': run, 'work': work, 'alias': current, 'contribution': contribution,
                'authorized': authorized, 'chain': chain, 'scope': scope, 'process': process}

    def _filesystem(self, state, task, snapshot, binding):
        path = Path(task['workspace'])
        if path.is_symlink() or path.resolve() != path:
            raise _invalid()
        source, contribution = binding['alias'], binding['contribution']
        repo = self.repository
        if (repo._integrity(path, source['commit_oid']) != source['tree_oid']
                or repo._integrity(path, contribution['commit_oid']) != contribution['tree_oid']
                or repo._integrity(path, snapshot['commit_oid']) != snapshot['tree_oid']):
            raise _invalid()
        for ancestor in {contribution['base_oid'], contribution['commit_oid'], source['base_oid'], *source.get('parent_commit_oids', [])}:
            repo._run(path, ['merge-base', '--is-ancestor', ancestor, source['commit_oid']])
        captured = repo._collect_diff(path, source['commit_oid'])
        if captured['has_changes'] or captured['tree_oid'] != source['tree_oid']:
            raise _invalid()
        flags = ['--no-ext-diff', '--no-textconv', '--no-renames']
        raw = repo._run(path, ['diff', '--name-only', '-z', *flags, contribution['base_oid'], contribution['commit_oid'], '--'])
        changed = sorted(name for name in raw.decode().split('\0') if name)
        if not changed or any(not _within(_path(name), binding['scope']) for name in changed):
            raise _invalid()
        if repo._run(path, ['diff', '--name-only', '-z', *flags, contribution['commit_oid'], source['commit_oid'], '--', *binding['scope']]):
            raise _invalid()
        patch = repo._run(path, ['diff', '--binary', *flags, contribution['base_oid'], contribution['commit_oid'], '--'])
        self.recovery._verify_process(binding['process'], {row['id']: row for row in state['attempt']})
        return {'source_child_snapshot_id': contribution['id'], 'source_child_commit': contribution['commit_oid'],
                'original_base_commit': contribution['base_oid'], 'original_changed_paths': changed,
                'original_patch_digest': 'sha256:' + hashlib.sha256(patch).hexdigest(),
                'reviewed_source_commit': source['commit_oid'], 'source_tree_oid': source['tree_oid']}

    async def inspect(self, task, snapshot, content):
        if not task.get('coding_step') or not content or content.get('status') != 'complete':
            return None
        work = await self.store.read('work_item', task['work_item_id'])
        if not work or work.get('kind') == 'aggregation' or not work.get('payload', {}).get('repair_base_snapshot_id'):
            return None
        values = await asyncio.gather(*(self.store.list(kind) for kind in _KINDS))
        state = _related(dict(zip(_KINDS, values, strict=True)), task['run_id'])
        try:
            if not self._review_origin(state, work):
                return None
            binding = self._binding(state, task, snapshot)
            blockers = await coding_usage_blockers(state, self.workflow.settings.data_dir, work['id'])
            if blockers:
                raise DomainError(blockers[0]['code'], blockers[0]['message'])
            await self.coding_steps.collect(task, content, snapshot)
            filesystem = await asyncio.to_thread(self._filesystem, state, task, snapshot, binding)
        except (AttributeError, KeyError, TypeError, ValueError, OSError) as error:
            raise _invalid() from error
        return {'state_digest': canonical_digest(self._evidence_state(state, binding)), 'binding': binding, 'filesystem': filesystem,
                'report': {**filesystem, 'run_id': task['run_id'], 'work_item_id': work['id'], 'attempt_id': task['attempt_id'],
                    'generation': work['generation'], 'checkpoint_chain_ids': binding['chain'],
                    'review_attempt_id': binding['authorized']['review']['id'],
                    'current_diff_has_changes': False, 'requires_independent_review': True, 'quality_result': 'unknown'}}

    def commit(self, tx, proof, task, snapshot):
        state = _related({kind: tx.list(kind) for kind in _KINDS}, task['run_id'])
        binding = self._binding(state, task, snapshot)
        if (canonical_digest(self._evidence_state(state, binding)) != proof['state_digest']
                or self._filesystem(state, task, snapshot, binding) != proof['filesystem']):
            raise _invalid()
        record = tx.put('retained_review_contribution', task['attempt_id'], {**proof['report'], 'created_at': utc_now()})
        tx.event('coding.review_contribution_retained', record, run_id=task['run_id'])
        return record

"""Explicit owner repair of a proven legacy empty recovery that lost reviewed source."""
from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.recovery import KINDS, RunRecoveryService, _ReadState, _related
from agentflow.control.remediation import _known_fingerprint, review_repair_manual_blockers
from agentflow.control.review_checkpoint import review_repair_source
from agentflow.control.service import ensure_revision
from agentflow.domain.planning import CODING_STEPS, descendants

_KINDS = tuple(dict.fromkeys((*KINDS, 'review_repair', 'run_recovery', 'failure_analysis', 'work_revision')))


class RestoreReviewBaselineRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    work_item_id: str = Field(min_length=1, max_length=200)
    review_repair_id: str = Field(min_length=1, max_length=200)
    empty_recovery_id: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=1)
    evidence_digest: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    reason: str = Field(min_length=1, max_length=2000)


class ReviewBaselineRecovery:
    def __init__(self, store, workflow):
        self.store, self.workflow = store, workflow
        self.recovery = RunRecoveryService(store, workflow)
        self.repository = self.recovery.repository

    async def _read(self, run_id):
        rows = await asyncio.gather(*(self.store.list(kind) for kind in _KINDS))
        return _related(dict(zip(_KINDS, rows, strict=True)), run_id)

    async def _proof(self, state, work_id, review_repair_id, empty_recovery_id):
        tx, run = _ReadState(state), state['run'][0]
        try:
            work = tx.get('work_item', work_id)
            repair = tx.get('review_repair', review_repair_id)
            empty = tx.get('run_recovery', empty_recovery_id)
            parent = tx.get('work_item', work['parent_stage_id'])
            alias = tx.get('code_snapshot', repair['checkpoint_alias_ids'][work_id])
            source = tx.get('code_snapshot', repair['base_snapshot_id'])
            review = tx.get('review', repair['review_attempt_id'])
            reviewed_attempt = tx.get('attempt', repair['review_attempt_id'])
            reviewed_work = [row['snapshot'] for row in state['work_revision']
                if row.get('work_item_id') == repair['review_work_item_id']
                and row.get('snapshot', {}).get('attempt_id') == reviewed_attempt['id']]
            if (len(reviewed_work) != 1 or reviewed_attempt.get('status') != 'completed'
                    or reviewed_attempt.get('run_id') != run['id']
                    or reviewed_attempt.get('work_item_id') != repair['review_work_item_id']
                    or reviewed_attempt.get('generation') != review['generation']
                    or reviewed_work[0].get('status') != 'completed'
                    or reviewed_work[0].get('quality_result') != 'failed'
                    or any(reviewed_work[0].get(field) != reviewed_attempt.get(field)
                           for field in ('generation', 'fencing_token', 'input_fingerprint'))):
                raise ValueError('original_review_identity_mismatch')
            analysis = tx.get('failure_analysis', empty['failure_analysis_id'])
            failed = tx.get('attempt', analysis['attempt_id'])
            current = tx.get('attempt', work['attempt_id'])
            wrong = tx.get('code_snapshot', work['attempt_id'])
            histories = [row for row in state['work_revision'] if row.get('work_item_id') == work_id
                and row.get('snapshot', {}).get('attempt_id') == failed['id']
                and row['snapshot'].get('generation') == failed['generation']]
            if len(histories) != 1:
                raise ValueError('ambiguous_original_work')
            old = histories[0]['snapshot']
            if (run.get('execution_state') != 'paused' or run.get('restore_reconciliation_required')
                    or work.get('archived') or work.get('status') != 'completed'
                    or work.get('kind') != 'stage_child' or work.get('step') not in CODING_STEPS
                    or work.get('run_id') != run['id'] or work.get('project_id') != run['project_id']
                    or parent.get('kind') != 'aggregation' or parent.get('status') != 'blocked'
                    or parent.get('runtime_failure_code') != 'assembly_base_mismatch'
                    or parent['id'] != repair['producer_work_item_id']
                    or parent.get('run_id') != run['id'] or parent.get('project_id') != run['project_id']
                    or set(parent['dependencies']) != set(parent['expanded_child_ids'])
                    or work_id not in parent['dependencies'] or work['dependencies'] != parent['original_dependencies']
                    or work['step'] not in tx.get('plan', run['plan_id']).get('authorized_rework_steps', [])
                    or repair.get('mode') != 'parallel_aggregate' or repair.get('run_id') != run['id']
                    or work_id not in repair['repair_work_item_ids']
                    or empty.get('actor') != 'system' or empty.get('mode') != 'retry'
                    or empty.get('run_id') != run['id'] or empty.get('work_item_id') != work_id
                    or work_id not in empty.get('affected_work_item_ids', [])
                    or parent['id'] not in empty.get('affected_work_item_ids', [])
                    or empty.get('checkpoint') != {'kind': 'upstream_checkpoint', 'items': []}
                    or analysis.get('repair_receipt_id') != empty['id'] or analysis.get('status') != 'repair_scheduled'
                    or analysis.get('repair_receipt_kind') != 'run_recovery'
                    or analysis.get('work_item_id') != work_id or analysis.get('run_id') != run['id']
                    or analysis.get('generation') != failed['generation'] or analysis.get('failure_code') != 'worker_timeout'
                    or failed.get('status') != 'failed' or failed.get('runtime_failure_code') != 'worker_timeout'
                    or failed.get('run_id') != run['id'] or failed.get('work_item_id') != work_id
                    or failed['generation'] != alias['generation'] + 1 or work['generation'] != failed['generation'] + 1
                    or any(old.get(field) != failed.get(field) for field in ('generation', 'fencing_token', 'input_fingerprint'))
                    or old.get('payload', {}).get('repair_base_snapshot_id') != alias['id']
                    or not old.get('payload', {}).get('change_expectation') or old.get('write_paths') != work['write_paths']
                    or current.get('status') != 'completed' or current.get('run_id') != run['id']
                    or current.get('work_item_id') != work_id
                    or any(current.get(field) != work.get(field) for field in ('generation', 'fencing_token', 'input_fingerprint'))
                    or wrong.get('stale') or wrong.get('run_id') != run['id'] or wrong.get('work_item_id') != work_id
                    or wrong.get('generation') != work['generation']
                    or alias.get('checkpoint_kind') != 'reviewed_aggregate_child_repair'
                    or alias.get('run_id') != run['id'] or alias.get('work_item_id') != work_id
                    or alias.get('child_scope') != work['write_paths'] or alias.get('source_snapshot_id') != source['id']
                    or alias.get('source_work_item_id') != parent['id']
                    or alias.get('source_review_id') != review['id'] or alias.get('source_review_attempt_id') != review['id']
                    or alias.get('source_review_work_item_id') != review['work_item_id']
                    or alias.get('source_review_generation') != review['generation']
                    or source.get('work_item_id') != parent['id'] or source.get('run_id') != run['id']
                    or source.get('generation') != alias.get('source_generation')
                    or any(alias.get(field) != source.get(field) for field in ('commit_oid', 'tree_oid', 'base_oid', 'repository_path'))
                    or source['commit_oid'] != repair['base_commit'] or review.get('reviewed_commit') != source['commit_oid']
                    or review.get('quality_result') != 'failed' or review.get('run_id') != run['id']
                    or review.get('work_item_id') != repair['review_work_item_id']):
                raise ValueError('baseline_replay_binding_invalid')
            findings = repair['findings_by_work_item'][work_id]
            if (not findings or any(finding not in review['blocking_findings'] for finding in findings)
                    or sorted({finding['path'] for finding in findings}) != alias['finding_paths']):
                raise ValueError('review_findings_mismatch')
            for identity in parent['dependencies']:
                sibling = tx.get('work_item', identity)
                if (not sibling or sibling.get('archived') or sibling.get('status') != 'completed'
                        or sibling.get('kind') != 'stage_child' or sibling.get('parent_stage_id') != parent['id']
                        or sibling.get('run_id') != run['id'] or sibling.get('project_id') != run['project_id']):
                    raise ValueError('coding_sibling_not_settled')
            review_repair_source(state, run, old, alias)
            proof = {'work': work, 'parent': parent, 'source': source, 'alias': alias, 'wrong': wrong,
                     'failed': failed, 'old': old, 'history_id': histories[0]['id']}
            proof['filesystem_digest'] = await asyncio.to_thread(self._filesystem_proof, state, proof)
            return proof
        except (AttributeError, KeyError, TypeError, ValueError, OSError) as error:
            raise DomainError('review_baseline_evidence_invalid', '原审查、空恢复、错误基线及 Git 证据无法完整绑定，未修改任何历史。') from error

    def _ancestor(self, path, ancestor, descendant):
        try:
            self.repository._run(path, ['merge-base', '--is-ancestor',
                                       self.repository._oid(ancestor), self.repository._oid(descendant)])
        except DomainError as error:
            if error.code == 'git_error' and (error.details or {}).get('exit_code') in {1, 128}:
                return False
            raise
        return True

    def _filesystem_proof(self, state, proof):
        """The same bounded, synchronous checks run before I/O and inside the writer."""
        tx, run = _ReadState(state), state['run'][0]
        source, wrong, alias = proof['source'], proof['wrong'], proof['alias']
        work, old, failed = proof['work'], proof['old'], proof['failed']
        try:
            heads = {}
            for snapshot in (source, wrong):
                path = Path(snapshot['repository_path'])
                if path.is_symlink() or path.resolve() != path:
                    raise ValueError('unsafe_snapshot_path')
                if self.repository._integrity(path, snapshot['commit_oid']) != snapshot['tree_oid']:
                    raise ValueError('snapshot_integrity_mismatch')
                if self.repository._collect_diff(path, snapshot['commit_oid'])['has_changes']:
                    raise ValueError('snapshot_workspace_changed')
                heads[str(path)] = self.repository._run(path, ['rev-parse', 'HEAD']).decode().strip()
                for ancestor in {snapshot['base_oid'], *snapshot.get('parent_commit_oids', [])}:
                    if not self._ancestor(path, ancestor, snapshot['commit_oid']):
                        raise ValueError('snapshot_ancestry_unproven')
            source_path = Path(source['repository_path'])
            for ancestor in alias.get('parent_commit_oids', []):
                if not self._ancestor(source_path, ancestor, source['commit_oid']):
                    raise ValueError('review_alias_ancestry_unproven')
            contribution = tx.get('code_snapshot', alias['source_child_snapshot_id'])
            if (self.repository._integrity(source_path, contribution['commit_oid']) != contribution['tree_oid']
                    or not self._ancestor(source_path, contribution['commit_oid'], source['commit_oid'])):
                raise ValueError('original_child_snapshot_unproven')
            old_path, old_commit = self.recovery._validate_workspace(
                {'work': old, 'context': tx.get('dispatch_context', failed['id']), 'project': state['project'][0]})
            if old_commit != source['commit_oid'] or self.repository._collect_diff(old_path, old_commit)['has_changes']:
                raise ValueError('old_failed_attempt_has_unpreserved_changes')
            heads[str(old_path)] = self.repository._run(old_path, ['rev-parse', 'HEAD']).decode().strip()
            current_path, current_base = self.recovery._validate_workspace(
                {'work': work, 'context': tx.get('dispatch_context', work['attempt_id']), 'project': state['project'][0]})
            if (current_path != Path(wrong['repository_path']) or current_base != wrong['base_oid']
                    or current_base == source['commit_oid'] or current_base != run['base_commit']
                    or self._ancestor(current_path, source['commit_oid'], wrong['commit_oid'])):
                raise ValueError('current_attempt_not_legacy_wrong_base')
            attempts = {row['id']: row for row in state['attempt']}
            for identity in (failed['id'], work['attempt_id']):
                self.recovery._verify_process(tx.get('supervised_attempt', identity), attempts)
            return canonical_digest({'heads': heads, 'old_path': str(old_path), 'old_commit': old_commit,
                                     'current_path': str(current_path), 'current_base': current_base})
        except (AttributeError, IndexError, KeyError, TypeError, ValueError, OSError) as error:
            raise DomainError('review_baseline_filesystem_changed',
                '审查来源、旧执行或当前工作区的代码、身份或停止证据已变化，未修改任务。') from error

    async def _inspect(self, run_id, work_id, review_repair_id, empty_recovery_id):
        state = await self._read(run_id)
        run = state['run'][0]
        blockers = self.recovery._common_blockers(state) + await self.recovery._process_blockers(state)
        if blockers:
            raise DomainError(blockers[0]['code'], blockers[0]['message'], details=blockers)
        proof = await self._proof(state, work_id, review_repair_id, empty_recovery_id)
        affected = descendants([row for row in state['work_item'] if not row.get('archived')], {work_id})
        target = {'work_item_id': work_id, 'root_work_item_ids': [work_id], 'affected_work_item_ids': sorted(affected)}
        blockers = await self.recovery._target_blockers(state, target)
        # This owner action is the selected child's explicit retry, not a decision for other tasks.
        others = {**target, 'work_item_id': '', 'affected_work_item_ids': sorted(affected - {work_id})}
        blockers += review_repair_manual_blockers(_ReadState(state), run, others)
        if any(row.get('status') == 'waiting_approval' for row in state['work_item'] if row['id'] in affected):
            blockers.append({'code': 'human_approval_pending', 'message': '受影响任务仍需人工审批。'})
        if any(not _known_fingerprint(run.get('input_fingerprint'))
               or not _known_fingerprint(row.get('run_input_fingerprint'))
               or row['run_input_fingerprint'] == run['input_fingerprint'] for row in state['candidate']) or state['delivery_intent']:
            blockers.append({'code': 'review_baseline_frozen', 'message': '当前版本已经冻结或进入交付，不能重放审查基线。'})
        if blockers:
            raise DomainError(blockers[0]['code'], blockers[0]['message'], details=blockers)
        view = {'run_id': run_id, 'work_item_id': work_id, 'review_repair_id': review_repair_id,
            'empty_recovery_id': empty_recovery_id, 'expected_revision': run['revision'],
            'evidence_digest': canonical_digest({'state': state, 'filesystem': proof['filesystem_digest']}),
            'source_commit': proof['source']['commit_oid'],
            'replaced_commit': proof['wrong']['commit_oid'], 'affected_work_item_ids': sorted(affected),
            'preserved_work_item_ids': sorted(row['id'] for row in state['work_item'] if not row.get('archived') and row['id'] not in affected)}
        return state, proof, view

    async def preview(self, run_id, work_id, review_repair_id, empty_recovery_id):
        return (await self._inspect(run_id, work_id, review_repair_id, empty_recovery_id))[2]

    async def restore(self, run_id, payload, key):
        request = RestoreReviewBaselineRequest.model_validate(payload)
        identity = str(uuid5(NAMESPACE_URL, f'review-baseline-recovery:{run_id}:{key}'))
        command = {'run_id': run_id, **request.model_dump()}
        try:
            if await self.store.read('run_recovery', identity):
                return await self.store.command('run.restore_review_baseline', key, command, lambda tx: {})
            state, proof, view = await self._inspect(run_id, request.work_item_id, request.review_repair_id, request.empty_recovery_id)
            ensure_revision(state['run'][0], request.expected_revision)
            if request.evidence_digest != view['evidence_digest']:
                raise DomainError('revision_conflict', '恢复证据已变化，请重新预览。')
            checkpoint_id = str(uuid5(NAMESPACE_URL, identity + ':checkpoint'))
            def apply(tx):
                if canonical_digest(_related({kind: tx.list(kind) for kind in _KINDS}, run_id)) != canonical_digest(state):
                    raise DomainError('revision_conflict', '恢复核验期间状态变化，请重新预览。')
                if self._filesystem_proof(state, proof) != proof['filesystem_digest']:
                    raise DomainError('review_baseline_filesystem_changed', '提交前代码或工作区身份已变化，请重新预览。')
                run, work, failed, source = state['run'][0], proof['work'], proof['failed'], proof['source']
                self.workflow._invalidate(tx, [row for row in state['work_item'] if not row.get('archived')], {work['id']},
                    request.reason, expand_roots=False, preserve_stage_ids=frozenset({proof['parent']['id']}))
                checkpoint = tx.put('code_snapshot', checkpoint_id, {'run_id': run_id, 'work_item_id': work['id'],
                    'generation': work['generation'], 'repository_path': source['repository_path'],
                    'commit_oid': source['commit_oid'], 'tree_oid': source['tree_oid'], 'base_oid': source['commit_oid'],
                    'parent_commit_oids': proof['alias'].get('parent_commit_oids', []),
                    'purpose': 'recovery_checkpoint', 'recovery_id': identity, 'stale': True,
                    'source_attempt_id': failed['id'], 'source_generation': failed['generation'],
                    'source_fencing_token': failed['fencing_token'], 'source_input_fingerprint': failed['input_fingerprint'],
                    'source_commit': source['commit_oid'], 'source_write_paths': list(work['write_paths']),
                    'source_repair_snapshot_id': proof['alias']['id'], 'quality_result': 'unknown'})
                updated = tx.get('work_item', work['id'])
                tx.put('work_item', work['id'], {**updated, 'payload': {**updated.get('payload', {}),
                    'repair_base_snapshot_id': checkpoint_id, 'recovery_checkpoint_id': checkpoint_id,
                    'change_expectation': proof['old']['payload']['change_expectation'], 'recovery_instruction': request.reason}}, updated['revision'])
                run = tx.put('run', run_id, {**run, 'quality_result': 'unknown', 'blocking_reasons': [],
                    'input_fingerprint': canonical_digest({'prior': run['input_fingerprint'], 'review_baseline_recovery': identity})}, run['revision'])
                result = tx.put('run_recovery', identity, {**view, 'actor': 'owner', 'mode': 'retry',
                    'recovery_kind': 'review_baseline_replay', 'iteration_id': run['iteration_id'], 'run': run,
                    'checkpoint': {'kind': 'verified_review_baseline', 'snapshot_id': checkpoint_id, 'commit_oid': checkpoint['commit_oid']},
                    'original_work_revision_id': proof['history_id'], 'source_review_alias_id': proof['alias']['id'],
                    'replaced_snapshot_id': proof['wrong']['id'], 'reason': request.reason, 'created_at': utc_now(),
                    'session_resume': 'unsupported_ephemeral', 'execution': 'paused_pending_owner_resume'})
                tx.event('review.baseline_restored', {'recovery_id': identity, **view}, run_id=run_id)
                return result
            return await self.store.command('run.restore_review_baseline', key, command, apply)
        except DomainError:
            if await self.store.read('run_recovery', identity):
                return await self.store.command('run.restore_review_baseline', key, command, lambda tx: {})
            raise

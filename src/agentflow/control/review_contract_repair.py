"""Review-owned triage, bounded repairs, diagnostic verification and full re-review."""
from __future__ import annotations

import asyncio
import json
import tarfile
import tempfile
from copy import deepcopy
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.coding_steps import CodingSteps
from agentflow.control.review_contract_binding import REVIEW_BINDING_KINDS, repair_batch
from agentflow.control.review_diagnostics import ReviewDiagnostics
from agentflow.control.review_disposition import ReviewDisposition, migration_edits
from agentflow.control.starter_execution import STARTER_SUPPORT_FILES
from agentflow.control.test_coverage_guard import TestCoverageGuard
from agentflow.control.test_migration_guard import TestMigrationGuard
from agentflow.domain.planning import ROLES
from agentflow.repository import RepositoryAdapter

REVIEW_CONTRACT_PROTOCOL_VERSION = 2
_GUARDED_KINDS = {'test_contract_migration': ('test_migration_check', 'test_migration_invalid'),
                  'test_coverage_extension': ('test_coverage_check', 'test_coverage_invalid')}


def identity(*parts):
    return str(uuid5(NAMESPACE_URL, canonical_digest(parts)))


def new_work(run, key, step, dependencies, *, paths=(), payload=None, kind='stage', approval=False):
    return {'run_id': run['id'], 'project_id': run['project_id'], 'key': key, 'step': step, 'role': ROLES[step],
        'dependencies': list(dependencies), 'write_paths': list(paths), 'payload': payload or {}, 'kind': kind,
        'generation': 1, 'fencing_token': 0, 'status': 'pending', 'quality_result': 'unknown',
        'attempt_id': None, 'artifact_ids': [], 'required': True, 'approval_required': approval,
        'input_fingerprint': run['input_fingerprint'], 'policy_fingerprint': run.get('policy_fingerprint', run['input_fingerprint'])}


class ReviewContractRepair:
    def __init__(self, store, workflow, nodes=None):
        self.store, self.workflow, self.nodes = store, workflow, nodes
        self.repository = RepositoryAdapter()
        self.disposition = ReviewDisposition(store, workflow.artifacts, workflow.settings)
        self.guard = TestMigrationGuard()
        self.coverage_guard = TestCoverageGuard()
        self.diagnostics = ReviewDiagnostics(store, workflow, nodes)
        self._locks = {}

    async def prepare_disposition(self, work, result):
        batch = await self.context_for(work)
        if not batch:
            raise DomainError('review_contract_binding_invalid', '归属分析不属于受管批次。')
        prepared = await self.store.read('review_disposition_check', work.get('attempt_id'))
        if (prepared and prepared.get('work_item_id') == work['id']
                and prepared.get('generation') == work['generation']
                and prepared.get('context_digest') == batch['context_digest']
                and prepared.get('result_digest') == canonical_digest(result)):
            return
        checked = self.disposition.validate(result, batch['context'])
        if any(issue.get('code') == 'invalid_result' for issue in checked['issues']):
            raise DomainError('review_disposition_invalid', '归属分析含有未授权的修复建议。', details=checked['issues'])
        # Report semantic and literal/adapter errors together, even when the
        # shape-valid disposition has missing citations or mismatched old text.
        migrations = [m for action in result['actions'] for m in action['migrations']]
        try:
            guard = await asyncio.to_thread(self.guard.validate_plan, migrations)
        except (ValueError, OSError) as error:
            raise DomainError('review_disposition_unavailable', '测试迁移保护环境不可用，未批准迁移。',
                details=[{'code': 'migration_guard_unavailable', 'message': str(error), 'error_type': type(error).__name__}]) from error
        issues = [*checked['issues'], *guard['errors']]
        if issues:
            raise DomainError('review_disposition_invalid', '归属分析未通过校验，请一次修正所列问题；未授权或不支持的迁移须回到生产修复或说明依据。', details=issues)
        record = {'run_id': work['run_id'], 'batch_id': batch['id'], 'context_digest': batch['context_digest'], 'work_item_id': work['id'],
                  'attempt_id': work['attempt_id'], 'generation': work['generation'], 'result_digest': canonical_digest(result)}
        def commit(tx):
            current = tx.get('work_item', work['id'])
            fresh = repair_batch(tx, current)
            if fresh['context_digest'] != batch['context_digest'] or current['attempt_id'] != work['attempt_id']:
                raise DomainError('review_contract_stale', '分析核验期间任务已变化。')
            self._verify_context(tx, fresh)
            return tx.put('review_disposition_check', work['attempt_id'], record)
        await self.store.command('review.disposition.prepare', work['attempt_id'], record, commit)

    async def _prepare_diagnostic_suite(self, run, source):
        try:
            return await self.diagnostics.prepare_builtin_suite(run, source)
        except (ValueError, OSError) as error:
            raise DomainError('review_disposition_unavailable', '既有测试诊断上下文不可用，未安排返工。',
                details=[{'code': 'diagnostic_preparation_failed', 'message': str(error), 'error_type': type(error).__name__}]) from error

    async def _context(self, run, reviewer):
        built = await self.disposition.build(run, reviewer)
        if not built['ok']:
            raise DomainError('review_disposition_unavailable', '无法核验审查归属。', details=built['issues'])
        context = built['context']
        canonical_steps = set()
        for document in context['accepted_documents']:
            artifact = await self.store.read('artifact', document['artifact_id'])
            if not artifact.get('readable'):
                canonical_steps.add(document['step'])
        if canonical_steps:
            selected = []
            for document in context['accepted_documents']:
                artifact = await self.store.read('artifact', document['artifact_id'])
                if document['step'] not in canonical_steps or not artifact.get('readable'):
                    selected.append(document)
            selected_ids = {row['artifact_id'] for row in selected}
            context['accepted_documents'] = selected
            context['accepted_requirements'] = [row for row in context['accepted_requirements'] if row['artifact_id'] in selected_ids]
        source = context['snapshot']
        prepared = await self._prepare_diagnostic_suite(run, source)
        assertions = []
        for manifest in prepared['manifests']:
            for case in manifest['cases']:
                assertions.extend({**assertion, 'path': manifest['file'], 'case_id': case['case_id']}
                                  for assertion in case['assertions'])
        context.update(assertions=assertions, assertion_manifest_required=False, diagnostic_suite=prepared)
        names = (await asyncio.to_thread(self.repository._run, Path(source['repository_path']),
            ['ls-tree', '-r', '--name-only', source['commit_oid']])).decode().splitlines()
        context['test_paths'] = [name for name in names if name.startswith(('tests/', '__tests__/'))
                                 or '.spec.' in name or '.test.' in name]
        plan = await self.store.read('plan', run['plan_id'])
        context['plan_version'] = {'id': plan['id'], 'revision': plan['revision']}
        acceptance = []
        for document in context['accepted_documents']:
            artifact = await self.store.read('artifact', document['artifact_id'])
            author = await self.store.read('work_item', artifact['work_item_id'])
            approvals = [row for row in await self.store.list('approval') if row.get('work_item_id') == author['id']]
            acceptance.append({'author': {k: author.get(k) for k in ('id', 'status', 'generation', 'artifact_ids',
                'approval_required', 'approved_fingerprint', 'output_fingerprint')}, 'approvals': approvals})
        context['acceptance_bindings'] = acceptance
        for owner in context['owners']:
            current = await self.store.read('work_item', owner['work_item_id'])
            owner['work_version'] = {k: current.get(k) for k in ('id', 'revision', 'generation', 'role', 'step', 'status', 'run_id', 'project_id', 'approval_required')}
        # Sectioned architecture prose remains authoritative even when it has no FR identifier.
        for document in context['accepted_documents']:
            context['accepted_requirements'].append({**document,
                'requirement_id': 'document:' + document['step'], 'text': document['text']})
        context['protected_paths'] = list(STARTER_SUPPORT_FILES)
        context['protocol_version'] = REVIEW_CONTRACT_PROTOCOL_VERSION
        return context

    @staticmethod
    def _verify_context(tx, batch):
        context = batch['context']
        if context.get('diagnostic') and tx.get('review_diagnostic', context['diagnostic']['id']) != context['diagnostic']:
            raise DomainError('review_contract_stale', '诊断证据版本已变化。')
        plan_version = context.get('plan_version')
        if plan_version and (tx.get('plan', plan_version['id']) or {}).get('revision') != plan_version['revision']:
            raise DomainError('review_contract_stale', '已接受计划版本已变化。')
        for binding in context.get('acceptance_bindings', []):
            author = tx.get('work_item', binding['author']['id'])
            if not author or any(author.get(k) != v for k, v in binding['author'].items()):
                raise DomainError('review_contract_stale', '需求作者或验收状态已变化。')
            for approval in binding['approvals']:
                if tx.get('approval', approval['id']) != approval:
                    raise DomainError('review_contract_stale', '需求审批状态已变化。')
        for review in context['reviews']:
            current = tx.get('review', review['id'])
            if not current or current != review:
                raise DomainError('review_contract_stale', '审查意见版本已变化。')
        snapshot = tx.get('code_snapshot', context['source_snapshot_id'])
        if snapshot != context['snapshot']:
            raise DomainError('review_contract_stale', '审查源码版本已变化。')
        for ref in context['accepted_documents']:
            artifact = tx.get('artifact', ref['artifact_id'])
            if (not artifact or artifact.get('stale') or artifact.get('digest') != ref['digest']
                    or artifact.get('revision') != ref['revision']):
                raise DomainError('review_contract_stale', '已接受需求版本已变化。')

    def _stopped_upgrade_state(self, tx, batch):
        """Bind every old triage execution before replacing its read-only evidence."""
        from agentflow.control.recovery import STOPPED, TERMINAL
        from agentflow.models.uncertainty import (
            acknowledged_invocation_ids,
            acknowledgment_state,
            attempt_uncertainty_blocks,
            invocation_blocks,
        )
        triage = tx.get('work_item', batch['triage_work_item_id'])
        if (batch.get('state') not in {'triaging', 'needs_attention'} or not triage
                or triage.get('status') not in (STOPPED - {'completed'}) | {'pending', 'queued'}
                or batch.get('assembly_work_item_id') or batch.get('validation_work_item_id')
                or set(batch.get('repair_work_item_ids', [])) - {triage['id']}
                or set(batch.get('work_specs', {})) - {triage['id']}
                or any(row.get('batch_id') == batch['id'] or row['id'] == batch['id'] for row in tx.list('review_repair'))
                or any(row['id'] != triage['id'] and row.get('payload', {}).get('review_contract_task') == batch['id']
                       for row in tx.list('work_item'))):
            raise DomainError('review_contract_upgrade_unavailable', '仅允许升级尚未派发修复且分析已停止的批次。')
        repair_batch(tx, triage)
        self._verify_context(tx, batch)
        attempts = [row for row in tx.list('attempt') if row.get('work_item_id') == triage['id']
                    or row['id'] == triage.get('attempt_id')]
        current = next((row for row in attempts if row['id'] == triage.get('attempt_id')), None)
        if (any(row.get('status') not in STOPPED or row.get('run_id') != batch['run_id']
                or row.get('work_item_id') != triage['id'] for row in attempts)
                or triage.get('attempt_id') and (not current or any(current.get(k) != triage.get(k)
                    for k in ('generation', 'fencing_token', 'input_fingerprint')))):
            raise DomainError('review_contract_upgrade_unavailable', '原分析执行尚未确认停止或身份不匹配。')
        identities = {row['id'] for row in attempts}
        process = {'attempt': attempts, 'run': [tx.get('run', batch['run_id'])], 'work_item': [triage]}
        for kind in ('supervised_attempt', 'dispatch_context', 'prelaunch_failure', 'model_attempt_budget', 'model_invocation'):
            process[kind] = [row for row in tx.list(kind) if row['id'] in identities
                            or row.get('attempt_id') in identities or row.get('work_item_id') == triage['id']]
        if any(row.get('state') not in TERMINAL for row in process['supervised_attempt']):
            raise DomainError('review_contract_upgrade_unavailable', '分析进程仍在执行或状态未知。')
        # A missing dispatch/supervisor row does not prove that a separately
        # recorded provider request ended. Preserve exact owner/timeout
        # acknowledgments and recheck their ledger/stop basis inside the writer.
        acknowledged = acknowledged_invocation_ids(acknowledgment_state(tx))
        if (any(invocation_blocks(row, acknowledged) for row in process['model_invocation'])
                or any(attempt_uncertainty_blocks(row, process['model_invocation'], acknowledged)
                       for row in process['model_attempt_budget'])):
            raise DomainError('review_contract_upgrade_unavailable', '原分析仍有预留、发送中或尚未核验的未知模型调用。')
        return process

    async def upgrade_triage_context(self, batch_id):
        """Archive a stopped, unexecuted legacy context; never retry or dispatch work."""
        from agentflow.control.recovery import RunRecoveryService, _ReadState
        from agentflow.models.uncertainty import ACK_KINDS
        kinds = tuple(dict.fromkeys((*REVIEW_BINDING_KINDS, *ACK_KINDS, 'review', 'review_repair',
            'supervised_attempt', 'prelaunch_failure', 'model_attempt_budget', 'model_invocation')))
        rows = await asyncio.gather(*(self.store.list(kind) for kind in kinds))
        state = _ReadState(dict(zip(kinds, rows, strict=True)))
        batch = state.get('review_contract_repair', batch_id)
        if not batch:
            raise DomainError('review_contract_binding_invalid', '返工批次不存在。')
        if batch['context'].get('protocol_version', 1) >= REVIEW_CONTRACT_PROTOCOL_VERSION:
            return batch
        process = self._stopped_upgrade_state(state, batch)
        recovery = RunRecoveryService(self.store, self.workflow)
        blockers = await recovery._process_blockers(process)
        if blockers:
            raise DomainError('review_contract_upgrade_unavailable', '原分析进程停止证据未通过核验。', details=blockers)
        run = process['run'][0]
        reviewer = state.get('work_item', batch['reviewer_id'])
        rebuilt = await self._context(run, reviewer)
        previous = batch['context']
        for key in ('run_id', 'source_snapshot_id', 'source_commit', 'snapshot', 'reviews', 'findings'):
            if rebuilt.get(key) != previous.get(key):
                raise DomainError('review_contract_stale', '升级不能替换原源码或审查意见。')
        old_document_ids = {row['artifact_id'] for row in previous.get('accepted_documents', [])}
        if any(row['artifact_id'] not in old_document_ids
                and row.get('step') not in {'unit_test_plan', 'integration_test_strategy'}
                for row in rebuilt.get('accepted_documents', [])):
            raise DomainError('review_contract_stale', '上下文升级只能补充已接受测试方案，不能增加新的产品契约。')
        # Every previously accepted identity and text remains authoritative.
        for key, identity_keys in (('accepted_documents', ('artifact_id',)),
                ('accepted_requirements', ('artifact_id', 'requirement_id')),
                ('owners', ('work_item_id',))):
            by_id = {tuple(row[k] for k in identity_keys): row for row in rebuilt.get(key, [])}
            for old in previous.get(key, []):
                fresh = by_id.get(tuple(old[k] for k in identity_keys))
                if not fresh or any(fresh.get(k) != value for k, value in old.items()):
                    raise DomainError('review_contract_stale', '升级不能替换原已接受文档或作者授权。')
        context = deepcopy(previous)
        for key in ('accepted_documents', 'accepted_requirements', 'acceptance_bindings'):
            context[key] = rebuilt.get(key, previous.get(key, []))
        context['protocol_version'] = REVIEW_CONTRACT_PROTOCOL_VERSION
        context_digest = canonical_digest(context)
        history_id = identity(batch['id'], batch['context_digest'], 'context-history')
        def upgrade(tx):
            current = tx.get('review_contract_repair', batch['id'])
            if current != batch or self._stopped_upgrade_state(tx, current) != process:
                raise DomainError('review_contract_stale', '上下文升级期间分析执行或证据已变化。')
            if tx.get('work_item', batch['reviewer_id']) != reviewer:
                raise DomainError('review_contract_stale', '上下文升级期间原审查任务已变化。')
            updated = {**current, 'context': context, 'context_digest': context_digest, 'state': 'triaging',
                'context_history_ids': [*current.get('context_history_ids', []), history_id]}
            self._verify_context(tx, updated)
            tx.put('review_contract_context_history', history_id, {'actor': 'controller', 'run_id': batch['run_id'],
                'batch_id': batch['id'], 'triage_work_item_id': batch['triage_work_item_id'],
                'context': previous, 'context_digest': batch['context_digest'], 'batch_revision': batch['revision'],
                'batch_state': batch['state'],
                'upgraded_context_digest': context_digest, 'created_at': utc_now()})
            result = tx.put('review_contract_repair', batch['id'], updated, current['revision'])
            tx.event('review.contract_context_upgraded', {'batch_id': batch['id'], 'history_id': history_id,
                'context_digest': context_digest}, run_id=batch['run_id'])
            return result
        return await self.store.command('review.contract.context_upgrade', history_id,
            {'previous': batch['context_digest'], 'context': context_digest}, upgrade)

    async def ensure_triage(self, reviewer_id, *, analysis_id=None):
        reviewer = await self.store.read('work_item', reviewer_id)
        if not reviewer or reviewer.get('status') != 'completed' or reviewer.get('quality_result') != 'failed':
            return None
        run = await self.store.read('run', reviewer['run_id'])
        if run['execution_state'] != 'running':
            return None
        stage_id = reviewer.get('parent_stage_id') or reviewer_id
        async with self._locks.setdefault(stage_id, asyncio.Lock()):
            existing = [row for row in await self.store.list('review_contract_repair')
                        if row.get('run_id') == run['id'] and row.get('stage_id') == stage_id
                        and row.get('state') in {'triaging', 'awaiting_approval', 'repairing', 'needs_attention', 'diagnostic_failed'}]
            if existing:
                return existing[-1]
            from agentflow.control.recovery import RunRecoveryService
            from agentflow.control.remediation import review_repair_allowed
            recovery = RunRecoveryService(self.store, self.workflow)
            state = await recovery._read(run['id'])
            blockers = recovery._common_blockers(state) + await recovery._process_blockers(state)
            if blockers:
                raise DomainError('review_disposition_unavailable', '返工前必须先核验执行状态和原授权。', details=blockers)
            prior = [row for row in await self.store.list('review_contract_repair')
                     if row.get('run_id') == run['id'] and row.get('stage_id') == stage_id]
            if not review_repair_allowed(self.workflow.settings.auto_review_repair_limit, len(prior)):
                raise DomainError('review_repair_limit_reached', '本轮审查返工已达到设置中的次数上限。')
            context = await self._context(run, reviewer)
            stage = await self.store.read('work_item', stage_id)
            child_ids = list(stage.get('expanded_child_ids', []))
            cohort = [await self.store.read('work_item', i) for i in child_ids] if child_ids else [stage]
            if any(row.get('status') != 'completed' or row.get('quality_result') not in {'passed', 'failed'} for row in cohort):
                raise DomainError('review_peers_pending', '仍有同批审查未结束，等待完整意见后再安排返工。')
            batch_id = identity('review-contract', run['id'], stage_id, context['source_commit'],
                                [row['id'] for row in context['reviews']])
            triage_id = identity(batch_id, 'triage')
            row = {'actor': 'controller', 'run_id': run['id'], 'stage_id': stage_id,
                'reviewer_id': reviewer_id, 'source_snapshot_id': context['source_snapshot_id'],
                'source_commit': context['source_commit'], 'context': context,
                'context_digest': canonical_digest(context), 'state': 'triaging', 'triage_work_item_id': triage_id,
                'review_work_ids': [stage_id, *child_ids], 'cohort': cohort, 'original_stage': stage,
                'work_specs': {}, 'created_at': utc_now(), 'repair_work_item_ids': [triage_id]}
            spec = new_work(run, 'review-disposition-' + batch_id, 'review_disposition',
                [context['snapshot']['work_item_id']], approval=stage.get('approval_required', False),
                payload={'review_contract_task': batch_id, 'review_contract_kind': 'triage'})
            row['work_specs'][triage_id] = spec
            def create(tx):
                old = tx.get('review_contract_repair', batch_id)
                if old:
                    return old
                current_run = tx.get('run', run['id'])
                if current_run['input_fingerprint'] != run['input_fingerprint'] or current_run['execution_state'] != 'running':
                    raise DomainError('review_contract_stale', '运行版本已变化。')
                self._verify_context(tx, row)
                if any(tx.get('work_item', w['id']) != w for w in cohort):
                    raise DomainError('review_contract_stale', '并行审查版本已变化。')
                if analysis_id:
                    from agentflow.control.failure_remediation import guard_automatic
                    guard_automatic(tx, self.workflow, analysis_id, tx.get('work_item', reviewer_id), 'repair_review_findings')
                for previous in prior:
                    if previous.get('state') == 'reviewing':
                        fresh = tx.get('review_contract_repair', previous['id'])
                        tx.put('review_contract_repair', fresh['id'], {**fresh, 'state': 'superseded',
                            'next_batch_id': batch_id}, fresh['revision'])
                tx.put('work_item', triage_id, spec)
                result = tx.put('review_contract_repair', batch_id, row)
                tx.event('review.triage_scheduled', {'batch_id': batch_id, 'work_item_id': triage_id}, run_id=run['id'])
                if analysis_id:
                    from agentflow.control.failure_remediation import finish_automatic
                    finish_automatic(tx, analysis_id, result, kind='review_contract_repair')
                return result
            return await self.store.command('review.contract.triage', batch_id, {'context': row['context_digest']}, create)

    def apply_disposition(self, tx, task, result):
        work = tx.get('work_item', task['work_item_id'])
        batch = repair_batch(tx, work)
        if not batch or batch['state'] not in {'triaging', 'awaiting_approval'} or batch['triage_work_item_id'] != work['id']:
            raise DomainError('review_contract_stale', '归属分析已被替换。')
        self._verify_context(tx, batch)
        prepared = tx.get('review_disposition_check', work['attempt_id'])
        if (not prepared or prepared.get('generation') != work['generation']
                or prepared.get('context_digest') != batch['context_digest']
                or prepared.get('result_digest') != canonical_digest(result)):
            raise DomainError('review_disposition_invalid', '归属分析缺少对应版本的迁移安全校验。')
        validation = self.disposition.validate(result, batch['context'])
        if not validation['ok']:
            raise DomainError('review_disposition_invalid', '审查归属分析未通过校验。', details=validation['issues'])
        actions = validation['actions']
        if work.get('approval_required') and work['status'] != 'completed':
            tx.put('review_contract_repair', batch['id'], {**batch, 'state': 'awaiting_approval',
                'pending_disposition': result, 'pending_disposition_digest': canonical_digest(result)}, batch['revision'])
            return
        if any(action['classification'] == 'needs_clarification' for action in actions):
            tx.put('review_contract_repair', batch['id'], {**batch, 'state': 'needs_attention', 'actions': actions}, batch['revision'])
            return
        run = tx.get('run', batch['run_id'])
        if run['execution_state'] not in {'running', 'paused'}:
            raise DomainError('review_contract_stale', '运行不可创建返工。')
        stage = tx.get('work_item', batch['stage_id'])
        if stage != batch['original_stage']:
            raise DomainError('review_contract_stale', '原审查阶段已变化。')
        for action in actions:
            if any(path in STARTER_SUPPORT_FILES or path.startswith(('tooling/', 'tests/support/')) for path in action['repair_paths']):
                raise DomainError('review_disposition_invalid', '契约迁移不能修改执行支撑文件。')
        from agentflow.domain.expansion import _validate_graph
        specs = dict(batch['work_specs'])
        grouped = {}
        for action in actions:
            grouped.setdefault((action['classification'], action['owner_work_item_id']), []).append(action)
        repair_ids = []
        previous_owner_action = {}
        for (kind, owner_id), assigned in sorted(grouped.items()):
            owner = tx.get('work_item', owner_id)
            frozen = next(o for o in batch['context']['owners'] if o['work_item_id'] == owner_id)
            if (owner['write_paths'] != frozen['write_paths'] or any(owner.get(k) != v
                    for k, v in frozen.get('work_version', {}).items())):
                raise DomainError('review_contract_stale', '原作者权限已变化。')
            action_id = identity(batch['id'], kind, owner_id)
            step = (owner['step'] if kind == 'test_coverage_extension' else
                    'implementation' if kind == 'production_fix' else 'review_unit_migration'
                    if owner['step'] == 'unit_test_implementation' else 'review_integration_migration')
            paths = sorted({p for p in assigned_action_paths(assigned)})
            base_id = identity(action_id, 'base')
            source = batch['context']['snapshot']
            tx.put('code_snapshot', base_id, {**{k: source[k] for k in ('repository_path', 'commit_oid', 'tree_oid')},
                'base_oid': source['commit_oid'], 'run_id': run['id'], 'work_item_id': action_id,
                'generation': 0, 'stale': False, 'checkpoint_kind': 'review_contract_repair_base',
                'source_snapshot_id': source['id'], 'review_contract_task': batch['id']})
            dependencies = [work['id'], *([previous_owner_action[owner_id]] if owner_id in previous_owner_action else [])]
            spec = new_work(run, 'review-contract-action-' + action_id, step, dependencies, paths=paths,
                approval=owner.get('approval_required', False),
                payload={'review_contract_task': batch['id'], 'review_contract_kind': kind,
                    'review_contract_owner': owner_id, 'review_contract_actions': assigned,
                    'repair_base_snapshot_id': base_id})
            specs[action_id] = spec
            tx.put('work_item', action_id, spec)
            self._allocate_budget(tx, run, owner, action_id, source['commit_oid'], batch['id'])
            repair_ids.append(action_id)
            previous_owner_action[owner_id] = action_id
        assembly_id, validation_id = identity(batch['id'], 'assembly'), identity(batch['id'], 'validation')
        specs[assembly_id] = new_work(run, 'review-contract-assembly-' + batch['id'], 'implementation', repair_ids,
            kind='aggregation', payload={'review_contract_task': batch['id'], 'review_contract_kind': 'assembly'})
        specs[validation_id] = new_work(run, 'review-contract-validation-' + batch['id'], 'review_validation', [assembly_id],
            payload={'review_contract_task': batch['id'], 'review_contract_kind': 'validation'})
        tx.put('work_item', assembly_id, specs[assembly_id])
        tx.put('work_item', validation_id, specs[validation_id])
        all_items = [row for row in tx.list('work_item') if row['run_id'] == run['id'] and not row.get('archived')]
        roots = set(batch['review_work_ids'])
        self.workflow._invalidate(tx, all_items, roots,
            'Re-review controlled contract migrations and production fixes against one verified new snapshot.', expand_roots=False)
        minimum = {}
        for work_id in batch['review_work_ids']:
            current = tx.get('work_item', work_id)
            minimum[work_id] = current['generation']
            field = 'original_dependencies' if current.get('kind') == 'aggregation' else 'dependencies'
            tx.put('work_item', work_id, {**current, field: [validation_id],
                'payload': {**current.get('payload', {}), 'review_contract_binding': batch['id']}}, current['revision'])
        binding = {'stage_id': stage['id'], 'review_ids': batch['review_work_ids'], 'child_ids': stage.get('expanded_child_ids', []),
            'minimum_generations': minimum, 'expansion_fingerprint': stage.get('expansion_fingerprint'),
            'expansion_id': next((row['id'] for row in tx.list('stage_expansion') if row.get('stage_work_item_id') == stage['id']
                                 and row.get('input_fingerprint') == stage.get('expansion_fingerprint')), None)}
        _validate_graph([row for row in tx.list('work_item') if row['run_id'] == run['id'] and not row.get('archived')], 4096)
        tx.put('review_contract_repair', batch['id'], {**batch, 'state': 'repairing', 'actions': actions,
            'work_specs': specs, 'repair_work_item_ids': repair_ids, 'assembly_work_item_id': assembly_id,
            'validation_work_item_id': validation_id, 'review_binding': binding}, batch['revision'])
        tx.put('review_repair', batch['id'], {'run_id': run['id'], 'mode': 'contract_migration',
            'review_work_item_id': batch['reviewer_id'], 'review_attempt_id': batch['context']['reviews'][0]['id'],
            'batch_id': batch['id'], 'repair_work_item_ids': repair_ids, 'base_commit': batch['source_commit'], 'created_at': utc_now()})
        tx.put('run', run['id'], {**run, 'blocking_reasons': [],
            'input_fingerprint': canonical_digest({'prior': run['input_fingerprint'], 'contract_batch': batch['id']})}, run['revision'])
        tx.event('review.contract_repairs_scheduled', {'batch_id': batch['id'], 'repair_work_item_ids': repair_ids}, run_id=run['id'])

    def _allocate_budget(self, tx, run, owner, action_id, source_commit, batch_id):
        pool_id = CodingSteps.budget_id(run['id'], owner['id'])
        pool = tx.get('coding_work_budget', pool_id)
        if pool is None:
            pool = tx.put('coding_work_budget', pool_id, {'run_id': run['id'], 'work_item_id': owner['id'],
                'base_commit': source_commit, 'max_steps': self.workflow.settings.max_coding_steps,
                'max_active_seconds': run['budget_limit']['max_active_seconds'], 'max_tool_calls': run['budget_limit']['max_tool_calls'],
                'active_seconds': 0.0, 'observed_tool_calls': 0, 'step_count': 0, 'uncertain': False})
        remaining = {k: pool[k] - pool[used] for k, used in (
            ('max_steps', 'step_count'), ('max_active_seconds', 'active_seconds'), ('max_tool_calls', 'observed_tool_calls'))}
        if pool['uncertain'] or any(value <= 0 for value in remaining.values()):
            raise DomainError('coding_budget_exhausted', '原作者的累计执行额度不足或尚未核验。')
        tx.put('coding_work_budget', CodingSteps.budget_id(run['id'], action_id), {'run_id': run['id'],
            'work_item_id': action_id, 'base_commit': source_commit, **remaining, 'active_seconds': 0.0,
            'observed_tool_calls': 0, 'step_count': 0, 'uncertain': False,
            'review_contract_owner_budget': pool_id, 'review_contract_batch': batch_id})

    async def context_for(self, work):
        batch = await self.store.read('review_contract_repair', work.get('payload', {}).get('review_contract_task'))
        return batch

    async def retry_diagnostic_findings(self, batch, run):
        """A verified failing suite creates new analysis, never a synthetic passed review."""
        from agentflow.control.remediation import review_repair_allowed
        diagnostic = await self.store.read('review_diagnostic', batch.get('failed_diagnostic_id'))
        if not diagnostic or diagnostic.get('state') != 'failed' or diagnostic.get('run_id') != run['id']:
            raise DomainError('review_contract_stale', '缺少真实诊断失败证据。')
        findings = []
        by_case = {name: case for case in diagnostic.get('affected_cases', []) for name in case['framework_case_ids']}
        for report in diagnostic.get('reports', []):
            receipt = await self.store.read('node_result', report.get('node_result_id'))
            if not receipt or receipt.get('job_id') != report.get('job_id') or receipt.get('assessment_state') != 'validated':
                raise DomainError('review_contract_stale', '诊断节点回执尚未核验。')
            for case in report['normalized_report']['cases']:
                if case['status'] == 'passed' or case['case_id'] not in by_case:
                    continue
                findings.append({'finding_id': identity(diagnostic['id'], report['job_id'], case['case_id'], case.get('attempt', 0)),
                    'diagnostic_id': diagnostic['id'], 'path': by_case[case['case_id']]['path'],
                    'message': case.get('message') or '既有测试未通过：' + case['case_id'], 'case_id': case['case_id']})
        if not findings:
            raise DomainError('review_diagnostic_failed', '诊断尚无可归属的测试失败，请核对构建或节点执行条件。', details=diagnostic.get('blockers'))
        prior = [row for row in await self.store.list('review_contract_repair') if row.get('run_id') == run['id']
                 and row.get('stage_id') == batch['stage_id']]
        if not review_repair_allowed(self.workflow.settings.auto_review_repair_limit, len(prior)):
            raise DomainError('review_repair_limit_reached', '诊断返工已达到现有审查返工次数上限。')
        assembly = await self.store.read('work_item', batch['assembly_work_item_id'])
        source = await self.store.read('code_snapshot', assembly.get('attempt_id'))
        if source != diagnostic['source'] or source.get('stale') or assembly['status'] != 'completed':
            raise DomainError('review_contract_stale', '失败诊断不属于当前合并源码。')
        prepared = await self._prepare_diagnostic_suite(run, source)
        context = deepcopy(batch['context'])
        context.update(snapshot=source, source_snapshot_id=source['id'], source_commit=source['commit_oid'],
            diagnostic=diagnostic, findings=findings, diagnostic_suite=prepared,
            assertions=[{**assertion, 'path': manifest['file'], 'case_id': case['case_id']}
                for manifest in prepared['manifests'] for case in manifest['cases'] for assertion in case['assertions']],
            previous_dispositions=[*context.get('previous_dispositions', []), *batch.get('actions', [])])
        context['reviews'] = [await self.store.read('review', row['id']) for row in context['reviews']]
        for owner in context['owners']:
            work = await self.store.read('work_item', owner['work_item_id'])
            owner['work_version'] = {k: work.get(k) for k in owner.get('work_version', {})}
        stage = await self.store.read('work_item', batch['stage_id'])
        batch_id = identity(batch['id'], diagnostic['id'], 'followup')
        triage_id = identity(batch_id, 'triage')
        spec = new_work(run, 'review-disposition-' + batch_id, 'review_disposition', [assembly['id']],
            approval=stage.get('approval_required', False),
            payload={'review_contract_task': batch_id, 'review_contract_kind': 'triage'})
        row = {'actor': 'controller', 'run_id': run['id'], 'stage_id': stage['id'], 'reviewer_id': batch['reviewer_id'],
            'state': 'triaging', 'source_snapshot_id': source['id'], 'source_commit': source['commit_oid'],
            'context': context, 'context_digest': canonical_digest(context), 'previous_batch_id': batch['id'],
            'triage_work_item_id': triage_id, 'review_work_ids': batch['review_work_ids'], 'original_stage': stage,
            'work_specs': {triage_id: spec}, 'repair_work_item_ids': [triage_id], 'created_at': utc_now()}
        def create(tx):
            if tx.get('review_contract_repair', batch_id):
                return tx.get('review_contract_repair', batch_id)
            current = tx.get('review_contract_repair', batch['id'])
            if current != batch or tx.get('run', run['id']) != run or tx.get('work_item', stage['id']) != stage:
                raise DomainError('review_contract_stale', '诊断返工准备期间版本已变化。')
            self._verify_context(tx, row)
            tx.put('work_item', triage_id, spec)
            validation = tx.get('work_item', batch['validation_work_item_id'])
            tx.put('work_revision', str(uuid4()), {'work_item_id': validation['id'], 'snapshot': validation,
                'reason': 'Failed diagnostic retained as history; new read-only analysis scheduled.'})
            tx.put('work_item', validation['id'], {**validation, 'required': False, 'archived': True}, validation['revision'])
            tx.put('review_contract_repair', batch['id'], {**current, 'state': 'superseded', 'next_batch_id': batch_id}, current['revision'])
            result = tx.put('review_contract_repair', batch_id, row)
            self.workflow._recompute_run(tx, run['id'])
            tx.event('review.diagnostic_repair_scheduled', {'batch_id': batch_id, 'diagnostic_id': diagnostic['id']}, run_id=run['id'])
            return result
        return await self.store.command('review.contract.diagnostic_retry', batch_id, {'context': row['context_digest']}, create)

    async def validate_action(self, task, snapshot):
        work = await self.store.read('work_item', task['work_item_id'])
        batch_id = task.get('review_contract_task') or work.get('payload', {}).get('review_contract_task')
        batch = await self.store.read('review_contract_repair', batch_id)
        if not batch:
            raise DomainError('review_contract_binding_invalid', '返工批次不存在。')
        records = {kind: {row['id']: row for row in await self.store.list(kind)}
                   for kind in REVIEW_BINDING_KINDS}
        class Read:
            def get(inner, kind, key):
                return batch if kind == 'review_contract_repair' and key == batch['id'] else records.get(kind, {}).get(key)
            def list(inner, kind):
                return list(records.get(kind, {}).values())
        repair_batch(Read(), work)
        kind = batch['work_specs'][work['id']]['payload']['review_contract_kind']
        if kind not in _GUARDED_KINDS:
            return
        check_kind, failure_code = _GUARDED_KINDS[kind]
        assigned = batch['work_specs'][work['id']]['payload']['review_contract_actions']
        guard = self.coverage_guard if kind == 'test_coverage_extension' else self.guard
        changes = work['write_paths'] if kind == 'test_coverage_extension' else migration_edits(assigned)
        def verify_frozen():
            with tempfile.TemporaryDirectory(prefix='agentflow-migration-') as directory:
                root = Path(directory)
                for name, source in [('before', batch['context']['snapshot']), ('after', snapshot)]:
                    archive, target = root / (name + '.tar'), root / name
                    target.mkdir()
                    self.diagnostics.pipeline._archive(Path(source['repository_path']), source['commit_oid'], archive)
                    with tarfile.open(archive) as tar:
                        tar.extractall(target, filter='data')
                return guard.verify(root / 'before', root / 'after', changes)
        report = await asyncio.to_thread(verify_frozen)
        if not report['ok']:
            raise DomainError(failure_code, '测试修改超出已批准的结构保护范围。', details=report)
        def record(tx):
            current = tx.get('work_item', work['id'])
            fresh = repair_batch(tx, current)
            if (fresh['context_digest'] != batch['context_digest'] or current.get('attempt_id') != task['attempt_id']
                    or current['generation'] != work['generation']):
                raise DomainError('review_contract_stale', '迁移核验期间任务版本已变化。')
            return tx.put(check_kind, task['attempt_id'], {'batch_id': batch['id'],
                'work_item_id': work['id'], 'run_id': work['run_id'], 'generation': work['generation'],
                'context_digest': batch['context_digest'], 'source_tree_oid': snapshot['tree_oid'],
                'source_commit': snapshot['commit_oid'], 'report': report})
        await self.store.command('review.contract.guard', task['attempt_id'], {'source': snapshot['commit_oid'], 'report': report}, record)

    async def validate_or_poll(self, claim):
        # A freshly claimed controller task can be seen by the scheduler's
        # recovery scan while its first dispatch is still preparing manifests.
        async with self._locks.setdefault('validation:' + claim['work_item']['id'], asyncio.Lock()):
            work = await self.store.read('work_item', claim['work_item']['id'])
            if (not work or work.get('attempt_id') != claim['attempt']['id']
                    or work.get('status') not in {'running', 'waiting_execution'}):
                return
            run = await self.store.read('run', work['run_id'])
            if run['execution_state'] != 'running':
                return
            attempt = await self.store.read('attempt', work['attempt_id'])
            await self._validate_or_poll({'work_item': work, 'attempt': attempt, 'run': run})

    async def _validate_or_poll(self, claim):
        work, attempt, run = claim['work_item'], claim['attempt'], claim['run']
        batch = await self.context_for(work)
        if not batch or batch.get('validation_work_item_id') != work['id']:
            raise DomainError('review_contract_binding_invalid', '诊断任务不属于当前返工批次。')
        assembly = await self.store.read('work_item', batch['assembly_work_item_id'])
        source = await self.store.read('code_snapshot', assembly.get('attempt_id'))
        if not source or source.get('stale') or source.get('generation') != assembly['generation']:
            raise DomainError('review_contract_stale', '诊断源码快照不可用。')
        from agentflow.control.recovery import _ReadState
        rows = await asyncio.gather(*(self.store.list(kind) for kind in REVIEW_BINDING_KINDS))
        binding_state = _ReadState(dict(zip(REVIEW_BINDING_KINDS, rows, strict=True)))
        for action_id in batch['repair_work_item_ids']:
            action = binding_state.get('work_item', action_id)
            if not action or repair_batch(binding_state, action) != batch:
                raise DomainError('review_contract_binding_invalid', '测试保护核验前返工任务身份或授权已变化。')
            spec = batch['work_specs'][action_id]
            kind = spec['payload']['review_contract_kind']
            if kind not in _GUARDED_KINDS:
                continue
            check_kind, failure_code = _GUARDED_KINDS[kind]
            check = await self.store.read(check_kind, action.get('attempt_id'))
            child = await self.store.read('code_snapshot', action.get('attempt_id'))
            if (not check or not child or check.get('source_commit') != child['commit_oid'] or not check.get('report', {}).get('ok')
                    or check.get('generation') != action['generation']
                    or check.get('work_item_id') != action_id or check.get('batch_id') != batch['id']
                    or check.get('run_id') != run['id'] or child.get('stale')
                    or child.get('generation') != action['generation']
                    or kind == 'test_coverage_extension' and (check.get('context_digest') != batch['context_digest']
                        or check.get('source_tree_oid') != child['tree_oid'])):
                raise DomainError(failure_code, '缺少对应测试修改版本的断言保护证据。')
            for path in spec['write_paths']:
                current = await asyncio.to_thread(self.repository._run, Path(source['repository_path']), ['ls-tree', source['commit_oid'], '--', path])
                verified = await asyncio.to_thread(self.repository._run, Path(child['repository_path']), ['ls-tree', child['commit_oid'], '--', path])
                if not current or current != verified:
                    raise DomainError(failure_code, '合并改变了已核验的测试内容或文件模式。')
        prepared = await self._prepare_diagnostic_suite(run, source)
        diagnostic_batch = {**batch, 'id': identity(batch['id'], attempt['id'], 'diagnostic')}
        result = await self.diagnostics.start_or_poll(run, diagnostic_batch, source, prepared['cases'], execution_spec=prepared['execution_spec'])
        if result['state'] == 'waiting':
            def waiting(tx):
                current = tx.get('work_item', work['id'])
                if current['attempt_id'] != attempt['id']:
                    raise DomainError('review_contract_stale', '诊断执行版本已变化。')
                if current['status'] == 'waiting_execution':
                    return current
                running = tx.get('attempt', attempt['id'])
                tx.put('attempt', running['id'], {**running, 'status': 'waiting_execution'}, running['revision'])
                return tx.put('work_item', work['id'], {**current, 'status': 'waiting_execution'}, current['revision'])
            await self.store.command('review.contract.waiting', attempt['id'], {}, waiting)
            return
        if result['state'] == 'failed' and result.get('reports'):
            blob = await self.workflow.artifacts.put_bytes(json.dumps(result, ensure_ascii=False).encode())
            await self.workflow.finish_attempt(attempt['id'], {'execution_status': 'completed', 'quality_result': 'failed',
                'input_fingerprint': attempt['input_fingerprint'], 'fencing_token': attempt['fencing_token'],
                'summary': '既有用例诊断失败，保留原报告并重新分析修复归属。'}, 'review-contract-failed:' + attempt['id'],
                verified_artifacts=[{'digest': blob['id'], 'name': 'review-diagnostics.json', 'media_type': 'application/json'}])
            def failed(tx):
                current = tx.get('review_contract_repair', batch['id'])
                return tx.put('review_contract_repair', current['id'], {**current, 'state': 'diagnostic_failed',
                    'failed_diagnostic_id': result['id']}, current['revision'])
            await self.store.command('review.contract.diagnostic_failed', attempt['id'], {'diagnostic': result['id']}, failed)
            return
        if result['state'] != 'passed':
            raise DomainError('review_diagnostic_failed', '既有用例诊断未通过，保留失败证据等待针对性修复。', details=result.get('blockers'))
        blob = await self.workflow.artifacts.put_bytes(json.dumps(result, ensure_ascii=False).encode())
        record = {'run_id': run['id'], 'work_item_id': work['id'], 'attempt_id': attempt['id'],
                  'batch_id': batch['id'], 'state': 'passed', 'source_snapshot_id': source['id'],
                  'source_commit': source['commit_oid'], 'source_tree_oid': source['tree_oid'],
                  'validation_generation': work['generation'], 'diagnostic_id': result['id']}
        await self.store.command('review.contract.validation', attempt['id'], record,
            lambda tx: tx.put('review_contract_validation', attempt['id'], record))
        await self.workflow.finish_attempt(attempt['id'], {'execution_status': 'completed', 'quality_result': 'passed',
            'input_fingerprint': attempt['input_fingerprint'], 'fencing_token': attempt['fencing_token'],
            'summary': '受影响既有测试诊断通过，等待独立复审。'}, 'review-contract-finish:' + attempt['id'],
            verified_artifacts=[{'digest': blob['id'], 'name': 'review-diagnostics.json', 'media_type': 'application/json'}])
        latest = await self.store.read('review_contract_repair', batch['id'])
        await self.store.command('review.contract.reviewing', attempt['id'], {},
            lambda tx: tx.put('review_contract_repair', latest['id'], {**latest, 'state': 'reviewing'}, latest['revision']))

    async def reconcile(self):
        for batch in await self.store.list('review_contract_repair'):
            if (batch['state'] in {'triaging', 'needs_attention'}
                    and batch['context'].get('protocol_version', 1) < REVIEW_CONTRACT_PROTOCOL_VERSION):
                try:
                    batch = await self.upgrade_triage_context(batch['id'])
                except DomainError:
                    # Refusal leaves frozen evidence and all recovery state intact.
                    pass
            if not self.nodes:
                continue
            run = await self.store.read('run', batch['run_id'])
            work_id = batch.get('validation_work_item_id')
            work = await self.store.read('work_item', work_id) if work_id else None
            if run and work and work['status'] == 'cancel_requested':
                jobs = [job for job in await self.store.list('node_job') if job.get('run_id') == run['id']
                        and job.get('parent_work_item_id') == work['id']]
                for job in jobs:
                    if job['state'] not in {'completed', 'failed', 'cancelled'}:
                        await self.nodes.cancel_job(job['id'], 'Parent review diagnostic cancelled', 'review-cancel:' + job['id'])
                jobs = [await self.store.read('node_job', job['id']) for job in jobs]
                if any(job['state'] not in {'completed', 'failed', 'cancelled'} for job in jobs):
                    continue
                attempt = await self.store.read('attempt', work['attempt_id'])
                await self.workflow.finish_attempt(attempt['id'], {'execution_status': 'cancelled', 'quality_result': 'unknown',
                    'input_fingerprint': attempt['input_fingerprint'], 'fencing_token': attempt['fencing_token'],
                    'summary': '诊断节点已停止，本次诊断已取消，可按正常恢复流程继续。'},
                    'review-contract-cancel:' + attempt['id'], verified_artifacts=[])
                continue
            if not run or run['execution_state'] != 'running':
                continue
            if batch['state'] == 'diagnostic_failed':
                try:
                    await self.retry_diagnostic_findings(batch, run)
                except DomainError as error:
                    await self._attention(batch, error)
                continue
            if batch['state'] == 'awaiting_approval':
                triage = await self.store.read('work_item', batch['triage_work_item_id'])
                if triage and triage['status'] == 'completed' and triage.get('approved_fingerprint'):
                    try:
                        await self.store.command('review.contract.approved', identity(batch['id'], triage['attempt_id'], triage['approved_fingerprint']),
                            {'disposition': batch['pending_disposition_digest'], 'approval': triage['approved_fingerprint']},
                            lambda tx: self.apply_disposition(tx, {'work_item_id': triage['id']}, batch['pending_disposition']) or {'scheduled': True})
                    except DomainError as error:
                        await self._attention(batch, error)
                continue
            if batch['state'] == 'reviewing':
                stage = await self.store.read('work_item', batch['stage_id'])
                if stage['status'] == 'completed' and stage['quality_result'] == 'passed':
                    await self.store.command('review.contract.resolved', batch['id'], {},
                        lambda tx: tx.put('review_contract_repair', batch['id'], {**batch, 'state': 'completed'}, batch['revision']))
                continue
            if work and work['status'] == 'completed' and batch['state'] == 'repairing':
                if work.get('quality_result') == 'failed':
                    diagnostic_id = identity(batch['id'], work['attempt_id'], 'diagnostic')
                    diagnostic = next((row for row in await self.store.list('review_diagnostic')
                        if row.get('run_id') == run['id'] and row.get('batch_id') == diagnostic_id), None)
                    if diagnostic and diagnostic['state'] == 'failed':
                        await self.store.command('review.contract.recollect-failed', work['attempt_id'], {},
                            lambda tx: tx.put('review_contract_repair', batch['id'], {**batch, 'state': 'diagnostic_failed',
                                'failed_diagnostic_id': diagnostic['id']}, batch['revision']))
                    continue
                verified = await self.store.read('review_contract_validation', work.get('attempt_id'))
                if verified and verified.get('state') == 'passed':
                    await self.store.command('review.contract.recollect-state', work['attempt_id'], {},
                        lambda tx: tx.put('review_contract_repair', batch['id'], {**batch, 'state': 'reviewing'}, batch['revision']))
                continue
            if not work or work['status'] not in {'waiting_execution', 'running'}:
                continue
            attempt = await self.store.read('attempt', work['attempt_id'])
            try:
                await self.validate_or_poll({'run': run, 'work_item': work, 'attempt': attempt})
            except DomainError as error:
                await self.workflow.block_attempt(attempt['id'], error.message, str(uuid4()),
                    failure_code=error.code if error.code in {'test_migration_invalid', 'test_coverage_invalid', 'review_diagnostic_failed'} else None,
                    failure_diagnostic={'code': error.code, 'message': error.message, 'details': error.details})

    async def _attention(self, batch, error):
        reason = {'code': error.code, 'message': error.message, 'details': error.details}
        def record(tx):
            current = tx.get('review_contract_repair', batch['id'])
            if current != batch:
                return {'changed': False}
            return tx.put('review_contract_repair', batch['id'], {**current, 'state': 'needs_attention',
                'reasons': [reason]}, current['revision'])
        await self.store.command('review.contract.attention', identity(batch['id'], batch['revision'], reason), reason, record)


def assigned_action_paths(actions):
    return [path for action in actions for path in action['repair_paths']]

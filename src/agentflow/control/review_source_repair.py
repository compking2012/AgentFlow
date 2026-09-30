"""Owner-authorized, file-scoped repair of a complete failed-review source snapshot.

This command is deliberately absent from model tools and automatic remediation.
It creates new work, preserving every original producer and its cumulative budget.
"""
from __future__ import annotations

import asyncio
import json
import re
import stat
from pathlib import Path, PurePosixPath
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.recovery import KINDS, RunRecoveryService, _ReadState, _related, coding_usage_blockers
from agentflow.control.remediation import review_repair_manual_blockers
from agentflow.control.review_producer import (
    _completed_review,
    _current_attempt,
    review_producer,
    sealed_review_group,
)
from agentflow.control.service import ensure_revision
from agentflow.control.starter_execution import STARTER_SUPPORT_FILES
from agentflow.domain.expansion import _path, _within
from agentflow.domain.planning import CODING_STEPS, descendants

_KINDS = tuple(dict.fromkeys((*KINDS, 'review_source_repair', 'run_recovery',
                             'product_test_repair', 'product_test_runtime_repair')))
_SOURCE_SUFFIXES = {'.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx', '.py', '.go', '.rs', '.java', '.kt',
                    '.swift', '.c', '.cc', '.cpp', '.h', '.hpp', '.cs', '.rb', '.php', '.html', '.css',
                    '.scss', '.vue', '.svelte', '.sql'}
_FROZEN_PARTS = {'node_modules', 'vendor', 'dist', 'build', 'target', 'coverage', 'artifacts', 'logs',
                 'test-results', 'playwright-report', 'tooling', 'scripts', 'config', 'configuration',
                 'secrets', 'credentials', 'runtime', 'supervisor'}


class ReviewSourceRepairRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: int = Field(ge=1)
    review_work_item_id: str = Field(min_length=1, max_length=200)
    write_paths: list[str] = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=4000)


def _error(message, code='review_source_repair_invalid'):
    return DomainError(code, message)


def _source_path(value):
    normalized = _path(value)
    path = PurePosixPath(normalized)
    parts = [part.casefold() for part in path.parts]
    if (normalized != value or normalized == '.' or path.suffix.casefold() not in _SOURCE_SUFFIXES
            or normalized.casefold() in {name.casefold() for name in STARTER_SUPPORT_FILES}
            or any(part.startswith('.') or part in _FROZEN_PARTS for part in parts)
            or re.search(r'(^|[._-])(config|lock|secret|credential|token|recipe|conftest|settings|setup)([._-]|$)', path.name, re.I)
            or path.name.casefold().startswith(('playwright.', 'vite.', 'webpack.', 'rollup.', 'eslint.'))):
        raise _error('授权路径必须是具体源码文件；不接受目录、运行时、敏感或冻结配置/工具文件。', 'invalid_review_repair_scope')
    return normalized


class OwnerReviewSourceRepair:
    def __init__(self, store, workflow):
        self.store, self.workflow = store, workflow
        self.recovery = RunRecoveryService(store, workflow)
        self.repository = self.recovery.repository

    async def _read(self, run_id):
        rows = await asyncio.gather(*(self.store.list(kind) for kind in _KINDS))
        return _related(dict(zip(_KINDS, rows, strict=True)), run_id)

    @staticmethod
    def _dynamic_authority(tx, run, plan, work, scopes, snapshots):
        """Recognize controller-created repairs without making them new scope grants."""
        product = tx.get('product_test_repair', work['id'])
        runtime = tx.get('product_test_runtime_repair', work['id'])
        if not product and not runtime:
            return None
        from agentflow.execution.manifests import SourceManifest
        try:
            receipt = product or runtime
            candidate = tx.get('candidate', receipt['candidate_id'])
            review = tx.get('work_item', receipt['review_work_item_id'])
            if (bool(product) == bool(runtime) or receipt.get('repair_work_item_id') != work['id']
                    or receipt.get('run_id') != run['id'] or not candidate or candidate.get('run_id') != run['id']
                    or plan.get('product_contract', {}).get('stack') != 'node_web_api'
                    or receipt.get('product_id') != plan['product_contract'].get('product_id')
                    or work.get('kind') in {'aggregation', 'stage_child'} or work.get('parent_stage_id')
                    or not review or review.get('run_id') != run['id'] or review.get('step') != 'code_review'
                    or review.get('role') != 'review' or review.get('write_paths') != []
                    or work['step'] not in plan.get('authorized_rework_steps', []) or not scopes):
                raise ValueError('dynamic_identity')
            manifest = candidate['source_manifest']
            source = SourceManifest.model_validate({key: value for key, value in manifest.items() if key != 'fingerprint'})
            if (source.fingerprint != manifest.get('fingerprint') or source.source_commit != candidate['source_commit']
                    or source.source_tree_oid != candidate['tree_oid']):
                raise ValueError('candidate_manifest')
            aliases = [row for row in tx.list('code_snapshot') if row.get('work_item_id') == work['id']
                       and row.get('run_id') == run['id'] and row.get('generation') == 0]
            if (len(aliases) != 1 or any(aliases[0].get(field) != value for field, value in {
                    'commit_oid': candidate['source_commit'], 'base_oid': candidate['source_commit'],
                    'tree_oid': candidate['tree_oid'], 'repository_path': candidate['source_repository']}.items())):
                raise ValueError('original_checkpoint')
            if product:
                if (work.get('key') != 'product-repair-' + work['id'] or work['step'] != 'implementation'
                        or work.get('role') != 'development' or work.get('write_paths') != ['src', 'public']
                        or work.get('payload', {}).get('product_frozen_repair') is not True
                        or receipt.get('preserved_test_source') != candidate['source_commit']):
                    raise ValueError('product_repair_scope')
            else:
                request = receipt.get('request', {})
                step = {'unit': 'unit_test_implementation', 'integration': 'integration_test_implementation'}.get(receipt.get('phase'))
                allowed = ['tests/unit.test.mjs'] if receipt.get('phase') == 'unit' else None
                if (receipt.get('actor') != 'owner' or work.get('key') != 'test-runtime-repair-' + work['id']
                        or work['step'] != step or work.get('role') != ('unit_test' if receipt.get('phase') == 'unit' else 'integration_test')
                        or work.get('payload', {}).get('test_runtime_repair_id') != receipt['id']
                        or work.get('write_paths') != receipt.get('write_paths')
                        or (work['write_paths'] != allowed if allowed else work['write_paths'] not in [['tests/api.spec.mjs'], ['tests/web.spec.mjs']])
                        or receipt.get('source_commit') != candidate['source_commit'] or receipt.get('source_tree_oid') != candidate['tree_oid']
                        or request.get('product_id') != receipt['product_id'] or request.get('candidate_id') != candidate['id']
                        or receipt.get('request_fingerprint') != canonical_digest(request)
                        or any(not any(_within(path, grant) for grant in scopes.values()) for path in work['write_paths'])):
                    raise ValueError('runtime_repair_scope')
            return {'candidate': candidate, 'snapshot': snapshots[work['id']], 'write_paths': work['write_paths'],
                    'original_scopes': [grant for grants in scopes.values() for grant in grants],
                    'ancestor_commits': sorted({row['commit_oid'] for identity, row in snapshots.items() if identity != work['id']})}
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise _error('动态修复缺少匹配的原始授权、候选或源码检查点。', 'review_repair_not_authorized') from error

    @staticmethod
    def _proof(state, request):
        tx, run = _ReadState(state), state['run'][0]
        ensure_revision(run, request.expected_revision)
        if run.get('execution_state') not in {'running', 'paused'}:
            raise _error('仅可在运行或已暂停且执行已停止的运行中准备审查修复。', 'review_repair_run_state')
        if not request.reason.strip():
            raise _error('必须说明本次所有者授权的修复目标。', 'invalid_review_repair_scope')
        paths = sorted({_source_path(path) for path in request.write_paths})
        if len(paths) != len(request.write_paths):
            raise _error('授权路径不能重复。', 'invalid_review_repair_scope')
        items = [item for item in state['work_item'] if not item.get('archived')]
        by_id = {item['id']: item for item in items}
        reviewer = by_id.get(request.review_work_item_id)
        if (not reviewer or reviewer.get('kind') == 'stage_child'
                or reviewer.get('parent_stage_id') or reviewer.get('project_id') != run['project_id']):
            raise _error('必须选择当前运行中的独立失败审查或全部子审查通过后的失败汇总。')
        review = _completed_review(tx, reviewer, 'failed')
        group = sealed_review_group(tx, run, reviewer) if reviewer.get('kind') == 'aggregation' else None
        producer = review_producer(tx, run, reviewer)
        if not review or not producer or reviewer.get('kind') == 'aggregation' and not group:
            raise _error('当前失败审查及其执行身份不一致，或审查没有唯一原始生产者。')
        if not producer or producer.get('step') not in CODING_STEPS or not _current_attempt(tx, producer):
            raise _error('失败审查的原始编码生产者尚未完成，或当前执行身份不一致。')
        plan = tx.get('plan', run['plan_id'])
        if (not plan or plan.get('state') != 'started' or plan.get('started_run_id') != run['id']
                or plan.get('project_id') != run['project_id'] or plan.get('iteration_id') != run['iteration_id']
                or 'implementation' not in plan.get('authorized_rework_steps', [])):
            raise _error('原始计划没有授权当前运行的 implementation 修复。', 'review_repair_not_authorized')
        if any(row.get(flag) for rows in state.values() for row in rows for flag in
               ('restore_uncertain', 'restore_reconciliation_required', 'restore_revalidation_required')):
            raise _error('恢复证据仍需核对，不能安排新的修复。', 'recovery_evidence_invalid')
        if (any(item.get('status') == 'waiting_approval' for item in items)
                or any(not approval.get('stale') and approval.get('decision') is None for approval in state['approval'])):
            raise _error('仍有人工审批待处理，本次修复不能代替审批。', 'human_approval_pending')
        if state['delivery_intent'] or any(not candidate.get('stale') and
                candidate.get('run_input_fingerprint', run['input_fingerprint']) == run['input_fingerprint']
                for candidate in state['candidate']):
            raise _error('当前源码已经冻结为候选或进入交付，不能插入修复。', 'review_repair_source_frozen')
        snapshots, contexts, scopes, visited, visiting = {}, {}, {}, set(), set()
        dynamic_bases = []
        specs = {spec['key']: spec for spec in plan.get('work_specs', [])}

        def accepted(work):
            if (work.get('run_id') != run['id'] or work.get('project_id') != run['project_id']
                    or work.get('status') != 'completed' or work.get('quality_result') in {'failed', 'inconclusive'}
                    or not _current_attempt(tx, work)):
                raise _error('原始计划的编码祖先没有当前已接受输出。', 'review_repair_ancestor_invalid')
            if work.get('approval_required') and (not work.get('approved_fingerprint') or not any(
                    approval.get('work_item_id') == work['id'] and not approval.get('stale')
                    and approval.get('decision') == 'approve' and approval.get('fingerprint') == work['approved_fingerprint']
                    for approval in state['approval'])):
                raise _error('原始编码祖先缺少有效的所有者批准。', 'human_approval_pending')
            snapshot = tx.get('code_snapshot', work['attempt_id'])
            if (not snapshot or snapshot.get('stale') or snapshot.get('run_id') != run['id']
                    or snapshot.get('work_item_id') != work['id'] or snapshot.get('generation') != work['generation']):
                raise _error('当前编码祖先缺少完整、有效的源码快照。')
            snapshots[work['id']] = snapshot
            if work.get('kind') != 'aggregation':
                context = tx.get('dispatch_context', work['attempt_id'])
                task = (context or {}).get('task', {})
                control = task.get('coding_step')
                if control and (control != tx.get('coding_step_control', work['attempt_id'])
                        or control.get('attempt_id') != work['attempt_id'] or control.get('work_item_id') != work['id']
                        or control.get('source_commit') != task.get('source_commit')
                        or any(control.get(field) != work.get(field) for field in
                               ('run_id', 'generation', 'fencing_token', 'input_fingerprint'))):
                    raise _error('编码小步与原始派发身份不一致。')
                original_base = control.get('base_commit') if control else task.get('source_commit')
                if (task.get('attempt_id') != work['attempt_id'] or task.get('step') != work['step']
                        or task.get('allowed_write_paths') != work.get('write_paths')
                        or original_base != snapshot.get('base_oid')
                        or task.get('workspace') != snapshot.get('repository_path')
                        or any(task.get(field) != work.get(field) for field in ('run_id', 'fencing_token', 'input_fingerprint'))
                        or task.get('work_item_id') != work['id']):
                    raise _error('编码祖先快照与冻结派发身份、源码或写入范围不一致。')
                contexts[work['id']] = context

        def visit(identity):
            if identity in visiting or identity not in by_id:
                raise _error('原始工作依赖图无效。')
            if identity in visited:
                return
            visiting.add(identity)
            work = by_id[identity]
            for parent in work.get('dependencies', []):
                visit(parent)
            if work.get('step') in CODING_STEPS:
                accepted(work)
                dynamic = OwnerReviewSourceRepair._dynamic_authority(tx, run, plan, work, scopes, snapshots)
                if dynamic is not None:
                    dynamic_bases.append(dynamic)
                    visiting.remove(identity)
                    visited.add(identity)
                    return
                owner = by_id.get(work.get('parent_stage_id')) if work.get('kind') == 'stage_child' else work
                spec = specs.get((owner or {}).get('key'))
                prior_repair = tx.get('review_source_repair', work.get('payload', {}).get('owner_review_source_repair_id'))
                if prior_repair is not None:
                    alias = tx.get('code_snapshot', prior_repair.get('snapshot_id'))
                    if (prior_repair.get('actor') != 'owner' or prior_repair.get('id') != work['id']
                            or prior_repair.get('repair_work_item_id') != work['id']
                            or prior_repair.get('run_id') != run['id'] or prior_repair.get('iteration_id') != run['iteration_id']
                            or work.get('key') != 'owner-review-repair-' + work['id']
                            or work.get('kind') in {'aggregation', 'stage_child'} or work.get('parent_stage_id')
                            or work.get('step') != 'implementation' or work.get('role') != 'development'
                            or work.get('dependencies') != [prior_repair.get('producer_work_item_id')]
                            or work.get('write_paths') != prior_repair.get('write_paths')
                            or not alias or alias.get('owner_repair_id') != prior_repair['id']
                            or alias.get('work_item_id') != work['id'] or alias.get('run_id') != run['id']
                            or alias.get('source_snapshot_id') != prior_repair.get('source_snapshot_id')
                            or alias.get('commit_oid') != prior_repair.get('source_commit')
                            or any(not any(_within(_source_path(path), grant) for grant in scopes.values())
                                   for path in work.get('write_paths', []))):
                        raise _error('此前所有者修复的授权回执与原始祖先范围不一致。', 'review_repair_not_authorized')
                    scopes[identity] = work['write_paths']
                    visiting.remove(identity)
                    visited.add(identity)
                    return
                if (not spec or spec.get('step') != work['step'] or spec.get('role') != work['role']
                        or work['step'] not in plan.get('authorized_rework_steps', [])):
                    raise _error('源码路径必须来自原始计划中已授权的编码祖先。', 'review_repair_not_authorized')
                original = spec.get('write_paths', ['.'])  # start_run's original coding grant
                actual = work.get('write_paths', [])
                if work.get('kind') == 'aggregation':
                    expansions = [row for row in state['stage_expansion'] if row.get('stage_work_item_id') == identity
                                  and row.get('input_fingerprint') == work.get('expansion_fingerprint')]
                    if len(expansions) != 1:
                        raise _error('聚合生产者的封存展开授权不唯一。')
                    expansion = expansions[0]
                    frozen = expansion.get('original_stage', {})
                    children = work.get('expanded_child_ids', [])
                    if (not children or len(set(children)) != len(children) or set(children) != set(work['dependencies'])
                            or set(children) != set(expansion.get('child_ids', []))
                            or any(frozen.get(field) != work.get(field) for field in ('id', 'run_id', 'project_id', 'key', 'step', 'role'))
                            or {item['id'] for item in items if item.get('parent_stage_id') == identity} != set(children)
                            or frozen.get('write_paths') != work.get('original_write_paths')
                            or frozen.get('dependencies') != work.get('original_dependencies')
                            or any(by_id[child].get('parent_stage_id') != identity or by_id[child].get('kind') != 'stage_child'
                                   or by_id[child].get('dependencies') != work['original_dependencies'] for child in children)):
                        raise _error('聚合生产者的原授权或子任务成员已变化。')
                    actual = work['original_write_paths']
                    for child in children:
                        if any(not _within(_path(path), actual) for path in by_id[child]['write_paths']):
                            raise _error('编码子任务超出封存的原始授权。', 'review_repair_not_authorized')
                if not actual or any(not _within(_path(path), original) for path in actual):
                    raise _error('当前源码授权超出原始计划授权。', 'review_repair_not_authorized')
                # Aggregator grants are evidence bounds; only actual coding file owners grant paths.
                if work.get('kind') != 'aggregation':
                    scopes[identity] = actual
            visiting.remove(identity)
            visited.add(identity)

        visit(producer['id'])
        for path in paths:
            if not any(_within(path, granted) for granted in scopes.values()):
                raise _error('所选文件不属于当前已接受的原始编码祖先授权：' + path, 'review_repair_not_authorized')
        findings = review['blocking_findings']
        if any(not isinstance(finding, dict) or finding.get('severity') != 'blocking' for finding in findings):
            raise _error('失败审查包含无效的 blocking finding。')
        finding_paths = {_source_path(finding.get('path')) for finding in findings}
        if not finding_paths <= set(paths):
            raise _error('显式授权必须覆盖全部 blocking finding 路径。', 'review_repair_scope_incomplete')
        source = snapshots[producer['id']]
        context = tx.get('dispatch_context', reviewer['attempt_id'])
        task = (context or {}).get('task', {})
        if (not context or task.get('source_commit') != source.get('commit_oid')
                or review.get('reviewed_commit') != source.get('commit_oid') or task.get('allowed_write_paths') != []):
            raise _error('失败审查冻结的源码与当前生产者快照或派发身份不一致。')
        roots = {reviewer['id'], *([child['id'] for child in group['children']] if group else [])}
        affected = descendants(items, roots)
        target = {'work_item_id': reviewer['id'], 'affected_work_item_ids': sorted(affected)}
        blockers = review_repair_manual_blockers(tx, run, target)
        if blockers:
            raise DomainError(blockers[0]['code'], blockers[0]['message'], details=blockers)
        if producer.get('payload', {}).get('recovery_model_binding'):
            binding = producer['payload']['recovery_model_binding']
            if binding.get('model_profile_id') != run.get('runtime_bindings', {}).get('coding_model_profile_id'):
                raise _error('生产者使用单独的模型覆盖，不能隐式切换新修复的模型。', 'review_repair_model_mismatch')
        return {'run': run, 'plan': plan, 'items': items, 'reviewer': reviewer, 'review': review, 'producer': producer,
                'source': source, 'snapshots': snapshots, 'contexts': contexts, 'context': context, 'paths': paths, 'affected': affected,
                'dynamic_bases': dynamic_bases,
                'review_group': group, 'review_roots': roots,
                'scope_owners': {path: sorted(identity for identity, grant in scopes.items() if _within(path, grant)) for path in paths}}

    def _filesystem_proof(self, state, proof):
        try:
            source = proof['source']
            repository = Path(source['repository_path'])
            evidence = []
            if repository.is_symlink() or repository.resolve() != repository:
                raise ValueError('unsafe_repository')
            captured = self.repository._collect_diff(repository, source['commit_oid'])
            if captured['has_changes'] or captured['tree_oid'] != source['tree_oid']:
                raise ValueError('snapshot_dirty')
            evidence.append((captured['head_oid'], captured['tree_oid']))
            for snapshot in proof['snapshots'].values():
                # Old coding workspaces may have been retired. Their immutable
                # objects must survive in the complete reviewed source itself.
                if self.repository._integrity(repository, snapshot['commit_oid']) != snapshot['tree_oid']:
                    raise ValueError('snapshot_tree_mismatch')
                for ancestor in {snapshot['commit_oid'], snapshot['base_oid'], *snapshot.get('parent_commit_oids', [])}:
                    self.repository._run(repository, ['merge-base', '--is-ancestor', self.repository._oid(ancestor), source['commit_oid']])
                evidence.append((snapshot['id'], snapshot['commit_oid'], snapshot['tree_oid']))
            for context in proof['contexts'].values():
                self.repository._run(repository, ['merge-base', '--is-ancestor',
                    self.repository._oid(context['task']['source_commit']), source['commit_oid']])
            for dynamic in proof['dynamic_bases']:
                candidate, snapshot = dynamic['candidate'], dynamic['snapshot']
                base, head = candidate['source_commit'], snapshot['commit_oid']
                if self.repository._integrity(repository, base) != candidate['tree_oid']:
                    raise ValueError('dynamic_candidate_tree')
                for ancestor in dynamic['ancestor_commits']:
                    self.repository._run(repository, ['merge-base', '--is-ancestor', ancestor, base])
                self.repository._run(repository, ['merge-base', '--is-ancestor', base, head])
                changed = self.repository._run(repository, ['diff', '--name-only', '-z', '--no-renames',
                    '--no-ext-diff', '--no-textconv', base, head, '--'])
                for relative in filter(None, changed.decode().split('\0')):
                    if not _within(_path(relative), dynamic['write_paths']) or not _within(relative, dynamic['original_scopes']):
                        raise ValueError('dynamic_changed_scope')
                evidence.append((candidate['id'], base, head, changed.decode()))
            for relative in proof['paths']:
                path = repository
                for part in PurePosixPath(relative).parts:
                    path = path / part
                    if path.is_symlink():
                        raise ValueError('symlink_scope')
                if not stat.S_ISREG(path.stat().st_mode):
                    raise ValueError('non_file_scope')
                entry = self.repository._run(repository, ['ls-tree', '-z', source['commit_oid'], '--', relative])
                if not entry or entry.split(b' ', 1)[0] not in {b'100644', b'100755'}:
                    raise ValueError('untracked_or_nonregular_scope')
                evidence.append((relative, entry.decode()))
            attempts = {attempt['id']: attempt for attempt in state['attempt']}
            for record in state['supervised_attempt']:
                self.recovery._verify_process(record, attempts)
            return canonical_digest(evidence)
        except (OSError, ValueError, KeyError, TypeError, DomainError) as error:
            raise _error('源码快照、文件、Git 祖先或旧进程停止证据不一致，未修改任务。',
                         'review_repair_source_invalid') from error

    async def schedule(self, run_id, payload, key):
        request = ReviewSourceRepairRequest.model_validate(payload)
        identity = str(uuid5(NAMESPACE_URL, f'owner-review-source-repair:{run_id}:{key}'))
        command = {'run_id': run_id, **request.model_dump()}
        try:
            if await self.store.read('review_source_repair', identity):
                return await self.store.command('run.review_source_repair', key, command, lambda tx: {})
            state = await self._read(run_id)
            proof = self._proof(state, request)
            blockers = self.recovery._common_blockers(state) + await self.recovery._process_blockers(state)
            for work_id in proof['snapshots']:
                blockers += await coding_usage_blockers(state, self.recovery.data_dir, work_id)
            if any(budget.get('uncertain') is not False for budget in state['coding_work_budget']):
                blockers.append({'code': 'coding_budget_uncertain', 'message': '历史编码工作的累计用量仍未知，不能以新修复绕过核对。'})
            if blockers:
                raise DomainError(blockers[0]['code'], blockers[0]['message'], details=blockers)
            filesystem = await asyncio.to_thread(self._filesystem_proof, state, proof)
            snapshot_id = str(uuid5(NAMESPACE_URL, identity + ':source'))
            def apply(tx):
                current = _related({kind: tx.list(kind) for kind in _KINDS}, run_id)
                if canonical_digest(current) != canonical_digest(state):
                    raise _error('修复核验期间状态已变化，请使用最新运行版本重试。', 'revision_conflict')
                if self._filesystem_proof(current, proof) != filesystem:
                    raise _error('修复核验期间文件证据已变化。', 'review_repair_source_invalid')
                run, source, reviewer, producer = proof['run'], proof['source'], proof['reviewer'], proof['producer']
                instruction = ('Owner-authorized scoped source repair. Preserve completed producer code, test plans, '
                    'product goals, platforms and acceptance criteria. Fix all blocking findings in the listed files only. '
                    'Correct test implementation mistakes only where explicitly authorized; never weaken assertions, '
                    'skip tests or fabricate passing evidence. Owner reason: ' + request.reason + '\nBlocking findings:\n'
                    + json.dumps(proof['review']['blocking_findings'], ensure_ascii=False))
                affected = self.workflow._invalidate(tx, proof['items'], proof['review_roots'],
                    'Re-review the owner-authorized source repair and verify all findings and test assertions independently.', expand_roots=False)
                tx.put('code_snapshot', snapshot_id, {'run_id': run_id, 'work_item_id': identity, 'generation': 0,
                    'repository_path': source['repository_path'], 'commit_oid': source['commit_oid'], 'tree_oid': source['tree_oid'],
                    'base_oid': source['commit_oid'], 'parent_commit_oids': source.get('parent_commit_oids', []), 'stale': False,
                    'source_snapshot_id': source['id'], 'source_review_attempt_id': reviewer['attempt_id'], 'owner_repair_id': identity})
                tx.put('work_item', identity, {'run_id': run_id, 'project_id': run['project_id'], 'key': 'owner-review-repair-' + identity,
                    'step': 'implementation', 'role': 'development', 'dependencies': [producer['id']], 'write_paths': proof['paths'],
                    'generation': 1, 'fencing_token': 0, 'status': 'pending', 'quality_result': 'unknown', 'required': True,
                    'attempt_id': None, 'artifact_ids': [], 'input_fingerprint': run['input_fingerprint'],
                    'policy_fingerprint': producer['policy_fingerprint'],
                    'approval_required': 'implementation' in proof['plan'].get('approval_steps', []),
                    'payload': {'repair_base_snapshot_id': snapshot_id, 'owner_review_source_repair_id': identity,
                                'change_expectation': instruction}})
                current_review = tx.get('work_item', reviewer['id'])
                group_binding = None
                if proof['review_group']:
                    group = proof['review_group']
                    group_binding = {'stage_expansion_id': group['expansion']['id'],
                        'expansion_fingerprint': group['expansion']['input_fingerprint'],
                        'previous_producer_work_item_id': producer['id'],
                        'previous_binding_receipt_id': reviewer.get('payload', {}).get('owner_review_producer_binding'),
                        'child_ids': sorted(child['id'] for child in group['children']),
                        'minimum_stage_generation': current_review['generation'],
                        'minimum_child_generations': {child['id']: tx.get('work_item', child['id'])['generation'] for child in group['children']},
                        'accepted_child_review_ids': {child['id']: child['attempt_id'] for child in group['children']}}
                    for child in group['children']:
                        revised = tx.get('work_item', child['id'])
                        tx.put('work_item', child['id'], {**revised, 'dependencies': [identity]}, revised['revision'])
                    tx.put('work_item', reviewer['id'], {**current_review, 'original_dependencies': [identity],
                        'payload': {**current_review.get('payload', {}), 'owner_review_producer_binding': identity}}, current_review['revision'])
                else:
                    tx.put('work_item', reviewer['id'], {**current_review, 'dependencies': [identity]}, current_review['revision'])
                tx.put('run', run_id, {**run, 'quality_result': 'unknown', 'blocking_reasons': [],
                    'input_fingerprint': canonical_digest({'prior': run['input_fingerprint'], 'owner_review_source_repair_id': identity})}, run['revision'])
                receipt = tx.put('review_source_repair', identity, {'run_id': run_id, 'iteration_id': run['iteration_id'],
                    'actor': 'owner', 'repair_work_item_id': identity, 'review_work_item_id': reviewer['id'],
                    'review_attempt_id': reviewer['attempt_id'], 'producer_work_item_id': producer['id'],
                    'source_snapshot_id': source['id'], 'source_commit': source['commit_oid'], 'snapshot_id': snapshot_id,
                    'write_paths': proof['paths'], 'scope_owners': proof['scope_owners'], 'reason': request.reason,
                    **({'review_group_binding': group_binding} if group_binding else {}),
                    'affected_work_item_ids': sorted(affected),
                    'preserved_work_item_ids': sorted(item['id'] for item in proof['items'] if item['id'] not in affected),
                    'created_at': utc_now(), 'run': tx.get('run', run_id)})
                tx.event('run.review_source_repair_scheduled', {k: v for k, v in receipt.items() if k != 'run'}, run_id=run_id)
                return receipt
            return await self.store.command('run.review_source_repair', key, command, apply)
        except DomainError:
            if await self.store.read('review_source_repair', identity):
                return await self.store.command('run.review_source_repair', key, command, lambda tx: {})
            raise

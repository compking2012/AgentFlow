"""Evidence analysis followed by bounded recovery; never changes runtime authority."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from uuid import uuid4

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.failure_messages import failure_display_message
from agentflow.control.planning_recovery import planning_failure_diagnostic, planning_state_diagnostic
from agentflow.control.recovery import (
    KINDS,
    RunRecoveryService,
    _ReadState,
    _related,
    validate_recovery_checkpoint,
)
from agentflow.control.remediation import (
    ReviewRemediation,
    review_repair_allowed,
    review_repair_count,
    review_repair_manual_blockers,
    review_repair_target,
)
from agentflow.control.review_producer import pending_review_peers
from agentflow.domain.planning import CODING_STEPS, EXECUTION_STEPS, ROLES
from agentflow.repository import RepositoryAdapter
from agentflow.runtime.failures import (
    known_failure_reason,
    read_frozen_codex_failure,
    read_role_failure,
    refine_codex_failure,
    runtime_failure_code,
)
from agentflow.runtime.trace import ExecutionTrace

# These instructions change neither the selected model nor the accepted scope.
# A retry always uses a fresh attempt and the independently verified checkpoint.
_RETRY = {
    'review_disposition_invalid': '从原审查、已接受需求和冻结断言清单重新完成归属分析，修正具体校验问题，不改变验收要求。',
    'test_migration_invalid': '从原受管检查点核对已批准迁移，仅精确替换指定期望参数；恢复被意外修改的其它字符、断言操作和用例。',
    'test_coverage_invalid': '对照冻结源码恢复被改动的原语句、断言、用例及配置，仅在现有用例中追加已批准的测试覆盖与必要的新助手；保留原作者权限、方案、累计用量及正式门禁。',
    'launcher_startup_timeout': '启动许可已失效且启动器已确认停止；在原模型、请求额度和已核验检查点下重新启动当前步骤。',
    'invalid_model_output': '依据本阶段既定结果格式重新完成输出，保留已完成的有效工作。',
    'final_schema_invalid': '从已保留的代码检查点继续，只补齐既定最终结果格式及必要的未完成工作。',
    'final_output_missing': '从已核验的检查点继续，并按本阶段约定生成完整最终结果。',
    'model_output_limit': '将当前任务内部的编辑拆成更小步骤，及时保存有效改动，并保持最终结果简短完整。',
    'model_connection_failed': '连接失败后重新执行当前步骤，复用已核验的产物和代码。',
    'model_rate_limited': '在有界等待后重新执行当前步骤，保持既有模型与请求额度。',
    'worker_exited': '旧执行已确认结束，从其已核验的检查点重新完成当前步骤。',
    'worker_internal_error': '核验原进程已结束和已保存产物后，使用新的受管执行重试当前步骤，保留既有成果、模型与执行额度。',
    'worker_timeout': '将本次工作组织成更小的内部步骤，从已核验的检查点继续。',
    'worker_log_limit': '保留必要诊断，减少重复输出，从已核验的检查点继续。',
    'isolation_probe_timeout': '重新进行启动前隔离检查，通过后再执行当前步骤。',
    'no_code_changes': (
        '先核对已保存的实现、审查意见与实际需要修改的文件；已有实现由平台核验后交独立复审。'
        ' 确有未完成工作时，在原授权范围内完成最小正确改动；不要用空注释或无关格式调整制造差异。'),
    'invalid_task_plan': '重新核对既定阶段及任务依赖，只引用当前计划中明确存在的阶段。',
    'role_tool_transcript_invalid': '旧角色对话的工具调用与结果消息未配对；使用新的角色对话重新完成当前步骤，保留原输入与已核验草稿。',
    'review_identity_mismatch': '重新审查控制器提供的准确代码版本，并在结果中返回完全一致的版本标识。',
}
_ROLE_PROTOCOL_STEPS = frozenset(ROLES) - CODING_STEPS - EXECUTION_STEPS - {'delivery'}
_UNCERTAIN = {'execution_unknown', 'execution_unconfirmed', 'execution_receipt_missing', 'terminal_event_missing'}
_STATE_FIELDS = ('id', 'run_id', 'project_id', 'step', 'generation', 'fencing_token',
                 'input_fingerprint', 'attempt_id', 'status', 'quality_result', 'runtime_failure_code',
                 'blocking_reason', 'failure_diagnostic', 'dependencies', 'write_paths', 'approval_required', 'payload')
_BOUNDED_CODING = 'bounded_coding_recovery'
_BOUNDED_BINDING_FIELDS = {'recovery_id', 'analysis_id', 'run_id', 'work_item_id', 'generation', 'plan_digest'}
_BOUNDED_RETRY = {
    'no_code_changes': _RETRY['no_code_changes'],
    'reasoning_output_limit': '保留当前模型、明确推理设置和单次输出上限，在有限响应内按更小步骤继续整个原任务。',
    'coding_no_progress': (
        '从已核验的代码检查点分析上一步未产生改动的原因，先核对原获准文件路径是否实际可写、所需父目录是否已存在。'
        ' 对照开发计划、上游文档和已保存代码，选择本任务范围内一个尚未完成的小改动并实际实现。'
        ' 不重复失败的权限操作，不修改权限或扩大写入范围；若获准路径仍不可写，报告具体路径和错误并停止。'),
}


def _valid_bounded_plan(plan):
    if not isinstance(plan, dict):
        return False
    ordinal = plan.get('round')
    return (type(plan.get('version')) is int and plan['version'] == 1
            and plan.get('strategy') == 'bounded_steps' and plan.get('enforcement') == 'planning_targets'
            and type(ordinal) is int and ordinal >= 1
            and type(plan.get('max_files_per_step')) is int and plan['max_files_per_step'] == 1
            and type(plan.get('max_actions_per_step')) is int and plan['max_actions_per_step'] == max(1, 3 - ordinal)
            and type(plan.get('max_changed_lines_per_step')) is int
            and plan['max_changed_lines_per_step'] == 80 // (2 ** min(ordinal - 1, 2))
            and type(plan.get('step_reduction_factor')) is float
            and plan['step_reduction_factor'] == 0.5 ** min(ordinal, 5)
            and type(plan.get('observed_output_cap')) is int and plan['observed_output_cap'] > 0
            and type(plan.get('no_progress_streak')) is int and 0 <= plan['no_progress_streak'] <= ordinal
            and plan.get('preserve_model_reasoning_and_budgets') is True and isinstance(plan.get('progress'), dict))


async def resolve_bounded_coding_recovery(store, run, work):
    """Return only the controller-authorized finite-step plan for this generation."""
    binding = work.get('payload', {}).get(_BOUNDED_CODING)
    if binding is None:
        return None
    try:
        if (not isinstance(binding, dict) or set(binding) != _BOUNDED_BINDING_FIELDS
                or work.get('archived') or work.get('step') not in CODING_STEPS
                or type(binding.get('generation')) is not int
                or any(binding.get(field) != expected for field, expected in {
                    'run_id': run['id'], 'work_item_id': work['id'], 'generation': work['generation']}.items())):
            raise ValueError('bounded_binding_mismatch')
        receipt = await store.read('run_recovery', binding['recovery_id'])
        analysis = await store.read('failure_analysis', binding['analysis_id'])
        authorization = (receipt or {}).get(_BOUNDED_CODING)
        plan = (analysis or {}).get(_BOUNDED_CODING)
        if (not receipt or receipt.get('actor') != 'system' or receipt.get('mode') != 'retry'
                or receipt.get('run_id') != run['id'] or receipt.get('work_item_id') != work['id']
                or receipt.get('failure_analysis_id') != binding['analysis_id']
                or work['id'] not in receipt.get('affected_work_item_ids', [])
                or not isinstance(authorization, dict) or authorization.get('binding') != binding
                or not analysis or analysis.get('run_id') != run['id'] or analysis.get('work_item_id') != work['id']
                or analysis.get('generation') != work['generation'] - 1
                or analysis.get('status') != 'repair_scheduled' or not _valid_bounded_plan(plan)
                or authorization.get('plan') != plan or binding['plan_digest'] != canonical_digest(plan)
                or plan['progress'].get('attempt_id') != analysis.get('attempt_id')
                or plan['progress'].get('work_item_id') != work['id']
                or plan['progress'].get('generation') != analysis.get('generation')):
            raise ValueError('bounded_receipt_mismatch')
        checkpoint_id = authorization['checkpoint_id']
        if (work.get('payload', {}).get('recovery_checkpoint_id') != checkpoint_id
                or work.get('payload', {}).get('repair_base_snapshot_id') != checkpoint_id):
            raise ValueError('bounded_checkpoint_mismatch')
        checkpoint = await store.read('code_snapshot', checkpoint_id)
        await validate_recovery_checkpoint(store, run, work, checkpoint, RepositoryAdapter(timeout=30))
        if checkpoint.get('tree_oid') != plan['progress']['tree_oid']:
            raise ValueError('bounded_progress_mismatch')
        return {**plan, 'analysis_id': analysis['id'], 'recovery_id': receipt['id'], 'checkpoint_id': checkpoint_id}
    except (KeyError, TypeError, ValueError, DomainError) as error:
        raise DomainError('invalid_bounded_coding_recovery', '分步编码恢复计划与当前代次、检查点或授权回执不一致。') from error


def failure_signature(work, attempt):
    return canonical_digest({'work': {field: work.get(field) for field in _STATE_FIELDS},
                             'attempt': attempt})


def failed_work(work):
    return (not work.get('archived') and (work.get('status') in {'failed', 'blocked', 'execution_unknown'}
            or (work.get('status') == 'completed' and work.get('quality_result') in {'failed', 'inconclusive'})))


def _policy(settings):
    return {'per_work_limit': settings.auto_failure_retry_limit,
            'run_limit': settings.auto_failure_run_limit,
            'timeout_limit': settings.auto_timeout_retry_limit,
            'review_limit': settings.auto_review_repair_limit,
            'delay_seconds': settings.auto_failure_retry_delay_seconds}


def _authorization(record):
    return canonical_digest({key: record.get(key) for key in (
        'run_id', 'work_item_id', 'attempt_id', 'generation', 'failure_signature', 'failure_code',
        'action', 'guard_state_digest', 'policy', 'not_before', 'repair_instruction', _BOUNDED_CODING)})


def _limits(rows, run_id, work_id, policy, timeout_records=(), *, current_attempt_id=None):
    from agentflow.control.timeout_recovery import retry_counts
    counts = retry_counts(rows, timeout_records, run_id, work_id, exclude_attempt_id=current_attempt_id)
    exhausted = []
    for count, limit, label, setting in [
        (counts['work'], policy['per_work_limit'], '本任务', 'app.auto_failure_retry_limit'),
        (counts['run'], policy['run_limit'], '本轮', 'app.auto_failure_run_limit'),
    ]:
        if not limit:
            exhausted.append(f'{label}自动重试已关闭（{setting}=0）')
        elif count >= limit:
            exhausted.append(f'{label}自动重试已用 {count}/{limit} 次（{setting}）')
    if exhausted:
        return [{'code': 'automatic_repair_limit', 'message': '；'.join(exhausted) + '。已保留检查点，可在执行设置查看上限。'}]
    return []


def guard_automatic(tx, workflow, analysis_id, work, action):
    """Validate the analyzed generation and policy in the same writer as invalidation."""
    record = tx.get('failure_analysis', analysis_id)
    attempt = tx.get('attempt', work.get('attempt_id')) if work.get('attempt_id') else None
    run = tx.get('run', work['run_id'])
    policy = _policy(workflow.settings)
    if any(row.get('run_id') == work['run_id'] and row.get('work_item_id') == work['id']
           and row.get('work_generation') == work.get('generation') and row.get('requires_explicit_retry')
           for row in tx.list('model_uncertainty_acknowledgment')):
        raise DomainError('manual_retry_after_model_ack', '未知模型调用已确认保留，请明确选择重试；确认本身不会启动 Agent。')
    if any(row.get('run_id') == work['run_id'] and row.get('work_item_id') == work['id']
           and row.get('work_generation') == work.get('generation') and row.get('requires_explicit_retry')
           for row in tx.list('work_execution_budget_adjustment')):
        raise DomainError('manual_retry_after_budget_change', '工作执行额度已追加，请明确选择重试；保存额度本身不会启动 Agent。')
    if (not record or record.get('actor') != 'controller' or record.get('phase') != 'analysis'
            or record.get('status') != 'ready' or record.get('action') != action
            or record.get('authorization_digest') != _authorization(record)
            or record.get('run_id') != work['run_id'] or record.get('work_item_id') != work['id']
            or record.get('attempt_id') != work.get('attempt_id') or record.get('generation') != work.get('generation')
            or record.get('failure_signature') != failure_signature(work, attempt)
            or record.get('policy') != policy or not failed_work(work)
            or not run or run.get('execution_state') != 'running' or run.get('restore_reconciliation_required')
            or work.get('project_id') != run.get('project_id')
            or type(record.get('not_before')) not in {int, float} or time.time() < record['not_before']):
        raise DomainError('automatic_repair_stale', '失败分析与当前任务、运行或恢复策略不一致，未开始重试。')
    blockers = [] if action == 'repair_review_findings' else _limits(tx.list('failure_analysis'), run['id'], work['id'], policy,
                       tx.list('timeout_recovery'), current_attempt_id=attempt['id'])
    if record.get('failure_code') == 'worker_timeout':
        from agentflow.control.timeout_recovery import retry_counts
        counts = retry_counts(tx.list('failure_analysis'), tx.list('timeout_recovery'), run['id'], work['id'],
                              exclude_attempt_id=attempt['id'])
        if not policy['timeout_limit'] or counts['timeouts_work'] >= policy['timeout_limit']:
            blockers.append({'code': 'automatic_timeout_retry_limit', 'message': '超时自动续跑已关闭或达到上限。'})
    if blockers:
        raise DomainError(blockers[0]['code'], blockers[0]['message'])
    current = _related({kind: tx.list(kind) for kind in KINDS}, run['id'])
    if canonical_digest(current) != record.get('guard_state_digest'):
        raise DomainError('automatic_repair_stale', '分析之后执行、预算或代码证据发生变化，需要重新分析。')
    return record


def finish_automatic(tx, analysis_id, receipt, *, kind):
    record = tx.get('failure_analysis', analysis_id)
    if not record or record.get('status') != 'ready':
        raise DomainError('automatic_repair_stale', '自动修复缺少当前有效分析。')
    updated = tx.put('failure_analysis', analysis_id, {**record, 'phase': 'repair', 'status': 'repair_scheduled',
        'repair_receipt_kind': kind, 'repair_receipt_id': receipt['id'],
        'affected_work_item_ids': receipt.get('affected_work_item_ids', []),
        'repair_work_item_ids': receipt.get('batch_repair_work_item_ids',
            receipt.get('repair_work_item_ids', record.get('repair_work_item_ids', []))),
        'preserved_work_item_ids': sorted(work['id'] for work in tx.list('work_item')
            if work.get('run_id') == record['run_id'] and not work.get('archived')
            and work['id'] not in receipt.get('affected_work_item_ids', [])),
        'repair_scheduled_at': utc_now(), 'blockers': []}, record['revision'])
    tx.event('failure.repair_scheduled', {'analysis_id': analysis_id, 'work_item_id': record['work_item_id'],
        'attempt_id': record['attempt_id'], 'repair_receipt_kind': kind, 'repair_receipt_id': receipt['id'],
        'affected_work_item_ids': updated['affected_work_item_ids']}, run_id=record['run_id'])
    return updated


class FailureRemediation:
    def __init__(self, store, workflow, *, recovery=None, review=None, preflight=None, models=None):
        self.store, self.workflow = store, workflow
        self.recovery = recovery or RunRecoveryService(store, workflow)
        self.review = review or ReviewRemediation(store, workflow)
        self.preflight = preflight
        from agentflow.control.timeout_recovery import TimeoutRecovery
        self.timeout_recovery = TimeoutRecovery(store, workflow, self.recovery, models)
        self.trace = ExecutionTrace(store)
        self._event_cursor = 0
        self._initialized = False
        self._deferred = {}

    async def _bounded_coding_plan(self, state, work, attempt, target, code):
        context = next((row for row in state['dispatch_context'] if row['id'] == attempt['id']), {})
        cap = context.get('task', {}).get('max_output_tokens')
        if type(cap) is not int or cap < 1:
            raise DomainError('bounded_output_evidence_invalid', '无法核验本次有限输出上限，不能自动改变执行策略。')
        if code == 'reasoning_output_limit':
            verified = await refine_codex_failure(self.store, self.workflow.settings.data_dir, attempt['id'],
                fencing_token=work['fencing_token'], input_fingerprint=work['input_fingerprint'], fallback='model_output_limit')
            if verified != 'reasoning_output_limit':
                raise DomainError('bounded_output_evidence_invalid', '缺少已核验的推理输出耗尽回执，保留现场等待核对。')
            calls = [row for row in state['model_invocation'] if row.get('attempt_id') == attempt['id']]
            if not calls:
                raise DomainError('bounded_output_evidence_invalid', '分析期间模型调用记录发生变化，需要重新核验。')
            latest = max(calls, key=lambda row: datetime.fromisoformat(row['created_at']).timestamp())
            if not isinstance(latest.get('usage'), dict) or latest['usage'].get('output_tokens') != cap:
                raise DomainError('bounded_output_evidence_invalid', '耗尽回执与已冻结的单次输出上限不一致，不能自动恢复。')
        progress = await self.recovery.coding_progress(state, target)
        prior = await resolve_bounded_coding_recovery(self.store, state['run'][0], work)
        stagnant = 0 if progress['has_code_changes'] else (prior.get('no_progress_streak', 0) if prior else 0) + 1
        history = [row for row in await self.store.list('failure_analysis')
            if row.get('run_id') == work['run_id'] and row.get('work_item_id') == work['id']
            and row.get('status') == 'repair_scheduled' and row.get(_BOUNDED_CODING)]
        ordinal = len(history) + 1
        next_action = _BOUNDED_RETRY[code]
        if stagnant >= 2:
            next_action += (' 连续未形成新进展，先核对上一轮失败工具的具体结果和修改责任，选择不同的处理方法；'
                            '不要原样重复失败命令，已完成的有效代码保留。')
        # Keep useful step targets after the first reductions. Retry policy,
        # evidence and remaining budgets decide whether another round is allowed.
        return {'version': 1, 'strategy': 'bounded_steps', 'enforcement': 'planning_targets',
            'round': ordinal, 'step_reduction_factor': 0.5 ** min(ordinal, 5), 'max_files_per_step': 1,
            'max_actions_per_step': max(1, 3 - ordinal), 'max_changed_lines_per_step': 80 // (2 ** min(ordinal - 1, 2)),
            'observed_output_cap': cap, 'no_progress_streak': stagnant, 'progress': progress,
            'preserve_model_reasoning_and_budgets': True,
            'next_action': next_action}

    async def _code(self, work, attempt):
        work_code = runtime_failure_code(work.get('runtime_failure_code'))
        attempt_code = runtime_failure_code(attempt.get('runtime_failure_code'))
        fallback_codes = {None, 'planning_validation_failed', 'planning_state_changed', 'work_blocked', 'controller_validation_failed'}
        code = attempt_code if work_code in fallback_codes and attempt_code not in fallback_codes else work_code or attempt_code
        if (work.get('status') == 'execution_unknown' or attempt.get('status') == 'execution_unknown') and code in fallback_codes:
            return 'execution_unconfirmed'
        code = code or known_failure_reason(work.get('blocking_reason'))
        if code and code not in {'planning_validation_failed', 'planning_state_changed', 'work_blocked', 'controller_validation_failed'}:
            return await self._refine_coding_code(code, work, attempt)
        if planning_state_diagnostic(work, attempt):
            return 'planning_state_changed'
        if await planning_failure_diagnostic(self.store, self.workflow.settings, work, attempt):
            return 'planning_validation_failed'
        if code in {'planning_validation_failed', 'planning_state_changed', 'controller_validation_failed'} or work.get('failure_diagnostic') or attempt.get('failure_diagnostic'):
            return 'controller_validation_failed'
        if work.get('status') == 'execution_unknown' or attempt.get('status') == 'execution_unknown':
            return 'execution_unconfirmed'
        if work.get('status') == 'completed':
            if work.get('step') == 'code_review' and work.get('quality_result') == 'failed':
                return 'review_failed'
            return 'test_failed' if work.get('step') in EXECUTION_STEPS else 'quality_failed'
        if work.get('blocking_reason') == 'Parallel code branches require reviewed candidate assembly':
            return 'source_assembly_required'
        if work.get('attempt_id') and attempt.get('id'):
            if work.get('step') in CODING_STEPS:
                code = await read_frozen_codex_failure(self.store, self.workflow.settings.data_dir, attempt['id'],
                    run_id=work['run_id'], work_item_id=work['id'], fencing_token=work['fencing_token'],
                    input_fingerprint=work['input_fingerprint'])
            else:
                code = await asyncio.to_thread(read_role_failure, self.workflow.settings.data_dir, attempt['id'])
        return await self._refine_coding_code(code, work, attempt) or 'controller_validation_failed'

    async def _refine_coding_code(self, code, work, attempt):
        if code in {'worker_exited', 'worker_internal_error', 'model_authentication_failed', 'model_request_failed'} and attempt.get('id'):
            from agentflow.runtime.task_authorization import authorization_failure
            code = await authorization_failure(self.store, self.workflow.settings.data_dir, attempt['id'],
                fencing_token=work['fencing_token'], input_fingerprint=work['input_fingerprint']) or code
        if (code == 'model_request_failed' and work.get('step') in _ROLE_PROTOCOL_STEPS
                and work.get('role') == ROLES.get(work.get('step')) and attempt.get('id')):
            refined = await asyncio.to_thread(read_role_failure, self.workflow.settings.data_dir, attempt['id'])
            if refined == 'role_tool_transcript_invalid':
                return refined
        if code in {'worker_exited', 'worker_internal_error', 'model_authentication_failed', 'model_request_failed'} and attempt.get('id'):
            from agentflow.models.transport_failures import transport_failure_for_attempt
            code = await transport_failure_for_attempt(self.store, attempt['id'],
                fencing_token=work['fencing_token'], input_fingerprint=work['input_fingerprint']) or code
        if code == 'model_output_limit' and work.get('step') in CODING_STEPS and attempt.get('id'):
            return await refine_codex_failure(self.store, self.workflow.settings.data_dir, attempt['id'],
                fencing_token=work['fencing_token'], input_fingerprint=work['input_fingerprint'], fallback=code)
        return code

    async def analyze(self, work_item_id):
        work = await self.store.read('work_item', work_item_id)
        if not work or not failed_work(work):
            return None
        state = await self.recovery._read(work['run_id'])
        run = state['run'][0]
        work = next((row for row in state['work_item'] if row['id'] == work_item_id), None)
        if not work or not failed_work(work) or run.get('execution_state') != 'running':
            return None
        attempt = next((row for row in state['attempt'] if row['id'] == work.get('attempt_id')), {})
        identity = 'failure-analysis-' + canonical_digest({'run_id': run['id'], 'work_item_id': work['id'],
            'attempt_id': work.get('attempt_id'), 'generation': work['generation']}).split(':')[1]
        previous = await self.store.read('failure_analysis', identity)
        if previous and previous.get('status') == 'repair_scheduled':
            if previous.get('repair_receipt_kind') == 'review_contract_repair':
                from agentflow.control.review_contract_view import review_contract_outcome
                batch = await self.store.read('review_contract_repair', previous['repair_receipt_id'])
                outcome = review_contract_outcome(batch or {})
                if outcome['blockers']:
                    self._deferred.pop(work['id'], None)
                    return await self._block(previous, work['id'], outcome['blockers'], allow_scheduled=True)
            return previous
        code = await self._code(work, attempt)
        diagnostic = (await planning_failure_diagnostic(self.store, self.workflow.settings, work, attempt)
            if code == 'planning_validation_failed' else None)
        diagnostic = diagnostic or work.get('failure_diagnostic') or attempt.get('failure_diagnostic')
        if not diagnostic and work.get('blocking_reason'):
            diagnostic = {'code': code, 'message': work['blocking_reason']}
        policy = _policy(self.workflow.settings)
        analyses, timeout_records = await asyncio.gather(self.store.list('failure_analysis'), self.store.list('timeout_recovery'))
        blockers = [] if code == 'review_failed' else _limits(analyses, run['id'], work['id'], policy, timeout_records,
                           current_attempt_id=attempt.get('id'))
        if code == 'worker_timeout':
            from agentflow.control.timeout_recovery import retry_counts
            counts = retry_counts(analyses, timeout_records, run['id'], work['id'], exclude_attempt_id=attempt.get('id'))
            if not policy['timeout_limit'] or counts['timeouts_work'] >= policy['timeout_limit']:
                blockers.append({'code': 'automatic_timeout_retry_limit', 'message': '超时自动续跑已关闭或达到上限。'})
        if any(row.get('run_id') == run['id'] and row.get('work_item_id') == work['id']
               and row.get('work_generation') == work['generation'] and row.get('requires_explicit_retry')
               for row in await self.store.list('model_uncertainty_acknowledgment')):
            blockers.append({'code': 'manual_retry_after_model_ack',
                'message': '未知模型调用已确认保留，请明确选择重试；确认本身不会启动 Agent。'})
        if any(row.get('run_id') == run['id'] and row.get('work_item_id') == work['id']
               and row.get('work_generation') == work['generation'] and row.get('requires_explicit_retry')
               for row in await self.store.list('work_execution_budget_adjustment')):
            blockers.append({'code': 'manual_retry_after_budget_change',
                'message': '工作执行额度已追加，请明确选择重试；保存额度本身不会启动 Agent。'})
        if (not attempt or attempt.get('run_id') != run['id'] or attempt.get('work_item_id') != work['id']
                or any(attempt.get(field) != work.get(field) for field in ('generation', 'fencing_token', 'input_fingerprint'))
                or work.get('project_id') != run.get('project_id')):
            blockers.append({'code': 'recovery_evidence_invalid', 'message': '失败任务与执行身份不一致，不能自动重试。'})
        action, instruction, bounded = 'needs_attention', '', None
        bounded_candidate = code in _BOUNDED_RETRY and work.get('step') in CODING_STEPS
        if code == 'planning_state_changed' and planning_state_diagnostic(work, attempt):
            action = 'retry_current'
            instruction = ('重新读取当前规划契约、目标阶段版本及前序产物，再核验已保存草稿。'
                '核验并发状态变化后的规划，只针对仍可展开的阶段重新提交计划。')
        elif code == 'planning_validation_failed' and diagnostic:
            action = 'retry_current'
            instruction = ('从已保存的规划草稿继续，仅修正以下字段错误，重新提交完整计划；保留已完成上游产物。\n'
                + json.dumps(diagnostic['details']['issues'], ensure_ascii=False, separators=(',', ':')))
        elif code in _RETRY and (code != 'role_tool_transcript_invalid'
                               or work.get('step') in _ROLE_PROTOCOL_STEPS
                               and work.get('role') == ROLES.get(work.get('step'))):
            action, instruction = 'retry_current', _RETRY[code]
            if code in {'review_disposition_invalid', 'test_migration_invalid', 'test_coverage_invalid'} and isinstance(diagnostic, dict):
                instruction += '\n具体校验问题：' + json.dumps(diagnostic.get('details'), ensure_ascii=False)
        elif bounded_candidate:
            action = 'retry_current'
            instruction = _BOUNDED_RETRY[code]
        elif code == 'review_failed':
            action, instruction = 'repair_review_findings', '依据独立审查的阻塞意见，仅修复原授权范围内的问题，再重新审查和验证。'
            reviewed = next((row for row in state['review'] if row['id'] == work.get('attempt_id')), None)
            if (not reviewed or reviewed.get('stale') or reviewed.get('run_id') != run['id']
                    or reviewed.get('work_item_id') != work['id'] or reviewed.get('generation') != work['generation']
                    or reviewed.get('quality_result') != 'failed' or not reviewed.get('blocking_findings')):
                blockers.append({'code': 'review_evidence_invalid', 'message': '审查失败证据与当前任务或版本不一致，不能自动修复。'})
            repairs = [row for row in await self.store.list('review_repair') if row.get('run_id') == run['id']]
            if not review_repair_allowed(policy['review_limit'],
                    review_repair_count(repairs, {row['id']: row for row in state['attempt']}, work['id'])):
                blockers.append({'code': 'review_repair_limit', 'message': '自动审查返工已关闭或达到配置的次数上限。'})
        elif code in {'source_assembly_required', 'assembly_required'} and self.preflight:
            try:
                if await self.preflight(run, work) is True:
                    action, instruction = 'retry_current', '代码来源已重新核验，只重新启动当前审查或执行步骤。'
            except (DomainError, OSError, ValueError):
                pass
        if code in _UNCERTAIN:
            blockers.append({'code': 'execution_unknown', 'message': '旧执行结果尚未确认，不能创建重复执行。'})
        if run.get('restore_reconciliation_required'):
            blockers.append({'code': 'recovery_restore_uncertain', 'message': '恢复的数据尚未完成核对，不能自动重试。'})
        if action == 'needs_attention':
            blockers.append({'code': 'analysis_requires_attention', 'message': (
                '测试或质量失败尚无经核验的问题归属；需要进一步分析，不能修改断言或盲目重复测试。'
                if code in {'test_failed', 'quality_failed'} else '该错误需要处理执行前提或核验证据，自动恢复不会更改权限、模型配置或预算。')})
        # Reuse the same process, budget, restore, and product-scope checks as owner recovery.
        blockers += self.recovery._common_blockers(state)
        blockers += await self.recovery._process_blockers(state)
        target = next((target for target in self.recovery._targets(state) if target['work_item_id'] == work['id']), None)
        if code == 'review_failed':
            target = review_repair_target(_ReadState(state), run, work)
            if target is None:
                from agentflow.control.late_test_review import late_test_refusal
                refusal = late_test_refusal(_ReadState(state), run, work)
                if refusal:
                    blockers.append(refusal)
            blockers += review_repair_manual_blockers(_ReadState(state), run, target)
            if pending_review_peers(_ReadState(state), run, work):
                blockers.append({'code': 'review_peers_incomplete',
                    'message': '等待当前并行审查全部完成后一次返工；尚未开始或尚未完成的审查会继续执行。'})
                self._deferred[work['id']] = time.time() + max(1, policy['delay_seconds'])
            if target and any(not review_repair_allowed(policy['review_limit'],
                    review_repair_count(repairs, {row['id']: row for row in state['attempt']}, identity))
                    for identity in target.get('review_work_item_ids', [])):
                if not any(row['code'] == 'review_repair_limit' for row in blockers):
                    blockers.append({'code': 'review_repair_limit',
                        'message': '本批并行审查中存在已达到返工次数上限的任务，未安排部分返工。'})
        blockers += await self.recovery._target_blockers(state, target)
        affected = set(target['affected_work_item_ids']) if target else {work['id']}
        if any(item.get('status') == 'waiting_approval' for item in state['work_item'] if item['id'] in affected):
            blockers.append({'code': 'human_approval_pending', 'message': '受影响任务正在等待人工审批，自动恢复不能替代该决定。'})
        if bounded_candidate and not blockers and target:
            try:
                bounded = await self._bounded_coding_plan(state, work, attempt, target, code)
                instruction += (f" 每次仅处理一个小功能或少量用例，以一个文件、{bounded['max_actions_per_step']}个目标动作、"
                    f"{bounded['max_changed_lines_per_step']}行以内改动为规划目标；这些目标不替代原权限与预算硬限制。"
                    ' 小步完成后报告continue和明确next_action，只有全部原任务与验收项完成才报告complete；不要重复长篇规划或重做成功内容。')
            except DomainError as error:
                blockers.append({'code': error.code, 'message': error.message})
        if target and action == 'retry_current':
            try:
                await self.recovery._checkpoints(state, target)
            except DomainError as error:
                blockers.append({'code': error.code, 'message': error.message})
        if action == 'retry_current' and not target:
            blockers.append({'code': 'work_not_retryable', 'message': '当前任务不具备明确的恢复目标。'})
        contract_outcome = None
        if code == 'review_failed':
            from agentflow.control.review_contract_view import review_contract_outcome
            stage_id = work.get('parent_stage_id') or work['id']
            batches = [batch for batch in await self.store.list('review_contract_repair')
                       if batch.get('run_id') == run['id'] and batch.get('stage_id') == stage_id
                       and batch.get('state') in {'needs_attention', 'awaiting_approval', 'diagnostic_failed'}
                       and any(review.get('id') == work.get('attempt_id')
                               for review in batch.get('context', {}).get('reviews', []))]
            if batches:
                batch = max(batches, key=lambda row: (row.get('created_at', ''), row.get('revision', 0), row['id']))
                contract_outcome = review_contract_outcome(batch)
                blockers.extend(contract_outcome['blockers'])
        not_before = previous.get('not_before') if previous else None
        timeout_allowance_pending = (code == 'worker_timeout' and blockers and all(
            row['code'] in {'coding_budget_exhausted', 'recovery_budget_uncertain'} for row in blockers))
        # The timeout allowance is part of the retry decision. Its reservation
        # must wait for the same cooldown as the later fresh execution.
        retry_eligible = action in {'retry_current', 'repair_review_findings'} and (not blockers or timeout_allowance_pending)
        if not retry_eligible:
            not_before = None
        elif action == 'repair_review_findings' and target is None:
            # Read-only triage is new analysis, not a retry of failed execution.
            not_before = time.time()
        elif type(not_before) not in {int, float}:
            not_before = time.time() + policy['delay_seconds']
        if retry_eligible and time.time() < not_before:
            blockers.append({'code': 'retry_backoff', 'message': '失败原因已分析，等待短暂冷却后重新核验并恢复。'})
            self._deferred[work['id']] = not_before
        else:
            self._deferred.pop(work['id'], None)
        by_id = {item['id']: item for item in state['work_item'] if not item.get('archived')}
        summary = failure_display_message(code, diagnostic) or ('代码来源存在尚未汇总的分支关系，当前 Agent 尚未开始执行。'
            if code in {'source_assembly_required', 'assembly_required'} else '当前阶段未完成，需要进一步核验失败证据。')
        if code == 'review_failed':
            active_reviews = [item for item in state['work_item'] if item['id'] != work['id']
                and not item.get('archived') and item.get('step') == 'code_review'
                and item.get('dependencies') == work.get('dependencies')
                and item.get('status') in {'running', 'waiting_execution', 'cancel_requested'}]
            if active_reviews or any(row['code'] == 'review_peers_incomplete' for row in blockers):
                summary = '代码审查发现阻塞问题，等待并行审查结束后返工；结束后会自动重新核验当前版本、执行额度与审批门禁。'
                blockers = [({**row, 'message': '等待并行审查结束后返工，不会修改正在审查的源码或重复启动执行。'}
                    if row['code'] == 'active_work' else row) for row in blockers]
            if any(row['code'] in {'active_work', 'execution_unknown', 'recovery_budget_uncertain',
                                   'review_peers_incomplete'} for row in blockers) and not contract_outcome:
                # Process/transport completion can happen without a Store event.
                # Re-evaluate evidence on a timer; a wait never authorizes repair.
                self._deferred[work['id']] = time.time() + max(1, policy['delay_seconds'])
        if bounded_candidate and code == 'reasoning_output_limit':
            summary = '单次有限输出被推理耗尽，未形成可用文本或工具调用；需要核验已保留代码并减小下一步任务。'
        elif bounded_candidate:
            summary = '本次编码小步没有新增代码进展；核验检查点、原获准路径及剩余开发计划后，有界重试一个更小的改动。'
        record = {'run_id': run['id'], 'work_item_id': work['id'], 'attempt_id': work.get('attempt_id'),
            'generation': work['generation'], 'step': work['step'], 'actor': 'controller', 'phase': 'analysis',
            'status': 'blocked' if blockers else 'ready', 'failure_code': code, 'summary': summary,
            **({'failure_diagnostic': diagnostic} if diagnostic else {}),
            'action': action, 'repair_instruction': instruction + (
                ' 保留成功上游及同级任务、原验收标准、测试断言、文件权限、模型设置和人工审批；禁止扩大预算或发布。' if instruction else ''),
            **({_BOUNDED_CODING: bounded} if bounded else {}),
            'analysis_steps': ['核对失败执行与当前任务的身份。', '检查前序产物、代码检查点、进程结束证据及预算。',
                               '区分需要分析的问题和可以恢复的步骤，仅重做受影响的依赖分支。'],
            'upstream_work_item_ids': list(work.get('dependencies', [])),
            'upstream_steps': [by_id[key]['step'] for key in work.get('dependencies', []) if key in by_id],
            'repair_work_item_ids': ([work['id']] if action == 'retry_current' else
                                     target['root_work_item_ids'] if target and action == 'repair_review_findings' else []),
            'affected_work_item_ids': sorted(affected),
            'preserved_work_item_ids': sorted(set(by_id) - affected),
            'failure_signature': failure_signature(work, attempt), 'guard_state_digest': canonical_digest(state),
            'observed_run_revision': run['revision'], 'policy': policy, 'not_before': not_before,
            'blockers': list({blocker['code']: blocker for blocker in blockers}.values()),
            'evidence': {'attempt_status': attempt.get('status'),
                'supervisor_present': any(row['id'] == work.get('attempt_id') for row in state['supervised_attempt']),
                'dispatch_context_present': any(row['id'] == work.get('attempt_id') for row in state['dispatch_context']),
                'model_invocation_count': sum(row.get('attempt_id') == work.get('attempt_id') for row in state['model_invocation'])}}
        record['authorization_digest'] = _authorization(record)

        def save(tx):
            current_work = tx.get('work_item', work['id'])
            current_attempt = tx.get('attempt', attempt['id']) if attempt else {}
            current_run = tx.get('run', run['id'])
            if (not current_work or current_run.get('execution_state') != 'running'
                    or failure_signature(current_work, current_attempt) != record['failure_signature']):
                return {'analyzed': False, 'reason': 'state_changed'}
            old = tx.get('failure_analysis', identity)
            if old and old.get('status') == 'repair_scheduled':
                return old
            if old and all(old.get(key) == value for key, value in record.items()):
                return old
            result = tx.put('failure_analysis', identity, {**record,
                'created_at': old['created_at'] if old else utc_now(), 'analyzed_at': utc_now()},
                old['revision'] if old else None)
            tx.event('failure.analysis_completed', {'analysis_id': identity, 'work_item_id': work['id'],
                'attempt_id': work.get('attempt_id'), 'status': result['status'], 'failure_code': code,
                'action': action, 'blocker_codes': [row['code'] for row in result['blockers']]}, run_id=run['id'])
            return result
        result = await self.store.command('failure.analyze', str(uuid4()), {'work_item_id': work['id']}, save)
        if result.get('analyzed') is False:
            return None
        await self._trace(result, 'analysis')
        return result

    async def _trace(self, record, phase):
        if not record or not record.get('attempt_id'):
            return
        evidence = record.get('evidence', {})
        content = '\n'.join([
            '原因：' + record.get('summary', ''),
            '依据：执行状态 ' + str(evidence.get('attempt_status') or '未确认')
                + '；已记录模型调用 ' + str(evidence.get('model_invocation_count', 0)) + ' 次。',
            '前序步骤：' + ('、'.join(record.get('upstream_steps', [])) or '无'),
            '重做指令：' + (record.get('repair_instruction') or '先处理下述阻塞原因，再重新分析。'),
            '阻塞说明：' + ('；'.join(row['message'] for row in record.get('blockers', [])) or '核验通过。'),
        ])
        if record.get('failure_code') == 'worker_timeout':
            allowance = next((row for row in await self.store.list('timeout_recovery')
                              if row.get('attempt_id') == record['attempt_id']), None)
            if allowance:
                content += (f"\n超时续跑：按配置授权第 {allowance['retry_ordinal']} 次重试；"
                            '原执行用量与未知费用保留，不清零、不重发旧请求。')
        title = {'analysis': '失败原因分析', 'repair': '自动修复已安排', 'blocked': '自动修复等待处理'}[phase]
        await self.trace.emit(record['attempt_id'], 'error' if phase == 'blocked' else 'status', title, content,
            key=record['id'] + ':' + phase + ':' + canonical_digest(content), status=record['status'])

    async def repair(self, work_item_id):
        analysis = await self.analyze(work_item_id)
        if (analysis and analysis['failure_code'] == 'worker_timeout' and analysis['status'] == 'blocked'
                and analysis['blockers'] and all(row['code'] in {'coding_budget_exhausted', 'recovery_budget_uncertain'}
                                                 for row in analysis['blockers'])):
            # Analysis stays read-only. A stopped timeout can reserve exactly one
            # policy-authorized retry before the ordinary checkpoint recovery.
            if await self.timeout_recovery.prepare(analysis['run_id'], work_item_id):
                analysis = await self.analyze(work_item_id)
            elif self.timeout_recovery.last_blocker:
                blocker = self.timeout_recovery.last_blocker
                if blocker['code'] == 'model_consumer_active':
                    # Closing a response only changes ModelService._active; it may
                    # not emit another Store event. Keep this retry on the timer.
                    self._deferred[work_item_id] = time.time() + self.workflow.settings.auto_failure_retry_delay_seconds
                    await self.trace.emit(analysis['attempt_id'], 'status', '超时恢复等待连接关闭',
                        '旧进程已停止，模型响应仍在关闭；平台会自动重新核验并续跑。',
                        key=analysis['id'] + ':timeout-transport-closing')
                return await self._block(analysis, work_item_id, blocker)
        if not analysis or analysis['status'] != 'ready':
            return analysis
        try:
            if analysis['action'] == 'repair_review_findings':
                if hasattr(self.review, 'repair_outcome'):
                    outcome = await self.review.repair_outcome(work_item_id, analysis_id=analysis['id'])
                else:
                    receipt = await self.review.repair(work_item_id, analysis_id=analysis['id'])
                    from agentflow.control.review_contract_view import review_contract_outcome
                    outcome = (review_contract_outcome(receipt) if receipt and 'state' in receipt else
                               {'outcome': 'scheduled' if receipt else 'not_applicable', 'receipt': receipt, 'blockers': []})
                if outcome['outcome'] != 'scheduled':
                    self._deferred.pop(work_item_id, None)
                    return await self._block(analysis, work_item_id, outcome['blockers'] or [{
                        'code': 'review_repair_state_changed', 'message': '当前审查状态或返工来源已变化，需要重新核验。'}])
                current = await self.store.read('failure_analysis', analysis['id'])
                receipt = outcome['receipt']
                if current and current.get('status') == 'ready' and receipt and 'stage_id' in receipt:
                    def record_existing(tx):
                        work = tx.get('work_item', work_item_id)
                        guard_automatic(tx, self.workflow, analysis['id'], work, 'repair_review_findings')
                        batch = tx.get('review_contract_repair', receipt['id'])
                        if batch != receipt or batch.get('state') not in {'triaging', 'repairing', 'reviewing'}:
                            raise DomainError('review_contract_stale', '已有返工批次状态发生变化，未重复安排。')
                        return finish_automatic(tx, analysis['id'], batch, kind='review_contract_repair')
                    await self.store.command('failure.review_existing', str(uuid4()), {'analysis_id': analysis['id']}, record_existing)
            else:
                await self.recovery.recover_automatic(analysis['run_id'], analysis['id'])
        except DomainError as error:
            return await self._block(analysis, work_item_id, {'code': error.code, 'message': error.message,
                **({'details': error.details} if error.details is not None else {})})
        result = await self.store.read('failure_analysis', analysis['id'])
        await self._trace(result, 'repair')
        return result

    async def _block(self, analysis, work_item_id, blocker, *, allow_scheduled=False):
        blockers = blocker if isinstance(blocker, list) else [blocker]
        def block(tx):
            record = tx.get('failure_analysis', analysis['id'])
            if not record or record.get('status') == 'repair_scheduled' and not allow_scheduled:
                return record
            result = tx.put('failure_analysis', record['id'], {**record, 'status': 'blocked',
                'blockers': blockers, 'not_before': None}, record['revision'])
            tx.event('failure.repair_blocked', {'analysis_id': record['id'], 'work_item_id': work_item_id,
                'code': blockers[0]['code']}, run_id=record['run_id'])
            return result
        result = await self.store.command('failure.block', str(uuid4()), {'analysis_id': analysis['id']}, block)
        await self._trace(result, 'repair' if result and result.get('status') == 'repair_scheduled' else 'blocked')
        return result

    async def reconcile(self):
        events = await self.store.events(self._event_cursor, limit=10000)
        if events:
            self._event_cursor = events[-1]['id']
        changed = {event['run_id'] for event in events if not event['type'].startswith('failure.')}
        due = {work_id for work_id, deadline in self._deferred.items() if deadline <= time.time()}
        if self._initialized and not changed and not due:
            return
        runs = {run['id']: run for run in await self.store.list('run') if run.get('execution_state') == 'running'}
        for work in await self.store.list('work_item'):
            if (work.get('run_id') in runs and failed_work(work)
                    and (not self._initialized or work['run_id'] in changed or work['id'] in due)):
                await self.repair(work['id'])
        self._initialized = True

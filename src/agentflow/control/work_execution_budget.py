"""Explicit owner additions to one stopped coding work's execution allowance."""
from __future__ import annotations

import math
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.coding_steps import CodingSteps
from agentflow.control.recovery import KINDS, RunRecoveryService, _failed, _related, coding_usage_blockers
from agentflow.domain.planning import CODING_STEPS

MAX_SAFE_INTEGER = 2**53 - 1
DIMENSIONS = (
    ('max_tool_calls', 'observed_tool_calls', 'additional_tool_calls', '工具调用', '次', 'observed'),
    ('max_active_seconds', 'active_seconds', 'additional_active_seconds', '执行时长', '秒', 'measured'),
    ('max_steps', 'step_count', 'additional_steps', '编码小步', '步', 'counted'),
)


class WorkExecutionBudgetExtension(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    expected_run_revision: int = Field(ge=1)
    expected_work_revision: int = Field(ge=1)
    expected_budget_revision: int = Field(ge=1)
    additional_tool_calls: int = Field(default=0, ge=0, le=MAX_SAFE_INTEGER)
    additional_active_seconds: float = Field(default=0.0, ge=0, le=86400)
    additional_steps: int = Field(default=0, ge=0, le=MAX_SAFE_INTEGER)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator('reason')
    @classmethod
    def meaningful_reason(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('A reason is required')
        return value

    @model_validator(mode='after')
    def positive_addition(self):
        if not (self.additional_tool_calls or self.additional_active_seconds or self.additional_steps):
            raise ValueError('At least one positive addition is required')
        return self


def _issue(code, message):
    return {'code': code, 'message': message}


def _number(value, *, integral=False):
    return (type(value) is int and 0 <= value <= MAX_SAFE_INTEGER) if integral else (
        type(value) in {int, float} and math.isfinite(value) and 0 <= value <= MAX_SAFE_INTEGER)


def _select_budget(state, work):
    identity = CodingSteps.budget_id(work['run_id'], work['id'])
    matches = [row for row in state['coding_work_budget']
               if row['id'] == identity or row.get('work_item_id') == work['id']]
    if not matches:
        return None, 'missing', [_issue('coding_budget_missing', '此工作尚无可核验的执行额度记录，不能通过追加操作创建或重置。')]
    if (len(matches) != 1 or matches[0]['id'] != identity
            or matches[0].get('run_id') != work['run_id'] or matches[0].get('work_item_id') != work['id']):
        return None, 'unknown', [_issue('coding_budget_uncertain', '执行额度的工作归属不一致，需先核验。')]
    budget = matches[0]
    valid = type(budget.get('uncertain')) is bool and not budget['uncertain']
    for limit, used, _, _, _, _ in DIMENSIONS:
        integral = limit != 'max_active_seconds'
        valid = valid and _number(budget.get(limit), integral=integral) and _number(budget.get(used), integral=integral)
    if not valid:
        return budget, 'unknown', [_issue('coding_budget_uncertain', '执行用量尚未核验，追加额度不能替代计量核对。')]
    return budget, 'known', []


def budget_view(run, work, budget, *, metering, adjustment_blockers):
    dimensions = []
    execution_blockers = []
    for limit_field, used_field, _, label, unit, usage_kind in DIMENSIONS:
        limit = budget.get(limit_field) if budget else None
        if not _number(limit, integral=limit_field != 'max_active_seconds'):
            limit = None
        used = budget.get(used_field) if budget and metering == 'known' else None
        balance = limit - used if limit is not None and used is not None else None
        exhausted = balance <= 0 if balance is not None else None
        dimensions.append({'key': limit_field, 'label': label, 'unit': unit, 'usage_kind': usage_kind,
            'used': used, 'limit': limit, 'remaining': max(0, balance) if balance is not None else None,
            'balance': balance, 'overrun': max(0, -balance) if balance is not None else None, 'exhausted': exhausted})
        if exhausted:
            execution_blockers.append(_issue('coding_budget_exhausted', f'{label}已用 {used:g} / 上限 {limit:g} {unit}，需追加额度后再重试。'))
    blockers = list({row['code']: row for row in adjustment_blockers}.values())
    return {'run_id': run['id'], 'work_item_id': work['id'], 'budget_id': budget['id'] if budget else None,
        'work_title': work.get('goal') or work.get('key') or work['id'],
        'run_revision': run['revision'], 'work_revision': work['revision'],
        'budget_revision': budget['revision'] if budget else None,
        'run_state': run['execution_state'], 'work_status': work['status'], 'work_generation': work['generation'],
        'metering': metering, 'can_extend': metering == 'known' and not blockers,
        'adjustment_blockers': blockers, 'execution_blockers': execution_blockers, 'dimensions': dimensions,
        'adjustable_fields': [row[2] for row in DIMENSIONS], 'requires_separate_retry': True}


class WorkExecutionBudgetService:
    def __init__(self, store, workflow):
        self.store, self.workflow = store, workflow
        self.recovery = RunRecoveryService(store, workflow)

    @staticmethod
    def _work(state, work_id):
        work = next((row for row in state['work_item'] if row['id'] == work_id), None)
        if not work:
            raise DomainError('not_found', '此运行中不存在指定工作。', 404)
        if work.get('step') not in CODING_STEPS or work.get('kind') == 'aggregation':
            raise DomainError('work_execution_budget_unsupported', '请选择实际执行编码的具体工作项。', 422)
        return work

    async def summary(self, state, work_id, *, common=None, target_blockers=None):
        run, work = state['run'][0], self._work(state, work_id)
        budget, metering, measurement = _select_budget(state, work)
        if metering == 'known':
            issues = ([row for row in target_blockers if row['code'] != 'coding_budget_exhausted']
                      if target_blockers is not None else
                      await coding_usage_blockers(state, self.workflow.settings.data_dir, work_id))
            if issues:
                metering = 'unknown'
                measurement += issues
        if common is None:
            common = self.recovery._common_blockers(state) + await self.recovery._process_blockers(state)
        blockers = [row for row in common if row['code'] != 'recovery_budget_exhausted'] + measurement
        if work.get('archived') or not _failed(work):
            blockers.append(_issue('work_not_recoverable', '仅能对已失败、阻塞或中断的工作追加执行额度。'))
        if run.get('restore_reconciliation_required'):
            blockers.append(_issue('recovery_restore_uncertain', '恢复的数据尚未完成核验，不能调整执行额度。'))
        return budget_view(run, work, budget, metering=metering, adjustment_blockers=blockers)

    async def view(self, run_id, work_id):
        return await self.summary(await self.recovery._read(run_id), work_id)

    async def extend(self, run_id, work_id, payload, key):
        try:
            return await self._extend(run_id, work_id, payload, key)
        except DomainError:
            identity = str(uuid5(NAMESPACE_URL, f'work-execution-budget:{run_id}:{work_id}:{key}'))
            if await self.store.read('work_execution_budget_adjustment', identity):
                request = WorkExecutionBudgetExtension.model_validate(payload)
                command = {'run_id': run_id, 'work_item_id': work_id, **request.model_dump()}
                return await self.store.command('work.execution_budget.extend', key, command, lambda tx: {})
            raise

    async def _extend(self, run_id, work_id, payload, key):
        request = WorkExecutionBudgetExtension.model_validate(payload)
        command = {'run_id': run_id, 'work_item_id': work_id, **request.model_dump()}
        identity = str(uuid5(NAMESPACE_URL, f'work-execution-budget:{run_id}:{work_id}:{key}'))
        if await self.store.read('work_execution_budget_adjustment', identity):
            return await self.store.command('work.execution_budget.extend', key, command, lambda tx: {})
        state = await self.recovery._read(run_id)
        work = self._work(state, work_id)
        before = await self.summary(state, work_id)
        for actual, expected in ((before['run_revision'], request.expected_run_revision),
                                 (before['work_revision'], request.expected_work_revision),
                                 (before['budget_revision'], request.expected_budget_revision)):
            if actual != expected:
                raise DomainError('revision_conflict', '工作、运行或执行额度已变化，请刷新后重新提交。')
        if not before['can_extend']:
            blocker = before['adjustment_blockers'][0]
            raise DomainError(blocker['code'], blocker['message'], details=before)
        budget, _, _ = _select_budget(state, work)
        changes = {}
        additions = {field: getattr(request, field) for _, _, field, _, _, _ in DIMENSIONS}
        for limit_field, _, addition_field, _, _, _ in DIMENSIONS:
            addition = additions[addition_field]
            if addition:
                maximum = budget[limit_field] + addition
                if not _number(maximum, integral=limit_field != 'max_active_seconds'):
                    raise DomainError('work_execution_limit_too_large', '追加后的执行额度超出可精确表示的范围。', 422)
                if limit_field == 'max_active_seconds' and maximum > 86400:
                    raise DomainError('work_execution_limit_too_large', '当前执行组件支持的工作时长上限为 86400 秒。', 422)
                changes[limit_field] = maximum
        observed = canonical_digest(state)

        def apply(tx):
            current = _related({kind: tx.list(kind) for kind in KINDS}, run_id)
            if canonical_digest(current) != observed:
                raise DomainError('revision_conflict', '额度核验期间执行或用量发生变化，未追加额度。')
            current_run = current['run'][0]
            current_work = self._work(current, work_id)
            current_budget, current_metering, _ = _select_budget(current, current_work)
            if (current_metering != 'known' or current_run['revision'] != request.expected_run_revision
                    or current_work['revision'] != request.expected_work_revision
                    or current_budget['revision'] != request.expected_budget_revision):
                raise DomainError('revision_conflict', '工作执行额度版本已变化，未重复追加。')
            updated = tx.put('coding_work_budget', current_budget['id'], {**current_budget, **changes,
                'last_adjustment_id': identity}, current_budget['revision'])
            result = budget_view(current_run, current_work, updated, metering='known', adjustment_blockers=[])
            result.update(adjustment_id=identity, adjusted_at=utc_now())
            audit = {'actor': 'owner', 'run_id': run_id, 'work_item_id': work_id,
                'work_generation': current_work['generation'], 'budget_id': updated['id'],
                'run_revision': current_run['revision'], 'work_revision': current_work['revision'],
                'previous_budget_revision': current_budget['revision'], 'budget_revision': updated['revision'],
                'previous_limits': {row[0]: current_budget[row[0]] for row in DIMENSIONS},
                'limits': {row[0]: updated[row[0]] for row in DIMENSIONS},
                'used': {row[1]: current_budget[row[1]] for row in DIMENSIONS},
                'additions': additions, 'reason': request.reason, 'requires_explicit_retry': True,
                'created_at': result['adjusted_at']}
            tx.put('work_execution_budget_adjustment', identity, audit)
            tx.event('work.execution_budget_extended', {'adjustment_id': identity, **audit}, run_id=run_id)
            return result
        return await self.store.command('work.execution_budget.extend', key, command, apply)

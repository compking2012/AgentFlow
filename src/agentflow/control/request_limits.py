"""Explicit owner grants of additional requests; never resets usage or money."""
from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentflow.common import DomainError, utc_now
from agentflow.models.budget import account_id
from agentflow.runtime.contracts import require_record


class RequestLimitExtension(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    expected_revision: int = Field(ge=1)
    max_model_requests: int = Field(ge=0, le=2000)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator('reason')
    @classmethod
    def meaningful_reason(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('A reason is required')
        return value


class RequestLimitService:
    def __init__(self, store):
        self.store = store

    async def extend(self, run_id: str, payload: dict, key: str) -> dict:
        request = RequestLimitExtension.model_validate(payload)
        command = {'run_id': run_id, **request.model_dump()}

        def apply(tx):
            run = require_record(tx, 'run', run_id)
            if run['revision'] != request.expected_revision:
                raise DomainError('revision_conflict', '运行已更新，请刷新后重新提交额度申请。')
            from agentflow.control.product_management import guard_product_run
            from agentflow.control.recovery import _failed
            guard_product_run(tx, run)
            if (run.get('delivery_ids') or any(d.get('run_id') == run_id and d.get('confirmed_at')
                    for d in tx.list('delivery')) or any(i.get('run_id') == run_id and i.get('status') == 'confirmed'
                    for i in tx.list('delivery_intent'))):
                raise DomainError('delivered_run', '已确认交付的运行不能追加额度，请创建新迭代。')
            if any(i.get('run_id') == run_id and i.get('status') != 'failed' for i in tx.list('delivery_intent')):
                raise DomainError('delivery_in_progress', '发布结果尚未核对，不能修改本轮额度。')
            state = run.get('execution_state')
            if state in {'completed', 'cancelled'}:
                if not any(w.get('run_id') == run_id and not w.get('archived') and _failed(w)
                           for w in tx.list('work_item')):
                    raise DomainError('run_not_recoverable', '该运行已结束且没有可恢复的失败或中断工作，无需追加额度。')
                # A stopped terminal Run keeps its state. This grant does not
                # invalidate work or schedule recovery; the owner does that later.
            elif state != 'paused':
                raise DomainError('run_not_paused', '请先暂停本轮运行，并等待所有执行确认结束。')
            iteration = require_record(tx, 'iteration', run['iteration_id'])
            if iteration.get('project_id') != run.get('project_id'):
                raise DomainError('budget_configuration_conflict', '运行与迭代的项目关联不一致。')
            run_limit, iteration_limit = run.get('budget_limit', {}), iteration.get('budget_limit', {})
            previous = run_limit.get('max_model_requests')
            iteration_previous = iteration_limit.get('max_model_requests')
            if (type(previous) is not int or type(iteration_previous) is not int
                    or min(previous, iteration_previous) < 0):
                raise DomainError('budget_configuration_conflict', '当前调用额度配置无效，不能自动修正。')
            if request.max_model_requests == 0:
                if previous == 0 and iteration_previous == 0:
                    raise DomainError('request_limit_not_increased', '本轮及所在迭代已经不限调用次数。', 422)
                # Zero removes both cumulative caps; it is never arithmetic zero.
                added, iteration_maximum = None, 0
            else:
                if previous == 0 or request.max_model_requests <= previous:
                    raise DomainError('request_limit_not_increased', '追加接口只能提高有限上限或将其设为 0（不限次数）。', 422)
                added = request.max_model_requests - previous
                iteration_maximum = 0 if iteration_previous == 0 else iteration_previous + added
            if iteration_maximum > 2000:
                raise DomainError('request_limit_too_large', '追加后的迭代累计调用上限不能超过 2000 次。', 422)

            related_runs = {r['id'] for r in tx.list('run') if r.get('iteration_id') == iteration['id']}
            attempts = [a for a in tx.list('attempt')
                        if a.get('iteration_id') == iteration['id'] or a.get('run_id') in related_runs]
            attempt_ids = {a['id'] for a in attempts}
            if any(a.get('status') not in {'completed', 'failed', 'cancelled', 'blocked'} for a in attempts):
                raise DomainError('active_attempts', '同一迭代仍有活跃或状态未知的 Agent 执行。')
            if any(w.get('run_id') in related_runs and w.get('status') in {
                    'running', 'waiting_execution', 'cancel_requested', 'cancelling', 'execution_unknown'}
                   for w in tx.list('work_item')):
                raise DomainError('active_attempts', '同一迭代仍有尚未确认停止的工作项。')
            if any((a.get('run_id') in related_runs or a['id'] in attempt_ids)
                   and a.get('state') not in {'completed', 'failed', 'cancelled'}
                   for a in tx.list('supervised_attempt')):
                raise DomainError('active_attempts', '同一迭代仍有运行中或状态未知的受管进程。')
            from agentflow.models.uncertainty import (
                acknowledged_invocation_ids,
                acknowledgment_state,
                attempt_uncertainty_blocks,
                invocation_blocks,
            )
            model_state = acknowledgment_state(tx)
            acknowledged = acknowledged_invocation_ids(model_state)
            if any((i.get('iteration_id') == iteration['id'] or i.get('run_id') in related_runs)
                   and invocation_blocks(i, acknowledged)
                   for i in tx.list('model_invocation')):
                raise DomainError('model_calls_unsettled', '同一迭代仍有预留、发送中或结果不确定的模型调用。')
            if any(a['id'] in attempt_ids and attempt_uncertainty_blocks(a, model_state['model_invocation'], acknowledged)
                   for a in tx.list('model_attempt_budget')):
                raise DomainError('model_calls_unsettled', '同一迭代仍有尚未核对的模型调用。')
            if any((j.get('run_id') in related_runs or j.get('attempt_id') in attempt_ids)
                   and j.get('state') not in {'completed', 'failed', 'cancelled'} for j in tx.list('node_job')):
                raise DomainError('node_jobs_in_flight', '同一迭代仍有未结束的执行节点作业。')

            accounts = {}
            for kind, owner, declared in [('run', run_id, run_limit),
                                           ('iteration', iteration['id'], iteration_limit)]:
                account = tx.get('budget_account', account_id(kind, owner))
                if not account:
                    raise DomainError('budget_account_missing', '现有预算账户不存在，不能通过追加操作创建或重置。')
                if (account.get('owner_kind') != kind or account.get('owner_id') != owner
                        or account.get('max_requests') != declared['max_model_requests']
                        or account.get('currency') != declared.get('currency')
                        or account.get('limit_micros') != declared.get('limit_micros')
                        or type(account.get('request_count')) is not int
                        or type(account.get('max_requests')) is not int
                        or account['request_count'] < 0
                        or (account['max_requests'] > 0 and account['request_count'] > account['max_requests'])):
                    raise DomainError('budget_configuration_conflict', '现有账户与运行额度不一致，不能自动调整。')
                if account.get('restore_uncertain') or account.get('reserved_micros') or account.get('uncertain_micros'):
                    raise DomainError('budget_requires_reconciliation', '现有预算仍有预留或未核对金额，请先完成核对。')
                accounts[kind] = account

            updated = tx.put('run', run_id, {**run, 'budget_limit': {
                **run_limit, 'max_model_requests': request.max_model_requests}}, run['revision'])
            tx.put('iteration', iteration['id'], {**iteration, 'budget_limit': {
                **iteration_limit, 'max_model_requests': iteration_maximum}}, iteration['revision'])
            for kind, maximum in [('run', request.max_model_requests), ('iteration', iteration_maximum)]:
                account = accounts[kind]
                tx.put('budget_account', account['id'], {**account, 'max_requests': maximum}, account['revision'])
            audit_id = str(uuid4())
            audit = {'actor': 'owner', 'run_id': run_id, 'iteration_id': iteration['id'],
                     'reason': request.reason, 'added_requests': added,
                     'unlimited_requests': request.max_model_requests == 0,
                     'previous_max_model_requests': previous, 'max_model_requests': request.max_model_requests,
                     'previous_iteration_max_requests': iteration_previous, 'iteration_max_requests': iteration_maximum,
                     'run_request_count': accounts['run']['request_count'],
                     'iteration_request_count': accounts['iteration']['request_count'],
                     'previous_run_revision': run['revision'], 'run_revision': updated['revision'], 'created_at': utc_now()}
            tx.put('request_limit_change', audit_id, audit)
            tx.event('run.request_limit_extended', {'change_id': audit_id, **audit}, run_id=run_id)
            return updated

        # Replay happens inside the writer before current-state checks; a retried
        # owner request cannot grant additional calls twice, even after resuming.
        return await self.store.command('run.request_limit', key, command, apply)

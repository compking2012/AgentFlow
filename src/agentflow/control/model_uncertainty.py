"""Explicit acceptance of one stopped, counted, unpriced request's unknown result."""
from uuid import NAMESPACE_URL, uuid5

import psutil
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.recovery import KINDS, RunRecoveryService, _related
from agentflow.models.uncertainty import (
    ACK_KIND,
    acknowledged_invocation_ids,
    acknowledgment_basis,
    request_is_counted,
)


class ModelUncertaintyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_run_revision: int = Field(ge=1)
    expected_work_revision: int = Field(ge=1)
    expected_attempt_revision: int = Field(ge=1)
    expected_invocation_revision: int = Field(ge=1)
    expected_attempt_budget_revision: int = Field(ge=1)
    expected_state_digest: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    accept_unknown_usage: bool
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator('accept_unknown_usage')
    @classmethod
    def explicit_acceptance(cls, value):
        if value is not True:
            raise ValueError('Explicit acknowledgment is required')
        return value

    @field_validator('reason')
    @classmethod
    def meaningful_reason(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('A reason is required')
        return value


class ModelUncertaintyService:
    def __init__(self, store, workflow, models):
        self.store, self.workflow, self.models = store, workflow, models
        self.recovery = RunRecoveryService(store, workflow)

    def _transport_stopped(self, state):
        active = getattr(self.models, '_active', None)
        return isinstance(active, dict) and not any(row['id'] in active for row in state['model_invocation'])

    async def _items(self, state):
        common = self.recovery._common_blockers(state, ignore_model_uncertainty=True)
        common = [row for row in common if row['code'] != 'recovery_budget_exhausted']
        common += await self.recovery._process_blockers(state)
        if not self._transport_stopped(state):
            common.append({'code': 'model_consumer_active', 'message': '模型响应仍可能被读取或消费，请等待连接结束后再确认。'})
        run = state['run'][0]
        accepted = acknowledged_invocation_ids(state)
        items = []
        for invocation in state['model_invocation']:
            if invocation.get('run_id') != run['id'] or invocation.get('state') != 'uncertain':
                continue
            attempt = next((row for row in state['attempt'] if row['id'] == invocation.get('attempt_id')), {})
            work = next((row for row in state['work_item'] if row['id'] == attempt.get('work_item_id')), {})
            budget = next((row for row in state['model_attempt_budget'] if row['id'] == attempt.get('id')), {})
            blockers = list(common)
            if invocation.get('cost_mode') != 'request_limited' or run.get('budget_limit', {}).get('cost_mode') != 'request_limited':
                blockers.append({'code': 'strict_pricing_requires_reconciliation', 'message': '严格金额模式必须核实实际费用，不能通过确认未知用量继续。'})
            if acknowledgment_basis(state, invocation) is None:
                blockers.append({'code': 'model_ack_evidence_invalid', 'message': '调用身份、已占请求次数或原执行停止证据尚未核验，不能确认。'})
            acknowledgment = next((row for row in state[ACK_KIND] if row.get('invocation_id') == invocation['id']
                                   and invocation['id'] in accepted), None)
            items.append({'run_id': run['id'], 'work_item_id': work.get('id'),
                'work_title': work.get('goal') or work.get('key') or work.get('id'),
                'attempt_id': attempt.get('id'), 'invocation_id': invocation['id'],
                'run_revision': run['revision'], 'work_revision': work.get('revision'),
                'attempt_revision': attempt.get('revision'), 'invocation_revision': invocation['revision'],
                'attempt_budget_revision': budget.get('revision'), 'expected_state_digest': canonical_digest(state),
                'state': invocation['state'], 'cost_mode': invocation.get('cost_mode'),
                'usage': invocation.get('usage'), 'actual_micros': invocation.get('actual_micros'),
                'request_counted': request_is_counted(state, invocation),
                'eligible': not blockers and acknowledgment is None, 'blockers': blockers,
                'acknowledged': acknowledgment is not None, 'acknowledgment_id': acknowledgment['id'] if acknowledgment else None,
                'requires_separate_retry': True})
        return items

    async def view(self, run_id):
        state = await self.recovery._read(run_id)
        return {'run_id': run_id, 'run_revision': state['run'][0]['revision'], 'items': await self._items(state)}

    async def acknowledge(self, run_id, invocation_id, payload, key):
        try:
            return await self._acknowledge(run_id, invocation_id, payload, key)
        except DomainError:
            identity = str(uuid5(NAMESPACE_URL, f'model-uncertainty:{run_id}:{invocation_id}:{key}'))
            if await self.store.read(ACK_KIND, identity):
                request = ModelUncertaintyRequest.model_validate(payload)
                command = {'run_id': run_id, 'invocation_id': invocation_id, **request.model_dump()}
                return await self.store.command('model.uncertainty.acknowledge', key, command, lambda tx: {})
            raise

    async def _acknowledge(self, run_id, invocation_id, payload, key):
        request = ModelUncertaintyRequest.model_validate(payload)
        command = {'run_id': run_id, 'invocation_id': invocation_id, **request.model_dump()}
        identity = str(uuid5(NAMESPACE_URL, f'model-uncertainty:{run_id}:{invocation_id}:{key}'))
        if await self.store.read(ACK_KIND, identity):
            return await self.store.command('model.uncertainty.acknowledge', key, command, lambda tx: {})
        state = await self.recovery._read(run_id)
        item = next((row for row in await self._items(state) if row['invocation_id'] == invocation_id), None)
        if item is None:
            raise DomainError('model_uncertainty_not_found', '指定运行中不存在这条结果未知的模型调用。', 404)
        for field in ('run_revision', 'work_revision', 'attempt_revision', 'invocation_revision', 'attempt_budget_revision'):
            if getattr(request, 'expected_' + field) != item[field]:
                raise DomainError('revision_conflict', '调用、工作或预算已变化，请刷新核对后重新确认。')
        if request.expected_state_digest != canonical_digest(state):
            raise DomainError('revision_conflict', '核验状态已变化，请刷新后重新确认。')
        if not item['eligible']:
            blocker = item['blockers'][0] if item['blockers'] else {'code': 'model_uncertainty_already_acknowledged', 'message': '这条未知调用已经确认。'}
            raise DomainError(blocker['code'], blocker['message'], details=item)
        invocation = next(row for row in state['model_invocation'] if row['id'] == invocation_id)
        basis = acknowledgment_basis(state, invocation)
        result = {**item, 'eligible': False, 'acknowledged': True, 'acknowledgment_id': identity, 'acknowledged_at': utc_now()}

        def apply(tx):
            current = _related({kind: tx.list(kind) for kind in KINDS}, run_id)
            if canonical_digest(current) != request.expected_state_digest or not self._transport_stopped(current):
                raise DomainError('revision_conflict', '确认期间执行、调用或预算发生变化，未接受未知结果。')
            process = next(row for row in current['supervised_attempt'] if row['id'] == item['attempt_id'])
            try:
                self.recovery._verify_process(process, {row['id']: row for row in current['attempt']})
            except DomainError:
                raise
            except (OSError, ValueError, TypeError, KeyError, psutil.Error):
                raise DomainError('model_ack_evidence_invalid', '原执行停止证据在确认期间发生变化，未接受未知结果。') from None
            work = next(row for row in current['work_item'] if row['id'] == item['work_item_id'])
            audit = {'actor': 'owner', 'run_id': run_id, 'iteration_id': invocation['iteration_id'],
                'work_item_id': work['id'], 'work_generation': work['generation'], 'attempt_id': item['attempt_id'],
                'invocation_id': invocation_id, 'basis': basis, 'reason': request.reason,
                'accept_unknown_usage': True, 'requires_explicit_retry': True,
                'versions': {field: item[field] for field in ('run_revision', 'work_revision', 'attempt_revision',
                    'invocation_revision', 'attempt_budget_revision')}, 'created_at': result['acknowledged_at'], 'result': result}
            tx.put(ACK_KIND, identity, audit)
            tx.event('model.uncertainty_acknowledged', {key: value for key, value in audit.items() if key != 'result'}, run_id=run_id)
            return result
        return await self.store.command('model.uncertainty.acknowledge', key, command, apply)

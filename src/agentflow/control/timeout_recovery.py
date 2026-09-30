"""Durable retries for independently proved stopped timeouts, never request replays."""
from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

import psutil

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.coding_steps import CodingSteps
from agentflow.control.recovery import (
    KINDS,
    _ReadState,
    _related,
    coding_usage_blockers,
    read_recovery_records,
)
from agentflow.control.review_contract_binding import delegated_owner_budget
from agentflow.domain.planning import CODING_STEPS
from agentflow.models.uncertainty import (
    ACK_KIND,
    TIMEOUT_KIND,
    acknowledged_invocation_ids,
    acknowledgment_basis,
    timeout_authorization_digest,
)
from agentflow.runtime.contracts import LaunchSpec
from agentflow.runtime.failures import _private_attempt_directory, _private_evidence


def retry_counts(analyses, recoveries, run_id, work_id, *, exclude_attempt_id=None):
    """A reserved retry and its later scheduled analysis are the same attempt."""
    scheduled = [row for row in analyses if row.get('run_id') == run_id
                 and row.get('status') == 'repair_scheduled' and row.get('failure_code') != 'review_failed']
    grants = [row for row in recoveries if row.get('run_id') == run_id]
    def key(row):
        return row.get('attempt_id') or ('record', row['id'])
    # Only exclude an unconsumed reservation. A scheduled retry always counts.
    consumed = {key(row) for row in scheduled}
    grants = [row for row in grants if key(row) != exclude_attempt_id or key(row) in consumed]
    all_rows = scheduled + grants
    timeouts = grants + [row for row in scheduled if row.get('failure_code') == 'worker_timeout']
    return {'work': len({key(row) for row in all_rows if row.get('work_item_id') == work_id}),
            'run': len({key(row) for row in all_rows}),
            'timeouts_work': len({key(row) for row in timeouts if row.get('work_item_id') == work_id})}


def _policy(settings):
    return {'timeout_limit': getattr(settings, 'auto_timeout_retry_limit', 0),
            'per_work_limit': settings.auto_failure_retry_limit,
            'run_limit': settings.auto_failure_run_limit}


def _positive(value):
    return type(value) in {int, float} and math.isfinite(value) and value > 0


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError('invalid_timestamp_type')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamp_timezone_missing')
    return result.timestamp()


def _grant_id(run_id, attempt_id):
    return str(uuid5(NAMESPACE_URL, f'timeout-recovery:{run_id}:{attempt_id}'))


class TimeoutRecovery:
    def __init__(self, store, workflow, recovery, models=None):
        self.store, self.workflow, self.recovery, self.models = store, workflow, recovery, models
        self.last_blocker = None

    async def _read(self, run_id):
        kinds = tuple(dict.fromkeys((*KINDS, TIMEOUT_KIND, 'failure_analysis')))
        return await read_recovery_records(self.store, run_id, kinds)

    def _transport_stopped(self, state):
        calls = state['model_invocation']
        if not calls:
            return True
        active = getattr(self.models, '_active', None)
        return isinstance(active, dict) and not any(row['id'] in active for row in calls)

    def _identity(self, state, work_id):
        run = state['run'][0]
        work = next((row for row in state['work_item'] if row['id'] == work_id), None)
        attempt = next((row for row in state['attempt'] if row['id'] == (work or {}).get('attempt_id')), None)
        process = next((row for row in state['supervised_attempt'] if row['id'] == (attempt or {}).get('id')), None)
        if (run.get('execution_state') != 'running' or not work or work.get('archived')
                or work.get('status') not in {'failed', 'blocked'} or work.get('kind') == 'aggregation'
                or not attempt or attempt.get('status') not in {'failed', 'blocked'}
                or work.get('runtime_failure_code') not in {None, 'worker_timeout'}
                or attempt.get('runtime_failure_code') != 'worker_timeout'
                or not process or process.get('state') != 'failed' or process.get('reason') != 'timeout'
                or process.get('run_id') != run['id']
                or attempt.get('work_item_id') != work_id or attempt.get('run_id') != run['id']
                or attempt.get('iteration_id') != run.get('iteration_id')
                or work.get('project_id') != run.get('project_id')
                or any(work.get(field) != attempt.get(field) for field in
                       ('generation', 'fencing_token', 'input_fingerprint'))
                or any(row.get(flag) for row in (run, work, attempt, process)
                       for flag in ('restore_uncertain', 'restore_reconciliation_required', 'restore_revalidation_required'))):
            raise DomainError('timeout_retry_ineligible', '只有当前运行中身份一致、已确认停止的超时执行可以自动续跑。')
        if any(row.get('run_id') == run['id'] and row.get('work_item_id') == work_id
               and row.get('work_generation') == work['generation'] and row.get('requires_explicit_retry')
               for kind in (ACK_KIND, 'work_execution_budget_adjustment') for row in state.get(kind, [])):
            raise DomainError('manual_retry_required', '已有明确要求单独重试的人工决定，未追加自动续跑额度。')
        return run, work, attempt, process

    def _receipt(self, process):
        identity = canonical_digest({'attempt_id': process['id']}).split(':')[1]
        with _private_attempt_directory(self.recovery.data_dir, 'supervisor', identity) as directory:
            receipt = json.loads(_private_evidence(directory, 'result.json', 65536))
            if receipt.get('reason') != 'timeout' or receipt.get('execution_status') != 'failed':
                raise DomainError('timeout_receipt_invalid', '原执行回执未证明超时终止。')
            try:
                config = json.loads(_private_evidence(directory, 'config.json', 1024 * 1024))
                grace = config['stop_grace_seconds']
            except FileNotFoundError:
                grace = LaunchSpec.model_fields['stop_grace_seconds'].default
            if type(grace) not in {int, float} or not math.isfinite(grace) or not 0 <= grace <= 30:
                raise ValueError('invalid_stop_grace')
            # The launcher's two grace waits, kill wait, descendant wait, three
            # output/input thread joins and polling interval bound disconnection.
            return receipt, 2 * grace + 3 + 2 + 3 * 2 + .04

    def _limits(self, state, work_id, *, exclude_attempt_id=None):
        policy = _policy(self.workflow.settings)
        count = retry_counts(state['failure_analysis'], state[TIMEOUT_KIND], state['run'][0]['id'],
                             work_id, exclude_attempt_id=exclude_attempt_id)
        if (any(type(value) is not int or value <= 0 for value in policy.values())
                or count['work'] >= policy['per_work_limit'] or count['run'] >= policy['run_limit']
                or count['timeouts_work'] >= policy['timeout_limit']):
            raise DomainError('automatic_timeout_retry_limit', '自动超时续跑已关闭或达到本任务、本轮重试上限。')
        return policy, count

    @staticmethod
    def _pending_invocations(state):
        accepted = acknowledged_invocation_ids(state)
        return [row for row in state['model_invocation']
                if row.get('state') not in {'settled', 'released', 'completed_unpriced'} and row['id'] not in accepted]

    def _pending_acknowledgments(self, state, attempt, receipt, window, pending):
        if pending and not self._transport_stopped(state):
            raise DomainError('model_consumer_active', '模型响应消费尚未确认结束，不能自动续跑。')
        results = []
        for invocation in pending:
            basis = acknowledgment_basis(state, invocation)
            if (not basis or invocation.get('attempt_id') != attempt['id']
                    or invocation.get('reason') not in {'consumer_disconnected', 'dispatch_result_unknown'}
                    or not receipt.get('finished_at')):
                raise DomainError('timeout_model_uncertainty', '未知模型调用未绑定到本次已停止的超时执行。')
            finished = _stamp(receipt['finished_at'])
            updated, dispatched = _stamp(invocation['updated_at']), _stamp(invocation['dispatch_started_at'])
            if not finished - window <= updated <= finished + window or dispatched > finished or dispatched > updated:
                raise DomainError('timeout_model_uncertainty', '模型断连时间不属于原超时终止窗口。')
            results.append({'invocation_id': invocation['id'], 'basis': basis, 'reason': invocation['reason']})
        return results

    def _group(self, state, work_id, pending):
        """Every pending request must belong to a current, eligible stopped timeout."""
        members = {work_id: self._identity(state, work_id)}
        attempts = {row['id']: row for row in state['attempt']}
        for invocation in pending:
            source = attempts.get(invocation.get('attempt_id'))
            if not source or not source.get('work_item_id'):
                raise DomainError('timeout_model_uncertainty', '未知模型调用缺少本轮当前超时任务身份。')
            member = self._identity(state, source['work_item_id'])
            if member[2]['id'] != invocation.get('attempt_id'):
                raise DomainError('timeout_model_uncertainty', '未知模型调用不属于任务的当前超时执行。')
            members[source['work_item_id']] = member
        targets = {target['work_item_id']: target for target in self.recovery._targets(state)}
        for identity in members:
            target = targets.get(identity)
            if not target or target['root_work_item_ids'] != [identity]:
                raise DomainError('timeout_retry_ineligible', '超时任务缺少独立且明确的恢复目标。')
            affected = set(target['affected_work_item_ids'])
            if affected.intersection(members) != {identity}:
                raise DomainError('timeout_group_dependency', '超时恢复目标互相依赖，不能分别授权后使同组执行失效。')
            if any(row['id'] in affected and row.get('status') == 'waiting_approval' for row in state['work_item']):
                raise DomainError('human_approval_pending', '受影响任务正在等待人工审批，未追加自动续跑额度。')
        return [members[identity] for identity in sorted(members)]

    def _group_limits(self, state, members):
        checked = [self._limits(state, work['id']) for _, work, _, _ in members]
        policy = checked[0][0]
        if checked[0][1]['run'] + len(members) > policy['run_limit']:
            raise DomainError('automatic_timeout_retry_limit', '本轮剩余自动重试次数不足以恢复全部相关超时任务，未追加部分额度。')
        return policy, [counts for _, counts in checked]

    def _budget(self, state, run, work):
        if work.get('step') not in CODING_STEPS:
            return None, {}, {}
        rows = [row for row in state['coding_work_budget'] if row.get('work_item_id') == work['id']]
        if len(rows) != 1 or rows[0]['id'] != CodingSteps.budget_id(run['id'], work['id']):
            raise DomainError('coding_budget_missing', '超时编码任务缺少可核验的累计额度。')
        budget = rows[0]
        control = next((row for row in state['coding_step_control'] if row['id'] == work['attempt_id']), None)
        usage = next((row for row in state['coding_step_usage'] if row['id'] == work['attempt_id']), None)
        context = next((row for row in state['dispatch_context'] if row['id'] == work['attempt_id']), None)
        if (not control or not usage or usage.get('known') is not True or not context
                or context.get('task', {}).get('coding_step') != control):
            raise DomainError('coding_budget_uncertain', '原编码执行缺少绑定到派发控制的已核验用量。')
        if (budget.get('run_id') != run['id'] or budget.get('uncertain') is not False
                or any(type(budget.get(field)) is not int or budget[field] < 0
                       for field in ('max_steps', 'step_count', 'max_tool_calls', 'observed_tool_calls'))
                or not _positive(budget.get('max_active_seconds'))
                or type(budget.get('active_seconds')) not in {int, float}
                or not math.isfinite(budget['active_seconds']) or budget['active_seconds'] < 0
                or budget['step_count'] >= budget['max_steps']):
            raise DomainError('coding_budget_uncertain', '原累计用量未知或小步次数已耗尽，不能追加超时额度。')
        original = run.get('budget_limit', {})
        if (not _positive(original.get('max_active_seconds'))
                or type(original.get('max_tool_calls')) is not int or original['max_tool_calls'] <= 0):
            raise DomainError('coding_budget_uncertain', '原单次执行额度无法核验。')
        old = {field: budget[field] for field in ('max_active_seconds', 'max_tool_calls')}
        new = {'max_active_seconds': max(old['max_active_seconds'], budget['active_seconds'] + original['max_active_seconds']),
               'max_tool_calls': max(old['max_tool_calls'], budget['observed_tool_calls'] + original['max_tool_calls'])}
        return budget, old, new

    def _verify_files(self, state, process, receipt, progress):
        attempts = {row['id']: row for row in state['attempt']}
        for supervised in state['supervised_attempt']:
            self.recovery._verify_process(supervised, attempts)
        if self._receipt(process)[0] != receipt:
            raise DomainError('timeout_receipt_invalid', '超时回执在授权期间变化。')
        if progress:
            work = next(row for row in state['work_item'] if row['id'] == progress['work_item_id'])
            context = next(row for row in state['dispatch_context'] if row['id'] == work['attempt_id'])
            path, source = self.recovery._validate_workspace({'work': work, 'context': context,
                'project': state['project'][0] if state['project'] else None})
            if (source != progress['source_commit']
                    or self.recovery.repository._collect_diff(path, source)['tree_oid'] != progress['tree_oid']):
                raise DomainError('recovery_checkpoint_invalid', '原代码在授权期间变化，未追加额度。')

    @staticmethod
    def _owner_allowance(state, work, budget, old, new):
        if not budget:
            return None, None
        pool = delegated_owner_budget(_ReadState(state), work, budget)
        if pool is None:
            return None, None
        if (pool.get('uncertain') is not False
                or any(type(pool.get(k)) is not int or pool[k] < 0
                       for k in ('max_steps', 'step_count', 'max_tool_calls', 'observed_tool_calls'))
                or any(type(pool.get(k)) not in {int, float} or not math.isfinite(pool[k]) or pool[k] < 0
                       for k in ('max_active_seconds', 'active_seconds'))
                or pool['step_count'] >= pool['max_steps']):
            raise DomainError('coding_budget_uncertain', '原作者累计额度未知或小步次数已耗尽，不能追加超时额度。')
        increment = {key: new[key] - old[key] for key in old}
        return pool, {'budget_id': pool['id'], 'work_item_id': pool['work_item_id'],
            'batch_id': budget['review_contract_batch'], 'budget_revision': pool['revision'],
            'old_limits': {key: pool[key] for key in old},
            'new_limits': {key: pool[key] + increment[key] for key in old}, 'additional_limits': increment,
            'preserved_usage': {key: pool[key] for key in ('active_seconds', 'observed_tool_calls', 'step_count', 'max_steps')}}

    def _verify_owner_grant(self, state, work, existing):
        budget = next((row for row in state['coding_work_budget'] if row['id'] == existing.get('budget_id')), None)
        pool = delegated_owner_budget(_ReadState(state), work, budget) if budget else None
        binding = existing.get('owner_budget')
        if pool is None:
            if binding:
                raise DomainError('timeout_evidence_invalid', '原作者超时授权已脱离对应执行。')
            return
        if (not binding or binding.get('budget_id') != pool['id'] or binding.get('work_item_id') != pool['work_item_id']
                or binding.get('batch_id') != budget['review_contract_batch']
                or binding.get('additional_limits') != {key: existing['new_limits'][key] - value
                                                       for key, value in existing['old_limits'].items()}
                or any(pool.get(key, -1) < value for key, value in binding.get('new_limits', {}).items())
                or any(pool.get(key, -1) < value for key, value in binding.get('preserved_usage', {}).items())):
            raise DomainError('timeout_evidence_invalid', '原作者预算不再满足已经记录的超时授权。')

    async def prepare(self, run_id, work_id):
        self.last_blocker = None
        try:
            return await self._prepare(run_id, work_id)
        except DomainError as error:
            self.last_blocker = {'code': error.code, 'message': error.message}
        except (OSError, ValueError, KeyError, TypeError, IndexError, psutil.Error):
            self.last_blocker = {'code': 'timeout_evidence_invalid', 'message': '超时、计量或代码证据无法核验，未追加额度。'}
        return False

    async def _prepare(self, run_id, work_id):
        state = await self._read(run_id)
        run, work, attempt, process = self._identity(state, work_id)
        identity = _grant_id(run_id, attempt['id'])
        existing = next((row for row in state[TIMEOUT_KIND] if row['id'] == identity), None)
        policy, counts = self._limits(state, work_id, exclude_attempt_id=attempt['id'] if existing else None)
        if existing:
            expected = {'run_id': run_id, 'iteration_id': run['iteration_id'], 'work_item_id': work_id,
                        'attempt_id': attempt['id'], 'attempt_generation': attempt['generation'],
                        'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint']}
            if (existing.get('authorization_digest') != timeout_authorization_digest(existing)
                    or existing.get('original_identity') != expected
                    or existing.get('process_evidence_digest') != canonical_digest(process)):
                raise DomainError('timeout_evidence_invalid', '已有超时授权与当前原执行身份不匹配。')
            self._verify_owner_grant(state, work, existing)
            await asyncio.to_thread(self._verify_files, state, process, existing['stop_receipt'], existing['coding_progress'])
            if existing['unknown_invocations'] and not self._transport_stopped(state):
                raise DomainError('model_consumer_active', '原模型请求仍可能被消费，未重新授权。')
            blockers = self.recovery._common_blockers(state)
            blockers += await coding_usage_blockers(state, self.recovery.data_dir, work_id)
            if blockers:
                raise DomainError(blockers[0]['code'], blockers[0]['message'])
            return True
        pending = self._pending_invocations(state)
        members = self._group(state, work_id, pending)
        policy, counts = self._group_limits(state, members)
        blockers = self.recovery._common_blockers(state, ignore_model_uncertainty=True)
        blockers += await self.recovery._process_blockers(state)
        if blockers:
            raise DomainError(blockers[0]['code'], blockers[0]['message'])
        attempt_ids = sorted(member[2]['id'] for member in members)
        command_id = identity if len(members) == 1 else str(uuid5(NAMESPACE_URL,
            'timeout-recovery-group:' + canonical_digest({'run_id': run_id, 'attempt_ids': attempt_ids})))
        digest = canonical_digest(state)
        grants = []
        for ordinal, (member, count) in enumerate(zip(members, counts, strict=True)):
            member_id = _grant_id(run_id, member[2]['id'])
            if any(row['id'] == member_id for row in state[TIMEOUT_KIND]):
                raise DomainError('timeout_evidence_invalid', '已有超时授权仍存在未确认调用，不能覆盖原授权。')
            grants.append(await self._draft(state, member, member_id, policy,
                {**count, 'run': count['run'] + ordinal}, digest,
                [row for row in pending if row.get('attempt_id') == member[2]['id']],
                {'group_id': command_id, 'group_attempt_ids': attempt_ids} if len(members) > 1 else {}))
        # Several historical delegated tasks may share a pool. Each child adds
        # only its existing policy increment; write the aggregate pool once.
        owner_updates = {}
        for grant in grants:
            binding, pool = grant['audit'].get('owner_budget'), grant.get('owner_pool')
            if not binding:
                continue
            update = owner_updates.setdefault(pool['id'], {'pool': pool, 'limits': dict(binding['old_limits'])})
            for key, value in binding['additional_limits'].items():
                update['limits'][key] += value
        for grant in grants:
            binding = grant['audit'].get('owner_budget')
            if binding:
                binding['new_limits'] = dict(owner_updates[binding['budget_id']]['limits'])
                grant['audit']['authorization_digest'] = timeout_authorization_digest(grant['audit'])
                for ack in grant['acks']:
                    ack['authorization_digest'] = grant['audit']['authorization_digest']

        def apply(tx):
            current = _related({kind: tx.list(kind) for kind in state}, run_id)
            if canonical_digest(current) != digest or _policy(self.workflow.settings) != policy:
                raise DomainError('timeout_retry_stale', '超时续跑核验期间状态或策略变化，未追加额度。')
            self._group_limits(current, self._group(current, work_id, self._pending_invocations(current)))
            for member, grant in zip(members, grants, strict=True):
                self._verify_files(current, member[3], grant['audit']['stop_receipt'], grant['audit']['coding_progress'])
                self._owner_allowance(current, member[1], grant['budget'],
                                      grant['audit']['old_limits'], grant['audit']['new_limits'])
            if pending and not self._transport_stopped(current):
                raise DomainError('model_consumer_active', '原模型请求仍可能被消费，未追加额度。')
            # Validate all acknowledgments together before writing any allowance.
            simulated = {**current,
                TIMEOUT_KIND: [*current[TIMEOUT_KIND], *[{**grant['audit'], 'id': grant['id']} for grant in grants]],
                ACK_KIND: [*current[ACK_KIND], *[ack for grant in grants for ack in grant['acks']]]}
            remaining = self.recovery._common_blockers(simulated)
            if remaining:
                raise DomainError(remaining[0]['code'], remaining[0]['message'])
            for grant in grants:
                audit, budget = grant['audit'], grant['budget']
                tx.put(TIMEOUT_KIND, grant['id'], audit)
                if budget and audit['new_limits'] != audit['old_limits']:
                    tx.put('coding_work_budget', budget['id'], {**budget, **audit['new_limits']}, budget['revision'])
                for ack in grant['acks']:
                    tx.put(ACK_KIND, ack['id'], ack)
                    tx.event('model.uncertainty_acknowledged', ack, run_id=run_id)
                tx.event('timeout.retry_authorized', {'timeout_recovery_id': grant['id'], **audit}, run_id=run_id)
            for update in owner_updates.values():
                pool = update['pool']
                if any(pool[key] != value for key, value in update['limits'].items()):
                    tx.put('coding_work_budget', pool['id'], {**pool, **update['limits']}, pool['revision'])
            return ({'authorized': True, 'timeout_recovery_id': grants[0]['id']} if len(grants) == 1
                    else {'authorized': True, 'timeout_recovery_ids': [grant['id'] for grant in grants]})
        # All entry points for the same group replay the same atomic reservation.
        payload = ({'run_id': run_id, 'work_item_id': work_id, 'attempt_id': attempt['id']} if len(members) == 1
                   else {'run_id': run_id, 'attempt_ids': attempt_ids})
        result = await self.store.command('timeout.retry.prepare', command_id, payload, apply)
        return result['authorized']

    async def _draft(self, state, member, identity, policy, counts, digest, pending, group):
        run, work, attempt, process = member
        run_id, work_id = run['id'], work['id']
        blockers = await coding_usage_blockers(state, self.recovery.data_dir, work_id)
        if blockers:
            raise DomainError(blockers[0]['code'], blockers[0]['message'])
        receipt, window = await asyncio.to_thread(self._receipt, process)
        acknowledgments = self._pending_acknowledgments(state, attempt, receipt, window, pending)
        budget, old, new = self._budget(state, run, work)
        owner_pool, owner_binding = self._owner_allowance(state, work, budget, old, new)
        if owner_pool:
            blockers = await coding_usage_blockers(state, self.recovery.data_dir, owner_pool['work_item_id'])
            if blockers:
                raise DomainError(blockers[0]['code'], blockers[0]['message'])
        target = {'root_work_item_ids': [work_id]}
        points = await self.recovery._checkpoints(state, target, freeze=True, recovery_id=identity,
                                                  include_unchanged=True)
        progress = None
        if work.get('step') in CODING_STEPS:
            if not points:
                raise DomainError('recovery_checkpoint_invalid', '超时任务缺少可保留的原代码检查点。')
            progress = await self.recovery.coding_progress(state, target)
        audit = {'actor': 'system', 'authorization_kind': 'timeout_retry_policy', 'version': 1,
            'status': 'authorized', 'run_id': run_id, 'iteration_id': run['iteration_id'],
            'work_item_id': work_id, 'attempt_id': attempt['id'], 'work_generation': work['generation'],
            'original_identity': {key: value for key, value in {
                'run_id': run_id, 'iteration_id': run['iteration_id'], 'work_item_id': work_id,
                'attempt_id': attempt['id'], 'attempt_generation': attempt['generation'],
                'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint']}.items()},
            'retry_ordinal': counts['timeouts_work'] + 1, 'retry_counts_before': counts, 'policy': policy,
            'budget_id': budget['id'] if budget else None, 'old_limits': old, 'new_limits': new,
            'original_budget_limit': run['budget_limit'],
            'preserved_usage': {field: budget[field] for field in
                ('active_seconds', 'observed_tool_calls', 'step_count', 'max_steps')} if budget else {},
            'process_evidence_digest': canonical_digest(process), 'stop_receipt': receipt,
            'stop_receipt_digest': canonical_digest(receipt), 'disconnect_window_seconds': window,
            'checkpoint': points, 'coding_progress': progress, 'unknown_invocations': acknowledgments,
            'guard_state_digest': digest, **group, 'created_at': utc_now()}
        if owner_binding:
            audit['owner_budget'] = owner_binding
        audit['authorization_digest'] = timeout_authorization_digest(audit)
        drafts = []
        for accepted in acknowledgments:
            ack_id = str(uuid5(NAMESPACE_URL, f'timeout-ack:{identity}:{accepted["invocation_id"]}'))
            drafts.append({'id': ack_id, 'actor': 'system', 'authorization_kind': 'timeout_retry_policy',
                'run_id': run_id, 'iteration_id': run['iteration_id'], 'work_item_id': work_id,
                'work_generation': work['generation'], 'attempt_id': attempt['id'],
                'invocation_id': accepted['invocation_id'], 'basis': accepted['basis'],
                'timeout_recovery_id': identity, 'authorization_digest': audit['authorization_digest'],
                'accept_unknown_usage': True, 'requires_explicit_retry': False,
                'reason': '按已启用的超时续跑策略保留原调用未知用量，新执行使用新的请求。', 'created_at': audit['created_at']})
        return {'id': identity, 'audit': audit, 'budget': budget, 'owner_pool': owner_pool, 'acks': drafts}

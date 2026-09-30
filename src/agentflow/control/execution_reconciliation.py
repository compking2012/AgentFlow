"""Resolve a late, verified failure receipt without claiming a review succeeded."""
from __future__ import annotations

import asyncio
import json
from uuid import NAMESPACE_URL, uuid5

import psutil

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.recovery import RunRecoveryService
from agentflow.runtime.failures import (
    _private_attempt_directory,
    _private_evidence,
    known_failure_reason,
    runtime_failure_message,
)
from agentflow.runtime.startup_evidence import prove_stopped_unpermitted_launcher
from agentflow.runtime.trace import ExecutionTrace


class ExecutionReconciliation:
    def __init__(self, store, workflow):
        self.store, self.workflow = store, workflow
        self.recovery = RunRecoveryService(store, workflow)
        self._lock = asyncio.Lock()
        from agentflow.control.coding_usage_reconciliation import LateCodingUsageReconciliation
        self.coding_usage = LateCodingUsageReconciliation(self)

    def _receipt(self, process, attempt):
        folder = canonical_digest({'attempt_id': attempt['id']})[7:]
        with _private_attempt_directory(self.workflow.settings.data_dir, 'supervisor', folder) as directory:
            raw = _private_evidence(directory, 'result.json', 65536)
        receipt = json.loads(raw)
        if (not isinstance(receipt, dict) or receipt.get('execution_status') != 'failed'
                or type(receipt.get('exit_code')) is not int or receipt['exit_code'] == 0):
            raise DomainError('late_receipt_not_failed', '旧回执不能证明执行失败，未改变未知状态。')
        observed = {**process, 'state': 'failed', 'reason': receipt.get('reason'), 'exit_code': receipt['exit_code']}
        if process['state'] == 'failed' and any(process.get(key) != observed[key] for key in ('reason', 'exit_code')):
            raise DomainError('late_receipt_conflict', '旧回执与已经记录的进程结果不一致。')
        self.recovery._verify_process(observed, {attempt['id']: attempt})
        return receipt, canonical_digest(receipt), observed

    @staticmethod
    def _matches(work, attempt, process, context, run, *, startup=False):
        if not all(isinstance(row, dict) for row in (work, attempt, process, context, run)):
            return False
        task = (context or {}).get('task') or {}
        if not isinstance(task, dict):
            return False
        statuses_match = (work.get('status') == attempt.get('status') == 'execution_unknown'
            or startup and work.get('status') == attempt.get('status') == 'blocked'
            and work.get('runtime_failure_code') == attempt.get('runtime_failure_code') == 'execution_unconfirmed'
            or startup and work.get('status') == attempt.get('status') == 'cancelled'
            and attempt.get('execution_status') == 'cancelled'
            and work.get('quality_result') == attempt.get('quality_result') == 'unknown')
        return (run and run.get('execution_state') in {'running', 'paused'}
            and not any(row.get(flag) for row in (run, work, attempt, process, context)
                        for flag in ('restore_uncertain', 'restore_reconciliation_required', 'restore_revalidation_required'))
            and not work.get('archived') and statuses_match and work.get('attempt_id') == attempt['id']
            and process.get('state') in ({'cancelled', 'execution_unknown'} if startup else {'failed', 'execution_unknown'})
            and process.get('attempt_id') == attempt['id'] and process.get('run_id') == run['id']
            and work.get('run_id') == run['id'] and attempt.get('run_id') == run['id']
            and attempt.get('work_item_id') == work['id'] and task.get('attempt_id') == attempt['id']
            and task.get('work_item_id') == work['id'] and task.get('run_id') == run['id']
            and task.get('iteration_id') == attempt.get('iteration_id') == run.get('iteration_id')
            and task.get('step') == work.get('step')
            and all(work.get(key) == attempt.get(key) for key in ('generation', 'fencing_token', 'input_fingerprint'))
            and all(task.get(key) == process.get(key) == attempt.get(key) for key in ('fencing_token', 'input_fingerprint')))


    @staticmethod
    def _startup_usage(invocations, budget, attempt):
        if (any(row.get('attempt_id') == attempt['id'] for row in invocations)
                or budget and (budget.get('id') != attempt['id'] or budget.get('attempt_id') != attempt['id']
                    or any(budget.get(flag) for flag in ('restore_uncertain', 'restore_reconciliation_required',
                                                       'restore_revalidation_required'))
                    or any(type(budget.get(key)) is not int or budget[key] != 0
                           for key in ('request_count', 'uncertain_invocations')))):
            raise DomainError('startup_usage_unproven', '原执行含模型调用或未核对用量，保留原状态。')

    def _startup_receipt(self, process, attempt, context):
        task = context['task']
        if (process.get('id') != attempt['id'] or context.get('id') != attempt['id']
                or type(attempt.get('generation')) is not int or attempt['generation'] < 1
                or type(attempt.get('fencing_token')) is not int or attempt['fencing_token'] < 1
                or 'generation' in task and task['generation'] != attempt['generation']
                or any(not isinstance(attempt.get(key), str) or not attempt[key]
                       for key in ('run_id', 'iteration_id', 'work_item_id', 'input_fingerprint'))):
            raise DomainError('startup_binding_invalid', '启动证据与原执行代次不一致。')
        return prove_stopped_unpermitted_launcher(self.workflow.settings.data_dir, process)

    async def _reconcile_startup(self, work, attempt, process, context, run):
        try:
            invocations, budget = await asyncio.gather(self.store.list('model_invocation'),
                self.store.read('model_attempt_budget', attempt['id']))
            self._startup_usage(invocations, budget, attempt)
            proof = await asyncio.to_thread(self._startup_receipt, process, attempt, context)
            identity = str(uuid5(NAMESPACE_URL, 'execution-reconciliation:' + attempt['id']))
            code = 'launcher_startup_timeout'

            def apply(tx):
                current = [tx.get(kind, row['id']) for kind, row in (
                    ('work_item', work), ('attempt', attempt), ('supervised_attempt', process), ('dispatch_context', context))]
                if current != [work, attempt, process, context] or not self._matches(
                        work, attempt, process, context, tx.get('run', run['id']), startup=True):
                    raise DomainError('late_receipt_stale', '核验期间状态已更新，等待下一次重新核验。')
                self._startup_usage(tx.list('model_invocation'), tx.get('model_attempt_budget', attempt['id']), attempt)
                verified = self._startup_receipt(process, attempt, context)
                if verified['evidence_digest'] != proof['evidence_digest']:
                    raise DomainError('late_receipt_changed', '核验期间原始回执发生变化，未修改执行状态。')
                now = utc_now()
                audit = tx.put('execution_reconciliation', identity, {
                    'run_id': run['id'], 'work_item_id': work['id'], 'attempt_id': attempt['id'],
                    'generation': attempt['generation'], 'fencing_token': attempt['fencing_token'],
                    'input_fingerprint': attempt['input_fingerprint'], 'dispatch_context_revision': context['revision'],
                    'dispatch_task_digest': canonical_digest(context['task']), 'actor': 'controller',
                    'original_work': work, 'original_attempt': attempt, 'original_process': process,
                    **verified, 'execution_status': 'cancelled', 'runtime_failure_code': code, 'created_at': now})
                observed = {**process, **verified['identity'], 'state': 'cancelled',
                    'reason': 'launch_not_authorized', 'exit_code': None, 'updated_at': now,
                    'execution_reconciliation_id': identity}
                tx.put('supervised_attempt', process['id'], observed, process['revision'])
                tx.put('work_item', work['id'], {**work, 'status': 'failed', 'quality_result': 'unknown',
                    'runtime_failure_code': code, 'blocking_reason': runtime_failure_message(code)}, work['revision'])
                tx.put('attempt', attempt['id'], {**attempt, 'status': 'failed', 'execution_status': 'failed',
                    'quality_result': 'unknown', 'runtime_failure_code': code, 'summary': runtime_failure_message(code),
                    'finished_at': verified['finished_at'], 'reconciled_at': now,
                    'execution_reconciliation_id': identity}, attempt['revision'])
                tx.event('attempt.execution_reconciled', {'attempt_id': attempt['id'], 'reconciliation_id': audit['id'],
                    'execution_status': 'cancelled', 'outcome': 'not_started', 'reason': verified['reason'],
                    'runtime_failure_code': code}, run_id=run['id'])
                self.workflow._recompute_run(tx, run['id'])
                return {'reconciled': True, 'reconciliation_id': identity}
            await self.store.command('execution.reconcile', identity,
                {'attempt_id': attempt['id'], 'receipt_digest': proof['evidence_digest']}, apply)
            await ExecutionTrace(self.store).emit(attempt['id'], 'status', 'Agent 启动等待超时',
                runtime_failure_message(code), key='startup-reconciled:' + attempt['id'], status='failed')
        except (DomainError, OSError, ValueError, TypeError, KeyError, psutil.Error):
            return

    async def reconcile(self):
        async with self._lock:
            for work in await self.store.list('work_item'):
                if (work.get('status') not in {'execution_unknown', 'cancelled'} and not (work.get('status') == 'blocked'
                        and work.get('runtime_failure_code') == 'execution_unconfirmed')
                        or not work.get('attempt_id') or work.get('archived')):
                    continue
                attempt, process, context, run = await asyncio.gather(
                    self.store.read('attempt', work['attempt_id']), self.store.read('supervised_attempt', work['attempt_id']),
                    self.store.read('dispatch_context', work['attempt_id']), self.store.read('run', work['run_id']))
                if self._matches(work, attempt, process, context, run, startup=True):
                    await self._reconcile_startup(work, attempt, process, context, run)
                if not attempt or not process or not self._matches(work, attempt, process, context, run):
                    continue
                try:
                    receipt, digest, observed = await asyncio.to_thread(self._receipt, process, attempt)
                except (DomainError, OSError, ValueError, TypeError, KeyError):
                    continue
                identity = str(uuid5(NAMESPACE_URL, 'execution-reconciliation:' + attempt['id']))
                code = known_failure_reason(receipt.get('reason')) or 'worker_exited'

                def apply(tx):
                    current = [tx.get(kind, row['id']) for kind, row in (
                        ('work_item', work), ('attempt', attempt), ('supervised_attempt', process), ('dispatch_context', context))]
                    if current != [work, attempt, process, context] or not self._matches(
                            work, attempt, process, context, tx.get('run', run['id'])):
                        # Do not cache a transient no-op under this attempt's
                        # reconciliation key; a later stable snapshot must retry.
                        raise DomainError('late_receipt_stale', '核验期间状态已更新，等待下一次重新核验。')
                    verified, current_digest, _ = self._receipt(process, attempt)
                    if current_digest != digest:
                        raise DomainError('late_receipt_changed', '核验期间原始回执发生变化，未修改执行状态。')
                    audit = tx.put('execution_reconciliation', identity, {'run_id': run['id'], 'work_item_id': work['id'],
                        'attempt_id': attempt['id'], 'actor': 'controller', 'original_work': work,
                        'original_attempt': attempt, 'original_process': process, 'receipt_digest': digest,
                        'receipt': verified, 'execution_status': 'failed', 'created_at': utc_now()})
                    if process['state'] != 'failed':
                        tx.put('supervised_attempt', process['id'], {**observed, 'updated_at': utc_now()}, process['revision'])
                    tx.put('work_item', work['id'], {**work, 'status': 'failed', 'quality_result': 'unknown',
                        'runtime_failure_code': code, 'blocking_reason': runtime_failure_message(code)}, work['revision'])
                    tx.put('attempt', attempt['id'], {**attempt, 'status': 'failed', 'execution_status': 'failed',
                        'quality_result': 'unknown', 'runtime_failure_code': code, 'summary': runtime_failure_message(code),
                        'finished_at': receipt.get('finished_at'), 'reconciled_at': utc_now(),
                        'execution_reconciliation_id': identity}, attempt['revision'])
                    tx.event('attempt.execution_reconciled', {'attempt_id': attempt['id'], 'reconciliation_id': audit['id'],
                        'execution_status': 'failed', 'runtime_failure_code': code}, run_id=run['id'])
                    self.workflow._recompute_run(tx, run['id'])
                    return {'reconciled': True, 'reconciliation_id': identity}
                try:
                    await self.store.command('execution.reconcile', identity, {'attempt_id': attempt['id'], 'receipt_digest': digest}, apply)
                except (DomainError, OSError, ValueError, TypeError, KeyError):
                    # A missing or changed receipt stays uncertain until fresh
                    # evidence exists. No new execution is started here.
                    continue
            await self.coding_usage.reconcile()

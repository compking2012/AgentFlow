"""Account a verified late failure exactly once; never change execution or model settlement."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from uuid import NAMESPACE_URL, uuid5

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.coding_steps import CodingSteps
from agentflow.domain.planning import CODING_STEPS
from agentflow.models.uncertainty import TERMINAL_INVOCATIONS
from agentflow.runtime.codex_usage import observed_tool_usage
from agentflow.runtime.coding_receipt import output_schema
from agentflow.runtime.events import CodexEventNormalizer
from agentflow.runtime.failures import _private_attempt_directory, _private_evidence

KINDS = ('run', 'work_item', 'attempt', 'supervised_attempt', 'dispatch_context', 'coding_step_control',
         'coding_step_usage', 'coding_work_budget', 'execution_reconciliation', 'model_invocation', 'model_attempt_budget',
         'review_contract_budget_charge')
FLAGS = ('restore_uncertain', 'restore_reconciliation_required', 'restore_revalidation_required')
BINDING = ('generation', 'fencing_token', 'input_fingerprint')


def invalid():
    return DomainError('late_coding_usage_unverified', '晚到编码用量尚无法完整核验，保留原未知计量。')


def duration(value):
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


class ReadState:
    def __init__(self, records):
        self.records = records

    def get(self, kind, identity):
        return next((row for row in self.records[kind] if row['id'] == identity), None)

    def list(self, kind):
        return self.records[kind]


class LateCodingUsageReconciliation:
    def __init__(self, execution):
        self.execution, self.store = execution, execution.store
        self.data_dir = execution.workflow.settings.data_dir.resolve()

    def _capture(self, tx, attempt_id):
        attempt = tx.get('attempt', attempt_id)
        if not attempt:
            raise invalid()
        work = tx.get('work_item', attempt.get('work_item_id'))
        run = tx.get('run', attempt.get('run_id'))
        process = tx.get('supervised_attempt', attempt_id)
        context = tx.get('dispatch_context', attempt_id)
        control = tx.get('coding_step_control', attempt_id)
        usage = tx.get('coding_step_usage', attempt_id)
        audit = tx.get('execution_reconciliation', attempt.get('execution_reconciliation_id'))
        if not all(isinstance(row, dict) for row in (work, run, process, context, control, usage, audit)):
            raise invalid()
        task = context.get('task')
        budget = tx.get('coding_work_budget', control.get('budget_id'))
        if not isinstance(task, dict) or not budget:
            raise invalid()
        if (run.get('execution_state') not in {'running', 'paused'} or run.get('delivery_ids')
                or work.get('archived') or work.get('step') not in CODING_STEPS
                or work.get('status') != 'failed' or attempt.get('status') != 'failed'
                or attempt.get('execution_status') != 'failed' or work.get('attempt_id') != attempt_id
                or work.get('run_id') != run['id'] or attempt.get('iteration_id') != run.get('iteration_id')
                or process.get('backend') != 'codex_exec' or process.get('state') != 'failed'
                or process.get('attempt_id') != attempt_id or process.get('run_id') != run['id']
                or task.get('attempt_id') != attempt_id or task.get('run_id') != run['id']
                or task.get('work_item_id') != work['id'] or task.get('iteration_id') != run.get('iteration_id')
                or task.get('step') != work['step'] or task.get('role') != work.get('role')
                or task.get('allowed_write_paths') != work.get('write_paths')
                or task.get('source_commit') != control.get('source_commit')
                or task.get('output_schema') != output_schema() or task.get('coding_step') != control
                or control.get('attempt_id') != attempt_id or control.get('work_item_id') != work['id']
                or control.get('run_id') != run['id']
                or any(work.get(k) != attempt.get(k) or control.get(k) != attempt.get(k) for k in BINDING)
                or any(task.get(k) != attempt.get(k) or process.get(k) != attempt.get(k)
                       for k in ('fencing_token', 'input_fingerprint'))
                or usage.get('known') is not False or usage.get('active_seconds') is not None
                or usage.get('observed_tool_calls') is not None or budget.get('uncertain') is not True
                or budget['id'] != CodingSteps.budget_id(run['id'], work['id'])
                or budget.get('run_id') != run['id'] or budget.get('work_item_id') != work['id']
                or audit.get('actor') != 'controller' or audit.get('execution_status') != 'failed'
                or audit.get('run_id') != run['id'] or audit.get('work_item_id') != work['id']
                or audit.get('attempt_id') != attempt_id
                or audit.get('receipt_digest') != canonical_digest(audit.get('receipt'))
                or audit.get('original_attempt', {}).get('status') != 'execution_unknown'
                or audit.get('original_work', {}).get('status') != 'execution_unknown'
                or any(audit.get('original_attempt', {}).get(k) != attempt.get(k)
                       for k in ('id', 'run_id', 'work_item_id', 'iteration_id', *BINDING))
                or any(audit.get('original_work', {}).get(k) != work.get(k)
                       for k in ('id', 'run_id', 'write_paths', *BINDING))):
            raise invalid()
        calls = sorted((row for row in tx.list('model_invocation') if row.get('attempt_id') == attempt_id), key=lambda r: r['id'])
        model_budget = tx.get('model_attempt_budget', attempt_id)
        if (any(row.get('state') not in TERMINAL_INVOCATIONS or row.get('run_id') != run['id']
                or row.get('iteration_id') != run.get('iteration_id') or
                any(row.get(k) != attempt.get(k) for k in ('fencing_token', 'input_fingerprint')) for row in calls)
                or (calls and not model_budget)
                or (model_budget and (model_budget.get('uncertain_invocations') != 0
                    or type(model_budget.get('request_count')) is not int or model_budget['request_count'] != len(calls)))):
            raise invalid()
        usages = sorted((row for row in tx.list('coding_step_usage')
                         if row.get('budget_id') == budget['id'] or row.get('work_item_id') == work['id']), key=lambda r: r['id'])
        controls = sorted((row for row in tx.list('coding_step_control')
                           if row.get('budget_id') == budget['id'] or row.get('work_item_id') == work['id']), key=lambda r: r['id'])
        control_ids = {row['id'] for row in controls}
        charges = sorted((row for row in tx.list('review_contract_budget_charge')
                          if row.get('owner_budget_id') == budget['id'] or row.get('owner_work_item_id') == work['id']),
                         key=lambda row: row['id'])
        from agentflow.control.review_contract_binding import delegated_usage_totals
        delegated = delegated_usage_totals(charges, run['id'], work['id'])
        if set(delegated) - {budget['id']} or {row['id'] for row in charges} & control_ids:
            raise invalid()
        charge = delegated.get(budget['id'], {'count': 0, 'seconds': 0.0, 'calls': 0, 'unknown': False})
        rows = [run, work, attempt, process, context, control, usage, budget, audit, model_budget or {}, *calls, *usages, *controls, *charges]
        if (any(row.get(flag) for row in rows for flag in FLAGS)
                or any(type(budget.get(k)) is not int or budget[k] < 0
                       for k in ('step_count', 'max_steps', 'observed_tool_calls', 'max_tool_calls'))
                or any(not duration(budget.get(k)) for k in ('active_seconds', 'max_active_seconds'))
                or budget['step_count'] != len(usages) + charge['count'] or budget['step_count'] != control.get('step_number')):
            raise invalid()
        seconds, tools = charge['seconds'], charge['calls']
        for row in usages:
            if (row['id'] not in control_ids or row.get('run_id') != run['id'] or row.get('work_item_id') != work['id']
                    or row.get('budget_id') != budget['id'] or type(row.get('known')) is not bool):
                raise invalid()
            if row['known']:
                if not duration(row.get('active_seconds')) or type(row.get('observed_tool_calls')) is not int or row['observed_tool_calls'] < 0:
                    raise invalid()
                seconds += row['active_seconds']
                tools += row['observed_tool_calls']
            elif row.get('active_seconds') is not None or row.get('observed_tool_calls') is not None:
                raise invalid()
        if (not math.isclose(budget['active_seconds'], seconds, rel_tol=0, abs_tol=1e-6)
                or budget['observed_tool_calls'] != tools
                or any(row.get('run_id') != run['id'] or row.get('work_item_id') != work['id']
                       or row.get('budget_id') != budget['id'] or row.get('attempt_id') != row['id'] for row in controls)):
            raise invalid()
        return {'attempt': attempt, 'work': work, 'run': run, 'process': process, 'context': context,
            'control': control, 'usage': usage, 'budget': budget, 'execution_reconciliation': audit,
            'model_invocations': calls, 'model_budget': model_budget, 'usages': usages, 'controls': controls, 'delegated_charges': charges, 'delegated_unknown': charge['unknown']}

    def _evidence(self, state):
        receipt, digest, _ = self.execution._receipt(state['process'], state['attempt'])
        audit = state['execution_reconciliation']
        if (receipt != audit['receipt'] or digest != audit['receipt_digest']
                or receipt.get('logs_truncated') is not False or not duration(receipt.get('active_seconds'))):
            raise invalid()
        folder = canonical_digest({'attempt_id': state['attempt']['id']})[7:]
        evidence = {}
        with _private_attempt_directory(self.data_dir, 'supervisor', folder) as directory:
            raw_receipt = _private_evidence(directory, 'result.json', 65536)
            raw_log = _private_evidence(directory, 'stdout.jsonl', 16 * 1024 * 1024)
            child = _private_evidence(directory, 'child.json', 65536)
            child_identity = json.loads(child)
            if (json.loads(raw_receipt) != receipt or not isinstance(child_identity, dict)
                    or not isinstance(child_identity.get('child'), dict) or not child_identity['child']):
                raise invalid()
            for name, raw in (('result.json', raw_receipt), ('stdout.jsonl', raw_log), ('child.json', child)):
                evidence[name] = 'sha256:' + hashlib.sha256(raw).hexdigest() if raw is not None else None
        raw_log.decode('utf-8')  # Invalid bytes cannot prove complete observation.
        parsed = CodexEventNormalizer().parse_bytes(raw_log)
        usage = observed_tool_usage(parsed, receipt.get('reason'))
        if (usage['tool_observation_complete'] is not True or any(event['type'].startswith('item.')
                and not isinstance(event['raw'].get('item'), dict) for event in parsed['events'])):
            raise invalid()
        return {'files': evidence, 'active_seconds': receipt['active_seconds'], **usage,
                'task_digest': canonical_digest(state['context']['task']),
                'schema_digest': canonical_digest(state['context']['task']['output_schema'])}

    async def reconcile(self):
        for attempt in await self.store.list('attempt'):
            if attempt.get('status') != 'failed' or not attempt.get('execution_reconciliation_id'):
                continue
            usage = await self.store.read('coding_step_usage', attempt['id'])
            if not usage or usage.get('known') is not False:
                continue
            try:
                rows = await asyncio.gather(*(self.store.list(kind) for kind in KINDS))
                observed = self._capture(ReadState(dict(zip(KINDS, rows, strict=True))), attempt['id'])
                proof = await asyncio.to_thread(self._evidence, observed)
                identity = str(uuid5(NAMESPACE_URL, 'late-coding-usage:' + attempt['id']))
                def apply(tx):
                    current = self._capture(tx, attempt['id'])
                    if current != observed or self._evidence(current) != proof:
                        raise invalid()
                    budget = current['budget']
                    remaining = bool(current['delegated_unknown'] or budget.get('review_contract_external_uncertain'))
                    remaining |= any(row['id'] != attempt['id'] and row['known'] is not True for row in current['usages'])
                    remaining |= bool({row['id'] for row in current['controls']} - {row['id'] for row in current['usages']})
                    audit = tx.put('coding_usage_reconciliation', identity, {'run_id': current['run']['id'],
                        'work_item_id': current['work']['id'], 'attempt_id': attempt['id'], 'actor': 'controller',
                        'execution_reconciliation_id': current['execution_reconciliation']['id'],
                        'original_usage': current['usage'], 'original_budget': budget, 'evidence': proof, 'created_at': utc_now()})
                    tx.put('coding_step_usage', attempt['id'], {**current['usage'], 'known': True,
                        'active_seconds': proof['active_seconds'], 'observed_tool_calls': proof['observed_tool_calls'],
                        'usage_reconciliation_id': identity, 'reconciled_at': utc_now()}, current['usage']['revision'])
                    tx.put('coding_work_budget', budget['id'], {**budget,
                        'active_seconds': budget['active_seconds'] + proof['active_seconds'],
                        'observed_tool_calls': budget['observed_tool_calls'] + proof['observed_tool_calls'],
                        'uncertain': remaining}, budget['revision'])
                    from agentflow.control.review_contract_binding import charge_owner_budget
                    charge_owner_budget(tx, current['context']['task'], tx.get('coding_work_budget', budget['id']),
                        known=True, seconds=proof['active_seconds'], calls=proof['observed_tool_calls'])
                    tx.event('coding.usage_reconciled', {'attempt_id': attempt['id'], 'reconciliation_id': audit['id']},
                             run_id=current['run']['id'])
                    return {'reconciliation_id': identity}
                await self.store.command('coding.usage_reconcile', identity,
                    {'attempt_id': attempt['id'], 'evidence': proof, 'original_usage': observed['usage']}, apply)
            except (DomainError, OSError, ValueError, TypeError, KeyError):
                # Incomplete files and transient races may become verifiable later.
                # Never cache a no-op or change the original unknown accounting.
                continue

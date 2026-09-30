"""Recollect a stopped coding result without launching or rebudgeting execution."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.recovery import RunRecoveryService, coding_usage_blockers, coding_usage_snapshot
from agentflow.domain.planning import CODING_STEPS
from agentflow.models.uncertainty import TERMINAL_INVOCATIONS
from agentflow.runtime.coding_receipt import output_schema
from agentflow.runtime.failures import _private_attempt_directory, _private_evidence
from agentflow.runtime.workspace import WorkspaceManager

KIND = 'coding_result_recollection'
RULE_VERSION = 3
_FLAGS = ('restore_uncertain', 'restore_reconciliation_required', 'restore_revalidation_required')
_BINDING = ('generation', 'fencing_token', 'input_fingerprint')
logger = logging.getLogger(__name__)


def _error(code='coding_result_recollection_invalid'):
    return DomainError(code, '旧编码结果的身份、代码或用量无法核验，保留原执行且未启动新任务。')


def _task(task):
    return {key: value for key, value in task.items() if key != 'recollection_id'}


def _authorization(record):
    return canonical_digest({key: record.get(key) for key in ('rule_version', 'actor', 'execution', 'run_id', 'work_item_id',
        'attempt_id', 'original_work', 'original_attempt', 'task', 'original_process', 'original_usage',
        'original_budget', 'original_model_budget', 'original_invocations', 'original_snapshot',
        'evidence', 'snapshot', 'result')})


def _current(tx, task):
    task = _task(task)
    attempt = tx.get('attempt', task['attempt_id'])
    work = tx.get('work_item', task['work_item_id'])
    run = tx.get('run', task['run_id'])
    context = tx.get('dispatch_context', task['attempt_id'])
    control = tx.get('coding_step_control', task['attempt_id'])
    process = tx.get('supervised_attempt', task['attempt_id'])
    usage = tx.get('coding_step_usage', task['attempt_id'])
    budget = tx.get('coding_work_budget', (control or {}).get('budget_id', 'missing'))
    model_budget = tx.get('model_attempt_budget', task['attempt_id'])
    calls = sorted((row for row in tx.list('model_invocation') if row.get('attempt_id') == task['attempt_id']),
                   key=lambda row: row['id'])
    rows = [run, work, attempt, process, usage, budget, control, *(calls or [])]
    if (not all(row is not None for row in rows) or not context or context.get('task') != task
            or run.get('execution_state') != 'running' or run.get('delivery_ids')
            or work.get('archived') or work.get('kind') == 'aggregation'
            or work.get('step') not in CODING_STEPS or task.get('step') != work.get('step')
            or work.get('status') not in {'failed', 'blocked'} or attempt.get('status') not in {'failed', 'blocked'}
            or work.get('attempt_id') != attempt['id'] or attempt.get('work_item_id') != work['id']
            or work.get('run_id') != run['id'] or attempt.get('run_id') != run['id']
            or task.get('iteration_id') != run.get('iteration_id')
            or attempt.get('iteration_id') != run.get('iteration_id')
            or control != task.get('coding_step') or control.get('attempt_id') != attempt['id']
            or control.get('work_item_id') != work['id'] or control.get('run_id') != run['id']
            or task.get('allowed_write_paths') != work.get('write_paths')
            or task.get('source_commit') != control.get('source_commit')
            or any(work.get(key) != attempt.get(key) or control.get(key) != attempt.get(key) for key in _BINDING)
            or any(task.get(key) != attempt.get(key) for key in ('fencing_token', 'input_fingerprint'))
            or process.get('attempt_id') != attempt['id'] or process.get('run_id') != run['id']
            or process.get('backend') != 'codex_exec' or process.get('state') != 'completed'
            or process.get('exit_code') != 0
            or any(process.get(key) != attempt.get(key) for key in ('fencing_token', 'input_fingerprint'))
            or any(row.get(flag) for row in [*rows, model_budget or {}] for flag in _FLAGS)
            or any(row.get('state') not in TERMINAL_INVOCATIONS or row.get('run_id') != run['id']
                   or row.get('iteration_id') != run['iteration_id']
                   or any(row.get(key) != attempt.get(key) for key in ('fencing_token', 'input_fingerprint'))
                   for row in calls)
            or (model_budget and model_budget.get('uncertain_invocations', 0) != 0)):
        raise _error()
    if any(type(row.get('overrun_micros')) in {int, float} and row['overrun_micros'] > 0 for row in calls):
        raise _error('coding_budget_exhausted')
    if (usage.get('known') is not True or usage.get('run_id') != run['id']
            or usage.get('work_item_id') != work['id'] or usage.get('budget_id') != budget['id']
            or budget.get('run_id') != run['id'] or budget.get('work_item_id') != work['id']
            or budget.get('uncertain') is not False
            or type(usage.get('active_seconds')) not in {int, float} or not math.isfinite(usage['active_seconds'])
            or usage['active_seconds'] < 0 or type(usage.get('observed_tool_calls')) is not int
            or usage['observed_tool_calls'] < 0):
        raise _error('coding_budget_uncertain')
    for used, maximum in [('active_seconds', 'max_active_seconds'), ('observed_tool_calls', 'max_tool_calls'),
                           ('step_count', 'max_steps')]:
        values = [budget.get(used), budget.get(maximum)]
        valid_type = (lambda value: type(value) in {int, float}) if used == 'active_seconds' else (lambda value: type(value) is int)
        if any(not valid_type(value) or not math.isfinite(value) or value < 0 for value in values):
            raise _error('coding_budget_uncertain')
        if values[0] > values[1]:
            raise _error('coding_budget_exhausted')
    if (budget['active_seconds'] < usage['active_seconds']
            or budget['observed_tool_calls'] < usage['observed_tool_calls']
            or budget['step_count'] != control.get('step_number')):
        raise _error('coding_budget_uncertain')
    return {'original_work': work, 'original_attempt': attempt, 'task': task,
        'original_process': process, 'original_usage': usage, 'original_budget': budget,
        'original_model_budget': model_budget, 'original_invocations': calls,
        'original_snapshot': tx.get('code_snapshot', attempt['id'])}


def guard_recollection(tx, recollection_id, *, attempt_id, work_item_id=None, task=None):
    """Authorize collector writes to this same stopped generation inside its writer."""
    record = tx.get(KIND, recollection_id)
    if (not record or record.get('status') != 'collecting' or record.get('rule_version') != RULE_VERSION
            or record.get('actor') != 'controller' or record.get('execution') != 'existing_result_only'
            or record.get('attempt_id') != attempt_id
            or (work_item_id is not None and record.get('work_item_id') != work_item_id)
            or record.get('authorization_digest') != _authorization(record)
            or (task is not None and _task(task) != record.get('task'))):
        raise _error()
    current = _current(tx, record['task'])
    for key, value in current.items():
        if key != 'original_snapshot' and value != record[key]:
            raise _error()
    snapshot = current['original_snapshot']
    if snapshot:
        validate_snapshot(record, record['task'], snapshot)
    elif record.get('original_snapshot'):
        raise _error()
    return record


def validate_snapshot(record, task, snapshot):
    """A replay may reuse an existing snapshot; it cannot replace its code tree."""
    task = _task(task)
    expected = record.get('snapshot', {})
    old = record.get('original_snapshot')
    if (task != record.get('task') or record.get('authorization_digest') != _authorization(record)
            or any(snapshot.get(key) != expected.get(key) for key in ('tree_oid', 'commit_oid'))
            or expected.get('source_commit') != task.get('source_commit')
            or expected.get('workspace') != task.get('workspace')
            or (snapshot.get('repository_path') is not None and snapshot['repository_path'] != task['workspace'])
            or (snapshot.get('work_item_id') is not None and snapshot['work_item_id'] != task['work_item_id'])
            or (snapshot.get('run_id') is not None and snapshot['run_id'] != task['run_id'])
            or (snapshot.get('generation') is not None and snapshot['generation'] != record['original_work']['generation'])
            or (snapshot.get('id') is not None and snapshot.get('base_oid') != task['coding_step']['base_commit'])
            or (old and snapshot.get('id') is not None and snapshot != old)
            or snapshot.get('stale') is True
            or (old and any(old.get(key) != snapshot.get(key) for key in ('tree_oid', 'commit_oid')))):
        raise _error('coding_result_snapshot_changed')


def _read_evidence(data_dir, task, result=None):
    """Read only fixed filenames inside controller-owned attempt namespaces."""
    identity = canonical_digest(task['attempt_id']).split(':')[1]
    folder = data_dir / 'attempt_artifacts' / identity
    if task.get('artifact_dir') and Path(task['artifact_dir']) != folder:
        raise _error()
    paths = {'codex_final.json': False}
    if result is not None:
        paths['codex_final.normalized.json'] = True
    for entry in (result or {}).get('artifacts', []):
        path = Path(entry['path'] if isinstance(entry, dict) else entry)
        if path.parent != folder or path.name not in {'codex_final.json', 'codex_final.normalized.json'}:
            raise _error()
        paths[path.name] = False
    evidence = {}
    with _private_attempt_directory(data_dir, 'attempt_artifacts', identity) as directory:
        for name, optional in sorted(paths.items()):
            try:
                raw = _private_evidence(directory, name, 2 * 1024 * 1024)
            except FileNotFoundError:
                if not optional:
                    raise
                evidence[name] = None
            else:
                evidence[name] = 'sha256:' + hashlib.sha256(raw).hexdigest()
    process_id = canonical_digest({'attempt_id': task['attempt_id']}).split(':')[1]
    with _private_attempt_directory(data_dir, 'supervisor', process_id) as directory:
        for name, limit in [('result.json', 65536), ('stdout.jsonl', 64 * 1024 * 1024), ('child.json', 65536)]:
            try:
                raw = _private_evidence(directory, name, limit)
            except FileNotFoundError:
                if name != 'child.json':
                    raise
                evidence[name] = None
            else:
                evidence[name] = 'sha256:' + hashlib.sha256(raw).hexdigest()
    return evidence


def verify_evidence(data_dir, record):
    """Recheck all source evidence immediately before the final database write."""
    try:
        if (record.get('status') != 'collecting' or record.get('authorization_digest') != _authorization(record)
                or record.get('attempt_id') != record['task']['attempt_id']
                or _read_evidence(Path(data_dir).resolve(), record['task'], record['result']) != record['evidence']):
            raise _error()
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise _error() from error


class _ReadState:
    def __init__(self, rows):
        self.rows = rows

    def get(self, kind, identity):
        return next((row for row in self.rows.get(kind, []) if row['id'] == identity), None)

    def list(self, kind):
        return self.rows.get(kind, [])


class CodingResultRecovery:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.store, self.workflow = scheduler.store, scheduler.workflow
        self.data_dir = scheduler.settings.data_dir.resolve()
        self.recovery = RunRecoveryService(self.store, self.workflow)
        if not hasattr(scheduler, '_coding_result_recovery_lock'):
            scheduler._coding_result_recovery_lock = asyncio.Lock()
        self._lock = scheduler._coding_result_recovery_lock

    @staticmethod
    def identity(attempt_id):
        return f'coding-result-v{RULE_VERSION}:' + attempt_id

    async def _read(self, task):
        kinds = ('run', 'work_item', 'attempt', 'dispatch_context', 'coding_step_control', 'supervised_attempt',
                 'coding_step_usage', 'coding_work_budget', 'model_attempt_budget', 'model_invocation', 'code_snapshot')
        values = await asyncio.gather(*(self.store.list(kind) for kind in kinds))
        return _current(_ReadState(dict(zip(kinds, values, strict=True))), task)

    def _files(self, task, result=None):
        return _read_evidence(self.data_dir, task, result)

    async def _prepare(self, work, attempt, task):
        observed = await self._read(task)
        await asyncio.to_thread(self.recovery._verify_process, observed['original_process'], {attempt['id']: attempt})
        WorkspaceManager(self.data_dir, create=False).assert_owned(Path(task['workspace']), attempt_id=attempt['id'])
        usage_state = await coding_usage_snapshot(self.store, work['run_id'], work['id'])
        blockers = await coding_usage_blockers(usage_state, self.data_dir, work['id'])
        if blockers:
            raise _error(blockers[0]['code'])
        before = await asyncio.to_thread(self._files, task)
        result = await self.scheduler.runtime.collect_completed_task(task)
        if (result.get('execution_status') != 'completed' or result.get('runtime_failure_code')
                or result.get('errors') or not result.get('artifacts')):
            raise _error('coding_result_not_complete')
        try:
            Draft202012Validator(output_schema()).validate(result.get('result'))
            if not result['result']['summary'].strip():
                raise ValueError('empty_summary')
        except (ValueError, TypeError, KeyError, DomainError, ValidationError) as error:
            raise _error('coding_result_not_complete') from error
        usage = observed['original_usage']
        if (result['result'].get('status') != 'complete' or result.get('tool_observation_complete') is not True
                or result.get('active_seconds') != usage['active_seconds']
                or result.get('observed_tool_calls') != usage['observed_tool_calls']):
            raise _error('coding_result_not_complete')
        after = await asyncio.to_thread(self._files, task, result)
        if any(after.get(key) != value for key, value in before.items()):
            raise _error()
        snapshot = await self.scheduler.repository.freeze_workspace(Path(task['workspace']), task['source_commit'],
                                                                     f"AgentFlow {task['step']}")
        record = {'rule_version': RULE_VERSION, 'run_id': work['run_id'], 'work_item_id': work['id'],
            'attempt_id': attempt['id'], **observed, 'evidence': after, 'result': result,
            'snapshot': {key: snapshot[key] for key in ('commit_oid', 'tree_oid', 'base_oid')},
            'status': 'collecting', 'actor': 'controller', 'execution': 'existing_result_only', 'created_at': utc_now()}
        record['snapshot'].update(workspace=task['workspace'], source_commit=task['source_commit'])
        record['authorization_digest'] = _authorization(record)
        if observed['original_snapshot']:
            validate_snapshot(record, task, observed['original_snapshot'])
        identity = self.identity(attempt['id'])
        def save(tx):
            current = _current(tx, task)
            if current != observed:
                raise _error()
            saved = tx.put(KIND, identity, record)
            tx.event('coding.result_recollection_started', {'attempt_id': attempt['id'], 'recollection_id': identity},
                     run_id=work['run_id'])
            return saved
        await self.store.command('coding.result.prepare', identity, {'attempt_id': attempt['id'], 'rule_version': RULE_VERSION}, save)
        return await self.store.read(KIND, identity)

    async def _finish(self, identity, *, error=None, original=None):
        def finish(tx):
            record = tx.get(KIND, identity)
            if record and record.get('status') in {'completed', 'blocked'}:
                return record
            basis = record or original
            attempt = tx.get('attempt', basis['attempt_id'])
            work = tx.get('work_item', basis['work_item_id'])
            completed = (record is not None and attempt is not None and work is not None
                and attempt.get('result_recollection_id') == identity and attempt.get('status') == 'completed'
                and work.get('status') in {'completed', 'waiting_approval'} and work.get('attempt_id') == attempt['id']
                and all(work.get(key) == basis['original_work'].get(key) for key in _BINDING))
            updated = {**basis, 'status': 'completed' if completed else 'blocked', 'finished_at': utc_now()}
            if not completed:
                updated['failure_code'] = error.code if isinstance(error, DomainError) else 'coding_result_recollection_invalid'
            result = tx.put(KIND, identity, updated, record['revision'] if record else None)
            tx.event('coding.result_recollection_completed' if completed else 'coding.result_recollection_blocked',
                     {'attempt_id': basis['attempt_id'], 'recollection_id': identity}, run_id=basis['run_id'])
            return result
        return await self.store.command('coding.result.finish', identity, {'recollection_id': identity}, finish)

    async def _resume(self, record):
        attempt = await self.store.read('attempt', record['attempt_id'])
        if attempt and attempt.get('result_recollection_id') == record['id'] and attempt.get('status') == 'completed':
            return await self._finish(record['id'])
        task = record['task']
        await asyncio.to_thread(self.recovery._verify_process, record['original_process'],
                                {record['attempt_id']: record['original_attempt']})
        await asyncio.to_thread(verify_evidence, self.data_dir, record)
        WorkspaceManager(self.data_dir, create=False).assert_owned(Path(task['workspace']), attempt_id=record['attempt_id'])
        def guard(tx):
            return guard_recollection(tx, record['id'], attempt_id=record['attempt_id'], task=task)
        # A new key forces the guard to run again after every restart.
        from uuid import uuid4
        await self.store.command('coding.result.recheck', str(uuid4()), {'recollection_id': record['id']}, guard)
        await self.scheduler._execute_existing(task, collected_result=record['result'], recollection_id=record['id'])
        return await self._finish(record['id'])

    async def reconcile(self):
        async with self._lock:
            existing = {row['attempt_id']: row for row in await self.store.list(KIND)
                        if row.get('rule_version') == RULE_VERSION}
            running = {row['id'] for row in await self.store.list('run') if row.get('execution_state') == 'running'}
            candidates = [row for row in await self.store.list('work_item') if row.get('run_id') in running
                and row.get('status') in {'failed', 'blocked'} and row.get('step') in CODING_STEPS
                and row.get('kind') != 'aggregation' and not row.get('archived') and row.get('attempt_id')]
            for work in candidates:
                attempt = await self.store.read('attempt', work['attempt_id'])
                if not attempt or 'final_schema_invalid' not in {attempt.get('runtime_failure_code'), work.get('runtime_failure_code')}:
                    continue
                if work['attempt_id'] in existing:
                    continue
                active = getattr(self.scheduler, '_active', {}).get(attempt['id'])
                if active and not active.done():
                    continue
                context = await self.store.read('dispatch_context', attempt['id'])
                identity = self.identity(attempt['id'])
                original = {'rule_version': RULE_VERSION, 'actor': 'controller', 'execution': 'existing_result_only',
                    'run_id': work['run_id'], 'work_item_id': work['id'], 'attempt_id': attempt['id'],
                    'original_work': work, 'original_attempt': attempt, 'task': (context or {}).get('task'),
                    'created_at': utc_now()}
                try:
                    if not context or not context.get('task'):
                        raise _error()
                    record = await self._prepare(work, attempt, context['task'])
                    existing[attempt['id']] = record
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if not isinstance(error, DomainError):
                        logger.exception('Coding result preflight failed; original attempt retained')
                    await self._finish(identity, error=error, original=original)
            for record in existing.values():
                if record.get('status') != 'collecting' or record.get('run_id') not in running:
                    continue
                try:
                    await self._resume(record)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if not isinstance(error, DomainError):
                        logger.exception('Coding result collection failed; original attempt retained')
                    await self._finish(record['id'], error=error)

"""Explicit, fenced recovery from known stopped work; never resumes an LLM session."""
from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

import psutil
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.review_checkpoint import (
    REVIEW_CHECKPOINT_KINDS,
    review_repair_source,
    validate_review_repair_source,
)
from agentflow.control.service import ensure_revision
from agentflow.domain.planning import CODING_STEPS, EXECUTION_STEPS, STEPS, descendants
from agentflow.models.budget import account_id
from agentflow.models.profiles import ModelProfile
from agentflow.repository import RepositoryAdapter
from agentflow.runtime.failures import _private_attempt_directory, _private_evidence
from agentflow.runtime.process_identity import (
    boot_relation,
    observe_process,
    process_is_stopped,
    same_launcher_identity,
)
from agentflow.runtime.workspace import WorkspaceManager

TERMINAL = {'completed', 'failed', 'cancelled'}
STOPPED = TERMINAL | {'blocked'}
KINDS = ('run', 'iteration', 'plan', 'project', 'product', 'work_item', 'attempt',
         'dispatch_context', 'supervised_attempt', 'prelaunch_failure', 'execution_reconciliation', 'model_invocation', 'model_attempt_budget',
         'budget_account', 'node_job', 'cross_scenario', 'code_snapshot', 'artifact', 'approval',
         'review', 'review_repair', 'review_source_repair', 'product_test_repair', 'product_test_runtime_repair', 'stage_expansion', 'check', 'candidate', 'target_matrix', 'delivery', 'delivery_intent', 'coding_step_checkpoint',
         'coding_work_budget', 'coding_step_control', 'coding_step_usage', 'role_output_checkpoint', 'task_authorization',
         'task_authorization_expiry',
         'work_execution_budget_adjustment', 'model_uncertainty_acknowledgment', 'timeout_recovery',
         'review_contract_repair', 'review_contract_validation', 'review_diagnostic', 'test_migration_check',
         'review_contract_budget_charge', 'review_disposition_check',
         'test_coverage_check', 'review_contract_context_history')

CODING_USAGE_KINDS = ('run', 'work_item', 'attempt', 'coding_work_budget', 'coding_step_control',
                     'coding_step_usage', 'code_snapshot', 'dispatch_context', 'supervised_attempt', 'prelaunch_failure',
                     'model_invocation', 'model_attempt_budget', 'task_authorization', 'execution_reconciliation',
                     'review_contract_budget_charge')


def coding_usage_state(records, run_id, work_id):
    """A target-only snapshot; the same selector is usable inside the writer."""
    work = next((row for row in records.get('work_item', []) if row['id'] == work_id), None)
    attempts = [row for row in records.get('attempt', []) if row.get('work_item_id') == work_id
                or row['id'] == (work or {}).get('attempt_id')]
    identities = {row['id'] for row in attempts}
    controls = [row for row in records.get('coding_step_control', [])
                if row.get('work_item_id') == work_id or row['id'] in identities]
    identities.update(row['id'] for row in controls)
    identities.update(row['attempt_id'] for row in controls if isinstance(row.get('attempt_id'), str))
    selected = {}
    for kind in CODING_USAGE_KINDS:
        rows = records.get(kind, [])
        if kind == 'run':
            fields = ('id', 'iteration_id', 'execution_state', 'restore_reconciliation_required',
                      'restore_revalidation_required', 'restore_uncertain')
            selected[kind] = [{key: row[key] for key in fields if key in row} for row in rows if row['id'] == run_id]
        elif kind == 'work_item':
            fields = ('id', 'run_id', 'project_id', 'attempt_id', 'status', 'generation', 'fencing_token',
                      'input_fingerprint', 'archived', 'restore_reconciliation_required', 'restore_uncertain')
            selected[kind] = [{key: work[key] for key in fields if key in work}] if work else []
        elif kind == 'coding_step_control':
            selected[kind] = controls
        elif kind == 'coding_work_budget':
            budget_ids = {row['budget_id'] for row in controls if isinstance(row.get('budget_id'), str)}
            selected[kind] = [row for row in rows if row.get('work_item_id') == work_id or row['id'] in budget_ids]
        elif kind == 'review_contract_budget_charge':
            selected[kind] = [row for row in rows if row.get('owner_work_item_id') == work_id]
        elif kind == 'coding_step_usage':
            selected[kind] = [row for row in rows if row.get('work_item_id') == work_id or row['id'] in identities]
        elif kind == 'code_snapshot':
            fields = ('id', 'run_id', 'work_item_id', 'generation', 'commit_oid')
            selected[kind] = [{key: row[key] for key in fields if key in row} for row in rows if row['id'] in identities]
        elif kind in {'model_invocation', 'task_authorization'}:
            selected[kind] = [row for row in rows if isinstance(row.get('attempt_id'), str) and row['attempt_id'] in identities]
        else:
            selected[kind] = [row for row in rows if row['id'] in identities
                              or isinstance(row.get('attempt_id'), str) and row['attempt_id'] in identities]
        selected[kind] = sorted(selected[kind], key=lambda row: row['id'])
    return selected


async def coding_usage_snapshot(store, run_id, work_id):
    heavy = {'model_invocation', 'dispatch_context'} if callable(getattr(store, 'list_linked', None)) else set()
    kinds = [kind for kind in CODING_USAGE_KINDS if kind not in heavy]
    values = await asyncio.gather(*(store.list(kind) for kind in kinds))
    records = dict(zip(kinds, values, strict=True))
    if heavy:
        work = next((row for row in records['work_item'] if row['id'] == work_id), {})
        identities = {row['id'] for row in records['attempt'] if row.get('work_item_id') == work_id
                      or row['id'] == work.get('attempt_id')}
        controls = [row for row in records['coding_step_control']
                    if row.get('work_item_id') == work_id or row['id'] in identities]
        identities.update(row['id'] for row in controls)
        identities.update(row['attempt_id'] for row in controls if isinstance(row.get('attempt_id'), str))
        records['model_invocation'], records['dispatch_context'] = await asyncio.gather(
            store.list_linked('model_invocation', {'attempt_id': sorted(identities)}),
            store.list_linked('dispatch_context', {'id': sorted(identities), 'attempt_id': sorted(identities)}))
    return coding_usage_state(records, run_id, work_id)


def _coding_usage_blockers(state, data_dir, work_id, exclude_attempt_id):
    from agentflow.runtime.prelaunch import _no_calls, _no_launch_directory, prove_prelaunch_failure
    run = next(iter(state.get('run', [])), None)
    work = next((row for row in state.get('work_item', []) if row['id'] == work_id), None)
    controls = state.get('coding_step_control', [])
    if not run or not work or work.get('run_id') != run['id']:
        return [_error('coding_budget_uncertain', '编码计量对应的运行或任务身份不一致。')]
    if exclude_attempt_id is not None and (work.get('attempt_id') != exclude_attempt_id or work.get('status') != 'running'):
        return [_error('coding_budget_uncertain', '只有当前正在准备的执行可以暂不计为历史欠账。')]
    attempts = {row['id']: row for row in state.get('attempt', [])}
    budgets = {row['id']: row for row in state.get('coding_work_budget', [])}
    usages = {row['id']: row for row in state.get('coding_step_usage', [])}
    contexts = {row['id']: row for row in state.get('dispatch_context', [])}
    supervisors = {row['id']: row for row in state.get('supervised_attempt', [])}
    prelaunch = {row['id']: row for row in state.get('prelaunch_failure', [])}
    startup = {row.get('attempt_id'): row for row in state.get('execution_reconciliation', [])}
    model_budgets = {row['id']: row for row in state.get('model_attempt_budget', [])}
    if set(usages) - {row['id'] for row in controls}:
        return [_error('coding_budget_uncertain', '存在无法绑定到原编码步骤的用量回执。')]
    totals = {}
    for control in controls:
        identity = control['id']
        attempt, usage = attempts.get(identity), usages.get(identity)
        budget = budgets.get(control.get('budget_id'))
        if (not attempt or not budget or control.get('attempt_id') != identity
                or control.get('run_id') != run['id'] or control.get('work_item_id') != work_id
                or budget.get('run_id') != run['id'] or budget.get('work_item_id') != work_id
                or attempt.get('run_id') != run['id'] or attempt.get('work_item_id') != work_id
                or type(control.get('generation')) is not int or control['generation'] < 1
                or type(control.get('fencing_token')) is not int or control['fencing_token'] < 1
                or not isinstance(control.get('input_fingerprint'), str) or not control['input_fingerprint']
                or any(attempt.get(field) != control[field] for field in ('generation', 'fencing_token', 'input_fingerprint'))
                or any(row.get('restore_reconciliation_required') or row.get('restore_revalidation_required')
                       or row.get('restore_uncertain') for row in (run, work, control, attempt, budget))):
            return [_error('coding_budget_uncertain', '历史编码步骤、执行与累计额度无法逐一核验。')]
        if usage is not None:
            if (usage.get('run_id') != run['id'] or usage.get('work_item_id') != work_id
                    or usage.get('budget_id') != budget['id'] or usage.get('known') is not True
                    or type(usage.get('active_seconds')) not in {int, float}
                    or not math.isfinite(usage['active_seconds']) or usage['active_seconds'] < 0
                    or type(usage.get('observed_tool_calls')) is not int or usage['observed_tool_calls'] < 0):
                return [_error('coding_budget_uncertain', '历史编码步骤存在未知或不匹配的用量，不能按零用量继续。')]
            aggregate = totals.setdefault(budget['id'], [0, 0.0, 0])
            aggregate[0] += 1
            aggregate[1] += usage['active_seconds']
            aggregate[2] += usage['observed_tool_calls']
            continue
        if identity == exclude_attempt_id:
            continue
        calls = [row for row in state.get('model_invocation', []) if row.get('attempt_id') == identity]
        authorization = any(row.get('attempt_id') == identity for row in state.get('task_authorization', []))
        context, supervised = contexts.get(identity), supervisors.get(identity)
        collected_code = any(row['id'] == identity for row in state.get('code_snapshot', []))
        if context is not None:
            proof = None
            if not collected_code and context.get('task', {}).get('coding_step') == control:
                proof = prove_prelaunch_failure(data_dir, attempt, context, prelaunch.get(identity),
                    supervised=supervised, invocations=calls, budget=model_budgets.get(identity), run=run)
                if not proof:
                    from agentflow.runtime.startup_evidence import prove_reconciled_startup
                    proof = prove_reconciled_startup(data_dir, attempt, context, supervised, startup.get(identity),
                        invocations=calls, budget=model_budgets.get(identity))
            if proof and proof.get('outcome') == 'not_started':
                continue
        elif (supervised is None and not authorization and not collected_code and _no_calls(calls, model_budgets.get(identity))
                and attempt.get('status') == 'blocked' and attempt.get('execution_status') is None
                and not attempt.get('finished_at') and not attempt.get('ended_at')
                and isinstance(attempt.get('summary'), str) and attempt['summary'].strip()
                and _no_launch_directory(data_dir, identity)):
            # A prepared control is not an execution. Before dispatch context and
            # its task authorization exist, a blocked original preparation with
            # no launch files/calls has no measured worker use to invent.
            continue
        return [_error('coding_budget_unaccounted', '历史编码步骤缺少用量回执，且不能证明从未启动；先核对耗时与工具用量，不能重试绕过计量。')]
    from agentflow.control.review_contract_binding import delegated_usage_totals
    delegated = delegated_usage_totals(state.get('review_contract_budget_charge', []), run['id'], work_id)
    for identity, charge in delegated.items():
        budget = budgets.get(identity)
        if (not budget or budget.get('run_id') != run['id'] or budget.get('work_item_id') != work_id or charge['unknown']):
            return [_error('coding_budget_uncertain', '委派返工用量未知或不属于原作者累计额度。')]
        aggregate = totals.setdefault(identity, [0, 0.0, 0])
        aggregate[0] += charge['count']
        aggregate[1] += charge['seconds']
        aggregate[2] += charge['calls']
    for identity, (count, seconds, calls) in totals.items():
        budget = budgets[identity]
        if (type(budget.get('step_count')) is not int or budget['step_count'] < count
                or type(budget.get('active_seconds')) not in {int, float}
                or not math.isfinite(budget['active_seconds']) or budget['active_seconds'] + 1e-6 < seconds
                or type(budget.get('observed_tool_calls')) is not int or budget['observed_tool_calls'] < calls):
            return [_error('coding_budget_uncertain', '累计编码额度低于已确认用量，不能恢复或重置计数。')]
    return []


async def coding_usage_blockers(state, data_dir, work_id, *, exclude_attempt_id=None):
    """Read-only missing-usage proof shared by recovery and fresh-step preparation."""
    try:
        run_id = state['run'][0]['id']
        selected = coding_usage_state(state, run_id, work_id)
        return await asyncio.to_thread(_coding_usage_blockers, selected, Path(data_dir), work_id, exclude_attempt_id)
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return [_error('coding_budget_uncertain', '编码步骤计量证据无法安全核验。')]


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: int = Field(ge=1)
    mode: Literal['retry', 'continue']
    work_item_id: str | None = Field(default=None, min_length=1, max_length=200)
    reason: str = Field(default='', max_length=2000)
    use_current_model_settings: bool = False
    task_correction: str | None = Field(default=None, min_length=1, max_length=2000)


def _recovery_command(run_id, request):
    # False is the pre-option request identity, including explicit UI false.
    excluded = {'use_current_model_settings'} if not request.use_current_model_settings else set()
    if request.task_correction is None:
        excluded.add('task_correction')
    payload = request.model_dump(exclude=excluded)
    return {'run_id': run_id, **payload}


_MODEL_BINDING = 'recovery_model_binding'
_MODEL_BINDING_FIELDS = {'recovery_id', 'run_id', 'iteration_id', 'work_item_id',
                         'generation', 'model_profile_id', 'profile_revision'}


def _reexecution_receipt(receipt):
    return receipt and (receipt.get('mode') == 'retry' or (
        receipt.get('mode') == 'continue' and receipt.get('actor') == 'owner'
        and receipt.get('execution') == 'fresh_attempt'))


def _accepted_coding_profile(raw):
    try:
        if not raw:
            raise ValueError('missing_profile')
        profile = ModelProfile.model_validate({name: raw[name] for name in ModelProfile.model_fields if name in raw})
        profile.assert_accepted('responses')
        if raw.get('id') != profile.model_profile_id:
            raise ValueError('profile_identity_mismatch')
        return profile
    except (ValidationError, ValueError, DomainError) as error:
        raise DomainError('recovery_model_unavailable', '当前编码模型尚未接受、配置不完整或不支持 Responses，不能用于重试。') from error


def _validate_recovery_model_binding(run, item, binding, receipt, raw_profile):
    try:
        if (not isinstance(binding, dict) or set(binding) != _MODEL_BINDING_FIELDS
                or item.get('step') not in CODING_STEPS or item.get('archived')
                or item.get('run_id') != run['id']
                or any(binding.get(field) != expected for field, expected in {
                    'run_id': run['id'], 'iteration_id': run['iteration_id'], 'work_item_id': item['id'],
                    'generation': item['generation']}.items())
                or type(binding.get('generation')) is not int or binding['generation'] < 1
                or type(binding.get('profile_revision')) is not int or binding['profile_revision'] < 1
                or any(not isinstance(binding.get(field), str) or not binding[field] for field in
                       ('recovery_id', 'run_id', 'iteration_id', 'work_item_id', 'model_profile_id'))):
            raise ValueError('binding_identity_mismatch')
        frozen = (receipt or {}).get('model_profile_bindings', {}).get(item['id'])
        owner_choice = _reexecution_receipt(receipt) and receipt.get('actor') == 'owner'
        system_inheritance = receipt and receipt.get('mode') == 'inherit_model_settings' and receipt.get('actor') == 'system'
        if (not receipt or receipt.get('id') != binding['recovery_id'] or not (owner_choice or system_inheritance)
                or receipt.get('run_id') != run['id'] or receipt.get('iteration_id') != run['iteration_id']
                or item['id'] not in receipt.get('affected_work_item_ids', [])
                or frozen != {**binding, 'payload_digest': canonical_digest(binding)}):
            raise ValueError('binding_receipt_mismatch')
        if system_inheritance:
            source = receipt.get('source_binding')
            if (not isinstance(source, dict) or set(source) != _MODEL_BINDING_FIELDS
                    or source.get('recovery_id') != receipt.get('source_recovery_id')
                    or receipt.get('source_payload_digest') != canonical_digest(source)
                    or type(source.get('generation')) is not int or source['generation'] + 1 != binding['generation']
                    or {k: v for k, v in source.items() if k not in {'recovery_id', 'generation'}}
                    != {k: v for k, v in binding.items() if k not in {'recovery_id', 'generation'}}):
                raise ValueError('inheritance_source_mismatch')
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise DomainError('invalid_recovery_model_binding', '重试模型设置与恢复回执或工作代次不匹配，不能执行。') from error
    profile = _accepted_coding_profile(raw_profile)
    if profile.model_profile_id != binding['model_profile_id'] or profile.revision != binding['profile_revision']:
        raise DomainError('stale_model_profile', '重试选定的模型版本已变化，请重新选择当前模型设置后重试。')


def _model_binding(item):
    payload = item.get('payload') or {}
    if not isinstance(payload, dict):
        raise DomainError('invalid_recovery_model_binding', '重试工作内容不是有效的配置记录。')
    if _MODEL_BINDING not in payload:
        return None
    binding = payload[_MODEL_BINDING]
    if (not isinstance(binding, dict) or not isinstance(binding.get('recovery_id'), str)
            or not binding['recovery_id'] or not isinstance(binding.get('model_profile_id'), str)
            or not binding['model_profile_id']):
        raise DomainError('invalid_recovery_model_binding', '重试模型设置缺少有效的恢复身份。')
    return binding


async def _read_recovery_model_binding(store, run, item):
    binding = _model_binding(item)
    if binding is None:
        return None
    receipt, profile = await asyncio.gather(store.read('run_recovery', binding['recovery_id']),
                                          store.read('model_profile', binding['model_profile_id']))
    _validate_recovery_model_binding(run, item, binding, receipt, profile)
    return {'binding': binding, 'receipt': receipt, 'profile': profile}


def inherit_recovery_model_binding(tx, item, revised, reason):
    """Reissue the verified choice for one new generation in the same writer transaction."""
    prior = _model_binding(item)
    if prior is None:
        return
    run = tx.get('run', item['run_id'])
    receipt = tx.get('run_recovery', prior['recovery_id'])
    profile = tx.get('model_profile', prior['model_profile_id'])
    _validate_recovery_model_binding(run, item, prior, receipt, profile)
    if (revised.get('run_id') != item['run_id'] or revised.get('id') != item['id']
            or revised.get('step') not in CODING_STEPS or revised.get('generation') != item['generation'] + 1):
        raise DomainError('invalid_recovery_model_binding', '模型设置只能继承到同一编码工作的下一代次。')
    identity = str(uuid5(NAMESPACE_URL, 'agentflow:model-binding-inheritance:' + canonical_digest({
        'source': prior, 'generation': revised['generation']})))
    binding = {**prior, 'recovery_id': identity, 'generation': revised['generation']}
    inherited = tx.put('run_recovery', identity, {'run_id': run['id'], 'iteration_id': run['iteration_id'],
        'mode': 'inherit_model_settings', 'actor': 'system', 'execution': 'inherit_binding_only',
        'affected_work_item_ids': [item['id']], 'source_recovery_id': prior['recovery_id'],
        'source_binding': prior, 'source_payload_digest': canonical_digest(prior),
        'model_profile_bindings': {item['id']: {**binding, 'payload_digest': canonical_digest(binding)}},
        'reason': reason, 'created_at': utc_now()})
    revised['payload'][_MODEL_BINDING] = binding
    tx.event('model.settings_inherited', {'receipt_id': inherited['id'], 'work_item_id': item['id'],
        'source_recovery_id': prior['recovery_id'], 'generation': binding['generation'],
        'model_profile_id': binding['model_profile_id'], 'profile_revision': binding['profile_revision']}, run_id=run['id'])


async def resolve_recovery_model_profile(store, run, item):
    """Accept an owner choice or its verified system inheritance for this generation."""
    resolved = await _read_recovery_model_binding(store, run, item)
    return resolved['binding']['model_profile_id'] if resolved else None


def _error(code, message):
    return {'code': code, 'message': message}


def _failed(item):
    return (item.get('status') in {'failed', 'blocked', 'cancelled'}
            or (item.get('status') == 'completed' and (item.get('quality_result') in {'failed', 'inconclusive'}
                or (item.get('step') in EXECUTION_STEPS | {'code_review'} and item.get('quality_result') != 'passed'))))


def _related(all_records, run_id):
    """The same selection runs before I/O and inside the final writer transaction."""
    run = next((r for r in all_records['run'] if r['id'] == run_id), None)
    if run is None:
        raise DomainError('not_found', '运行不存在。', 404)
    work_ids = {w['id'] for w in all_records['work_item'] if w.get('run_id') == run_id}
    attempt_ids = {a['id'] for a in all_records['attempt']
                   if a.get('run_id') == run_id or a.get('work_item_id') in work_ids}
    candidate_ids = {c['id'] for c in all_records['candidate'] if c.get('run_id') == run_id}
    owners = {account_id('run', run_id), account_id('iteration', run['iteration_id'])}
    plan = next((p for p in all_records['plan'] if p['id'] == run.get('plan_id')), {})
    product_id = plan.get('product_contract', {}).get('product_id')
    selected = {}
    for kind, rows in all_records.items():
        if kind in {'run', 'iteration', 'plan', 'project'}:
            identity = run_id if kind == 'run' else run.get(kind + '_id')
            selected[kind] = [r for r in rows if r['id'] == identity]
        elif kind == 'budget_account':
            selected[kind] = [r for r in rows if r['id'] in owners]
        elif kind == 'product':
            selected[kind] = [r for r in rows if r.get('project_id') == run.get('project_id')
                              or r.get('run_id') == run_id or run_id in (r.get('run_ids') or []) or r['id'] == product_id]
        else:
            selected[kind] = [r for r in rows if r.get('run_id') == run_id
                or r.get('work_item_id', r.get('parent_work_item_id')) in work_ids
                or (kind in {'dispatch_context', 'supervised_attempt', 'prelaunch_failure', 'model_attempt_budget'} and r['id'] in attempt_ids)
                or (kind == 'model_invocation' and r.get('iteration_id') == run['iteration_id'])
                or (kind == 'target_matrix' and r['id'] in candidate_ids)]
    return selected


async def read_recovery_records(store, run_id, kinds=KINDS):
    """Prefilter bulky evidence; the original selector remains authoritative."""
    heavy = {'model_invocation', 'dispatch_context'} if callable(getattr(store, 'list_linked', None)) else set()
    ordinary = [kind for kind in kinds if kind not in heavy]
    values = await asyncio.gather(*(store.list(kind) for kind in ordinary))
    records = dict(zip(ordinary, values, strict=True))
    if heavy:
        run = next((row for row in records['run'] if row['id'] == run_id), None)
        if not run:
            raise DomainError('not_found', '运行不存在。', 404)
        work_ids = {row['id'] for row in records['work_item'] if row.get('run_id') == run_id}
        attempt_ids = {row['id'] for row in records['attempt']
                       if row.get('run_id') == run_id or row.get('work_item_id') in work_ids}
        links = {'run_id': [run_id], 'work_item_id': sorted(work_ids), 'parent_work_item_id': sorted(work_ids)}
        records['dispatch_context'] = await store.list_linked('dispatch_context', {**links, 'id': sorted(attempt_ids)})
        if isinstance(run.get('iteration_id'), str):
            records['model_invocation'] = await store.list_linked('model_invocation',
                {**links, 'iteration_id': [run['iteration_id']]})
        else:
            # Preserve legacy corruption diagnostics rather than hiding evidence
            # when the iteration identity itself cannot be selected safely.
            records['model_invocation'] = await store.list('model_invocation')
    return _related(records, run_id)


async def validate_recovery_checkpoint(store, run, work, snapshot, repository):
    """Bind reuse to the explicit recovery receipt and the stopped original attempt."""
    try:
        identity = work.get('payload', {}).get('recovery_checkpoint_id')
        if (not snapshot or snapshot['id'] != identity
                or work['payload'].get('repair_base_snapshot_id') != identity
                or snapshot.get('purpose') != 'recovery_checkpoint'
                or snapshot.get('run_id') != run['id'] or snapshot.get('work_item_id') != work['id']
                or snapshot.get('generation') != work['generation'] - 1):
            raise ValueError('checkpoint_binding_mismatch')
        receipt = await store.read('run_recovery', snapshot['recovery_id'])
        attempt = await store.read('attempt', snapshot['source_attempt_id'])
        if (not receipt or work['id'] not in receipt['affected_work_item_ids']
                or not attempt or attempt.get('status') not in STOPPED
                or attempt.get('work_item_id') != work['id']
                or attempt.get('generation') != snapshot.get('source_generation', snapshot['generation'])
                or attempt.get('fencing_token') != snapshot['source_fencing_token']
                or attempt.get('input_fingerprint') != snapshot['source_input_fingerprint']):
            raise ValueError('checkpoint_attempt_mismatch')
        if snapshot.get('source_review_snapshot_id'):
            prior = await store.read('code_snapshot', snapshot['source_review_snapshot_id'])
            await validate_review_repair_source(store, run, work, prior)
            if (not prior or prior.get('run_id') != run['id'] or prior.get('work_item_id') != work['id']
                    or prior.get('generation') != snapshot.get('source_generation')
                    or prior.get('commit_oid') != snapshot['commit_oid'] or prior.get('tree_oid') != snapshot['tree_oid']
                    or prior.get('source_child_snapshot_id', prior['id']) != attempt['id']
                    or snapshot.get('source_review_write_paths') != work.get('write_paths')
                    or (prior.get('checkpoint_kind') in REVIEW_CHECKPOINT_KINDS
                        and prior.get('child_scope') != work['write_paths'])):
                raise ValueError('checkpoint_preserved_review_mismatch')
        if 'source_commit' in snapshot:
            context = await store.read('dispatch_context', snapshot['source_attempt_id'])
            task = (context or {}).get('task', {})
            if (task.get('source_commit') != snapshot['source_commit']
                    or task.get('allowed_write_paths') != snapshot.get('source_write_paths')
                    or snapshot.get('source_write_paths') != work.get('write_paths')
                    or task.get('work_item_id') != work['id'] or task.get('run_id') != run['id']
                    or task.get('attempt_id') != attempt['id']
                    or any(task.get(field) != attempt.get(field)
                           for field in ('fencing_token', 'input_fingerprint'))):
                raise ValueError('checkpoint_source_binding_mismatch')
            repair_id = snapshot.get('source_repair_snapshot_id')
            if repair_id:
                prior = await store.read('code_snapshot', repair_id)
                if prior and prior.get('checkpoint_kind') in REVIEW_CHECKPOINT_KINDS:
                    await validate_review_repair_source(store, run, work, prior)
                if (not prior or prior.get('run_id') != run['id'] or prior.get('work_item_id') != work['id']
                        or prior.get('generation') != snapshot.get('source_generation') - 1
                        or prior.get('commit_oid') != snapshot['source_commit']
                        or (prior.get('checkpoint_kind') in REVIEW_CHECKPOINT_KINDS
                            and prior.get('child_scope') != snapshot['source_write_paths'])):
                    raise ValueError('checkpoint_review_source_mismatch')
        points = receipt['checkpoint'].get('items', [receipt['checkpoint']])
        if not any(p.get('snapshot_id') == identity and p.get('commit_oid') == snapshot['commit_oid'] for p in points):
            raise ValueError('checkpoint_receipt_mismatch')
        path = Path(snapshot['repository_path'])
        if path.is_symlink() or path.resolve() != path:
            raise ValueError('unsafe_checkpoint_path')
        if await asyncio.to_thread(repository._integrity, path, snapshot['commit_oid']) != snapshot['tree_oid']:
            raise ValueError('checkpoint_tree_mismatch')
        await asyncio.to_thread(repository._integrity, path, snapshot['base_oid'])
    except (OSError, KeyError, ValueError, TypeError, DomainError) as error:
        raise DomainError('invalid_repair_checkpoint', '恢复检查点已变化或身份不匹配，不能继续执行。') from error


async def resolve_role_output_checkpoint(store, settings, run, work):
    """Resolve a recovery-authorized partial role output, never an agent-supplied path."""
    from agentflow.adapters.openhands.output_builder import result_identity
    checkpoint_id = work.get('payload', {}).get('role_output_checkpoint_id')
    if not checkpoint_id:
        return None
    try:
        point = await store.read('role_output_checkpoint', checkpoint_id)
        if (not point or work.get('archived') or work.get('step') in CODING_STEPS | EXECUTION_STEPS
                or point.get('run_id') != run['id'] or point.get('work_item_id') != work['id']
                or point.get('authorized_generation') != work['generation']
                or point.get('purpose') != 'role_output_recovery'):
            raise ValueError('role_checkpoint_binding_mismatch')
        receipt, attempt, context = await asyncio.gather(
            store.read('run_recovery', point['recovery_id']), store.read('attempt', point['source_attempt_id']),
            store.read('dispatch_context', point['source_attempt_id']))
        if (not _reexecution_receipt(receipt) or receipt.get('actor') not in {'owner', 'system'}
                or receipt.get('run_id') != run['id'] or work['id'] not in receipt.get('affected_work_item_ids', [])
                or not any(saved.get('checkpoint_id') == checkpoint_id
                           and all(saved.get(field) == point.get(field) for field in ('digest', 'work_item_id',
                               'authorized_generation', 'source_attempt_id', 'source_generation', 'schema_digest', 'progress_digest'))
                           for saved in receipt.get('role_output_checkpoints', []))
                or not attempt or attempt.get('status') not in STOPPED or attempt.get('run_id') != run['id']
                or attempt.get('work_item_id') != work['id'] or attempt.get('generation') != point.get('source_generation')
                or not context or result_identity(context['task']) != point.get('source_identity')
                or any(attempt.get(field) != point['source_identity'].get(field)
                       for field in ('run_id', 'iteration_id', 'work_item_id', 'fencing_token', 'input_fingerprint'))):
            raise ValueError('role_checkpoint_receipt_mismatch')
        expected = settings.data_dir.resolve() / 'attempt_artifacts' / canonical_digest(attempt['id']).split(':')[1]
        filename = 'openhands_final.json' if point.get('checkpoint_kind') == 'planning_final' else 'role_output_checkpoint.json'
        if point.get('checkpoint_kind') == 'planning_final' and work.get('step') not in {'goal', 'development_plan'}:
            raise ValueError('planning_final_step_mismatch')
        if (point.get('source_directory') != str(expected) or point.get('path') != str(expected / filename)
                or expected.is_symlink() or expected.resolve() != expected):
            raise ValueError('unsafe_role_checkpoint_path')
        return point
    except (OSError, KeyError, TypeError, ValueError, DomainError) as error:
        raise DomainError('invalid_role_output_checkpoint', '角色草稿检查点与当前任务、来源或恢复回执不一致。') from error


async def restore_role_output_checkpoint(store, settings, work, target_task):
    """Controller-only import into the new attempt before starting its role worker."""
    from agentflow.adapters.openhands.output_builder import import_partial, result_identity
    current = await store.read('work_item', work['id'])
    run = await store.read('run', work['run_id'])
    if not current or current.get('attempt_id') != work.get('attempt_id'):
        raise DomainError('stale_attempt', '角色草稿目标执行已变化。')
    point = await resolve_role_output_checkpoint(store, settings, run, current)
    if point is None:
        return None
    identity = result_identity(target_task)
    expected = {'attempt_id': current['attempt_id'], 'run_id': current['run_id'], 'iteration_id': run['iteration_id'],
        'work_item_id': current['id'], 'fencing_token': current['fencing_token'], 'input_fingerprint': current['input_fingerprint']}
    attempt = await store.read('attempt', current['attempt_id'])
    if (current.get('status') != 'running' or not attempt or attempt.get('status') != 'running'
            or any(identity.get(field) != value for field, value in expected.items())
            or any(attempt.get(field) != value for field, value in expected.items() if field != 'attempt_id')
            or (identity['schema_digest'] != point['schema_digest'] and point.get('checkpoint_kind') != 'planning_final')):
        raise DomainError('invalid_role_output_checkpoint', '草稿导入目标身份或结果格式不匹配。')
    artifact_root = settings.data_dir.resolve() / 'attempt_artifacts' / canonical_digest(current['attempt_id']).split(':')[1]
    supplied = target_task.get('artifact_dir') if isinstance(target_task, dict) else target_task.artifact_dir
    if not isinstance(supplied, (str, Path)) or Path(supplied) != artifact_root:
        raise DomainError('invalid_role_output_checkpoint', '草稿只能导入当前执行的控制器私有产物目录。')
    try:
        if point.get('checkpoint_kind') == 'planning_final':
            from agentflow.control.planning_recovery import restore_planning_final
            source_context = await store.read('dispatch_context', point['source_attempt_id'])
            imported = await asyncio.to_thread(restore_planning_final, settings, point, source_context['task'],
                target_task, artifact_root)
        else:
            imported = await asyncio.to_thread(import_partial, target_task, artifact_root=artifact_root,
                source_directory=Path(point['source_directory']), expected_digest=point['digest'],
                expected_source_identity=point['source_identity'])
    except (OSError, ValueError, TypeError, DomainError) as error:
        raise DomainError('invalid_role_output_checkpoint', '已授权的角色草稿无法安全导入。') from error
    def record(tx):
        latest = tx.get('work_item', current['id'])
        if (not latest or latest.get('status') != 'running'
                or any(latest.get(field) != current.get(field) for field in ('attempt_id', 'generation', 'fencing_token', 'input_fingerprint'))
                or latest.get('payload', {}).get('role_output_checkpoint_id') != point['id']
                or tx.get('role_output_checkpoint', point['id']) != point):
            raise DomainError('stale_attempt', '草稿导入期间目标任务或授权发生变化。')
        value = tx.put('role_output_import', current['attempt_id'], {'run_id': run['id'], 'work_item_id': current['id'],
            'attempt_id': current['attempt_id'], 'source_checkpoint_id': point['id'], 'source_attempt_id': point['source_attempt_id'],
            'source_identity': point['source_identity'], 'target_identity': identity,
            'mode': imported.get('mode', 'resume_output'), 'reason': imported.get('reason'),
            'progress_digest': imported['progress_digest'], 'schema_digest': point['schema_digest'], 'created_at': utc_now()})
        tx.event('role.output_checkpoint_imported', {'work_item_id': current['id'], 'attempt_id': current['attempt_id'],
            'checkpoint_id': point['id']}, run_id=run['id'])
        return value
    await store.command('role.output_import', 'role-output-import:' + current['attempt_id'],
        {'checkpoint_id': point['id'], 'target_identity': identity}, record)
    return imported


class _ReadState:
    def __init__(self, state):
        self.state = state

    def get(self, kind, identity):
        return next((r for r in self.state.get(kind, []) if r['id'] == identity), None)

    def list(self, kind):
        return self.state.get(kind, [])


class RunRecoveryService:
    def __init__(self, store, workflow):
        self.store, self.workflow = store, workflow
        self.data_dir = workflow.settings.data_dir.resolve()
        self.repository = RepositoryAdapter(timeout=30)

    async def _read(self, run_id):
        return await read_recovery_records(self.store, run_id)

    @staticmethod
    def _run(state):
        return state['run'][0]

    @staticmethod
    def _common_blockers(state, *, allow_execution_wait=False, ignore_model_uncertainty=False):
        run = state['run'][0]
        blockers = []
        from agentflow.control.product_management import guard_product_run
        try:
            guard_product_run(_ReadState(state), run)
        except DomainError as error:
            blockers.append(_error(error.code, error.message))
        if run.get('delivery_ids') or any(d.get('confirmed_at') for d in state['delivery']):
            blockers.append(_error('delivered_run', '已经确认交付的版本不能改写，请创建新迭代。'))
        if run.get('execution_state') in {'publishing', 'cancelling'} or any(
                i.get('status') not in {'failed'} for i in state['delivery_intent']):
            blockers.append(_error('delivery_in_progress', '发布或停止过程尚未确认结束，请先完成状态核对。'))
        current_items = [w for w in state['work_item'] if not w.get('archived')]
        attempts = {a['id']: a for a in state['attempt']}
        candidates = {c['id']: c for c in state['candidate']}
        resumable_waits = {w['id'] for w in current_items
            if allow_execution_wait and run.get('execution_state') == 'paused'
            and w.get('status') == 'waiting_execution' and w.get('step') in EXECUTION_STEPS
            and attempts.get(w.get('attempt_id'), {}).get('status') == 'waiting_execution'
            and candidates.get(w.get('candidate_id'), {}).get('run_input_fingerprint') == run['input_fingerprint']
            and not candidates[w['candidate_id']].get('stale')}
        for item in current_items:
            attempt = attempts.get(item.get('attempt_id'))
            if item.get('attempt_id') and (not attempt or attempt.get('work_item_id') != item['id']
                    or any(attempt.get(f) != item.get(f) for f in ('generation', 'fencing_token', 'input_fingerprint'))):
                blockers.append(_error('recovery_evidence_invalid', '任务与旧执行身份不匹配，请先核对。'))
            if item.get('status') == 'execution_unknown':
                blockers.append(_error('execution_unknown', '仍有执行结果未知的工作，请先核对旧执行。'))
                break
            if (item.get('status') in {'running', 'waiting_execution', 'cancel_requested', 'cancelling'}
                    and item['id'] not in resumable_waits):
                blockers.append(_error('active_work', '仍有正在执行或等待停止的工作。'))
                break
        for attempt in state['attempt']:
            if attempt.get('work_item_id') in resumable_waits and attempt.get('status') == 'waiting_execution':
                continue
            # A delivery approval is a controller gate, not a live process.
            if attempt.get('status') == 'waiting_approval':
                item = next((w for w in current_items if w.get('attempt_id') == attempt['id']), None)
                if item and item['status'] in {'waiting_approval', 'pending_delivery', 'cancelled'}:
                    continue
            if attempt.get('status') not in STOPPED:
                code = 'execution_unknown' if attempt.get('status') == 'execution_unknown' else 'active_work'
                blockers.append(_error(code, '历史执行尚未确认结束，不能创建重复执行。'))
                break
        for supervised in state['supervised_attempt']:
            if supervised.get('state') not in TERMINAL:
                code = 'execution_unknown' if supervised.get('state') == 'execution_unknown' else 'active_work'
                blockers.append(_error(code, '受管进程仍在执行或状态未知。'))
                break
        if any(j.get('state') not in TERMINAL and not (j.get('parent_work_item_id') in resumable_waits
                and j.get('state') in {'queued', 'leased', 'running'}) for j in state['node_job']) or any(
                s.get('status') not in TERMINAL and not (s.get('parent_work_item_id') in resumable_waits
                    and s.get('status') in {'created', 'ready', 'dispatching', 'waiting_node', 'blocked'})
                for s in state['cross_scenario']):
            blockers.append(_error('active_work', '执行节点或跨端场景仍有未结束的任务。'))
        if not ignore_model_uncertainty:
            from agentflow.models.uncertainty import (
                acknowledged_invocation_ids,
                attempt_uncertainty_blocks,
                invocation_blocks,
            )
            acknowledged = acknowledged_invocation_ids(state)
            if any(invocation_blocks(i, acknowledged) for i in state['model_invocation']):
                blockers.append(_error('recovery_budget_uncertain', '仍有预留、发送中或结果未知的模型调用。'))
            if any(attempt_uncertainty_blocks(b, state['model_invocation'], acknowledged) for b in state['model_attempt_budget']):
                blockers.append(_error('recovery_budget_uncertain', '旧执行存在尚未核对的模型调用。'))
        accounts = {a['id']: a for a in state['budget_account']}
        iteration = state['iteration'][0] if state['iteration'] else {}
        for kind, owner, declared in [('run', run['id'], run.get('budget_limit', {})),
                                     ('iteration', run['iteration_id'], iteration.get('budget_limit', {}))]:
            account = accounts.get(account_id(kind, owner))
            if not account:
                # Before the first dispatch the scheduler has not opened accounts.
                if (not state['budget_account'] and not state['supervised_attempt'] and not state['dispatch_context']
                        and not state['model_invocation'] and not state['model_attempt_budget']):
                    if type(declared.get('max_model_requests')) is int and declared['max_model_requests'] >= 0:
                        continue
                blockers.append(_error('recovery_budget_missing', '预算账户缺失，不能通过恢复创建或重置历史额度。'))
                continue
            if (account.get('restore_uncertain') or account.get('reserved_micros', 0)
                    or account.get('uncertain_micros', 0)):
                blockers.append(_error('recovery_budget_uncertain', '预算含未核对金额，请先核对。'))
            numeric = ('request_count', 'max_requests', 'settled_micros', 'reserved_micros', 'limit_micros')
            if (any(type(account.get(n)) is not int or account[n] < 0 for n in numeric)
                    or account.get('owner_kind') != kind or account.get('owner_id') != owner
                    or account.get('max_requests') != declared.get('max_model_requests')
                    or account.get('currency') != declared.get('currency')
                    or account.get('limit_micros') != declared.get('limit_micros')):
                blockers.append(_error('recovery_budget_uncertain', '预算配置与累计用量不一致，不能自动修正。'))
                continue
            if ((account['max_requests'] > 0 and account['request_count'] >= account['max_requests'])
                    or (run.get('budget_limit', {}).get('cost_mode', 'strict') != 'request_limited'
                        and account['settled_micros'] + account['reserved_micros'] >= account['limit_micros'])):
                blockers.append(_error('recovery_budget_exhausted', '本轮或迭代预算已经用尽；恢复不会增加额度。'))
        return list({b['code']: b for b in blockers}.values())

    @staticmethod
    def _stopped_process(identity):
        if not process_is_stopped(identity):
            raise DomainError('active_work', '旧进程仍在运行，不能重复启动。')

    async def _target_blockers(self, state, target):
        """Preserve each coding work's shared time/tool/step allowance across retries."""
        if not target:
            return []
        from agentflow.control.coding_steps import CodingSteps
        run = state['run'][0]
        items = {work['id']: work for work in state['work_item']}
        for identity in target['root_work_item_ids']:
            work = items[identity]
            if work.get('step') not in CODING_STEPS or work.get('kind') == 'aggregation':
                continue
            usage_blockers = await coding_usage_blockers(state, self.data_dir, identity)
            if usage_blockers:
                return usage_blockers
            rows = [row for row in state.get('coding_work_budget', []) if row.get('work_item_id') == identity]
            evidence = any(row.get('work_item_id') == identity for kind in (
                'coding_step_control', 'coding_step_usage', 'coding_step_checkpoint') for row in state.get(kind, []))
            if not rows and not evidence:
                continue  # A legacy attempt has not entered the step controller.
            if len(rows) != 1 or rows[0].get('id') != CodingSteps.budget_id(run['id'], identity):
                return [_error('coding_budget_missing', '编码工作的小步累计额度缺失或归属不明确，不能重置额度重试。')]
            budget = rows[0]
            integers = ('max_steps', 'max_tool_calls', 'observed_tool_calls', 'step_count')
            durations = ('max_active_seconds', 'active_seconds')
            if (budget.get('run_id') != run['id'] or type(budget.get('uncertain')) is not bool
                    or any(type(budget.get(field)) is not int or budget[field] < 0 for field in integers)
                    or any(type(budget.get(field)) not in {int, float} or not math.isfinite(budget[field])
                           or budget[field] < 0 for field in durations) or budget['uncertain']):
                return [_error('coding_budget_uncertain', '编码工作的累计用量尚未核验，自动恢复不会更改或重置额度。')]
            if (budget['active_seconds'] >= budget['max_active_seconds']
                    or budget['observed_tool_calls'] >= budget['max_tool_calls']
                    or budget['step_count'] >= budget['max_steps']):
                return [_error('coding_budget_exhausted', '本编码工作的累计小步次数、时长或工具额度已经用尽，未安排重复执行。')]
        return []

    def _verify_process(self, record, attempts):
        attempt = attempts.get(record['id'])
        if (not attempt or record.get('attempt_id') != attempt['id']
                or record.get('fencing_token') != attempt.get('fencing_token')
                or record.get('input_fingerprint') != attempt.get('input_fingerprint')):
            raise ValueError('process_attempt_mismatch')
        identity = canonical_digest({'attempt_id': record['id']}).split(':')[1]
        expected = self.data_dir / 'supervisor' / identity
        if Path(record.get('directory', '')) != expected:
            raise ValueError('process_directory_mismatch')
        if record.get('reason') == 'launcher_spawn_failed' and record.get('pid') is None:
            if (expected / 'identity.json').exists() or (expected / 'result.json').exists():
                raise ValueError('unexpected_launch_evidence')
            return
        with _private_attempt_directory(self.data_dir, 'supervisor', identity) as directory:
            receipt = json.loads(_private_evidence(directory, 'result.json', 65536))
            if (not isinstance(receipt, dict) or not same_launcher_identity(receipt, record)
                    or not record.get('nonce') or receipt.get('execution_status') != record['state']):
                raise ValueError('receipt_identity_mismatch')
            self._stopped_process(receipt)
            try:
                child = json.loads(_private_evidence(directory, 'child.json', 65536))
            except FileNotFoundError:
                child = None
            if child:
                if not same_launcher_identity(child, record):
                    raise ValueError('child_identity_mismatch')
                self._stopped_process({**child['child'], 'boot_fingerprint': record['boot_fingerprint'],
                                       'boot_identity_source': record.get('boot_identity_source')})
            # A launcher PID can be gone while a redirected background descendant
            # is still alive in its process group. Never infer group cleanup from
            # the leader's absence alone, and never signal any process here.
            if os.name == 'posix' and boot_relation(record) != 'changed':
                try:
                    leader = psutil.Process(record['pid'])
                    if (observe_process(record).get('birth_matches') is False
                            and os.getpgid(leader.pid) == leader.pid):
                        # A new group leader with this numeric PID proves the
                        # old group ID has been reused. Its members are not our
                        # descendants; never signal or block on those processes.
                        return
                except (psutil.NoSuchProcess, ProcessLookupError):
                    pass
                for process in psutil.process_iter(['pid', 'status'], ad_value=None):
                    try:
                        if os.getpgid(process.pid) == record['pid'] and process.info['status'] != psutil.STATUS_ZOMBIE:
                            raise DomainError('active_work', '旧执行进程组仍有后台任务，不能重复启动。')
                    except ProcessLookupError:
                        pass

    async def _process_blockers(self, state):
        from agentflow.runtime.prelaunch import prove_prelaunch_failure
        attempts = {a['id']: a for a in state['attempt']}
        supervised = {a['id']: a for a in state['supervised_attempt']}
        prelaunch = {r['id']: r for r in state.get('prelaunch_failure', [])}
        budgets = {r['id']: r for r in state.get('model_attempt_budget', [])}
        runs = {r['id']: r for r in state.get('run', [])}
        try:
            for context in state['dispatch_context']:
                if context['id'] not in supervised:
                    attempt = attempts.get(context['id'])
                    proof = await asyncio.to_thread(prove_prelaunch_failure, self.data_dir,
                        attempt, context, prelaunch.get(context['id']),
                        invocations=[i for i in state.get('model_invocation', []) if i.get('attempt_id') == context['id']],
                        budget=budgets.get(context['id']), run=runs.get((attempt or {}).get('run_id')))
                    if proof:
                        continue
                    raise ValueError('missing_supervisor_evidence')
            for record in supervised.values():
                if record.get('state') in TERMINAL:
                    await asyncio.to_thread(self._verify_process, record, attempts)
        except DomainError as error:
            return [_error(error.code, error.message)]
        except (OSError, ValueError, TypeError, KeyError, psutil.Error):
            return [_error('recovery_evidence_invalid', '旧进程的回执缺失、损坏或身份不匹配，请先核对。')]
        return []

    @staticmethod
    def _targets(state):
        items = [w for w in state['work_item'] if not w.get('archived')]
        targets = []
        for item in items:
            children = [w for w in items if w.get('parent_stage_id') == item['id']]
            roots = {w['id'] for w in children if _failed(w)}
            if item.get('kind') == 'aggregation' and children:
                if not roots and _failed(item):
                    roots = {item['id']}
            elif _failed(item):
                roots = {item['id']}
            if roots:
                affected = descendants(items, roots)
                targets.append({'work_item_id': item['id'], 'stage_key': item.get('key'), 'step': item['step'],
                    'status': item['status'], 'root_work_item_ids': sorted(roots),
                    'affected_work_item_ids': sorted(affected),
                    'preserved_work_item_ids': sorted(w['id'] for w in items if w['id'] not in affected),
                    'checkpoint': {'kind': 'upstream_checkpoint'}})
        # Prefer the containing stage, then its children, in pipeline order.
        return sorted(targets, key=lambda t: (STEPS.index(t['step']) if t['step'] in STEPS else len(STEPS),
            bool(next(w for w in items if w['id'] == t['work_item_id']).get('parent_stage_id')), t['work_item_id']))

    @classmethod
    def _cancelled_continuation(cls, state):
        """Restore every interrupted branch in one fenced recovery command."""
        if state['run'][0].get('execution_state') != 'cancelled':
            return None
        targets = cls._targets(state)
        if not targets:
            return None
        roots = set().union(*(set(target['root_work_item_ids']) for target in targets))
        items = [work for work in state['work_item'] if not work.get('archived')]
        affected = descendants(items, roots)
        return {**targets[0], 'root_work_item_ids': sorted(roots),
            'affected_work_item_ids': sorted(affected),
            'preserved_work_item_ids': sorted(work['id'] for work in items if work['id'] not in affected)}

    def _checkpoint_sources(self, state, target):
        sources = []
        for identity in target['root_work_item_ids']:
            work = next(w for w in state['work_item'] if w['id'] == identity)
            if work['step'] not in CODING_STEPS or work.get('kind') == 'aggregation':
                continue
            snapshots = [s for s in state['code_snapshot'] if s.get('work_item_id') == identity
                         and s.get('generation') == work['generation'] and not s.get('stale')]
            if len(snapshots) > 1:
                raise DomainError('recovery_checkpoint_invalid', '存在多个代码检查点，不能自动选择。')
            if snapshots:
                sources.append({'kind': 'prior_code_snapshot', 'work': work, 'snapshot': snapshots[0]})
                continue
            context = next((c for c in state['dispatch_context'] if c['id'] == work.get('attempt_id')), None)
            if context:
                sources.append({'kind': 'stopped_workspace', 'work': work, 'context': context,
                                'project': state['project'][0] if state.get('project') else None})
            elif work.get('payload', {}).get('recovery_checkpoint_id'):
                # A retry can be cancelled or blocked before it gets a workspace.
                # Preserve the already-authorized checkpoint across that failure.
                previous = next((s for s in state['code_snapshot']
                    if s['id'] == work['payload']['recovery_checkpoint_id']), None)
                sources.append({'kind': 'prior_code_snapshot', 'work': work,
                                'snapshot': previous, 'preserved_recovery': True})
            elif work.get('payload', {}).get('coding_step_checkpoint_id'):
                # Preparing the next small step can stop before a workspace is
                # dispatched. Its preceding verified contribution is still the
                # recovery source, even though it belongs to an older attempt.
                point = next((row for row in state['coding_step_checkpoint']
                              if row['id'] == work['payload']['coding_step_checkpoint_id']), None)
                previous = next((row for row in state['code_snapshot']
                                 if point and row['id'] == point.get('snapshot_id')), None)
                sources.append({'kind': 'prior_code_snapshot', 'work': work,
                                'snapshot': previous, 'preserved_coding': point})
            elif work.get('payload', {}).get('repair_base_snapshot_id'):
                # Review repair can stop before dispatch. Its reviewed source
                # must not disappear merely because no new worker was launched.
                previous = next((row for row in state['code_snapshot']
                    if row['id'] == work['payload']['repair_base_snapshot_id']), None)
                sources.append({'kind': 'prior_code_snapshot', 'work': work,
                                'snapshot': previous, 'preserved_review': True})
        return sources

    def _validate_workspace(self, source):
        work, task = source['work'], source['context'].get('task', {})
        fields = {'work_item_id': work['id'], 'attempt_id': work['attempt_id'], 'run_id': work['run_id'],
                  'fencing_token': work['fencing_token'], 'input_fingerprint': work['input_fingerprint'],
                  'allowed_write_paths': work['write_paths'], 'step': work['step']}
        if any(task.get(k) != v for k, v in fields.items()):
            raise ValueError('dispatch_identity_mismatch')
        metadata = WorkspaceManager(self.data_dir, self.repository, create=False).registration(work['attempt_id'])
        if metadata.get('version', 1) == 2:
            project = source.get('project')
            if (project and (metadata['project_id'] != project['id'] or metadata['project_root'] != project['local_path'])
                    or work.get('project_id') and metadata['project_id'] != work['project_id']):
                raise ValueError('workspace_project_mismatch')
        workspace = Path(metadata['path'])
        if Path(task.get('workspace', '')) != workspace:
            raise ValueError('workspace_identity_mismatch')
        if (metadata.get('attempt_id') != work['attempt_id'] or metadata.get('path') != str(workspace)
                or metadata.get('base_oid') != task.get('source_commit')):
            raise ValueError('workspace_metadata_mismatch')
        self.repository._integrity(workspace, task['source_commit'])
        return workspace, task['source_commit']

    async def _legacy_recovery_parent(self, state, work, current_path, snapshot):
        """Prove the old base-as-parent bug, then import its authorized source.

        No historical commit or receipt changes. The next recovery commit makes
        the proven original source a real second parent, preserving its objects.
        """
        if (snapshot.get('purpose') != 'recovery_checkpoint'
                or snapshot['id'] != work.get('payload', {}).get('recovery_checkpoint_id')):
            raise ValueError('checkpoint_ancestry_invalid')
        await validate_recovery_checkpoint(self.store, self._run(state), work, snapshot, self.repository)
        receipt = await self.store.read('run_recovery', snapshot['recovery_id'])
        if receipt.get('mode') != 'retry' or receipt.get('actor') != 'owner':
            raise ValueError('legacy_checkpoint_owner_receipt_required')
        attempt = next((row for row in state['attempt'] if row['id'] == snapshot['source_attempt_id']), None)
        context = next((row for row in state['dispatch_context'] if row['id'] == snapshot['source_attempt_id']), None)
        if not attempt or not context or attempt.get('status') not in STOPPED:
            raise ValueError('legacy_checkpoint_source_missing')
        original_work = {**work, 'attempt_id': attempt['id'], 'generation': attempt['generation'],
                         'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint']}
        original_path, original_source = await asyncio.to_thread(self._validate_workspace,
            {'work': original_work, 'context': context, 'project': state['project'][0] if state['project'] else None})
        if (original_path != Path(snapshot['repository_path']) or original_source == snapshot['base_oid']
                or original_source not in snapshot.get('parent_commit_oids', [])):
            raise ValueError('legacy_checkpoint_source_mismatch')
        actual = (await asyncio.to_thread(self.repository._run, original_path,
            ['show', '-s', '--format=%P', snapshot['commit_oid']])).decode().split()
        if actual != [snapshot['base_oid']]:
            raise ValueError('legacy_checkpoint_not_base_parent_bug')
        for ancestor in {snapshot['base_oid'], *snapshot['parent_commit_oids']}:
            if not await self.repository.contains_ancestor(original_path, ancestor, original_source):
                raise ValueError('legacy_checkpoint_unproven_parent')
        changes = self.repository._parse_changes(await asyncio.to_thread(self.repository._run, original_path,
            ['diff', '--raw', '--no-abbrev', '-z', '-M', '--no-ext-diff', '--no-textconv',
             original_source, snapshot['commit_oid']]))
        scopes = context['task']['allowed_write_paths']
        for change in changes:
            for changed in {change['path'], change['old_path']}:
                if not any(scope == '.' or changed == scope.rstrip('/') or changed.startswith(scope.rstrip('/') + '/')
                           for scope in scopes):
                    raise DomainError('write_scope_violation', '旧恢复快照与原派发版本之间存在越界改动，不能修复父链。')
        with tempfile.TemporaryDirectory(prefix='agentflow-recovery-lineage-') as temporary:
            bundle = Path(temporary).resolve() / 'source.bundle'
            evidence = await self.repository.prepare_bundle(original_path, original_source, bundle)
            imported = await self.repository.import_bundle(current_path, bundle, original_source,
                                                            expected_sha256=evidence['sha256'])
        return original_source, {'kind': 'verified_legacy_recovery_parent',
            'source_checkpoint_id': snapshot['id'], 'source_recovery_id': snapshot['recovery_id'],
            'source_attempt_id': attempt['id'], 'legacy_commit_oid': snapshot['commit_oid'],
            'original_source_commit': original_source, 'original_source_tree': imported['tree_oid'],
            'bundle_sha256': imported['bundle_sha256'], 'verified_write_paths': scopes}

    async def _checkpoints(self, state, target, *, freeze=False, recovery_id=None, include_unchanged=False):
        checkpoints = []
        for source in self._checkpoint_sources(state, target):
            work = source['work']
            try:
                if source['kind'] == 'prior_code_snapshot':
                    prior = source['snapshot']
                    if source.get('preserved_recovery'):
                        await validate_recovery_checkpoint(self.store, self._run(state), work, prior, self.repository)
                    if source.get('preserved_review'):
                        review_repair_source(state, self._run(state), work, prior)
                        original_id = (prior or {}).get('source_child_snapshot_id', (prior or {}).get('id'))
                        original = next((row for row in state['attempt'] if row['id'] == original_id), None)
                        if (not prior or not original or original.get('status') != 'completed'
                                or prior.get('generation') != work['generation'] - 1
                                or original.get('work_item_id') != work['id'] or original.get('run_id') != work['run_id']
                                or original.get('generation') != prior['generation']
                                or (prior.get('checkpoint_kind') in REVIEW_CHECKPOINT_KINDS
                                    and prior.get('child_scope') != work['write_paths'])):
                            raise ValueError('review_checkpoint_identity_mismatch')
                        prior = {**prior, 'source_attempt_id': original['id'],
                                 'source_generation': original['generation'],
                                 'source_fencing_token': original['fencing_token'],
                                 'source_input_fingerprint': original['input_fingerprint'],
                                 'source_review_snapshot_id': prior['id'],
                                 'source_review_write_paths': list(work['write_paths']),
                                 'base_oid': prior['commit_oid']}
                    if 'preserved_coding' in source:
                        from agentflow.control.coding_steps import CodingSteps
                        point = source['preserved_coding']
                        await CodingSteps(self.store, self.workflow.settings, self.repository).validate_source(
                            self._run(state), work)
                        original = next((row for row in state['attempt']
                                         if point and row['id'] == point.get('attempt_id')), None)
                        if (not prior or not point or not original or original.get('status') != 'completed'
                                or not original.get('coding_step_complete') or original.get('work_complete') is not False
                                or original.get('work_item_id') != work['id'] or original.get('run_id') != work['run_id']
                                or point.get('generation') != work['generation'] - 1
                                or point.get('generation') != original.get('generation')
                                or prior.get('generation') != point['generation']
                                or prior['id'] != original['id'] or point.get('snapshot_id') != original['id']
                                or point.get('budget_id') != CodingSteps.budget_id(work['run_id'], work['id'])
                                or prior.get('tree_oid') != point.get('tree_oid')
                                or prior.get('base_oid') != point.get('base_commit')):
                            raise ValueError('coding_checkpoint_identity_mismatch')
                        prior = {**prior, 'source_attempt_id': original['id'],
                                 'source_generation': original['generation'],
                                 'source_fencing_token': original['fencing_token'],
                                 'source_input_fingerprint': original['input_fingerprint'],
                                 'source_coding_checkpoint_id': point['id']}
                    path = Path(prior['repository_path'])
                    if path.is_symlink() or path.resolve() != path:
                        raise ValueError('unsafe_checkpoint_path')
                    tree = await asyncio.to_thread(self.repository._integrity, path, prior['commit_oid'])
                    if (tree != prior['tree_oid'] or prior.get('run_id') != work['run_id']
                            or (not source.get('preserved_recovery') and not source.get('preserved_review')
                                and 'preserved_coding' not in source and prior['id'] != work.get('attempt_id'))):
                        raise ValueError('checkpoint_identity_mismatch')
                    lineage = prior.get('parent_commit_oids', [])
                    if not isinstance(lineage, list) or not all(isinstance(value, str) and value for value in lineage):
                        raise ValueError('checkpoint_ancestry_invalid')
                    for ancestor in {prior['base_oid'], *lineage}:
                        if not await self.repository.contains_ancestor(path, ancestor, prior['commit_oid']):
                            raise ValueError('checkpoint_ancestry_invalid')
                    snapshot = prior
                else:
                    path, base = await asyncio.to_thread(self._validate_workspace, source)
                    if not freeze:
                        checkpoints.append({'kind': 'stopped_workspace', 'work_item_id': work['id'],
                                            'source_attempt_id': work['attempt_id']})
                        continue
                    previous_id = work.get('payload', {}).get('recovery_checkpoint_id')
                    previous = next((s for s in state['code_snapshot'] if s['id'] == previous_id), None)
                    if previous_id:
                        await validate_recovery_checkpoint(self.store, self._run(state), work, previous, self.repository)
                        if source['context']['task']['source_commit'] != previous['commit_oid']:
                            raise ValueError('repeated_recovery_source_mismatch')
                        base = previous['base_oid']
                    task = source['context']['task']
                    if task.get('coding_step'):
                        control = next((row for row in state['coding_step_control']
                                        if row['id'] == work['attempt_id']), None)
                        from agentflow.control.coding_steps import CodingSteps
                        if (not control or task['coding_step'] != control
                                or control.get('run_id') != work['run_id'] or control.get('work_item_id') != work['id']
                                or control.get('attempt_id') != work['attempt_id']
                                or control.get('budget_id') != CodingSteps.budget_id(work['run_id'], work['id'])
                                or any(control.get(field) != work.get(field)
                                       for field in ('generation', 'fencing_token', 'input_fingerprint'))
                                or control.get('source_commit') != task['source_commit']
                                or not await self.repository.contains_ancestor(
                                    path, control['base_commit'], task['source_commit'])):
                            raise ValueError('coding_workspace_baseline_mismatch')
                        # No new edit in this attempt does not erase the earlier
                        # verified steps already present in its source commit.
                        base = control['base_commit']
                    source_commit = task['source_commit']
                    repair_id = work.get('payload', {}).get('repair_base_snapshot_id')
                    if repair_id:
                        prior = next((row for row in state['code_snapshot'] if row['id'] == repair_id), None)
                        if prior and prior.get('checkpoint_kind') in REVIEW_CHECKPOINT_KINDS:
                            review_repair_source(state, self._run(state), work, prior)
                        if (not prior or prior.get('run_id') != work['run_id']
                                or prior.get('work_item_id') != work['id']
                                or prior.get('generation') != work['generation'] - 1
                                or prior.get('commit_oid') != source_commit
                                or (prior.get('checkpoint_kind') in REVIEW_CHECKPOINT_KINDS
                                    and prior.get('child_scope') != work['write_paths'])):
                            raise ValueError('repair_source_binding_mismatch')
                    parents, additional_parents, lineage_repairs = {source_commit}, [], []
                    for ancestor in state['code_snapshot']:
                        if ancestor.get('run_id') == work['run_id'] and ancestor.get('commit_oid') == source_commit:
                            lineage = ancestor.get('parent_commit_oids', [])
                            if (not isinstance(lineage, list) or not all(isinstance(value, str) and value for value in lineage)
                                    or not isinstance(ancestor.get('base_oid'), str) or not ancestor['base_oid']):
                                raise ValueError('checkpoint_ancestry_invalid')
                            values = {source_commit, ancestor['base_oid'], *lineage}
                            proven = [await self.repository.contains_ancestor(path, value, source_commit) for value in values]
                            if not all(proven):
                                parent, evidence = await self._legacy_recovery_parent(state, work, path, ancestor)
                                additional_parents.append(parent)
                                lineage_repairs.append(evidence)
                            parents.update(values)
                    snapshot = await self.repository.freeze_workspace(path, source_commit, 'AgentFlow recovery checkpoint',
                        diff_base_oid=base, additional_parent_oids=additional_parents)
                    if not snapshot['diff']['has_changes'] and not include_unchanged:
                        # The source is still the accepted checkpoint when this
                        # attempt made no edit. Retain that exact commit instead
                        # of dropping the source and reverting to project HEAD.
                        if snapshot['tree_oid'] != await asyncio.to_thread(
                                self.repository._integrity, path, source_commit):
                            raise ValueError('unchanged_source_tree_mismatch')
                        snapshot = {**snapshot, 'commit_oid': source_commit}
                    for change in snapshot['diff']['changes']:
                        for changed_path in {change['path'], change['old_path']}:
                            if not any(scope == '.' or changed_path == scope.rstrip('/')
                                       or changed_path.startswith(scope.rstrip('/') + '/') for scope in work['write_paths']):
                                raise DomainError('write_scope_violation', '旧工作区包含超出原授权文件范围的改动，不能自动复用。')
                    # A stopped coding attempt may itself have started from a
                    # reviewed aggregate or an earlier recovery checkpoint.
                    # Retain that source's transitive contribution metadata.
                    parents.update(snapshot['parent_commit_oids'])
                    parents.discard(snapshot['commit_oid'])
                    for parent in parents:
                        if not await self.repository.contains_ancestor(path, parent, snapshot['commit_oid']):
                            raise ValueError('checkpoint_ancestry_invalid')
                    snapshot = {**snapshot, 'repository_path': str(path), 'parent_commit_oids': sorted(parents),
                                'lineage_repairs': lineage_repairs,
                                'source_commit': source_commit, 'source_write_paths': list(work['write_paths']),
                                **({'source_repair_snapshot_id': repair_id} if repair_id else {})}
                preserved_source = source.get('preserved_recovery') or source.get('preserved_review') or 'preserved_coding' in source
                checkpoints.append({'kind': source['kind'], 'work_item_id': work['id'],
                    'snapshot_id': str(uuid5(NAMESPACE_URL, f'{recovery_id}:{work["id"]}')) if freeze else snapshot['id'],
                    'commit_oid': snapshot['commit_oid'],
                    **({'record': {'run_id': work['run_id'], 'work_item_id': work['id'],
                        'generation': work['generation'], 'repository_path': snapshot['repository_path'],
                        'commit_oid': snapshot['commit_oid'], 'tree_oid': snapshot['tree_oid'],
                        'base_oid': snapshot['base_oid'], 'parent_commit_oids': snapshot.get('parent_commit_oids', []),
                        **({'lineage_repairs': snapshot['lineage_repairs']} if snapshot.get('lineage_repairs') else {}),
                        'source_attempt_id': snapshot['source_attempt_id'] if preserved_source else work.get('attempt_id'),
                        'source_generation': snapshot.get('source_generation', snapshot['generation']) if preserved_source else work['generation'],
                        'source_fencing_token': snapshot['source_fencing_token'] if preserved_source else work['fencing_token'],
                        'source_input_fingerprint': snapshot['source_input_fingerprint'] if preserved_source else work['input_fingerprint'],
                        **({'source_coding_checkpoint_id': snapshot['source_coding_checkpoint_id']}
                           if preserved_source and snapshot.get('source_coding_checkpoint_id') else {}),
                        **{field: snapshot[field] for field in
                           ('source_commit', 'source_write_paths', 'source_repair_snapshot_id',
                            'source_review_snapshot_id', 'source_review_write_paths') if field in snapshot},
                        'recovery_id': recovery_id,
                        'stale': True, 'purpose': 'recovery_checkpoint', 'quality_result': 'unknown'}} if freeze else {})})
            except DomainError as error:
                if error.code == 'write_scope_violation':
                    raise
                raise DomainError('recovery_checkpoint_invalid', '旧代码检查点无法验证，未丢弃旧代码或重建工作区。') from error
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise DomainError('recovery_checkpoint_invalid', '旧代码检查点或工作区身份无法验证。') from error
        return checkpoints

    async def coding_progress(self, state, target):
        """Capture a stopped coding tree without changing its HEAD, index or files."""
        identities = target.get('root_work_item_ids', [])
        if len(identities) != 1:
            raise DomainError('bounded_coding_unavailable', '分步恢复必须对应一个明确的编码任务。')
        work = next((row for row in state['work_item'] if row['id'] == identities[0]), None)
        attempt = next((row for row in state['attempt'] if row['id'] == (work or {}).get('attempt_id')), None)
        context = next((row for row in state['dispatch_context'] if row['id'] == (work or {}).get('attempt_id')), None)
        if (not work or work.get('archived') or work.get('step') not in CODING_STEPS
                or work.get('kind') == 'aggregation' or work.get('status') not in STOPPED
                or not attempt or attempt.get('status') not in STOPPED or not context
                or attempt.get('run_id') != work.get('run_id') or attempt.get('work_item_id') != work['id']
                or any(attempt.get(field) != work.get(field) for field in ('generation', 'fencing_token', 'input_fingerprint'))):
            raise DomainError('bounded_coding_unavailable', '当前编码执行或来源尚未核验，不能生成分步恢复计划。')
        blockers = await self._process_blockers(state)
        if blockers:
            raise DomainError(blockers[0]['code'], blockers[0]['message'])
        try:
            path, source = await asyncio.to_thread(self._validate_workspace, {'work': work, 'context': context,
                'project': state['project'][0] if state.get('project') else None})
            snapshot = await self.repository.collect_diff(path, source)
            for change in snapshot['changes']:
                for changed in {change['path'], change['old_path']}:
                    if not any(scope == '.' or changed == scope.rstrip('/')
                               or changed.startswith(scope.rstrip('/') + '/') for scope in work['write_paths']):
                        raise DomainError('write_scope_violation', '旧代码含超出授权文件范围的变化，不能作为自动分步恢复进展。')
            source_tree = await asyncio.to_thread(self.repository._integrity, path, source)
            return {'work_item_id': work['id'], 'attempt_id': attempt['id'], 'generation': work['generation'],
                'fencing_token': work['fencing_token'], 'input_fingerprint': work['input_fingerprint'],
                'source_commit': source, 'source_tree_oid': source_tree, 'tree_oid': snapshot['tree_oid'],
                'changed_paths': sorted({change['path'] for change in snapshot['changes']}),
                'has_code_changes': snapshot['tree_oid'] != source_tree}
        except DomainError as error:
            if error.code == 'write_scope_violation':
                raise
            raise DomainError('recovery_checkpoint_invalid', '编码进展无法核验，保留原代码并停止自动恢复。') from error
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise DomainError('recovery_checkpoint_invalid', '编码进展或来源身份无法核验。') from error

    async def _role_checkpoints(self, state, target, recovery_id):
        points = []
        for work_id in target['root_work_item_ids']:
            work = next(row for row in state['work_item'] if row['id'] == work_id)
            if (work.get('step') in CODING_STEPS | EXECUTION_STEPS or work.get('kind') == 'aggregation'
                    or work.get('status') == 'completed'):
                continue
            context = next((row for row in state['dispatch_context'] if row['id'] == work.get('attempt_id')), None)
            source_attempt = next((row for row in state['attempt'] if row['id'] == work.get('attempt_id')), None)
            export = None
            if context and source_attempt:
                folder = self.data_dir / 'attempt_artifacts' / canonical_digest(source_attempt['id']).split(':')[1]
                if folder.is_symlink() or folder.resolve() != folder:
                    raise DomainError('invalid_role_output_checkpoint', '角色草稿目录无法核验。')
                if folder.is_dir():
                    from agentflow.adapters.openhands.output_builder import export_partial, result_identity
                    expected = result_identity(context['task'])
                    if (expected.get('attempt_id') != source_attempt['id'] or source_attempt.get('status') not in STOPPED
                            or source_attempt.get('run_id') != work['run_id'] or source_attempt.get('work_item_id') != work['id']
                            or any(expected.get(field) != source_attempt.get(field)
                                   for field in ('run_id', 'iteration_id', 'work_item_id', 'fencing_token', 'input_fingerprint'))):
                        raise DomainError('invalid_role_output_checkpoint', '旧角色草稿与执行身份不匹配。')
                    try:
                        if work.get('step') in {'goal', 'development_plan'} and (folder / 'openhands_final.json').exists():
                            from agentflow.control.planning_recovery import export_planning_final
                            export = await asyncio.to_thread(export_planning_final, self.data_dir, context['task'], source_attempt)
                        else:
                            export = await asyncio.to_thread(export_partial, folder, expected_identity=expected)
                    except (OSError, ValueError, TypeError) as error:
                        raise DomainError('invalid_role_output_checkpoint', '旧角色草稿无法安全导出。') from error
                    if export:
                        export = {**export, 'source_directory': str(folder), 'source_attempt_id': source_attempt['id'],
                                  'source_generation': source_attempt['generation']}
            if export is None and work.get('payload', {}).get('role_output_checkpoint_id'):
                export = await resolve_role_output_checkpoint(self.store, self.workflow.settings, self._run(state), work)
            if not export:
                continue
            checkpoint_id = str(uuid5(NAMESPACE_URL, f'role-output-recovery:{recovery_id}:{work_id}'))
            record = {key: export[key] for key in ('digest', 'path', 'source_directory', 'source_identity', 'schema_digest',
                'progress_digest', 'draft_count', 'committed_chunks', 'result_bytes', 'source_attempt_id', 'source_generation')}
            if export.get('checkpoint_kind'):
                record['checkpoint_kind'] = export['checkpoint_kind']
            record.update(run_id=work['run_id'], work_item_id=work_id, authorized_generation=work['generation'] + 1,
                          recovery_id=recovery_id, purpose='role_output_recovery')
            points.append({'checkpoint_id': checkpoint_id, **{key: record[key] for key in ('work_item_id', 'digest',
                           'authorized_generation', 'source_attempt_id', 'source_generation', 'schema_digest', 'progress_digest')},
                           'record': record})
        return points

    async def options(self, run_id):
        state = await self._read(run_id)
        common = self._common_blockers(state) + await self._process_blockers(state)
        targets = self._targets(state)
        for target in targets:
            target_blockers = await self._target_blockers(state, target)
            blockers = list(common) + target_blockers
            try:
                points = await self._checkpoints(state, target)
                target['checkpoint'] = points[0] if len(points) == 1 else {'kind': 'upstream_checkpoint', 'items': points}
            except DomainError as error:
                blockers.append(_error(error.code, error.message))
            target.update(eligible=not blockers, blockers=blockers)
            work = next(row for row in state['work_item'] if row['id'] == target['work_item_id'])
            if (work.get('step') in CODING_STEPS and work.get('kind') != 'aggregation'
                    and any(row.get('work_item_id') == work['id'] for row in state['coding_work_budget'])):
                from agentflow.control.work_execution_budget import WorkExecutionBudgetService
                target['execution_budget'] = await WorkExecutionBudgetService(self.store, self.workflow).summary(
                    state, work['id'], common=common, target_blockers=target_blockers)
            target.pop('root_work_item_ids')
        continuation = self._common_blockers(state, allow_execution_wait=True) + await self._process_blockers(state)
        cancelled = self._run(state).get('execution_state') == 'cancelled'
        resume_target = self._cancelled_continuation(state)
        if self._run(state).get('execution_state') not in {'paused', 'cancelled'}:
            continuation.append(_error('run_not_paused', '暂停或已取消的运行可以继续调度。'))
        if targets and not cancelled:
            continuation.append(_error('retry_required', '先重试失败或中断的工作，才能继续。'))
        if cancelled:
            continuation.extend(blocker for target in targets for blocker in target['blockers'])
        if not resume_target and not any(not w.get('archived') and w.get('status') in {
                'pending', 'pending_delivery', 'waiting_approval', 'waiting_execution'} for w in state['work_item']):
            continuation.append(_error('no_pending_work', '没有等待继续的工作。'))
        continuation = list({(b['code'], b['message']): b for b in continuation}.values())
        return {'run_id': run_id, 'expected_revision': self._run(state)['revision'],
                'session_resume': 'unsupported_ephemeral', 'continue': {'eligible': not continuation, 'blockers': continuation,
                    'affected_work_item_ids': resume_target['affected_work_item_ids'] if resume_target else [],
                    'preserved_work_item_ids': resume_target['preserved_work_item_ids'] if resume_target else
                        sorted(w['id'] for w in state['work_item'] if not w.get('archived'))},
                'retry_options': targets}

    @staticmethod
    def _clone_candidate(tx, state, affected, fingerprint, recovery_id):
        items = {w['id']: w for w in state['work_item']}
        candidates = [c for c in state['candidate'] if not c.get('stale')
                      and c.get('run_input_fingerprint') == state['run'][0]['input_fingerprint']]
        # With unchanged code, retain successful upstream build/test evidence and
        # give the failed phases fresh node idempotency keys via a new candidate ID.
        reuse = affected and all(items[i]['step'] in EXECUTION_STEPS | {'delivery', 'retrospective'} for i in affected)
        for candidate in candidates:
            tx.put('candidate', candidate['id'], {**candidate, 'stale': True}, candidate['revision'])
        if not reuse or not candidates:
            return
        if len(candidates) != 1:
            raise DomainError('recovery_checkpoint_invalid', '候选源码身份不唯一。')
        prior = candidates[0]
        phases = {('unit' if items[i]['step'] == 'unit_test_execution' else 'integration')
                  for i in affected if items[i]['step'] in EXECUTION_STEPS}
        jobs = {j['id']: j for j in state['node_job']}
        build_ok = bool(prior.get('platform_manifest')) and all(jobs.get(j, {}).get('state') == 'completed'
            and jobs[j].get('quality_result') == 'passed' for j in prior.get('build_job_ids', []))
        phase_jobs = {phase: ids for phase, ids in prior.get('phase_jobs', {}).items() if phase not in phases}
        if any(jobs.get(j, {}).get('state') != 'completed' or jobs[j].get('quality_result') != 'passed'
               for j in phase_jobs.get('install', [])):
            phase_jobs.pop('install', None)
        changes = {'phase_jobs': phase_jobs, 'state': 'platform_artifacts_frozen' if build_ok else 'source_frozen'}
        if not build_ok:
            if any(w['step'] in EXECUTION_STEPS and w['id'] not in affected and w['status'] == 'completed'
                   for w in items.values()):
                raise DomainError('recovery_checkpoint_invalid', '已完成测试的构建证据不一致，不能自动复用。')
            changes.update(build_job_ids=[], platform_manifest=None, matrix_binding=None,
                           phase_jobs={}, fingerprint=prior['source_manifest']['fingerprint'])
        identity = str(uuid5(NAMESPACE_URL, f'recovery-candidate:{recovery_id}'))
        body = {k: v for k, v in prior.items() if k not in {'id', 'revision'}}
        tx.put('candidate', identity, {**body, **changes, 'run_input_fingerprint': fingerprint,
            'stale': False, 'recovery_id': recovery_id, 'prior_candidate_id': prior['id']})
        old_matrix = next((m for m in state['target_matrix'] if m['id'] == prior['id']), None)
        if old_matrix:
            tx.put('target_matrix', identity, {k: v for k, v in old_matrix.items() if k not in {'id', 'revision'}})

    async def _model_choices(self, state, target, use_current):
        """Resolve outside the writer, then compare these exact records at commit."""
        if not target:
            return {}, {}
        coding = [w for w in state['work_item'] if w['id'] in target['affected_work_item_ids']
                  and w.get('step') in CODING_STEPS and not w.get('archived')]
        choices, observed = {}, {}
        if use_current:
            if target['step'] not in CODING_STEPS:
                raise DomainError('recovery_model_not_applicable', '仅编码步骤重试可以选择当前模型设置。', 422)
            binding = await self.store.read('product_model_binding', 'default')
            profile_id = (binding or {}).get('coding_model_profile_id')
            if not isinstance(profile_id, str) or not profile_id:
                raise DomainError('recovery_model_unavailable', '请先配置当前编码模型，再选择使用当前模型设置重试。')
            raw = await self.store.read('model_profile', profile_id)
            profile = _accepted_coding_profile(raw)
            observed['product_model_binding', 'default'] = binding
            observed['model_profile', profile_id] = raw
            choices = {w['id']: {'model_profile_id': profile_id, 'profile_revision': profile.revision} for w in coding}
        else:
            for work in coding:
                prior = await _read_recovery_model_binding(self.store, self._run(state), work)
                if not prior:
                    continue
                binding = prior['binding']
                choices[work['id']] = {field: binding[field] for field in ('model_profile_id', 'profile_revision')}
                observed['run_recovery', binding['recovery_id']] = prior['receipt']
                observed['model_profile', binding['model_profile_id']] = prior['profile']
        return choices, observed

    async def recover(self, run_id, payload, key):
        try:
            return await self._recover(run_id, payload, key)
        except DomainError:
            # Another waiter with the same key can commit while this caller is
            # reading evidence. Replay it before returning a stale-read error.
            identity = str(uuid5(NAMESPACE_URL, f'run-recovery:{run_id}:{key}'))
            if await self.store.read('run_recovery', identity):
                request = RecoveryRequest.model_validate(payload)
                return await self.store.command('run.recover', key,
                    _recovery_command(run_id, request), lambda tx: {})
            raise

    async def recover_automatic(self, run_id, analysis_id):
        """Internal controller entry point; owner request payloads cannot select this actor."""
        analysis = await self.store.read('failure_analysis', analysis_id)
        if not analysis or analysis.get('run_id') != run_id:
            raise DomainError('automatic_repair_stale', '自动恢复缺少本轮失败分析。')
        key = 'automatic-failure:' + analysis_id
        payload = {'expected_revision': analysis['observed_run_revision'], 'mode': 'retry',
                   'work_item_id': analysis['work_item_id'], 'reason': (
                       '在原配置和预算内从保存草稿修正计划；精确错误清单由已核验的诊断恢复。'
                       if analysis.get('failure_code') == 'planning_validation_failed' else analysis['repair_instruction'])}
        return await self._recover(run_id, payload, key, analysis_id=analysis_id)

    async def _recover(self, run_id, payload, key, *, analysis_id=None):
        request = RecoveryRequest.model_validate(payload)
        if request.task_correction is not None and (analysis_id is not None or request.mode != 'retry'
                or not request.work_item_id or not request.task_correction.strip()):
            raise DomainError('invalid_recovery_correction', '纠正任务指令必须由所有者明确选择单个任务重试。', 422)
        if request.mode == 'continue' and (request.work_item_id is not None or request.use_current_model_settings):
            raise DomainError('invalid_recovery', '继续调度不接受单个任务目标或更换模型设置。', 422)
        command = _recovery_command(run_id, request)
        recovery_id = str(uuid5(NAMESPACE_URL, f'run-recovery:{run_id}:{key}'))
        # A lost acknowledgement must replay even after subsequent work progresses.
        if await self.store.read('run_recovery', recovery_id):
            return await self.store.command('run.recover', key, command, lambda tx: {})
        state = await self._read(run_id)
        ensure_revision(self._run(state), request.expected_revision)
        blockers = self._common_blockers(state, allow_execution_wait=request.mode == 'continue') + await self._process_blockers(state)
        if blockers:
            raise DomainError(blockers[0]['code'], blockers[0]['message'], details=blockers)
        targets = self._targets(state)
        target = None
        if request.mode == 'retry':
            target = next((t for t in targets if t['work_item_id'] == request.work_item_id), None) if request.work_item_id else next(iter(targets), None)
            if not target:
                raise DomainError('work_not_retryable', '请选择已失败、阻塞或取消的工作。')
            if request.task_correction is not None and set(target['root_work_item_ids']) != {request.work_item_id}:
                raise DomainError('invalid_recovery_correction', '纠正任务指令不能隐式改写多个子任务。', 422)
        elif self._run(state).get('execution_state') == 'cancelled':
            target = self._cancelled_continuation(state)
            if not target and not any(not w.get('archived') and w.get('status') in {
                    'pending', 'pending_delivery', 'waiting_approval', 'waiting_execution'} for w in state['work_item']):
                raise DomainError('no_pending_work', '没有等待继续的工作。')
        elif self._run(state).get('execution_state') != 'paused' or targets:
            raise DomainError('retry_required' if targets else 'run_not_paused', '暂停的运行可以继续；失败或中断的工作需要重试。')
        elif not any(w.get('status') in {'pending', 'pending_delivery', 'waiting_approval', 'waiting_execution'} for w in state['work_item']):
            raise DomainError('no_pending_work', '没有等待继续的工作。')
        targeted = await self._target_blockers(state, target)
        if targeted:
            raise DomainError(targeted[0]['code'], targeted[0]['message'])
        automatic_analysis = None
        if analysis_id:
            from agentflow.control.failure_remediation import guard_automatic
            def preflight(tx):
                work = tx.get('work_item', target['work_item_id'])
                analysis = guard_automatic(tx, self.workflow, analysis_id, work, 'retry_current')
                if set(target['root_work_item_ids']) != {work['id']}:
                    raise DomainError('automatic_repair_stale', '自动恢复只能重试已分析的失败任务。')
                return analysis
            automatic_analysis = await self.store.command('failure.recovery_preflight', str(uuid4()), {'analysis_id': analysis_id}, preflight)
        bounded_plan = (automatic_analysis or {}).get('bounded_coding_recovery')
        model_choices, observed_models = await self._model_choices(state, target, request.use_current_model_settings)
        checkpoints = await self._checkpoints(state, target, freeze=True, recovery_id=recovery_id,
            include_unchanged=bool(bounded_plan)) if target else []
        bounded_checkpoint = next((point for point in checkpoints if point['work_item_id'] == target['work_item_id']), None) if bounded_plan else None
        if bounded_plan and (not bounded_checkpoint or bounded_checkpoint['record']['tree_oid'] != bounded_plan['progress']['tree_oid']):
            raise DomainError('automatic_repair_stale', '代码在分步分析后发生变化，必须重新核验后再恢复。')
        role_points = await self._role_checkpoints(state, target, recovery_id) if target else []
        planning_diagnostics = {}
        if target:
            from agentflow.control.planning_recovery import (
                planning_failure_diagnostic,
                planning_state_diagnostic,
            )
            for work_id in target['root_work_item_ids']:
                original = next(item for item in state['work_item'] if item['id'] == work_id)
                original_attempt = next((item for item in state['attempt'] if item['id'] == original.get('attempt_id')), {})
                diagnostic = (planning_state_diagnostic(original, original_attempt)
                    or await planning_failure_diagnostic(self.store, self.workflow.settings, original, original_attempt))
                if diagnostic:
                    planning_diagnostics[work_id] = diagnostic
        # Freeze and Git integrity checks happen outside the SQLite writer. Recheck
        # process evidence after capture, then compare every relevant record atomically.
        blockers = await self._process_blockers(state)
        blockers += await self._target_blockers(state, target)
        if blockers:
            raise DomainError(blockers[0]['code'], blockers[0]['message'])
        observed = canonical_digest(state)

        def apply(tx):
            current = _related({kind: tx.list(kind) for kind in KINDS}, run_id)
            ensure_revision(self._run(current), request.expected_revision)
            if canonical_digest(current) != observed:
                raise DomainError('revision_conflict', '恢复校验期间执行或检查点发生变化，请刷新后重试。')
            for (kind, identity), record in observed_models.items():
                if tx.get(kind, identity) != record:
                    raise DomainError('recovery_model_changed', '模型设置在恢复校验期间发生变化，请刷新后重新确认。')
            run = self._run(current)
            if analysis_id:
                from agentflow.control.failure_remediation import guard_automatic
                analyzed = guard_automatic(tx, self.workflow, analysis_id, tx.get('work_item', target['work_item_id']), 'retry_current')
                if analyzed.get('bounded_coding_recovery') != bounded_plan:
                    raise DomainError('automatic_repair_stale', '分步恢复计划已变化，未使用旧分析安排重试。')
            affected = set()
            model_bindings = {}
            bounded_authorization = None
            reason = request.reason.strip() or '用户要求从已核验的检查点重新执行，保留原验收标准和质量门禁。'
            fingerprint = run['input_fingerprint']
            if target:
                roots = set(target['root_work_item_ids'])
                parents = frozenset(w.get('parent_stage_id') for w in current['work_item'] if w['id'] in roots)
                if next(w for w in current['work_item'] if w['id'] == target['work_item_id']).get('kind') == 'aggregation':
                    parents |= {target['work_item_id']}
                affected = self.workflow._invalidate(tx, [w for w in current['work_item'] if not w.get('archived')], roots, reason,
                                                     expand_roots=False, preserve_stage_ids=parents,
                                                     inherit_model_settings=bool(analysis_id))
                # Recovery repeats the same accepted work. Its execution advice
                # must not replace review findings or an owner's correction.
                for original in current['work_item']:
                    if original['id'] not in roots:
                        continue
                    work = tx.get('work_item', original['id'])
                    restored = {**work.get('payload', {}), 'recovery_instruction': reason}
                    if original['id'] in planning_diagnostics:
                        diagnostic = planning_diagnostics[original['id']]
                        restored['planning_recovery_diagnostic'] = diagnostic
                        if diagnostic['code'] == 'stale_planning_contract':
                            restored['recovery_instruction'] += ('\n规划版本已变化：重新读取当前规划契约与目标阶段版本，再核验已保存草稿；'
                                '仅提交仍可展开的阶段，不沿用旧版本假设。')
                        else:
                            restored['recovery_instruction'] += ('\n从已保存草稿修正规划，使用 result_revise_parallel_work 保留正文并替换 parallel_work；'
                                '原结果尚未通过计划校验。精确错误：' + json.dumps(diagnostic['details']['issues'], ensure_ascii=False))
                    if 'change_expectation' in original.get('payload', {}):
                        restored['change_expectation'] = original['payload']['change_expectation']
                    else:
                        restored.pop('change_expectation', None)
                    if request.task_correction is not None:
                        restored['change_expectation'] = request.task_correction
                    tx.put('work_item', work['id'], {**work, 'payload': restored}, work['revision'])
                fingerprint = canonical_digest({'previous': run['input_fingerprint'], 'recovery_id': recovery_id,
                                                'affected': sorted(affected)})
                self._clone_candidate(tx, current, affected, fingerprint, recovery_id)
                for checkpoint in checkpoints:
                    tx.put('code_snapshot', checkpoint['snapshot_id'], checkpoint['record'])
                    work = tx.get('work_item', checkpoint['work_item_id'])
                    tx.put('work_item', work['id'], {**work, 'payload': {**work.get('payload', {}),
                        'repair_base_snapshot_id': checkpoint['snapshot_id'],
                        'recovery_checkpoint_id': checkpoint['snapshot_id']}}, work['revision'])
                if bounded_plan:
                    work = tx.get('work_item', target['work_item_id'])
                    binding = {'recovery_id': recovery_id, 'analysis_id': analysis_id, 'run_id': run_id,
                        'work_item_id': work['id'], 'generation': work['generation'], 'plan_digest': canonical_digest(bounded_plan)}
                    tx.put('work_item', work['id'], {**work, 'payload': {**work.get('payload', {}),
                        'bounded_coding_recovery': binding}}, work['revision'])
                    bounded_authorization = {'binding': binding, 'plan': bounded_plan,
                                             'checkpoint_id': bounded_checkpoint['snapshot_id']}
                for point in role_points:
                    tx.put('role_output_checkpoint', point['checkpoint_id'], point['record'])
                    work = tx.get('work_item', point['work_item_id'])
                    tx.put('work_item', work['id'], {**work, 'payload': {**work.get('payload', {}),
                        'role_output_checkpoint_id': point['checkpoint_id'],
                        'recovery_instruction': work['payload'].get('recovery_instruction', '')
                            + ' 已保留此前验证过的角色草稿，请从恢复的结果构建器继续未完成段落；草稿尚未通过最终结果与质量门禁。'}}, work['revision'])
                for work_id, choice in model_choices.items():
                    if analysis_id:
                        # _invalidate issued a controller inheritance receipt;
                        # an automatic retry cannot masquerade as a new owner choice.
                        continue
                    work = tx.get('work_item', work_id)
                    if work_id not in affected or work.get('archived'):
                        continue
                    binding = {**choice, 'recovery_id': recovery_id, 'run_id': run_id,
                        'iteration_id': run['iteration_id'], 'work_item_id': work_id, 'generation': work['generation']}
                    tx.put('work_item', work_id, {**work, 'payload': {**work.get('payload', {}),
                        _MODEL_BINDING: binding}}, work['revision'])
                    model_bindings[work_id] = {**binding, 'payload_digest': canonical_digest(binding)}
            updated = tx.put('run', run_id, {**run, 'execution_state': 'running',
                'quality_result': 'unknown' if target else run['quality_result'],
                'blocking_reasons': [] if target else run.get('blocking_reasons', []),
                'input_fingerprint': fingerprint}, run['revision'])
            self.workflow._recompute_run(tx, run_id)
            from agentflow.control.product_management import product_for_run
            product = product_for_run(tx, run)
            if product and product.get('run_id') == run_id and product.get('state') == 'cancelled':
                # The product coordinator skips cancelled products. Restore its
                # current run atomically so later export/delivery can advance.
                tx.put('product', product['id'], {**product, 'state': 'running', 'phase': 'reconciling',
                    'blocking_reasons': []}, product['revision'])
                tx.event('product.run_resumed', {'product_id': product['id'], 'run_id': run_id}, run_id=run_id)
            points = [{k: v for k, v in c.items() if k != 'record'} for c in checkpoints]
            receipt = tx.put('run_recovery', recovery_id, {'run_id': run_id, 'mode': request.mode,
                'iteration_id': run['iteration_id'],
                'work_item_id': target['work_item_id'] if target else None,
                'affected_work_item_ids': sorted(affected),
                'preserved_work_item_ids': sorted(w['id'] for w in current['work_item'] if w['id'] not in affected and not w.get('archived')),
                'checkpoint': points[0] if len(points) == 1 else {'kind': 'upstream_checkpoint', 'items': points},
                'session_resume': 'unsupported_ephemeral', 'execution': 'fresh_attempt' if target else 'resume_scheduling',
                **({'model_profile_bindings': model_bindings,
                    'model_settings_source': 'current_configuration' if request.use_current_model_settings else 'prior_recovery'} if model_bindings else {}),
                'actor': 'system' if analysis_id else 'owner',
                **({'task_correction': request.task_correction} if request.task_correction is not None else {}),
                **({'failure_analysis_id': analysis_id} if analysis_id else {}),
                **({'bounded_coding_recovery': bounded_authorization} if bounded_authorization else {}),
                **({'role_output_checkpoints': [{key: value for key, value in point.items() if key != 'record'}
                                               for point in role_points]} if role_points else {}),
                'reason': reason, 'created_at': utc_now(), 'run': tx.get('run', updated['id'])})
            if analysis_id:
                from agentflow.control.failure_remediation import finish_automatic
                finish_automatic(tx, analysis_id, receipt, kind='run_recovery')
            tx.event('run.recovered', {k: v for k, v in receipt.items() if k != 'run'}, run_id=run_id)
            return receipt
        return await self.store.command('run.recover', key, command, apply)

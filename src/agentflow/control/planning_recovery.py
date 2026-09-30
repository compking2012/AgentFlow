"""Recover controller-validated planning output, never runtime authority failures."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from agentflow.common import DomainError, canonical_digest
from agentflow.domain.planning import CODING_STEPS, ROLES
from agentflow.runtime.failures import (
    _private_attempt_directory,
    _private_evidence,
    read_role_failure_diagnostic,
    valid_planning_failure_details,
)

PLANNING_STEPS = {'goal', 'development_plan'}
LEGACY_WRITE_MESSAGE = 'Non-coding stages cannot acquire source write permission'


def structured_planning_diagnostic(work, attempt):
    value = work.get('failure_diagnostic') or attempt.get('failure_diagnostic')
    if (work.get('step') not in PLANNING_STEPS or work.get('role') != ROLES.get(work.get('step'))
            or not isinstance(value, dict)):
        return None
    if value.get('code') != 'planning_validation_failed' or not valid_planning_failure_details(value.get('details')):
        return None
    return value


def read_completed_plan(data_dir, attempt_id, schema):
    """Fixed private files only, with the accepted worker result as corroboration."""
    with _private_attempt_directory(data_dir, 'attempt_artifacts', canonical_digest(attempt_id).split(':')[1]) as directory:
        final = json.loads(_private_evidence(directory, 'openhands_final.json', 8 * 1024 * 1024))
        receipt = json.loads(_private_evidence(directory, 'role_result.json', 8 * 1024 * 1024))
    if (not isinstance(final, dict) or not isinstance(receipt, dict)
            or receipt.get('execution_status') != 'completed' or receipt.get('result') != final):
        raise ValueError('planning_final_receipt_mismatch')
    try:
        Draft202012Validator(schema).validate(final)
    except ValidationError as error:
        raise ValueError('planning_final_schema_invalid') from error
    return final


async def planning_failure_diagnostic(store, settings, work, attempt):
    if work.get('step') not in PLANNING_STEPS or work.get('role') != ROLES.get(work.get('step')):
        return None
    structured = structured_planning_diagnostic(work, attempt)
    if structured:
        return structured
    if work.get('step') in PLANNING_STEPS and attempt.get('id'):
        worker_diagnostic = await asyncio.to_thread(read_role_failure_diagnostic, settings.data_dir, attempt['id'])
        if worker_diagnostic:
            return worker_diagnostic
    # This historical error belongs to graph expansion; the similarly named
    # runtime write_scope_violation must never enter this migration path.
    original = work.get('failure_diagnostic') or attempt.get('failure_diagnostic') or {}
    if (work.get('step') not in PLANNING_STEPS
            or work.get('blocking_reason') != LEGACY_WRITE_MESSAGE
            or original and (original.get('code') != 'role_write_scope' or original.get('message') != LEGACY_WRITE_MESSAGE)):
        return None
    context, supervisor = await asyncio.gather(store.read('dispatch_context', attempt.get('id')),
                                               store.read('supervised_attempt', attempt.get('id')))
    if not context or not supervisor or supervisor.get('state') != 'completed':
        return None
    task = context.get('task', {})
    if (task.get('attempt_id') != attempt.get('id')
            or task.get('step') != work.get('step') or task.get('role') != work.get('role')
            or any(task.get(field) != attempt.get(field)
            for field in ('run_id', 'iteration_id', 'work_item_id', 'fencing_token', 'input_fingerprint'))):
        return None
    try:
        final = await asyncio.to_thread(read_completed_plan, settings.data_dir, attempt['id'], task['output_schema'])
    except (OSError, ValueError, TypeError, KeyError, DomainError):
        return None
    stages = {item['key']: item for item in await store.list('work_item')
              if item.get('run_id') == work['run_id'] and not item.get('archived')}
    issues = []
    for group_index, group in enumerate(final.get('parallel_work', [])):
        if not isinstance(group, dict):
            continue
        stage = stages.get(group.get('stage_key'))
        if not stage or stage.get('step') in CODING_STEPS:
            continue
        for child_index, child in enumerate(group.get('children', [])):
            if isinstance(child, dict) and child.get('write_paths'):
                issues.append({'code': 'role_write_scope', 'stage_key': stage['key'],
                    'child_key': child.get('key', ''),
                    'path': f'/parallel_work/{group_index}/children/{child_index}/write_paths',
                    'message': LEGACY_WRITE_MESSAGE})
    if not issues:
        return None
    return {'code': 'planning_validation_failed', 'message': LEGACY_WRITE_MESSAGE, 'details': {
        'origin': 'planning_validation', 'phase': 'plan_validation', 'category': 'correctable_output',
        'issues': issues, 'legacy_code': 'role_write_scope'}}


def export_planning_final(data_dir, task, attempt):
    """Describe an immutable, completed result as a draft source; do not rewrite it."""
    from agentflow.adapters.openhands.output_builder import result_identity
    result = read_completed_plan(data_dir, attempt['id'], task['output_schema'])
    identity = result_identity(task)
    digest = canonical_digest(result)
    folder = Path(data_dir) / 'attempt_artifacts' / canonical_digest(attempt['id']).split(':')[1]
    return {'checkpoint_kind': 'planning_final', 'digest': digest, 'path': str(folder / 'openhands_final.json'),
        'source_directory': str(folder), 'source_identity': identity, 'schema_digest': identity['schema_digest'],
        'progress_digest': digest, 'draft_count': 1, 'committed_chunks': 1,
        'result_bytes': len(json.dumps(result, ensure_ascii=False).encode()),
        'source_attempt_id': attempt['id'], 'source_generation': attempt['generation']}


def restore_planning_final(settings, point, source_task, target_task, artifact_root):
    from agentflow.adapters.openhands.output_builder import import_planning_final
    result = read_completed_plan(settings.data_dir, point['source_attempt_id'], source_task['output_schema'])
    if canonical_digest(result) != point['digest']:
        raise ValueError('planning_final_digest_changed')
    try:
        return import_planning_final(target_task, artifact_root=artifact_root, result=result,
            source_identity=point['source_identity'], source_digest=point['digest'])
    except ValidationError as error:
        raise ValueError('planning_final_target_schema_invalid') from error


def planning_state_diagnostic(work, attempt):
    """Only the explicit controller planning-fence error allows a fresh snapshot."""
    value = work.get('failure_diagnostic') or attempt.get('failure_diagnostic')
    if (work.get('step') in PLANNING_STEPS and work.get('role') == ROLES.get(work.get('step'))
            and isinstance(value, dict) and value.get('code') == 'stale_planning_contract'):
        return value
    return None

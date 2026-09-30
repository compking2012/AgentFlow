"""Controller-owned bindings for auxiliary review repairs, never model authority."""
from __future__ import annotations

import math
from uuid import NAMESPACE_URL, uuid5

from agentflow.common import DomainError, canonical_digest

# Read-only projections resolving a controlled review producer must include
# every fact consumed by bound_producer / repair_batch / verify_frozen_context.
REVIEW_BINDING_KINDS = (
    'work_item', 'attempt', 'code_snapshot', 'plan', 'artifact', 'approval',
    'review_contract_repair', 'review_contract_validation', 'review_diagnostic',
    'coding_step_checkpoint', 'node_job', 'run', 'run_recovery', 'dispatch_context',
)


INTERNAL_REVIEW_STEPS = frozenset({
    'review_disposition', 'review_validation', 'review_unit_migration', 'review_integration_migration',
})


def _optional_record(tx, kind, identity):
    """Missing graph edges are unresolved facts, not invalid Store lookups."""
    return tx.get(kind, identity) if isinstance(identity, str) and identity else None


def _recovery_checkpoint_bound(tx, work, snapshot):
    """Record proof for a historical recovery point; async source validation
    separately verifies Git/tree integrity before the worker uses its files.
    RunRecovery deliberately marks these snapshots stale during invalidation.
    """
    from agentflow.control.recovery import STOPPED

    if not snapshot:
        return False
    payload = work.get('payload', {})
    run = _optional_record(tx, 'run', work.get('run_id'))
    receipt = _optional_record(tx, 'run_recovery', snapshot.get('recovery_id'))
    attempt = _optional_record(tx, 'attempt', snapshot.get('source_attempt_id'))
    context = _optional_record(tx, 'dispatch_context', snapshot.get('source_attempt_id'))
    task = (context or {}).get('task', {})
    if (not run or snapshot.get('purpose') != 'recovery_checkpoint'
            or snapshot.get('id') != payload.get('recovery_checkpoint_id')
            or snapshot.get('id') != payload.get('repair_base_snapshot_id')
            or snapshot.get('run_id') != run['id'] or snapshot.get('work_item_id') != work['id']
            or snapshot.get('generation') != work['generation'] - 1
            or not receipt or receipt.get('actor') not in {'owner', 'system'}
            or receipt.get('mode') not in {'retry', 'continue'} or receipt.get('execution') != 'fresh_attempt'
            or receipt.get('run_id') != run['id'] or receipt.get('iteration_id') != run.get('iteration_id')
            or work['id'] not in receipt.get('affected_work_item_ids', [])
            or not attempt or attempt.get('status') not in STOPPED or attempt.get('run_id') != run['id']
            or attempt.get('work_item_id') != work['id']
            or attempt.get('generation') != snapshot.get('source_generation', snapshot['generation'])
            or attempt.get('fencing_token') != snapshot.get('source_fencing_token')
            or attempt.get('input_fingerprint') != snapshot.get('source_input_fingerprint')
            or not snapshot.get('source_commit') or task.get('source_commit') != snapshot['source_commit']
            or task.get('allowed_write_paths') != snapshot.get('source_write_paths')
            or snapshot.get('source_write_paths') != work.get('write_paths')
            or task.get('work_item_id') != work['id'] or task.get('run_id') != run['id']
            or task.get('attempt_id') != attempt['id']
            or any(task.get(k) != attempt.get(k) for k in ('fencing_token', 'input_fingerprint'))):
        return False
    checkpoint = receipt.get('checkpoint') or {}
    points = checkpoint.get('items', [checkpoint])
    if not any(point.get('snapshot_id') == snapshot['id'] and point.get('work_item_id') == work['id']
               and point.get('commit_oid') == snapshot.get('commit_oid') for point in points):
        return False
    repair_id = snapshot.get('source_repair_snapshot_id')
    if repair_id:
        prior = _optional_record(tx, 'code_snapshot', repair_id)
        if (not prior or prior.get('run_id') != run['id'] or prior.get('work_item_id') != work['id']
                or prior.get('generation') != snapshot.get('source_generation', snapshot['generation']) - 1
                or prior.get('commit_oid') != snapshot['source_commit']):
            return False
    return True


def verify_frozen_context(tx, batch):
    """Recheck authorization facts, excluding reviews deliberately invalidated by repair.

    Original owner generation/status may change when normal downstream work is
    invalidated. Its plan, role, write scope and approval policy may not change.
    """
    context = batch.get('context') or {}
    source = context.get('snapshot')
    if (not source or context.get('source_snapshot_id') != source.get('id')
            or tx.get('code_snapshot', source['id']) != source or source.get('stale')
            or source.get('run_id') != batch['run_id']):
        raise DomainError('review_contract_stale', '冻结审查源码已变化。')
    if batch.get('context_digest') and canonical_digest(context) != batch['context_digest']:
        raise DomainError('review_contract_binding_invalid', '冻结上下文摘要不匹配。')
    diagnostic = context.get('diagnostic')
    if diagnostic and tx.get('review_diagnostic', diagnostic['id']) != diagnostic:
        raise DomainError('review_contract_stale', '失败诊断证据已变化。')
    version = context.get('plan_version')
    if version:
        plan = tx.get('plan', version['id'])
        if (not plan or plan.get('revision') != version['revision'] or plan.get('state') != 'started'
                or plan.get('started_run_id') != batch['run_id']):
            raise DomainError('review_contract_stale', '已接受计划已变化。')
    for document in context.get('accepted_documents', []):
        artifact = tx.get('artifact', document['artifact_id'])
        if (not artifact or artifact.get('stale') or artifact.get('digest') != document['digest']
                or artifact.get('revision') != document['revision']):
            raise DomainError('review_contract_stale', '已接受需求版本已变化。')
    for binding in context.get('acceptance_bindings', []):
        frozen = binding['author']
        author = tx.get('work_item', frozen['id'])
        approvals = [row for row in tx.list('approval') if row.get('work_item_id') == frozen['id']]
        if (not author or any(author.get(k) != v for k, v in frozen.items())
                or sorted(approvals, key=lambda row: row['id']) != sorted(binding['approvals'], key=lambda row: row['id'])):
            raise DomainError('review_contract_stale', '需求验收或审批已被撤销。')
    authorization = ('run_id', 'project_id', 'step', 'role', 'write_paths', 'approval_required')
    for frozen in context.get('owners', []):
        owner = tx.get('work_item', frozen['work_item_id'])
        version = frozen.get('work_version', {})
        if (not owner or owner.get('run_id') != batch['run_id'] or owner.get('archived')
                or owner.get('write_paths') != frozen['write_paths']
                or any(owner.get(k) != version[k] for k in authorization if k in version)):
            raise DomainError('review_contract_stale', '原作者授权范围或审批要求已变化。')
        for spec in batch.get('work_specs', {}).values():
            if (spec.get('payload', {}).get('review_contract_owner') == owner['id']
                    and bool(spec.get('approval_required')) != bool(owner.get('approval_required'))):
                raise DomainError('review_contract_stale', '返工任务不能绕过原作者审批。')


def repair_batch(tx, work, *, verify_context=True):
    identity = work.get('payload', {}).get('review_contract_task')
    if not identity:
        if (work.get('step') in INTERNAL_REVIEW_STEPS
                or any(k.startswith('review_contract_') and k != 'review_contract_binding'
                       for k in work.get('payload', {}))
                or any(work.get('id') in row.get('work_specs', {}) for row in tx.list('review_contract_repair'))):
            raise DomainError('review_contract_binding_invalid', '内部审查任务缺少控制器身份。')
        return None
    batch = tx.get('review_contract_repair', identity)
    spec = (batch or {}).get('work_specs', {}).get(work['id'])
    if (not batch or batch.get('actor') != 'controller' or batch.get('run_id') != work.get('run_id')
            or not spec or any(work.get(k) != spec.get(k) for k in
                              ('key', 'kind', 'step', 'role', 'write_paths', 'run_id', 'project_id',
                               'required', 'approval_required', 'dependencies'))
            or any(work.get('payload', {}).get(k) != spec.get('payload', {}).get(k) for k in
                   ('review_contract_task', 'review_contract_kind', 'review_contract_owner', 'review_contract_actions'))):
        raise DomainError('review_contract_binding_invalid', '审查返工任务的身份或授权范围不匹配。')
    original_base = spec.get('payload', {}).get('repair_base_snapshot_id')
    current_base = work.get('payload', {}).get('repair_base_snapshot_id')
    if original_base:
        base = tx.get('code_snapshot', original_base)
        source = batch['context']['snapshot']
        if (not base or base.get('review_contract_task') != batch['id'] or base.get('work_item_id') != work['id']
                or base.get('source_snapshot_id') != source['id'] or base.get('commit_oid') != source['commit_oid']
                or base.get('tree_oid') != source['tree_oid']):
            raise DomainError('review_contract_binding_invalid', '返工的原始源码绑定无效。')
        if current_base != original_base:
            snapshot = _optional_record(tx, 'code_snapshot', current_base)
            point = _optional_record(tx, 'coding_step_checkpoint', work.get('payload', {}).get('coding_step_checkpoint_id'))
            recovered = (work.get('payload', {}).get('recovery_checkpoint_id') == current_base
                         and _recovery_checkpoint_bound(tx, work, snapshot))
            if (not snapshot or snapshot.get('work_item_id') != work['id'] or snapshot.get('run_id') != work['run_id']
                    or (snapshot.get('stale') and not recovered) or snapshot.get('base_oid') != source['commit_oid']
                    or not (recovered or point and point.get('work_item_id') == work['id']
                            and point.get('run_id') == work['run_id'] and point.get('commit_oid') == snapshot.get('commit_oid')
                            and point.get('snapshot_id') == current_base and point.get('next_generation') == work['generation'])):
                raise DomainError('review_contract_binding_invalid', '返工不能替换未经核验的源码检查点。')
    elif current_base:
        raise DomainError('review_contract_binding_invalid', '该辅助任务不能另行选择源码。')
    if verify_context:
        verify_frozen_context(tx, batch)
    return batch


def delegated_owner_budget(tx, work, budget):
    """Resolve a delegated allowance back to the frozen original author."""
    pool_id = budget.get('review_contract_owner_budget')
    if not pool_id:
        if work.get('payload', {}).get('review_contract_owner'):
            raise DomainError('review_contract_binding_invalid', '返工额度缺少原作者预算绑定。')
        return None
    from agentflow.control.coding_steps import CodingSteps
    batch = repair_batch(tx, work)
    owner = tx.get('work_item', work.get('payload', {}).get('review_contract_owner', 'missing'))
    pool = tx.get('coding_work_budget', pool_id)
    if (not batch or not owner or not pool or owner['id'] == work['id']
            or budget.get('review_contract_batch') != batch['id']
            or budget.get('run_id') != work['run_id'] or budget.get('work_item_id') != work['id']
            or pool_id != CodingSteps.budget_id(work['run_id'], owner['id'])
            or pool.get('run_id') != work['run_id'] or pool.get('work_item_id') != owner['id']
            or pool.get('review_contract_owner_budget')):
        raise DomainError('review_contract_binding_invalid', '返工执行额度与原作者预算不匹配。')
    return pool


def owner_budget_allowance(tx, work, budget, *, released_controls=()):
    """Outstanding coding controls reserve, but never debit, shared owner limits."""
    pool = delegated_owner_budget(tx, work, budget)
    children = [row for row in tx.list('coding_work_budget')
                if row.get('review_contract_owner_budget') == (pool or budget)['id']]
    if pool is None and not children:
        return None
    pool = pool or budget
    if (pool.get('uncertain') is not False
            or any(type(pool.get(field)) is not int or pool[field] < 0
                   for field in ('max_steps', 'step_count', 'max_tool_calls', 'observed_tool_calls'))
            or any(type(pool.get(field)) not in {int, float} or not math.isfinite(pool[field]) or pool[field] < 0
                   for field in ('max_active_seconds', 'active_seconds'))):
        raise DomainError('coding_budget_uncertain', '原作者累计额度或用量尚未核验。')
    budget_ids = {pool['id'], *(row['id'] for row in children)}
    remaining = {'max_active_seconds': pool['max_active_seconds'] - pool['active_seconds'],
                 'max_tool_calls': pool['max_tool_calls'] - pool['observed_tool_calls'],
                 'max_steps': pool['max_steps'] - pool['step_count']}
    for control in tx.list('coding_step_control'):
        if control.get('budget_id') not in budget_ids or control['id'] in released_controls:
            continue
        usage = tx.get('coding_step_usage', control['id'])
        if usage:
            if usage.get('known') is not True:
                raise DomainError('coding_budget_uncertain', '同一原作者仍有未核验的执行用量。')
            continue
        attempt = tx.get('attempt', control['id'])
        if (not attempt or attempt.get('status') not in {'running', 'cancel_requested'}
                or attempt.get('run_id') != work['run_id']
                or attempt.get('work_item_id') != control.get('work_item_id')):
            raise DomainError('coding_budget_uncertain', '同一原作者仍有已停止但用量或未启动证明缺失的执行。')
        if (control.get('run_id') != work['run_id']
                or type(control.get('max_active_seconds')) not in {int, float}
                or not math.isfinite(control['max_active_seconds']) or control['max_active_seconds'] <= 0
                or type(control.get('max_tool_calls')) is not int or control['max_tool_calls'] <= 0):
            raise DomainError('coding_budget_uncertain', '原作者尚未结算的执行预留无法核验。')
        remaining['max_active_seconds'] -= control['max_active_seconds']
        remaining['max_tool_calls'] -= control['max_tool_calls']
        remaining['max_steps'] -= 1
    if any(value <= 0 for value in remaining.values()):
        raise DomainError('coding_budget_exhausted', '原作者累计额度已用尽或已由尚未结算的执行预留。')
    return remaining


def charge_owner_budget(tx, task, budget, *, known, seconds, calls):
    """Charge one step once; reconcile a verified late receipt by adding only usage.

    Authorization revocation stops new work, not accounting for already-run work.
    Unexplained uncertainty stays sticky; direct/other delegated unknown receipts
    are recomputed independently so reconciling one receipt cannot clear them.
    """
    pool_id = budget.get('review_contract_owner_budget')
    if not pool_id:
        return
    if type(known) is not bool or (known and (type(seconds) not in {int, float}
            or not math.isfinite(seconds) or seconds < 0 or type(calls) is not int or calls < 0)):
        raise DomainError('review_contract_usage_invalid', '返工用量必须是已核验的非负有限数。')
    work = tx.get('work_item', task['work_item_id'])
    batch = repair_batch(tx, work, verify_context=False)
    pool = tx.get('coding_work_budget', pool_id)
    owner = tx.get('work_item', work['payload']['review_contract_owner'])
    if (not batch or budget.get('review_contract_batch') != batch['id'] or not pool or not owner
            or pool.get('run_id') != task['run_id'] or pool.get('work_item_id') != owner['id']
            or budget.get('run_id') != task['run_id'] or budget.get('work_item_id') != work['id']):
        raise DomainError('review_contract_binding_invalid', '返工计量与原作者额度不匹配。')
    previous = tx.get('review_contract_budget_charge', task['attempt_id'])
    if previous:
        if any(previous.get(k) != v for k, v in {'run_id': task['run_id'], 'batch_id': batch['id'],
                'work_item_id': work['id'], 'owner_work_item_id': owner['id'], 'owner_budget_id': pool_id}.items()):
            raise DomainError('review_contract_binding_invalid', '用量回执属于其他返工任务。')
        if previous['known']:
            if known and (previous['active_seconds'] != seconds or previous['observed_tool_calls'] != calls):
                raise DomainError('review_contract_usage_conflict', '已核验用量不能被另一回执改写。')
            return
        if not known:
            return
    charges = [row for row in tx.list('review_contract_budget_charge') if row.get('owner_budget_id') == pool_id]
    usages = [row for row in tx.list('coding_step_usage') if row.get('budget_id') == pool_id]
    # Missing receipts remain reservations until preparation rechecks either
    # settlement or positive no-launch proof. Do not make a proven unused
    # reservation into sticky uncertainty merely because its attempt stopped.
    direct_unknown = any(row.get('known') is not True for row in usages)
    prior_unknown = any(row.get('known') is not True for row in charges)
    external_unknown = pool.get('review_contract_external_uncertain', False)
    if pool.get('uncertain') and not direct_unknown and not prior_unknown:
        external_unknown = True
    remaining = direct_unknown or external_unknown or not known or any(
        row['id'] != task['attempt_id'] and row.get('known') is not True for row in charges)
    tx.put('coding_work_budget', pool_id, {**pool, 'step_count': pool['step_count'] + (0 if previous else 1),
        'active_seconds': pool['active_seconds'] + (seconds if known else 0),
        'observed_tool_calls': pool['observed_tool_calls'] + (calls if known else 0),
        'uncertain': bool(remaining), 'review_contract_external_uncertain': bool(external_unknown)}, pool['revision'])
    receipt = {'run_id': task['run_id'], 'batch_id': batch['id'], 'work_item_id': work['id'],
        'owner_work_item_id': owner['id'], 'owner_budget_id': pool_id, 'known': known,
        'active_seconds': seconds if known else None, 'observed_tool_calls': calls if known else None}
    tx.put('review_contract_budget_charge', task['attempt_id'], receipt, previous['revision'] if previous else None)


def delegated_usage_totals(charges, run_id, owner_id):
    """Aggregate owner charges without inventing owner coding-step controls."""
    totals, seen = {}, set()
    for row in charges:
        if (row.get('run_id') != run_id or row.get('owner_work_item_id') != owner_id
                or row.get('work_item_id') == owner_id or type(row.get('known')) is not bool
                or any(not isinstance(row.get(k), str) or not row[k] for k in
                       ('id', 'owner_budget_id', 'work_item_id', 'batch_id')) or row['id'] in seen):
            raise ValueError('Invalid delegated coding usage identity')
        seen.add(row['id'])
        known, seconds, calls = row['known'], row.get('active_seconds'), row.get('observed_tool_calls')
        if ((known and (type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds < 0
                        or type(calls) is not int or calls < 0))
                or (not known and (seconds is not None or calls is not None))):
            raise ValueError('Invalid delegated coding usage measurement')
        total = totals.setdefault(row['owner_budget_id'], {'count': 0, 'seconds': 0.0, 'calls': 0, 'unknown': False})
        total['count'] += 1
        total['seconds'] += seconds if known else 0
        total['calls'] += calls if known else 0
        total['unknown'] |= not known
    return totals


def bound_producer(tx, run, reviewer):
    identity = reviewer.get('payload', {}).get('review_contract_binding')
    if not identity:
        return None
    batch = tx.get('review_contract_repair', identity)
    binding = (batch or {}).get('review_binding', {})
    stage = _optional_record(tx, 'work_item', batch.get('stage_id')) if batch else None
    if (not batch or batch.get('actor') != 'controller' or batch.get('run_id') != run['id']
            or not stage or reviewer['id'] not in binding.get('review_ids', [])
            or stage['id'] != binding.get('stage_id')
            or stage.get('payload', {}).get('review_contract_binding') != identity
            or reviewer.get('generation', 0) < binding.get('minimum_generations', {}).get(reviewer['id'], 10**12)):
        return None
    validation = _optional_record(tx, 'work_item', batch.get('validation_work_item_id'))
    assembly = _optional_record(tx, 'work_item', batch.get('assembly_work_item_id'))
    snapshot = _optional_record(tx, 'code_snapshot', (assembly or {}).get('attempt_id'))
    checked = _optional_record(tx, 'review_contract_validation', (validation or {}).get('attempt_id'))
    diagnostic = _optional_record(tx, 'review_diagnostic', (checked or {}).get('diagnostic_id'))
    if (not validation or validation.get('status') != 'completed' or not checked or checked.get('state') != 'passed'
            or checked.get('batch_id') != identity or checked.get('source_snapshot_id') != (snapshot or {}).get('id')
            or checked.get('validation_generation') != validation.get('generation')
            or not assembly or assembly.get('status') != 'completed' or not snapshot or snapshot.get('stale')
            or snapshot.get('work_item_id') != assembly['id'] or snapshot.get('generation') != assembly['generation']
            or snapshot.get('run_id') != run['id'] or checked.get('source_commit') != snapshot['commit_oid']
            or not diagnostic or diagnostic.get('state') != 'passed'
            or diagnostic.get('source_manifest', {}).get('source_commit') != snapshot['commit_oid']):
        return None
    validation_attempt = _optional_record(tx, 'attempt', validation.get('attempt_id'))
    assembly_attempt = _optional_record(tx, 'attempt', assembly.get('attempt_id'))
    for item, attempt in ((validation, validation_attempt), (assembly, assembly_attempt)):
        # Assembly completion freezes code, not review quality. The real scheduler
        # correctly reports unknown until these independent reviewers inspect it.
        allowed_quality = {'passed'} if item['id'] == validation['id'] else {'unknown', 'passed'}
        if (not attempt or attempt.get('status') != 'completed' or item.get('quality_result') not in allowed_quality
                or attempt.get('quality_result') != item.get('quality_result')
                or attempt.get('run_id') != run['id'] or attempt.get('work_item_id') != item['id']
                or any(attempt.get(k) != item.get(k) for k in ('generation', 'fencing_token', 'input_fingerprint'))):
            return None
    expected_batch = str(uuid5(NAMESPACE_URL, canonical_digest((identity, validation_attempt['id'], 'diagnostic'))))
    frozen = diagnostic.get('source') or {}
    if (checked.get('run_id') != run['id'] or checked.get('work_item_id') != validation['id']
            or checked.get('attempt_id') != validation_attempt['id'] or checked.get('source_tree_oid') != snapshot['tree_oid']
            or diagnostic.get('run_id') != run['id'] or diagnostic.get('batch_id') != expected_batch
            or diagnostic.get('blockers') or diagnostic.get('source_manifest', {}).get('source_tree_oid') != snapshot['tree_oid']
            or any(frozen.get(k) != snapshot.get(k) for k in ('id', 'run_id', 'work_item_id', 'generation', 'commit_oid', 'tree_oid'))):
        return None
    job_ids = diagnostic.get('build_job_ids', []) + diagnostic.get('test_job_ids', [])
    if (not diagnostic.get('build_job_ids') or not diagnostic.get('test_job_ids')
            or set(job_ids) != set(diagnostic.get('jobs', [])) or len(job_ids) != len(set(job_ids))):
        return None
    for job_id in job_ids:
        job = tx.get('node_job', job_id)
        if (not job or job.get('run_id') != run['id'] or job.get('parent_work_item_id') != validation['id']
                or job.get('parent_generation') != validation['generation']
                or job.get('parent_input_fingerprint') != validation_attempt['input_fingerprint']
                or job.get('state') != 'completed' or job.get('quality_result') != 'passed'
                or job.get('source_manifest') != diagnostic['source_manifest']):
            return None
    try:
        repair_batch(tx, assembly)
        repair_batch(tx, validation)
    except DomainError:
        return None
    if reviewer['id'] == stage['id'] and stage.get('kind') == 'aggregation':
        if stage.get('original_dependencies') != [validation['id']]:
            return None
    elif reviewer.get('dependencies') != [validation['id']]:
        return None
    return assembly


def valid_group_binding(tx, run, stage, expansion, identities, original):
    producer = bound_producer(tx, run, stage)
    batch = _optional_record(tx, 'review_contract_repair', stage.get('payload', {}).get('review_contract_binding'))
    binding = (batch or {}).get('review_binding', {})
    return bool(producer and binding.get('expansion_id') == expansion['id']
        and binding.get('expansion_fingerprint') == expansion['input_fingerprint']
        and set(identities) == set(binding.get('child_ids', []))
        and original == [batch['validation_work_item_id']]
        and all((_optional_record(tx, 'work_item', identity) or {}).get('payload', {}).get('review_contract_binding') == batch['id']
                for identity in identities))

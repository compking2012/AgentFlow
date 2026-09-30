"""A narrow owner acknowledgment is not a model settlement or free request."""
from agentflow.common import canonical_digest
from agentflow.models.budget import account_id
from agentflow.runtime.process_identity import same_launcher_identity

TIMEOUT_KIND = 'timeout_recovery'
ACK_KIND = 'model_uncertainty_acknowledgment'
ACK_KINDS = ('run', 'iteration', 'work_item', 'attempt', 'supervised_attempt', 'model_invocation',
             'model_attempt_budget', 'budget_account', ACK_KIND, TIMEOUT_KIND)
TERMINAL_INVOCATIONS = {'settled', 'released', 'completed_unpriced'}


def acknowledgment_state(tx):
    return {kind: tx.list(kind) for kind in ACK_KINDS}


def _record(state, kind, identity):
    return next((row for row in state.get(kind, []) if row['id'] == identity), None)


def request_is_counted(state, invocation):
    calls = state.get('model_invocation', [])
    attempt = _record(state, 'model_attempt_budget', invocation.get('attempt_id'))
    related = [row for row in calls if row.get('attempt_id') == invocation.get('attempt_id')]
    if (not attempt or type(attempt.get('request_count')) is not int
            or attempt['request_count'] != len(related) or not related
            or type(attempt.get('uncertain_invocations')) is not int
            or attempt['uncertain_invocations'] != sum(row.get('state') == 'uncertain' for row in related)
            or attempt.get('restore_uncertain')):
        return False
    for kind, field in [('run', 'run_id'), ('iteration', 'iteration_id')]:
        account = _record(state, 'budget_account', account_id(kind, invocation.get(field)))
        count = sum(row.get(field) == invocation.get(field) for row in calls)
        if (not account or account.get('owner_kind') != kind or account.get('owner_id') != invocation.get(field)
                or type(account.get('request_count')) is not int or account['request_count'] != count
                or account.get('restore_uncertain')):
            return False
    return True


def acknowledgment_basis(state, invocation):
    """Immutable identity plus unchanged provider uncertainty and stop evidence."""
    run = _record(state, 'run', invocation.get('run_id'))
    iteration = _record(state, 'iteration', invocation.get('iteration_id'))
    attempt = _record(state, 'attempt', invocation.get('attempt_id'))
    work = _record(state, 'work_item', (attempt or {}).get('work_item_id'))
    process = _record(state, 'supervised_attempt', invocation.get('attempt_id'))
    if (not run or not iteration or not attempt or not work or not process
            or invocation.get('state') != 'uncertain' or invocation.get('cost_mode') != 'request_limited'
            or run.get('budget_limit', {}).get('cost_mode') != 'request_limited'
            or iteration.get('budget_limit', {}).get('cost_mode') != 'request_limited'
            or type(invocation.get('amount_micros')) is not int or invocation['amount_micros'] != 0
            or invocation.get('actual_micros') is not None
            or attempt.get('status') not in {'failed', 'completed', 'blocked', 'cancelled'}
            or process.get('state') not in {'failed', 'completed', 'cancelled'}
            or attempt.get('run_id') != run['id'] or work.get('run_id') != run['id']
            or attempt.get('iteration_id') != invocation.get('iteration_id')
            or invocation.get('iteration_id') != run.get('iteration_id')
            or any(invocation.get(key) != attempt.get(key) for key in ('fencing_token', 'input_fingerprint'))
            or process.get('attempt_id') != attempt['id']
            or any(process.get(key) != attempt.get(key) for key in ('fencing_token', 'input_fingerprint'))
            or any(row.get(flag) for row in (run, work, attempt, process, invocation)
                   for flag in ('restore_uncertain', 'restore_reconciliation_required', 'restore_revalidation_required'))
            or not request_is_counted(state, invocation)):
        return None
    return {'run_id': run['id'], 'iteration_id': run['iteration_id'], 'work_item_id': work['id'],
        'attempt_id': attempt['id'], 'attempt_generation': attempt['generation'],
        'invocation_id': invocation['id'], 'invocation_revision': invocation['revision'],
        'invocation_digest': canonical_digest(invocation), 'process_evidence_digest': canonical_digest(process),
        'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint']}


def timeout_authorization_digest(record):
    """Bind the full durable policy decision, excluding storage metadata."""
    return canonical_digest({key: value for key, value in record.items()
                             if key not in {'id', 'revision', 'authorization_digest'}})


def _timeout_acknowledged(state, acknowledgment, invocation, basis):
    record = _record(state, TIMEOUT_KIND, acknowledgment.get('timeout_recovery_id'))
    process = _record(state, 'supervised_attempt', invocation.get('attempt_id'))
    if not record or not process:
        return False
    try:
        policy, counts = record['policy'], record['retry_counts_before']
        identity, receipt = record['original_identity'], record['stop_receipt']
        identity_fields = ('run_id', 'iteration_id', 'work_item_id', 'attempt_id',
                           'attempt_generation', 'fencing_token', 'input_fingerprint')
        if (acknowledgment.get('actor') != 'system'
                or acknowledgment.get('authorization_kind') != 'timeout_retry_policy'
                or acknowledgment.get('requires_explicit_retry') is not False
                or record.get('actor') != 'system' or record.get('version') != 1
                or record.get('authorization_kind') != 'timeout_retry_policy' or record.get('status') != 'authorized'
                or record.get('authorization_digest') != timeout_authorization_digest(record)
                or acknowledgment.get('authorization_digest') != record['authorization_digest']
                or identity != {field: basis[field] for field in identity_fields}
                or any(record.get(field) != basis[field] for field in
                       ('run_id', 'iteration_id', 'work_item_id', 'attempt_id'))
                or record.get('work_generation') != basis['attempt_generation']
                or acknowledgment.get('work_generation') != basis['attempt_generation']
                or any(acknowledgment.get(field) != basis[field] for field in
                       ('run_id', 'iteration_id', 'work_item_id', 'attempt_id'))
                or record.get('process_evidence_digest') != basis['process_evidence_digest']
                or record.get('stop_receipt_digest') != canonical_digest(receipt)
                or process.get('state') != 'failed' or process.get('reason') != 'timeout'
                or receipt.get('execution_status') != 'failed' or receipt.get('reason') != 'timeout'
                or not same_launcher_identity(receipt, process)
                or any(type(policy.get(field)) is not int or policy[field] <= 0
                       for field in ('timeout_limit', 'per_work_limit', 'run_limit'))
                or any(type(counts.get(field)) is not int or counts[field] < 0
                       for field in ('work', 'run', 'timeouts_work'))
                or counts['work'] >= policy['per_work_limit'] or counts['run'] >= policy['run_limit']
                or counts['timeouts_work'] >= policy['timeout_limit']
                or type(record.get('retry_ordinal')) is not int
                or record['retry_ordinal'] != counts['timeouts_work'] + 1
                or invocation.get('reason') not in {'consumer_disconnected', 'dispatch_result_unknown'}
                or not any(row == {'invocation_id': invocation['id'], 'basis': basis, 'reason': invocation['reason']}
                           for row in record['unknown_invocations'])):
            return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def acknowledged_invocation_ids(state):
    accepted = set()
    for acknowledgment in state.get(ACK_KIND, []):
        invocation = _record(state, 'model_invocation', acknowledgment.get('invocation_id'))
        if (not invocation or acknowledgment.get('accept_unknown_usage') is not True
                or not acknowledgment.get('reason')):
            continue
        basis = acknowledgment_basis(state, invocation)
        if not basis or acknowledgment.get('basis') != basis:
            continue
        owner = acknowledgment.get('actor') == 'owner' and acknowledgment.get('requires_explicit_retry') is True
        if owner or _timeout_acknowledged(state, acknowledgment, invocation, basis):
            accepted.add(invocation['id'])
    return accepted


def invocation_blocks(invocation, acknowledged):
    return invocation.get('state') not in TERMINAL_INVOCATIONS and invocation['id'] not in acknowledged


def attempt_uncertainty_blocks(budget, calls, acknowledged):
    count = budget.get('uncertain_invocations', 0)
    if type(count) is not int or count < 0 or budget.get('restore_uncertain'):
        return True
    if not count:
        return False
    uncertain = [row for row in calls if row.get('attempt_id') == budget['id'] and row.get('state') == 'uncertain']
    return count != len(uncertain) or any(row['id'] not in acknowledged for row in uncertain)

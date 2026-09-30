"""Closed proxy exception evidence; reading diagnostics never authorizes another call."""
from __future__ import annotations

from datetime import datetime

import httpx

from agentflow.common import utc_now
from agentflow.domain.planning import CODING_STEPS, ROLES

_KINDS = (
    (httpx.ConnectError, 'connect_error', 'model_transport_not_sent'),
    (httpx.ConnectTimeout, 'connect_timeout', 'model_transport_connect_timeout'),
    (httpx.ReadTimeout, 'read_timeout', 'model_transport_read_timeout'),
    (httpx.WriteTimeout, 'write_timeout', 'model_transport_write_timeout'),
    (httpx.PoolTimeout, 'pool_timeout', 'model_transport_pool_timeout'),
    (httpx.RemoteProtocolError, 'remote_protocol_error', 'model_transport_protocol_error'),
    (httpx.LocalProtocolError, 'local_protocol_error', 'model_transport_protocol_error'),
    (httpx.ReadError, 'read_error', 'model_transport_connection_error'),
    (httpx.WriteError, 'write_error', 'model_transport_connection_error'),
    (httpx.CloseError, 'close_error', 'model_transport_connection_error'),
)
_CODES = {kind: code for _, kind, code in _KINDS}
_CODES['other_error'] = 'model_request_outcome_unknown'
_FIELDS = {'version', 'origin', 'invocation_id', 'phase', 'kind', 'delivery', 'observed_at'}


def caught_transport_failure(error, invocation_id, *, phase='send', delivery='unknown'):
    """Only the catch site supplies phase/delivery; exception text is never retained."""
    kind = next((kind for cls, kind, _ in _KINDS if isinstance(error, cls)), 'other_error')
    return {'version': 1, 'origin': 'model_proxy', 'invocation_id': invocation_id,
            'phase': phase, 'kind': kind, 'delivery': delivery, 'observed_at': utc_now()}


def _timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamp_without_zone')
    return result


def transport_failure_code(metadata, invocation_id, *, state, reason):
    """Reject unknown fields and contradictory settlement evidence, including booleans."""
    try:
        if (not isinstance(metadata, dict) or set(metadata) != _FIELDS
                or type(metadata['version']) is not int or metadata['version'] != 1
                or metadata['origin'] != 'model_proxy' or metadata['invocation_id'] != invocation_id
                or metadata['phase'] != 'send' or metadata['kind'] not in _CODES):
            return None
        _timestamp(metadata['observed_at'])
        if metadata['kind'] == 'connect_error':
            if (metadata['delivery'], state, reason) != ('not_sent', 'released', 'connection_not_established'):
                return None
        elif (metadata['delivery'], state, reason) != ('unknown', 'uncertain', 'dispatch_result_unknown'):
            return None
        return _CODES[metadata['kind']]
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _last_invocation(rows, count):
    if not rows or type(count) is not int or count < 1:
        return None
    numbered = [row for row in rows if 'request_ordinal' in row]
    if numbered:
        ordinals = [row['request_ordinal'] for row in numbered]
        if (any(type(value) is not int or not 1 <= value <= count for value in ordinals)
                or len(set(ordinals)) != len(ordinals) or max(ordinals) != count):
            return None
        latest = next(row for row in numbered if row['request_ordinal'] == count)
        # Legacy rows predate ordinal capture. They may not supersede the last
        # numbered reservation, and ambiguous timestamps are not ordering proof.
        if any(_timestamp(row['created_at']) >= _timestamp(latest['created_at'])
               for row in rows if 'request_ordinal' not in row):
            return None
        return latest
    # With no ordinals, a missing row might be a later call. Timestamp order
    # proves the latest invocation only when the complete request set is present.
    if len(rows) != count:
        return None
    times = [_timestamp(row['created_at']) for row in rows]
    latest = max(times)
    return rows[times.index(latest)] if times.count(latest) == 1 else None


def _same_identity(row, identity):
    return all(type(row.get(field)) is type(value) and row[field] == value for field, value in identity.items())


async def transport_failure_for_attempt(store, attempt_id, *, fencing_token, input_fingerprint):
    """Refine only the last call under the exact frozen attempt and authorization."""
    try:
        attempt = await store.read('attempt', attempt_id)
        if (not attempt or attempt.get('status') not in {'running', 'failed', 'blocked'}
                or type(fencing_token) is not int or fencing_token < 1
                or type(attempt.get('fencing_token')) is not int
                or attempt.get('fencing_token') != fencing_token or attempt.get('input_fingerprint') != input_fingerprint):
            return None
        dispatch = await store.read('dispatch_context', attempt_id)
        task = (dispatch or {}).get('task', {})
        identity = {'attempt_id': attempt_id, **{field: attempt[field] for field in
                    ('run_id', 'iteration_id', 'fencing_token', 'input_fingerprint')}}
        if (not _same_identity(task, identity) or any(value is None for value in identity.values())
                or not attempt.get('work_item_id') or task.get('work_item_id') != attempt['work_item_id']
                or not task.get('profile_id') or task.get('step') not in ROLES):
            return None
        protocol = 'responses' if task['step'] in CODING_STEPS else 'chat_completions'
        rows = [row for row in await store.list('model_invocation') if row.get('attempt_id') == attempt_id]
        budget = await store.read('model_attempt_budget', attempt_id)
        if not budget or budget.get('attempt_id') != attempt_id:
            return None
        inv = _last_invocation(rows, budget.get('request_count'))
        if (not inv or not _same_identity(inv, identity)
                or inv.get('operation_id') != inv.get('id') or inv.get('profile_id') != task['profile_id']
                or inv.get('protocol') != protocol or type(inv.get('profile_revision')) is not int
                or inv['profile_revision'] < 1):
            return None
        authorities = [row for row in await store.list('task_authorization')
            if _same_identity(row, identity)
            and row.get('model_profile_id') == inv['profile_id']
            and type(row.get('expected_profile_revision')) is int
            and row['expected_profile_revision'] == inv['profile_revision']
            and isinstance(row.get('protocols'), list) and protocol in row['protocols']]
        if len(authorities) != 1:
            return None
        if 'transport_failure' in inv:
            return transport_failure_code(inv['transport_failure'], inv['id'], state=inv['state'], reason=inv.get('reason'))
        # Legacy proxy catches prove an unknown outcome only, never a timeout class.
        if inv.get('state') == 'uncertain' and inv.get('reason') == 'dispatch_result_unknown':
            return 'model_request_outcome_unknown'
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return None

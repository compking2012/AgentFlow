"""Finite startup envelopes and child-runtime deadlines for local task credentials."""
from __future__ import annotations

import asyncio
import math
import os
import time
from datetime import UTC, datetime, timedelta

from agentflow.common import DomainError, canonical_digest
from agentflow.runtime.process_identity import (
    boot_relation,
    current_boot_identity,
    observe_process,
    same_launcher_identity,
)


def _number(value):
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def authorization_window(seconds, *, startup_seconds):
    if not _number(seconds) or seconds <= 0 or seconds > 86400 or not _number(startup_seconds) or not 0 < startup_seconds <= 930:
        raise DomainError('invalid_execution_window', 'Task launch and execution windows must be finite and bounded', 422)
    now, monotonic = datetime.now(UTC), time.monotonic()
    source, fingerprint = current_boot_identity()
    return {'expires_at': (now + timedelta(seconds=startup_seconds + seconds)).isoformat(),
        'execution_clock': {'version': 1, 'window_seconds': seconds, 'issued_monotonic': monotonic,
            'launch_deadline_monotonic': monotonic + startup_seconds,
            'authorization_deadline_monotonic': monotonic + startup_seconds + seconds,
            'boot_identity_source': source, 'boot_fingerprint': fingerprint}}


def _child(data_dir, attempt_id, *, require_active=False):
    import json

    from agentflow.runtime.failures import _private_attempt_directory, _private_evidence
    identity = canonical_digest({'attempt_id': attempt_id}).split(':')[1]
    with _private_attempt_directory(data_dir, 'supervisor', identity) as directory:
        def stopped():
            for name in ('cancel.json', 'result.json'):
                try:
                    os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                return True
            return False
        if require_active and stopped():
            raise ValueError('execution_already_stopping_or_terminal')
        child = json.loads(_private_evidence(directory, 'child.json', 65536))
        if require_active and stopped():
            raise ValueError('execution_stopped_during_clock_read')
        return child


async def execution_context(store, data_dir, raw, context):
    """Resolve only the same boot's sealed child clock; never use launcher birth time."""
    clock = raw.get('execution_clock')
    if clock is None:
        return context
    try:
        duration = clock['window_seconds']
        issued, launch, outer = (clock[key] for key in ('issued_monotonic', 'launch_deadline_monotonic', 'authorization_deadline_monotonic'))
        if (type(clock.get('version')) is not int or clock['version'] != 1 or any(not _number(value) for value in (duration, issued, launch, outer))
                or not 0 < duration <= 86400 or not 0 < launch - issued <= 930
                or not math.isclose(outer - launch, duration, abs_tol=1e-6)
                or boot_relation(clock) != 'same'):
            raise ValueError('invalid_execution_clock')
        process = await store.read('supervised_attempt', context.attempt_id)
        dispatch = await store.read('dispatch_context', context.attempt_id)
        task = (dispatch or {}).get('task', {})
        if (not process or process.get('state') != 'running' or process.get('run_id') != context.run_id
                or any(process.get(field) != getattr(context, field) for field in ('attempt_id', 'fencing_token', 'input_fingerprint'))
                or task.get('attempt_id') != context.attempt_id or task.get('run_id') != context.run_id
                or task.get('fencing_token') != context.fencing_token or task.get('input_fingerprint') != context.input_fingerprint
                or task.get('deadline_seconds') != duration):
            raise ValueError('execution_not_active')
        # Child creation and its private receipt precede model traffic in normal
        # launches. A very fast child may arrive during that short publication gap.
        wait_until = time.monotonic() + min(1.0, max(0.0, outer - time.monotonic()))
        while True:
            try:
                child = await asyncio.to_thread(_child, data_dir, context.attempt_id, require_active=True)
                break
            except FileNotFoundError:
                if time.monotonic() >= wait_until:
                    raise
                await asyncio.sleep(.01)
        start, deadline = child['execution_started_monotonic'], child['execution_deadline_monotonic']
        if (not same_launcher_identity(child, process) or boot_relation(child) != 'same'
                or type(child.get('execution_clock_version')) is not int or child['execution_clock_version'] != 1
                or child.get('execution_window_seconds') != duration
                or not isinstance(child.get('child'), dict) or type(child['child'].get('pid')) is not int
                or not all(_number(value) for value in (start, deadline)) or start < issued
                or not math.isclose(deadline - start, duration, abs_tol=1e-6)):
            raise ValueError('child_execution_clock_mismatch')
        effective = min(deadline, outer) if start <= launch else launch
        effective_wall = child['execution_deadline_at'] if start <= launch else (
            datetime.fromisoformat(raw['expires_at'].replace('Z', '+00:00')) - timedelta(seconds=duration)).isoformat()
        # Recheck stored lifecycle after the filesystem read/wait; terminal work
        # does not gain a new token window merely because its old child file exists.
        if await store.read('supervised_attempt', context.attempt_id) != process:
            raise ValueError('execution_changed_during_clock_read')
        final_child = await asyncio.to_thread(_child, data_dir, context.attempt_id, require_active=True)
        if final_child != child:
            raise ValueError('child_clock_changed')
        live_child = {**child['child'], **{key: child[key] for key in ('boot_identity_source', 'boot_fingerprint')}}
        launcher_observation, child_observation = await asyncio.gather(
            asyncio.to_thread(observe_process, process), asyncio.to_thread(observe_process, live_child))
        if not launcher_observation['verified'] or not child_observation['verified']:
            raise ValueError('execution_process_identity_unverified')
        # Observation may yield; a terminal receipt published meanwhile wins.
        if await asyncio.to_thread(_child, data_dir, context.attempt_id, require_active=True) != child:
            raise ValueError('child_clock_changed')
        return context.model_copy(update={'expires_at': effective_wall,
            'deadline_monotonic': effective, 'deadline_boot_identity': {
                key: clock[key] for key in ('boot_identity_source', 'boot_fingerprint')},
            'deadline_evidence': {'child_digest': canonical_digest(child), 'process_nonce': process['nonce'],
                                  'deadline_kind': 'execution' if start <= launch else 'startup'}})
    except (KeyError, TypeError, ValueError, OSError, AttributeError) as error:
        raise DomainError('stale_task_token', 'Task execution clock is unavailable, changed, or no longer active', 403) from error


async def record_expiry(store, raw, context, attempt, work, protocol):
    try:
        context.assert_current(protocol)
    except DomainError as error:
        if error.code != 'task_authorization_expired':
            raise
    else:
        raise DomainError('invalid_expiry_evidence', 'An active authorization cannot be recorded as expired', 409)
    identity = raw['id']
    record = {'actor': 'controller', 'runtime_failure_code': 'task_authorization_expired',
        'authorization_id': identity, 'authorization_digest': canonical_digest(raw),
        'attempt_id': context.attempt_id, 'work_item_id': work['id'], 'run_id': context.run_id,
        'iteration_id': context.iteration_id, 'generation': attempt['generation'],
        'fencing_token': context.fencing_token, 'input_fingerprint': context.input_fingerprint,
        'expires_at': context.expires_at, 'deadline_monotonic': context.deadline_monotonic,
        'deadline_boot_identity': context.deadline_boot_identity, 'deadline_evidence': context.deadline_evidence,
        'observed_at': datetime.now(UTC).isoformat(), 'observed_monotonic': time.monotonic(), 'protocol': protocol}
    def save(tx):
        current_attempt = tx.get('attempt', context.attempt_id)
        # This records a denial already observed by the controller; the worker
        # may finish normally between that check and this writer transaction.
        # Historical identity is sufficient; this receipt grants no execution.
        if (tx.get('task_authorization', identity) != raw or not current_attempt
                or work['id'] != attempt.get('work_item_id')
                or any(current_attempt.get(field) != attempt.get(field) for field in
                       ('work_item_id', 'run_id', 'iteration_id', 'generation', 'fencing_token', 'input_fingerprint'))):
            raise DomainError('stale_task_token', 'Task identity changed while recording authorization expiry', 403)
        saved = tx.put('task_authorization_expiry', identity, record)
        tx.event('task.authorization_expired', {'attempt_id': context.attempt_id, 'authorization_id': identity}, run_id=context.run_id)
        return saved
    return await store.command('task.authorization_expired', identity, {'authorization_id': identity}, save)


async def authorization_failure(store, data_dir, attempt_id, *, fencing_token, input_fingerprint):
    """Consume controller denial metadata; never infer expiry from HTTP/prose/exit code."""
    try:
        attempt = await store.read('attempt', attempt_id)
        if (not attempt or attempt.get('status') not in {'running', 'failed', 'blocked'}
                or attempt.get('fencing_token') != fencing_token or attempt.get('input_fingerprint') != input_fingerprint):
            return None
        for row in await store.list('task_authorization_expiry'):
            if (row.get('actor') != 'controller' or row.get('runtime_failure_code') != 'task_authorization_expired'
                    or row.get('attempt_id') != attempt_id
                    or any(row.get(field) != attempt.get(field) for field in
                           ('work_item_id', 'run_id', 'iteration_id', 'generation', 'fencing_token', 'input_fingerprint'))):
                continue
            authority = await store.read('task_authorization', row['authorization_id'])
            if not authority or canonical_digest(authority) != row.get('authorization_digest'):
                continue
            if row.get('deadline_monotonic') is not None:
                if (not _number(row.get('observed_monotonic')) or not _number(row['deadline_monotonic'])
                        or row['observed_monotonic'] < row['deadline_monotonic']):
                    continue
                child = await asyncio.to_thread(_child, data_dir, attempt_id)
                clock = authority.get('execution_clock') or {}
                expected = (min(child['execution_deadline_monotonic'], clock['authorization_deadline_monotonic'])
                    if child['execution_started_monotonic'] <= clock['launch_deadline_monotonic']
                    else clock['launch_deadline_monotonic'])
                if (canonical_digest(child) != (row.get('deadline_evidence') or {}).get('child_digest')
                        or row['deadline_monotonic'] != expected
                        or row.get('deadline_boot_identity') != {key: clock[key] for key in ('boot_identity_source', 'boot_fingerprint')}
                        or (row.get('deadline_evidence') or {}).get('process_nonce') != child.get('nonce')):
                    continue
            elif (row.get('expires_at') != authority.get('expires_at') or authority.get('execution_clock')
                    or datetime.fromisoformat(row['observed_at'].replace('Z', '+00:00')) < datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00'))):
                continue
            return 'task_authorization_expired'
    except (KeyError, TypeError, ValueError, OSError, AttributeError):
        return None
    return None

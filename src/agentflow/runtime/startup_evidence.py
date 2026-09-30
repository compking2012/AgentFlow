"""Read-only proof that a revoked launcher stopped before permission or Agent work."""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import psutil

from agentflow.common import DomainError, canonical_digest
from agentflow.runtime.failures import _invalid_constant, _unique_object
from agentflow.runtime.launcher import STARTUP_CLOCK_FIELDS, startup_deadline
from agentflow.runtime.process_identity import STABLE_BOOT_SOURCES, observe_process, same_launcher_identity

_TIMEOUT = 'launcher_handshake_timeout'
_ABSENT = ('child.json',)
_LOGS = ('stdout.jsonl', 'stderr.log')


def _reject(reason):
    raise DomainError('startup_not_proven', '启动取消证据不完整，保留原执行状态。', details={'reason': reason})


@contextmanager
def _directory(data_dir, folder):
    # Every path component under the configured data root is opened without
    # following links. No path provided by a receipt is ever opened.
    descriptors = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        for name in (str(data_dir), 'supervisor', folder):
            descriptor = os.open(name, flags, **({'dir_fd': descriptors[-1]} if descriptors else {}))
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or (len(descriptors) > 1 and info.st_mode & 0o077)):
                _reject('unsafe_directory')
        yield descriptors[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read(directory, name, *, empty=False):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_mode & 0o077
                or before.st_uid != os.getuid() or before.st_size > 65536):
            _reject('unsafe_receipt')
        raw = os.read(descriptor, 65537)
        after = os.fstat(descriptor)
        if len(raw) > 65536 or any(getattr(before, key) != getattr(after, key)
                for key in ('st_ino', 'st_dev', 'st_mode', 'st_uid', 'st_nlink', 'st_size', 'st_mtime_ns')):
            _reject('changed_receipt')
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        if empty:
            if raw:
                _reject('execution_output_present')
            return None, digest
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if not isinstance(value, dict):
            _reject('invalid_receipt')
        return value, digest
    finally:
        os.close(descriptor)


def _time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        _reject('unbound_time')
    return result


def _stopped(identity):
    """Prove process and group stopped; reused identities never authorize signals."""
    observation = observe_process(identity)
    if observation.get('stopped') is not True:
        _reject('launcher_not_stopped')
    if observation.get('boot_relation') == 'changed':
        return observation
    try:
        leader = psutil.Process(identity['pid'])
        if observation.get('birth_matches') is False and os.getpgid(leader.pid) == leader.pid:
            return {**observation, 'group_state': 'reused_by_unrelated_leader'}
    except (psutil.NoSuchProcess, ProcessLookupError):
        pass
    for process in psutil.process_iter(['pid', 'status'], ad_value=None):
        try:
            if (os.getpgid(process.pid) == identity['pid']
                    and process.info['status'] not in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}):
                _reject('launcher_group_not_stopped')
        except ProcessLookupError:
            pass
    return {**observation, 'group_state': 'stopped'}



def _optional(directory, name, **kwargs):
    try:
        return _read(directory, name, **kwargs)
    except FileNotFoundError:
        return None, None


def _versioned_clock(process, identity, receipt, permit):
    rows = (process, identity, receipt) + ((permit,) if permit is not None else ())
    if not any('startup_clock_version' in row for row in rows):
        return False
    if any(any(type(row.get(key)) is not type(process.get(key)) or row.get(key) != process.get(key)
               for key in STARTUP_CLOCK_FIELDS) for row in rows):
        _reject('startup_clock_mismatch')
    source, boot = identity.get('boot_identity_source'), identity.get('boot_fingerprint')
    if source not in STABLE_BOOT_SOURCES:
        _reject('startup_boot_unverified')
    for row, keys in ((process, ('startup_started_at', 'startup_deadline_at')),
                      (identity, ('launcher_ready_at',)), (receipt, ('launcher_finished_at',))):
        for key in keys:
            _time(row.get(key))
    deadline = startup_deadline(process, source, boot)
    started = process['startup_started_monotonic']
    ready, finished = identity.get('launcher_ready_monotonic'), receipt.get('launcher_finished_monotonic')
    if (any(type(value) not in {int, float} or not math.isfinite(value) for value in (ready, finished))
            or not started <= ready <= finished or receipt.get('child_started') is not False):
        _reject('startup_lifecycle_invalid')
    if permit is not None:
        issued = permit.get('launcher_go_monotonic')
        if (permit.get('nonce') != process.get('nonce')
                or type(permit.get('fencing_token')) is not int or permit['fencing_token'] != process['fencing_token']
                or type(issued) not in {int, float} or not math.isfinite(issued)
                or not ready <= issued < deadline
                or any(key in process and process[key] != permit.get(key)
                       for key in ('launcher_go_at', 'launcher_go_monotonic'))):
            _reject('startup_permit_invalid')
    return receipt.get('startup_stop_reason') == 'startup_deadline_expired' and finished >= deadline

def prove_stopped_unpermitted_launcher(data_dir, process):
    """Return bounded original evidence only for an exact timeout revocation.

    Historical missing DB identity is accepted only for the original timeout
    state and nonce cancellation, backed by matching identity/result receipts.
    Callers must independently bind work/dispatch and prove zero model usage.
    """
    if os.name != 'posix' or not isinstance(process, dict):
        _reject('unsupported_process')
    attempt_id = process.get('attempt_id')
    if not isinstance(attempt_id, str) or not attempt_id or process.get('id') != attempt_id:
        _reject('attempt_mismatch')
    folder = canonical_digest({'attempt_id': attempt_id})[7:]
    expected = Path(data_dir) / 'supervisor' / folder
    if Path(process.get('directory', '')) != expected:
        _reject('directory_mismatch')
    original_timeout = process.get('state') == 'execution_unknown' and process.get('reason') == _TIMEOUT
    revoked = process.get('launch_authorization_revoked_reason') == _TIMEOUT
    if (process.get('state') not in {'execution_unknown', 'cancelled'}
            or process.get('state') == 'cancelled' and process.get('reason') != 'launch_not_authorized'):
        _reject('not_timeout_revocation')
    with _directory(data_dir, folder) as directory:
        identity, identity_digest = _read(directory, 'identity.json')
        receipt, receipt_digest = _read(directory, 'result.json')
        cancel, cancel_digest = _optional(directory, 'cancel.json')
        permit, permit_digest = _optional(directory, 'go.json')
        logs = {name: _optional(directory, name, empty=True)[1] for name in _LOGS}
        for name in _ABSENT:
            try:
                os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            _reject('execution_evidence_present')
    if (not same_launcher_identity(identity, receipt)
            or receipt.get('execution_status') != 'cancelled' or receipt.get('exit_code') is not None
            or receipt.get('reason') != 'launch_not_authorized'
            or any(identity.get(key) != process.get(key) for key in ('attempt_id', 'operation_id', 'nonce', 'fencing_token'))
            or type(process.get('fencing_token')) is not int):
        _reject('receipt_identity_mismatch')
    expired = _versioned_clock(process, identity, receipt, permit)
    versioned = process.get('startup_clock_version') == 1
    if not expired and not (original_timeout or revoked):
        _reject('not_timeout_revocation')
    if (permit is not None and not expired) or any(logs.values()) and not versioned:
        _reject('execution_evidence_present')
    missing_identity = all(process.get(key) is None for key in ('pid', 'process_started_at', 'boot_fingerprint'))
    if not same_launcher_identity(identity, process) and not (missing_identity and original_timeout and not versioned
            and not any(process.get(key) for key in ('boot_identity_source', 'process_birth_source', 'process_birth_fingerprint'))):
        _reject('stored_identity_mismatch')
    if cancel is None:
        if not expired:
            _reject('missing_cancellation')
    elif cancel.get('nonce') != process.get('nonce'):
        _reject('cancel_nonce_mismatch')
    elif cancel.get('reason') is None:
        if not original_timeout or set(cancel) != {'nonce'} or versioned:
            _reject('unbound_cancellation')
    elif (cancel.get('reason') != _TIMEOUT or not revoked
            or cancel.get('revoked_at') != process.get('launch_authorization_revoked_at')):
        _reject('cancellation_not_timeout')
    ready, finished = _time(identity.get('ready_at')), _time(receipt.get('finished_at'))
    created = _time(process.get('created_at'))
    # UTC is display information after a wall-clock adjustment. Version 1
    # already proves chronology using the shared same-boot monotonic clock.
    if not versioned and (finished < ready or ready < created):
        _reject('receipt_time_mismatch')
    if cancel and cancel.get('revoked_at'):
        _time(cancel['revoked_at'])
    if (cancel and cancel.get('revoked_at') and not versioned
            and not (_time(process['created_at']) <= _time(cancel['revoked_at']) <= finished)):
        _reject('revocation_time_mismatch')
    observation = _stopped(identity)
    return {'outcome': 'not_started', 'reason': _TIMEOUT, 'identity': identity, 'receipt': receipt,
            'cancel': cancel, 'permit': permit, 'deadline_expired_before_child': expired,
            'legacy_missing_identity': missing_identity, 'observation': observation,
            'receipt_digest': receipt_digest,
            'evidence_digest': canonical_digest({'identity': identity_digest, 'receipt': receipt_digest,
                                                  'cancel': cancel_digest, 'permit': permit_digest, 'logs': logs, 'absent': list(_ABSENT)}),
            'started_at': process['created_at'], 'ready_at': identity['ready_at'],
            'finished_at': receipt['finished_at']}


def prove_reconciled_startup(data_dir, attempt, context, process, audit, *, invocations, budget):
    """Revalidate a sealed original proof for recovery without inventing usage."""
    from agentflow.runtime.prelaunch import _no_calls
    if (not all(isinstance(row, dict) for row in (attempt, context, process, audit))
            or audit.get('actor') != 'controller' or audit.get('outcome') != 'not_started'
            or audit.get('runtime_failure_code') != 'launcher_startup_timeout'
            or audit.get('reason') != _TIMEOUT or not _no_calls(invocations, budget)
            or attempt.get('execution_reconciliation_id') != audit.get('id')
            or process.get('execution_reconciliation_id') != audit.get('id')
            or attempt.get('runtime_failure_code') != 'launcher_startup_timeout'
            or attempt.get('status') != 'failed' or process.get('state') != 'cancelled'
            or process.get('reason') != 'launch_not_authorized'
            or context.get('id') != attempt.get('id') or audit.get('attempt_id') != attempt.get('id')
            or any(audit.get(key) != attempt.get(key) for key in
                   ('run_id', 'work_item_id', 'generation', 'fencing_token', 'input_fingerprint'))
            or audit.get('dispatch_context_revision') != context.get('revision')
            or audit.get('dispatch_task_digest') != canonical_digest(context.get('task'))
            or any(row.get(flag) for row in (attempt, context, process, audit)
                   for flag in ('restore_uncertain', 'restore_reconciliation_required', 'restore_revalidation_required'))):
        return None
    try:
        proof = prove_stopped_unpermitted_launcher(data_dir, audit.get('original_process'))
        if (proof['evidence_digest'] != audit.get('evidence_digest')
                or not same_launcher_identity(proof['identity'], process)):
            return None
        return proof
    except (DomainError, OSError, ValueError, TypeError, KeyError, psutil.Error):
        return None

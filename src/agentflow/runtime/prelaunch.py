"""Evidence that an adapter failed before the durable supervisor launch boundary."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.domain.planning import CODING_STEPS, EXECUTION_STEPS
from agentflow.runtime.failures import _private_evidence

FAILURES = {'isolation_unverified', 'isolation_probe_timeout', 'sdk_version_unverified',
            'capability_unverified', 'unsafe_sandbox_roots', 'invalid_role_output_checkpoint', 'coding_workspace_unwritable'}
PHASES = {'adapter_preflight', 'sandbox_validation', 'backend_probe', 'envelope_validation'}
IDENTITY = ('run_id', 'iteration_id', 'work_item_id', 'generation', 'fencing_token', 'input_fingerprint')
_LEGACY_SANDBOX_MESSAGE = 'Filesystem/network isolation probe failed; execution blocked'
_PROBES = ('allowed_workspace_probe', 'protected_read_probe', 'protected_write_probe',
           'allowed_port_probe', 'denied_port_probe')


class _NotProven(Exception):
    pass


def _no_launch_directory(data_dir, attempt_id):
    """Absence must include dangling links, empty intents and every possible permit."""
    root = directory = None
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        root = os.open(data_dir, flags)
        try:
            directory = os.open('supervisor', flags, dir_fd=root)
        except FileNotFoundError:
            return True
        name = canonical_digest({'attempt_id': attempt_id}).split(':')[1]
        try:
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    except OSError:
        return False
    finally:
        for descriptor in (directory, root):
            if descriptor is not None:
                os.close(descriptor)


def _identity(attempt, context):
    task = (context or {}).get('task')
    if (not attempt or not isinstance(task, dict) or context.get('id') != attempt['id']
            or task.get('attempt_id') != attempt['id'] or task.get('step') in EXECUTION_STEPS | {'delivery'}
            or type(attempt.get('generation')) is not int or attempt['generation'] < 1
            or type(attempt.get('fencing_token')) is not int or attempt['fencing_token'] < 1
            or type(context.get('revision')) is not int or context['revision'] < 1
            or type(task.get('fencing_token')) is not int
            or any(not isinstance(attempt.get(k), str) or not attempt[k]
                   for k in ('run_id', 'iteration_id', 'work_item_id', 'input_fingerprint'))
            or any(task.get(k) != attempt[k] for k in IDENTITY if k != 'generation')):
        raise _NotProven()
    if any(record.get('restore_reconciliation_required') or record.get('restore_revalidation_required')
           or record.get('restore_uncertain') for record in (attempt, context)):
        raise _NotProven()
    return {'attempt_id': attempt['id'], **{k: attempt[k] for k in IDENTITY},
            'dispatch_context_revision': context['revision'], 'dispatch_task_digest': canonical_digest(task)}


def _no_calls(invocations, budget):
    return not invocations and (not budget or (not budget.get('restore_uncertain')
        and type(budget.get('request_count')) is int and budget['request_count'] == 0
        and type(budget.get('uncertain_invocations')) is int and budget['uncertain_invocations'] == 0))


def _receipt_proof(attempt, context, receipt):
    binding = _identity(attempt, context)
    if (not isinstance(receipt, dict) or receipt.get('id') != attempt['id']
            or receipt.get('actor') != 'controller' or receipt.get('source') != 'controller_preflight'
            or receipt.get('outcome') != 'not_started' or receipt.get('phase') not in PHASES
            or receipt.get('failure_code') not in FAILURES
            or any(type(receipt.get(k)) is not type(v) or receipt.get(k) != v for k, v in binding.items())):
        return None
    return {'outcome': 'not_started', 'source': 'controller_preflight', 'phase': receipt['phase'],
            'failure_code': receipt['failure_code'], **binding}


def _legacy_probe_proof(data_dir, attempt, context):
    """Narrow migration for historical OpenHands sandbox probes, never prose alone.

    A policy must hash to its private filename with this controller's executable,
    name this attempt's exact workspace/artifact/home paths, and have a complete
    failed probe receipt recorded during its bounded preflight interval. A different
    controller executable or unknown evidence keeps recovery blocked.
    """
    if attempt.get('summary') != _LEGACY_SANDBOX_MESSAGE:
        return None
    task = context['task']
    if task.get('step') in CODING_STEPS or task.get('role') == 'development':
        return None
    workspace = Path(task.get('workspace', ''))
    if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir() or workspace.resolve() != workspace:
        return None
    started = datetime.fromisoformat(attempt['started_at'])
    if started.tzinfo is None:
        return None
    started = started.astimezone(UTC)
    name = canonical_digest(attempt['id']).split(':')[1]
    home = data_dir / 'openhands_homes' / name
    artifacts = data_dir / 'attempt_artifacts' / name
    markers = ['(subpath ' + json.dumps(str(p), ensure_ascii=False) + ')' for p in (workspace, artifacts, home)]
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    root = directory = None
    matches = []
    try:
        root = os.open(data_dir, flags)
        directory = os.open('sandbox_profiles', flags, dir_fd=root)
        files = [file for file in os.listdir(directory) if re.fullmatch(r'[0-9a-f]{64}\.probe\.json', file)]
        if len(files) > 2048:
            return None
        for file in files:
            try:
                probe = json.loads(_private_evidence(directory, file, 65536))
                if (not isinstance(probe, dict) or probe.get('verified') is not False
                        or type(probe.get('probe_version', 1)) is not int or probe.get('probe_version', 1) != 1
                        or probe.get('filesystem') != 'hard' or probe.get('network') != 'hard'
                        or any(type(probe.get(k)) is not int or not -128 <= probe[k] <= 255 for k in _PROBES)):
                    continue
                checked = datetime.fromisoformat(probe['checked_at'])
                if (checked.tzinfo is None or not started <= checked.astimezone(UTC) <= started + timedelta(minutes=5)
                        or checked.astimezone(UTC) > datetime.now(UTC)):
                    continue
                successful = (probe[_PROBES[0]] == 0 and probe[_PROBES[1]] != 0 and probe[_PROBES[2]] != 0
                              and probe[_PROBES[3]] == 0 and probe[_PROBES[4]] == 1)
                if successful:
                    continue
                stem = file.removesuffix('.probe.json')
                policy = _private_evidence(directory, stem + '.sb', 512 * 1024).decode('utf-8')
                digest = canonical_digest({'policy': policy, 'executable': str(Path(sys.executable).resolve())})
                if (digest != 'sha256:' + stem or probe.get('policy_fingerprint') != digest
                        or not all(marker in policy for marker in markers)):
                    continue
                # V1 reused -1 for a wait timeout and the real SIGHUP exit status.
                # It proves failed preflight here, not which of those causes occurred.
                matches.append({'outcome': 'not_started', 'source': 'legacy_sandbox_probe',
                    'phase': 'sandbox_validation', 'failure_code': 'isolation_unverified',
                    'policy_fingerprint': digest, 'probe_digest': canonical_digest(probe),
                    **_identity(attempt, context)})
            except (OSError, ValueError, TypeError, KeyError):
                continue
    finally:
        for descriptor in (directory, root):
            if descriptor is not None:
                os.close(descriptor)
    return matches[0] if len(matches) == 1 else None


def prove_prelaunch_failure(data_dir, attempt, context, receipt=None, *, supervised=None,
                            invocations=(), budget=None, run=None):
    """Read-only proof for a blocked attempt. No marker means no general exemption."""
    try:
        data_dir = Path(data_dir)
        if (not attempt or attempt.get('status') != 'blocked' or supervised is not None
                or (run or {}).get('restore_reconciliation_required')
                or data_dir.is_symlink() or not _no_calls(invocations, budget)):
            return None
        _identity(attempt, context)
        if not _no_launch_directory(data_dir, attempt['id']):
            return None
        # A malformed explicit receipt cannot be papered over with a legacy probe.
        if receipt is not None:
            return _receipt_proof(attempt, context, receipt)
        return _legacy_probe_proof(data_dir.resolve(), attempt, context)
    except (OSError, ValueError, KeyError, TypeError, _NotProven):
        return None


async def read_prelaunch_failure(store, data_dir, attempt_id):
    attempt, context, receipt, supervised, budget = await asyncio.gather(
        store.read('attempt', attempt_id), store.read('dispatch_context', attempt_id),
        store.read('prelaunch_failure', attempt_id), store.read('supervised_attempt', attempt_id),
        store.read('model_attempt_budget', attempt_id))
    invocations = [i for i in await store.list('model_invocation') if i.get('attempt_id') == attempt_id]
    run = await store.read('run', attempt['run_id']) if attempt else None
    return await asyncio.to_thread(prove_prelaunch_failure, data_dir, attempt, context, receipt,
        supervised=supervised, invocations=invocations, budget=budget, run=run)


async def prelaunch_failure_code(store, data_dir, attempt_id):
    """A closed UI category; never include exception, prompt or probe text."""
    proof = await read_prelaunch_failure(store, data_dir, attempt_id)
    if not proof:
        return None
    if proof['failure_code'] in {'isolation_probe_timeout', 'invalid_role_output_checkpoint', 'coding_workspace_unwritable'}:
        return proof['failure_code']
    return ('isolation_unverified' if proof['failure_code'] in {
        'isolation_unverified', 'isolation_probe_timeout', 'unsafe_sandbox_roots'} else 'worker_environment_unavailable')


async def record_prelaunch_failure(store, data_dir, task, *, phase='adapter_preflight', failure_code):
    """Called by the controller only at a known failure before Supervisor.start.

    The record attests non-execution, never successful execution, quality or approval.
    If launch/usage/ownership is uncertain, return None and preserve the normal block.
    """
    if failure_code not in FAILURES or phase not in PHASES or not isinstance(task, dict) or not task.get('attempt_id'):
        return None
    if getattr(store, 'data_dir', None) is not None and Path(store.data_dir).resolve() != Path(data_dir).resolve():
        return None
    attempt_id = task['attempt_id']
    try:
        if not await asyncio.to_thread(_no_launch_directory, Path(data_dir), attempt_id):
            return None
        def record(tx):
            attempt = tx.get('attempt', attempt_id)
            context = tx.get('dispatch_context', attempt_id)
            binding = _identity(attempt, context)
            if (attempt['status'] not in {'running', 'blocked'}
                    or any(task.get(k) != context['task'].get(k) for k in (*IDENTITY, 'attempt_id') if k != 'generation')
                    or tx.get('supervised_attempt', attempt_id) is not None):
                raise _NotProven()
            work = tx.get('work_item', attempt['work_item_id'])
            run = tx.get('run', attempt['run_id'])
            if (not work or not run or work.get('attempt_id') != attempt_id or work.get('status') not in {'running', 'blocked'}
                    or any(work.get(k) != attempt[k] for k in ('generation', 'fencing_token', 'input_fingerprint'))
                    or (run or {}).get('restore_reconciliation_required')
                    or not _no_calls([i for i in tx.list('model_invocation') if i.get('attempt_id') == attempt_id],
                                     tx.get('model_attempt_budget', attempt_id))
                    or not _no_launch_directory(Path(data_dir), attempt_id)):
                raise _NotProven()
            value = tx.put('prelaunch_failure', attempt_id, {**binding, 'phase': phase, 'failure_code': failure_code,
                'outcome': 'not_started', 'source': 'controller_preflight', 'actor': 'controller', 'created_at': utc_now()})
            tx.event('attempt.prelaunch_failed', {**binding, 'phase': phase, 'failure_code': failure_code}, run_id=attempt['run_id'])
            return {'receipt': value}
        result = await store.command('runtime.prelaunch_failure', attempt_id,
            {'attempt_id': attempt_id, 'phase': phase, 'failure_code': failure_code,
             'fencing_token': task.get('fencing_token'), 'input_fingerprint': task.get('input_fingerprint')}, record)
        return result['receipt']
    except (OSError, ValueError, TypeError, KeyError, DomainError, _NotProven):
        return None

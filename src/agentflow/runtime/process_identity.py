"""Controller process identities; separate from node executor capability fingerprints."""
from __future__ import annotations

import math
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from uuid import UUID

import psutil

from agentflow.common import canonical_digest, utc_now

from .process_birth import BIRTH_FIELDS, BIRTH_SOURCES, process_birth_identity

IDENTITY_FIELDS = ('attempt_id', 'operation_id', 'nonce', 'fencing_token', 'pid',
                   'process_started_at', 'boot_fingerprint')
STABLE_BOOT_SOURCES = {'macos_bootsessionuuid', 'linux_boot_id'}


@lru_cache(maxsize=1)
def current_boot_identity() -> tuple[str, str]:
    """Kernel boot-session IDs survive wall-clock corrections. Cache for this process."""
    try:
        if sys.platform == 'darwin':
            value = subprocess.run(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'],
                                   check=True, capture_output=True, text=True, timeout=2).stdout.strip()
            source = 'macos_bootsessionuuid'
        elif sys.platform.startswith('linux'):
            value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            source = 'linux_boot_id'
        else:
            raise OSError('Kernel boot-session ID unavailable')
        return source, canonical_digest({source: str(UUID(value))})
    except (OSError, ValueError, subprocess.SubprocessError):
        # A legacy wall-clock timestamp is never evidence that a reboot occurred.
        return 'legacy_boot_time', canonical_digest({'boot_time': psutil.boot_time()})


def boot_relation(identity: dict) -> str:
    """Return same/changed/unknown; only stable session IDs can prove a reboot."""
    source = identity.get('boot_identity_source')
    if source in STABLE_BOOT_SOURCES:
        current_source, fingerprint = current_boot_identity()
        if source == current_source:
            return 'same' if identity['boot_fingerprint'] == fingerprint else 'changed'
        return 'unknown'
    if source in {None, 'legacy_boot_time'}:
        return ('same' if identity['boot_fingerprint'] == canonical_digest({'boot_time': psutil.boot_time()})
                else 'unknown')
    return 'unknown'


def valid_process_identity(identity: dict) -> bool:
    return (isinstance(identity, dict) and type(identity.get('pid')) is int and identity['pid'] > 0
            and type(identity.get('process_started_at')) in {int, float}
            and math.isfinite(identity['process_started_at']) and identity['process_started_at'] > 0
            and isinstance(identity.get('boot_fingerprint'), str)
            and re.fullmatch(r'sha256:[0-9a-f]{64}', identity['boot_fingerprint']) is not None
            and (identity.get('boot_identity_source') is None
                 or isinstance(identity['boot_identity_source'], str)
                 and identity['boot_identity_source'] in STABLE_BOOT_SOURCES | {'legacy_boot_time'})
            and (not any(field in identity for field in BIRTH_FIELDS)
                 or isinstance(identity.get('process_birth_source'), str)
                 and identity['process_birth_source'] in BIRTH_SOURCES
                 and isinstance(identity.get('process_birth_fingerprint'), str)
                 and re.fullmatch(r'sha256:[0-9a-f]{64}', identity['process_birth_fingerprint']) is not None))


def same_launcher_identity(receipt: dict, record: dict) -> bool:
    return (valid_process_identity(receipt) and valid_process_identity(record)
            and all(field in receipt and field in record and receipt[field] == record[field]
                    for field in IDENTITY_FIELDS)
            and type(receipt['fencing_token']) is int and receipt['fencing_token'] > 0
            and all(isinstance(receipt[field], str) and receipt[field]
                    for field in ('attempt_id', 'operation_id', 'nonce'))
            and receipt.get('boot_identity_source') == record.get('boot_identity_source')
            and all(receipt.get(field) == record.get(field) for field in BIRTH_FIELDS))


def observe_process(identity: dict) -> dict:
    observed = {'verified': False, 'alive': False, 'stopped': None, 'observed_at': utc_now(),
                'pid': identity.get('pid'), 'expected_created_at': identity.get('process_started_at'),
                'expected_boot': identity.get('boot_fingerprint')}
    if not valid_process_identity(identity):
        return {**observed, 'reason': 'invalid_process_identity'}
    try:
        relation = boot_relation(identity)
        observed.update(boot_relation=relation, observed_boot=current_boot_identity()[1])
        if relation == 'changed':
            return {**observed, 'stopped': True, 'reason': 'boot_identity_changed'}
        process = psutil.Process(identity['pid'])
        observed.update(observed_created_at=process.create_time(), observed_status=process.status())
        if observed['observed_status'] in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}:
            return {**observed, 'stopped': True, 'reason': 'process_status_stopped'}
        if 'process_birth_source' in identity:
            birth = process_birth_identity(identity['pid'])
            observed.update(observed_birth=birth)
            if birth['process_birth_source'] != identity['process_birth_source']:
                return {**observed, 'reason': 'process_birth_source_unverified'}
            same_birth = all(birth[field] == identity[field] for field in BIRTH_FIELDS)
            observed['birth_matches'] = same_birth
            if not same_birth:
                return {**observed, 'stopped': True, 'reason': 'process_birth_changed'}
        else:
            same_birth = abs(observed['observed_created_at'] - identity['process_started_at']) < .01
            if not same_birth:
                # Historical receipts have only adjusted wall time. A mismatch
                # cannot distinguish PID reuse from clock correction, so it
                # authorizes neither a signal nor declaring this process stopped.
                return {**observed, 'reason': 'legacy_process_birth_unverified'}
        alive = same_birth
        observed.update(alive=alive, stopped=not alive, verified=alive and relation == 'same')
        observed['reason'] = ('verified' if observed['verified'] else
                              'boot_identity_unverified' if alive else 'creation_time_or_status_mismatch')
    except psutil.NoSuchProcess as exc:
        observed.update(stopped=True, inspection_error=True, reason=type(exc).__name__)
    except (psutil.Error, OSError) as exc:
        observed.update(inspection_error=True, reason=type(exc).__name__)
    return observed


def process_is_stopped(identity: dict) -> bool:
    observation = observe_process(identity)
    if observation['stopped'] is None:
        raise ValueError(observation['reason'])
    return observation['stopped']

"""Exact Darwin timeval fallback when libproc denies a protected reused PID."""
import ctypes
import errno
import os
import struct
import sys
from types import SimpleNamespace

import psutil
import pytest

from agentflow.common import canonical_digest
from agentflow.runtime import process_birth, process_identity


def denied_proc(error=errno.EPERM):
    def call(*_args):
        ctypes.set_errno(error)
        return 0
    return call


def kinfo_reply(pid=321, seconds=1790407109, microseconds=200183):
    # Darwin LP64 public extern_proc ABI, independently spelled out from headers.
    data = bytearray(648)
    struct.pack_into('=q', data, 0, seconds)
    struct.pack_into('=i', data, 8, microseconds)
    data[12:16] = b'\xab\xcd\xef\x01'  # timeval padding must never become microseconds.
    struct.pack_into('=i', data, 40, pid)
    return bytes(data)


def sysctl_reply(data, *, error=0, actual_size=None, query_size=None):
    calls = []
    def call(mib, count, output, length, new_value, new_length):
        assert list(mib) == [1, 14, 1, 321] and count == 4
        assert new_value is None and new_length == 0
        size = ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t))
        calls.append(output is not None)
        if error:
            ctypes.set_errno(error)
            return -1
        if output is None:
            size.contents.value = len(data) if query_size is None else query_size
        else:
            assert size.contents.value >= len(data)
            ctypes.memmove(output, data, len(data))
            size.contents.value = len(data) if actual_size is None else actual_size
        return 0
    return call, calls


def fallback(monkeypatch, response, *, denial=errno.EPERM):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(process_birth, '_mac_proc_pidinfo', lambda: denied_proc(denial))
    monkeypatch.setattr(process_birth, '_mac_sysctl', lambda: response, raising=False)


def expected_birth(seconds=1790407109, microseconds=200183):
    return {'process_birth_source': 'macos_proc_bsdinfo',
        'process_birth_fingerprint': canonical_digest({'source': 'macos_proc_bsdinfo',
            'pid': 321, 'birth': (seconds, microseconds)})}


@pytest.mark.parametrize('denial', [errno.EPERM, errno.EACCES])
def test_permission_denial_reads_exact_sysctl_timeval_with_compatible_fingerprint(monkeypatch, denial):
    response, calls = sysctl_reply(kinfo_reply())
    fallback(monkeypatch, response, denial=denial)
    assert process_birth.process_birth_identity(321) == expected_birth()
    assert calls == [False, True]


def test_sysctl_birth_never_round_trips_through_float_seconds(monkeypatch):
    seconds = 2 ** 53 - 1
    for micros in (200183, 200184):
        response, _ = sysctl_reply(kinfo_reply(seconds=seconds, microseconds=micros))
        fallback(monkeypatch, response)
        assert process_birth.process_birth_identity(321) == expected_birth(seconds, micros)


@pytest.mark.parametrize('data,options', [
    (kinfo_reply(pid=322), {}), (kinfo_reply(seconds=0), {}),
    (kinfo_reply(microseconds=-1), {}), (kinfo_reply(microseconds=1000000), {}),
    (kinfo_reply(), {'actual_size': 647}), (b'x' * 16, {}),
    (b'', {'query_size': 1024 * 1024}), (b'', {'error': errno.EPERM}),
    (b'', {'error': errno.EACCES}), (b'', {'error': errno.EIO}),
])
def test_invalid_or_unavailable_sysctl_evidence_stays_fail_closed(monkeypatch, data, options):
    response, _ = sysctl_reply(data, **options)
    fallback(monkeypatch, response)
    with pytest.raises(OSError):
        process_birth.process_birth_identity(321)


@pytest.mark.parametrize('kind', ['no_record', 'esrch'])
def test_sysctl_explicitly_missing_pid_remains_native_stop_evidence(monkeypatch, kind):
    response, _ = sysctl_reply(b'', **({'error': errno.ESRCH} if kind == 'esrch' else {}))
    fallback(monkeypatch, response)
    with pytest.raises(psutil.NoSuchProcess):
        process_birth.process_birth_identity(321)


@pytest.mark.parametrize('bytes_returned,error', [(1, 0), (0, errno.EIO), (0, errno.ESRCH)])
def test_non_permission_libproc_failures_do_not_fall_back(monkeypatch, bytes_returned, error):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    def proc(*_args):
        ctypes.set_errno(error)
        return bytes_returned
    monkeypatch.setattr(process_birth, '_mac_proc_pidinfo', lambda: proc)
    def forbidden():
        raise AssertionError('Only explicit permission denials may use the fallback')
    monkeypatch.setattr(process_birth, '_mac_sysctl', forbidden, raising=False)
    with pytest.raises(psutil.NoSuchProcess if error == errno.ESRCH else OSError):
        process_birth.process_birth_identity(321)


@pytest.mark.parametrize('raw_micros,unavailable,stopped', [(200183, False, False), (200184, False, True),
                                                         (200184, True, None)])
def test_observer_uses_exact_birth_and_never_wall_clock_or_denial_to_prove_exit(
        monkeypatch, raw_micros, unavailable, stopped):
    response, _ = sysctl_reply(kinfo_reply(microseconds=raw_micros), error=errno.EPERM if unavailable else 0)
    fallback(monkeypatch, response)
    boot = canonical_digest('same kernel boot')
    monkeypatch.setattr(process_identity, 'current_boot_identity', lambda: ('macos_bootsessionuuid', boot))
    monkeypatch.setattr(psutil, 'Process', lambda _pid: SimpleNamespace(
        create_time=lambda: 1790398120.680446, status=lambda: psutil.STATUS_SLEEPING))
    identity = {'pid': 321, 'process_started_at': 42.0, 'boot_identity_source': 'macos_bootsessionuuid',
                'boot_fingerprint': boot, **expected_birth()}
    result = process_identity.observe_process(identity)
    assert result['stopped'] is stopped
    assert result['verified'] is (stopped is False)
    if stopped is True:
        assert result['reason'] == 'process_birth_changed'


@pytest.mark.skipif(sys.platform != 'darwin', reason='Native Darwin sysctl/libproc parity')
def test_native_sysctl_matches_libproc_for_current_process(monkeypatch):
    before = process_birth.process_birth_identity(os.getpid())
    monkeypatch.setattr(process_birth, '_mac_proc_pidinfo', lambda: denied_proc())
    assert process_birth.process_birth_identity(os.getpid()) == before


def test_sysctl_capacity_estimate_can_exceed_one_complete_process_record(monkeypatch):
    response, calls = sysctl_reply(kinfo_reply(), query_size=3888)
    fallback(monkeypatch, response)
    assert process_birth.process_birth_identity(321) == expected_birth()
    assert calls == [False, True]

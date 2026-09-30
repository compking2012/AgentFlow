"""Kernel process birth tokens, unaffected by psutil wall-clock adjustments."""
from __future__ import annotations

import ctypes
import errno
import os
import sys
from functools import lru_cache
from pathlib import Path

import psutil

from agentflow.common import canonical_digest

BIRTH_FIELDS = ('process_birth_source', 'process_birth_fingerprint')
BIRTH_SOURCES = {'macos_proc_bsdinfo', 'linux_proc_start_ticks'}


class _MacBsdInfo(ctypes.Structure):
    # Public <sys/proc_info.h> proc_bsdinfo (PROC_PIDTBSDINFO), not kinfo_proc.
    _fields_ = [(name, ctypes.c_uint32) for name in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ('nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')]
    _fields_ += [('nice', ctypes.c_int32), ('start_seconds', ctypes.c_uint64),
                ('start_microseconds', ctypes.c_uint64)]


@lru_cache(maxsize=1)
def _mac_proc_pidinfo():
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    function = library.proc_pidinfo
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    return function


class _MacKinfoPrefix(ctypes.Structure):
    # Darwin LP64 <sys/proc.h>: kinfo_proc starts with extern_proc. Its first
    # union holds timeval (long tv_sec, int32_t tv_usec), padded to two pointers.
    _fields_ = [('start_seconds', ctypes.c_long), ('start_microseconds', ctypes.c_int32),
                ('vmspace', ctypes.c_void_p), ('sigacts', ctypes.c_void_p),
                ('flags', ctypes.c_int32), ('status', ctypes.c_char), ('pid', ctypes.c_int32)]


# Public Darwin LP64 kinfo_proc ABI: extern_proc plus eproc. Parse only the
# prefix we need, but require a complete single record; unknown ABIs fail closed.
_MAC_KINFO_PROC_SIZE = 648


@lru_cache(maxsize=1)
def _mac_sysctl():
    function = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True).sysctl
    function.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    function.restype = ctypes.c_int
    return function


def _mac_sysctl_birth(pid: int) -> tuple[int, int]:
    # psutil's macOS raw creation time uses KERN_PROC_PID too, but its public
    # value is clock-adjusted and even its raw result is a float. Read the exact
    # C seconds/microseconds so fingerprints remain identical to proc_bsdinfo.
    if ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(ctypes.c_long) != 8:
        raise OSError('Unsupported Darwin process-info ABI')
    function = _mac_sysctl()
    mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
    length = ctypes.c_size_t()

    def read(output):
        ctypes.set_errno(0)
        if function(mib, 4, output, ctypes.byref(length), None, 0) != 0:
            code = ctypes.get_errno() or errno.EIO
            if code == errno.ESRCH:
                raise psutil.NoSuchProcess(pid)
            raise OSError(code, os.strerror(code))
        if length.value == 0:
            raise psutil.NoSuchProcess(pid)

    read(None)
    capacity = length.value
    if not _MAC_KINFO_PROC_SIZE <= capacity <= 16384:
        raise OSError('Invalid kernel process-info size')
    buffer = ctypes.create_string_buffer(capacity)
    read(buffer)
    # The sizing query includes spare records (KERN_PROCSLOP). A PID lookup
    # itself must return exactly one complete kinfo_proc, not the estimate.
    if length.value != _MAC_KINFO_PROC_SIZE:
        raise OSError('Incomplete kernel process-info response')
    info = _MacKinfoPrefix.from_buffer_copy(buffer)
    if info.pid != pid or info.start_seconds <= 0 or not 0 <= info.start_microseconds < 1_000_000:
        raise OSError('Invalid kernel process birth record')
    return info.start_seconds, info.start_microseconds


def process_birth_identity(pid: int) -> dict[str, str]:
    if type(pid) is not int or pid <= 0:
        raise ValueError('Invalid process ID')
    if sys.platform == 'darwin':
        info = _MacBsdInfo()
        ctypes.set_errno(0)
        size = _mac_proc_pidinfo()(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size != ctypes.sizeof(info):
            code = ctypes.get_errno() or errno.EIO
            if code == errno.ESRCH:
                raise psutil.NoSuchProcess(pid)
            if size <= 0 and code in {errno.EPERM, errno.EACCES}:
                birth = _mac_sysctl_birth(pid)
            else:
                raise OSError(code, os.strerror(code))
        else:
            if info.pid != pid or info.start_seconds <= 0 or info.start_microseconds >= 1_000_000:
                raise OSError('Invalid kernel process birth record')
            birth = (info.start_seconds, info.start_microseconds)
        # The two native APIs expose the same integer pair. Keep the existing
        # source label and digest format for already-issued launcher receipts.
        source = 'macos_proc_bsdinfo'
    elif sys.platform.startswith('linux'):
        try:
            data = (Path('/proc') / str(pid) / 'stat').read_bytes()
        except FileNotFoundError:
            raise psutil.NoSuchProcess(pid) from None
        # comm may contain spaces and parentheses; field 22 follows the final ') '.
        fields = data.rsplit(b') ', 1)[-1].split()
        try:
            if int(data.split(b' ', 1)[0]) != pid or b') ' not in data:
                raise ValueError
            birth = int(fields[19])
            if birth < 0:
                raise ValueError
        except (ValueError, IndexError):
            raise OSError('Invalid kernel process birth record') from None
        source = 'linux_proc_start_ticks'
    else:
        raise OSError('Stable kernel process birth identity unavailable')
    return {'process_birth_source': source,
            'process_birth_fingerprint': canonical_digest({'source': source, 'pid': pid, 'birth': birth})}

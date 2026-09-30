"""Owner-only local launcher channel; refreshing the UI does not restart Agent jobs."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from agentflow.common import DomainError


def socket_path(data_dir: Path) -> Path:
    if os.name != "posix":
        raise DomainError("unsupported_owner_ipc", "Local owner IPC requires a POSIX controller")
    directory = Path("/tmp").resolve() / f"agentflow-owner-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise DomainError("unsafe_owner_ipc", "Owner IPC directory does not have private ownership", 403)
    name = hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:32]
    return directory / (name + ".sock")


class OwnerBroker:
    def __init__(self, data_dir, tokens, origin):
        self.path, self.tokens, self.origin = socket_path(data_dir), tokens, origin
        self.server = None
        self.inode = None
        self._lock_fd = None
        self._closed = True
        self._writers = set()
        self._handlers = set()
        self._lifecycle = asyncio.Lock()

    async def start(self):
        async with self._lifecycle:
            await self._start()

    async def _start(self):
        if self.server is not None:
            return
        # The private per-channel lock also closes the check/bind race between two
        # cooperative launchers. A live older/noncooperative socket is probed too.
        import fcntl
        try:
            descriptor = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                os.close(descriptor)
                raise DomainError("unsafe_owner_ipc", "Owner channel lock must be a private regular file", 403)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(descriptor)
                raise DomainError("owner_ipc_in_use", "The owner channel already has an active broker", 409) from exc
            self._lock_fd = descriptor
            if self.path.exists() or self.path.is_symlink():
                info = self.path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise DomainError("unsafe_owner_ipc", "Refusing to replace an unowned IPC path", 403)
                try:
                    _, writer = await asyncio.wait_for(asyncio.open_unix_connection(self.path), 2)
                except (ConnectionRefusedError, FileNotFoundError):
                    if self.path.exists() and self.path.lstat().st_ino == info.st_ino:
                        self.path.unlink()
                else:
                    writer.close()
                    await writer.wait_closed()
                    raise DomainError("owner_ipc_in_use", "Refusing to replace an active owner channel", 409)
            self._closed = False
            self.server = await asyncio.start_unix_server(self._handle, path=self.path, limit=256)
            self.path.chmod(0o600)
            info = self.path.lstat()
            self.inode = (info.st_dev, info.st_ino)
        except BaseException:
            if self.server is not None:
                self.server.close()
                await self.server.wait_closed()
                self.server = None
            self._closed = True
            self._release_lock()
            raise

    def _release_lock(self):
        if self._lock_fd is not None:
            import fcntl
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self._writers.add(writer)
        self._handlers.add(task)
        try:
            if self._closed:
                return
            message = await asyncio.wait_for(reader.readline(), 2)
            if self._closed or message != b"open\n":
                return
            code = self.tokens.new_bootstrap()
            writer.write(json.dumps({"url": self.origin + "/#bootstrap=" + code}).encode() + b"\n")
            await writer.drain()
        except (OSError, ValueError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self._writers.discard(writer)
            self._handlers.discard(task)

    async def close(self):
        async with self._lifecycle:
            await self._close()

    async def _close(self):
        self._closed = True
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for writer in list(self._writers):
            writer.close()
        if self._handlers:
            await asyncio.gather(*list(self._handlers), return_exceptions=True)
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == self.inode:
                self.path.unlink()
        self.inode = None
        self._release_lock()


async def launch_url(data_dir: Path) -> str:
    path = socket_path(data_dir)
    try:
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise DomainError("unsafe_owner_ipc", "Owner channel is not private", 403)
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path, limit=2048), 2)
        try:
            writer.write(b"open\n")
            await writer.drain()
            value = json.loads(await asyncio.wait_for(reader.readline(), 2))
        finally:
            writer.close()
            await writer.wait_closed()
        if not isinstance(value, dict) or not isinstance(value.get("url"), str):
            raise ValueError("invalid owner response")
        url = value["url"]
        parts = urlsplit(url)
        fragment = parse_qs(parts.fragment, keep_blank_values=True, strict_parsing=True)
        if (parts.scheme != "http" or not ipaddress.ip_address(parts.hostname).is_loopback
                or parts.username or parts.password or parts.path != "/" or parts.query
                or set(fragment) != {"bootstrap"} or len(fragment["bootstrap"]) != 1
                or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", fragment["bootstrap"][0])):
            raise ValueError("invalid local launch address")
        return url
    except (OSError, ValueError, KeyError, TimeoutError) as exc:
        raise DomainError("controller_unavailable", "Start the local controller before opening the dashboard", 503) from exc

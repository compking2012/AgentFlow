"""Measured macOS filesystem/network isolation for local Web/API jobs and previews."""
from __future__ import annotations

import asyncio
import math
import os
import platform
import plistlib
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import psutil

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.execution.manifests import file_digest
from agentflow.execution.models import ToolObservation
from agentflow.execution.process import CommandSpec, ProcessExecutor
from agentflow.runtime.launcher import atomic_json
from agentflow.runtime.sandbox import MacSeatbeltSandbox


class _RegistryTunnel:
    """A bounded CONNECT relay to the one approved package host; never a general proxy."""
    def __init__(self, addresses, *, idle_timeout_seconds=30):
        if (type(idle_timeout_seconds) not in {int, float}
                or not math.isfinite(idle_timeout_seconds) or idle_timeout_seconds <= 0):
            raise ValueError('Registry idle timeout must be positive and finite')
        self.addresses = addresses
        self.idle_timeout_seconds = float(idle_timeout_seconds)
        self.server = None
        self.sessions = set()
        self.port = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=16384)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def _handle(self, reader, writer):
        current = asyncio.current_task()
        if len(self.sessions) >= 128:
            writer.close()
            return
        self.sessions.add(current)
        upstream = None
        relays = []
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            first = headers.split(b"\r\n", 1)[0].split()
            if len(first) != 3 or first[0] != b"CONNECT" or first[1].lower() != b"registry.npmjs.org:443":
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            for address in self.addresses:
                try:
                    upstream_reader, upstream = await asyncio.wait_for(asyncio.open_connection(address, 443), timeout=5)
                    break
                except (OSError, TimeoutError):
                    continue
            if upstream is None:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            activity = asyncio.Event()
            draining = 0

            async def copy(incoming, outgoing):
                nonlocal draining
                while block := await incoming.read(65536):
                    activity.set()
                    outgoing.write(block)
                    draining += 1
                    try:
                        await outgoing.drain()
                    finally:
                        draining -= 1
                        activity.set()

            async def idle_deadline():
                # HTTPS can stall after CONNECT but before npm's fetch timer
                # takes ownership. Progress in either direction keeps a live
                # transfer open. drain() can remain pending while a slow peer
                # consumes buffered bytes, so backpressure is bounded by the
                # overall job deadline rather than this read-idle deadline.
                while True:
                    activity.clear()
                    try:
                        await asyncio.wait_for(activity.wait(), self.idle_timeout_seconds)
                    except TimeoutError:
                        if not draining:
                            return

            relays = [asyncio.create_task(copy(reader, upstream)), asyncio.create_task(copy(upstream_reader, writer)),
                      asyncio.create_task(idle_deadline())]
            await asyncio.wait(relays, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            for relay in relays:
                relay.cancel()
            if relays:
                await asyncio.gather(*relays, return_exceptions=True)
            if upstream:
                upstream.close()
            writer.close()
            self.sessions.discard(current)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        sessions = list(self.sessions)
        for session in sessions:
            session.cancel()
        if sessions:
            await asyncio.gather(*sessions, return_exceptions=True)


class LocalExecutionSandbox:
    def __init__(self, data_dir: Path, protected_ports=(), *, package_fetch_timeout_seconds=30):
        if (type(package_fetch_timeout_seconds) not in {int, float}
                or not math.isfinite(package_fetch_timeout_seconds) or package_fetch_timeout_seconds < 1):
            raise ValueError('Package fetch timeout must be at least one second')
        self.data_dir = Path(data_dir).resolve()
        self.package_fetch_timeout_seconds = float(package_fetch_timeout_seconds)
        self.protected_ports = sorted({int(port) for port in protected_ports})
        self.engine = MacSeatbeltSandbox(self.data_dir / "local_isolation")
        self._proxies = {}

    def _protected_paths(self):
        from agentflow.configuration import configuration_path
        return [self.data_dir / relative for relative in
                ("state", "auth", "secrets", "nodes/pki", "model_invocations", "supervisor", "workspace_metadata")] + [
                    configuration_path(), configuration_path().parent / 'active-instance.json',
                    configuration_path().parent / 'cli_submissions']

    @staticmethod
    def _runtime_reads(executable: Path):
        paths = [executable.resolve().parent, Path(sys.prefix), Path(sys.base_prefix),
                 Path(__file__).resolve().parents[1] / "agentflow/execution/launcher.py"]
        paths.extend(Path(path) for path in ("/opt/homebrew", "/usr/local/lib", "/Library/Fonts",
                                           "/private/var/db/timezone", "/private/var/db/icu") if Path(path).exists())
        if executable.name in {"npm-cli.js", "npx-cli.js"}:
            paths.append(executable.parent.parent)
        return paths

    async def prepare(self, executable: Path, *, read_roots: list[Path], write_roots: list[Path],
                      evidence_dir: Path, allow_package_downloads=False,
                      allowed_loopback_ports=(), allow_browser_ipc=False,
                      browser_bundle_id=None) -> tuple[list[str], dict]:
        if platform.system() != "Darwin" or not shutil.which("sandbox-exec"):
            raise DomainError("local_isolation_unavailable", "This local executor requires a verified macOS sandbox")
        executable = Path(executable).resolve(strict=True)
        reads = [Path(path).resolve() for path in [*read_roots, *self._runtime_reads(executable)]]
        writes = [Path(path).resolve() for path in write_roots]
        evidence_dir = Path(evidence_dir).resolve()
        for permitted in reads + writes:
            for protected in self._protected_paths():
                if permitted == protected or protected.is_relative_to(permitted) or permitted.is_relative_to(protected):
                    raise DomainError("unsafe_local_execution_scope", "Product execution cannot include controller state or credentials")
        if not writes:
            raise DomainError("local_runtime_directory_required", "An isolated writable runtime directory is required")
        for path in writes:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        allowed_ports = sorted({int(port) for port in allowed_loopback_ports})
        if any(not 1024 <= port <= 65535 for port in allowed_ports) or set(allowed_ports) & set(self.protected_ports):
            raise DomainError("unsafe_local_execution_port", "Execution ports must be unprivileged and separate from controller ports")
        addresses = []
        proxy = None
        if allow_package_downloads:
            from ipaddress import ip_address
            answers = await asyncio.to_thread(socket.getaddrinfo, "registry.npmjs.org", 443, type=socket.SOCK_STREAM)
            addresses = sorted({answer[4][0] for answer in answers})
            if not addresses or any(not ip_address(address).is_global for address in addresses):
                raise DomainError("package_registry_unavailable", "The package registry did not resolve to public addresses")
            proxy = await _RegistryTunnel(addresses, idle_timeout_seconds=self.package_fetch_timeout_seconds).start()
            self._proxies[proxy.port] = proxy
            allowed_ports.append(proxy.port)
        listeners = []
        for _ in range(2):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(4)
            listeners.append(listener)
        forbidden = self.data_dir / "state" / f".local-execution-secret-probe-{uuid4().hex}"
        forbidden.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        forbidden.write_text("protected controller probe")
        forbidden.chmod(0o600)
        allowed = writes[0] / f".local-execution-write-probe-{uuid4().hex}"
        allowed.write_text("allowed")
        def policy(ports):
            text = self.engine.policy(reads, writes, ports)
            text += '\n(allow signal (target same-sandbox))'
            if allow_browser_ipc:
                text += '\n(allow mach-register (global-name-prefix "org.chromium.crashpad.child_port_handshake."))'
                text += '\n(allow iokit-open (iokit-user-client-class "RootDomainUserClient"))'
                if browser_bundle_id in {"com.google.Chrome", "com.google.chrome.for.testing", "org.chromium.Chromium", "org.chromium.headless_shell"}:
                    text += f'\n(allow mach-register (global-name-prefix "{browser_bundle_id}.MachPortRendezvousServer."))'
            for directory in writes:
                text += f'\n(allow network-bind network-inbound network-outbound (subpath {self.engine._quote(str(directory))}))'
            for port in ports:
                text += f'\n(allow network-bind (local ip "localhost:{port}"))'
                text += f'\n(allow network-inbound (local ip "localhost:{port}"))'
            return text
        rules = policy(allowed_ports)
        profile = evidence_dir / f"{canonical_digest(rules)[7:]}.sb"
        profile.write_text(rules)
        profile.chmod(0o600)
        # Exercise the same exact-port generator against two real listeners.
        network_profile = evidence_dir / f"{uuid4().hex}.network-probe.sb"
        network_profile.write_text(policy([listeners[0].getsockname()[1]]))
        network_profile.chmod(0o600)
        try:
            permitted = await self.engine._run_probe(profile,
                f"from pathlib import Path;p=Path({str(allowed)!r});assert p.read_text()=='allowed';p.write_text('updated')")
            denied_read = await self.engine._run_probe(profile,
                f"from pathlib import Path;Path({str(forbidden)!r}).read_text()")
            denied_write = await self.engine._run_probe(profile,
                f"from pathlib import Path;Path({str(forbidden)!r}).write_text('must-not-write')")
            network = []
            for listener in listeners:
                port = listener.getsockname()[1]
                network.append(await self.engine._run_probe(network_profile,
                    f"import socket;s=socket.create_connection(('127.0.0.1',{port}),timeout=1);s.close()"))
        finally:
            forbidden.unlink(missing_ok=True)
            allowed.unlink(missing_ok=True)
            for listener in listeners:
                listener.close()
        evidence = {"verified": permitted == 0 and denied_read != 0 and denied_write != 0 and network == [0, 1],
                    "filesystem": "hard", "network": "hard", "process_tree": "observed",
                    "allowed_workspace_probe": permitted, "protected_read_probe": denied_read,
                    "protected_write_probe": denied_write, "allowed_loopback_probe": network[0],
                    "denied_control_port_probe": network[1], "protected_ports": self.protected_ports,
                    "allowed_loopback_ports": allowed_ports,
                    "package_registry_addresses": addresses, "policy_fingerprint": canonical_digest(rules),
                    "package_proxy_port": proxy.port if proxy else None,
                    "browser_crashpad_ipc": bool(allow_browser_ipc),
                    "profile_path": str(profile), "checked_at": utc_now(), "platform": platform.platform(),
                    "listen_address_enforcement": "caller_must_monitor",
                    "note": "Process-group cleanup is observed; arbitrary detached descendants require reconciliation."}
        atomic_json(evidence_dir / "isolation.json", evidence)
        if not evidence["verified"]:
            await self.release(evidence)
            raise DomainError("local_isolation_unverified", "Local execution isolation checks did not pass", details=evidence)
        return ["/usr/bin/sandbox-exec", "-f", str(profile)], evidence

    async def release(self, evidence):
        proxy = self._proxies.pop(evidence.get("package_proxy_port"), None)
        if proxy:
            await proxy.close()

    async def close(self):
        for proxy in list(self._proxies.values()):
            await proxy.close()
        self._proxies.clear()


class LocalIsolatedExecutor(ProcessExecutor):
    # Contract: each command must pass prepare() before any project code starts.
    supports_verified_isolation = True

    def __init__(self, data_dir: Path, protected_ports=(), browser_executable=None, *, package_fetch_timeout_seconds=30):
        self.sandbox = LocalExecutionSandbox(data_dir, protected_ports,
            package_fetch_timeout_seconds=package_fetch_timeout_seconds)
        self.browser_executable = browser_executable
        self._job_ports: dict[str, int] = {}
        self._job_secondary_ports: dict[str, int] = {}
        self._cache_seeded = False
        from agentflow.testing import adapters
        from node_agent import runner
        self.driver_fingerprint = canonical_digest({str(path.name): file_digest(path) for path in
            (Path(__file__), Path(runner.__file__), Path(adapters.__file__))})

    def augment_capability_report(self, report):
        return report.model_copy(update={"tools": [*report.tools, ToolObservation(name="agentflow-local-executor",
            path=str(Path(__file__).resolve()), version="1:" + self.driver_fingerprint, available=True,
            executable_fingerprint=self.driver_fingerprint,
            detail="Verified per-command filesystem/port sandbox; listener addresses are monitored") ]})

    def _seed_cache(self, destination: Path):
        # The executor's cache contains only packages its own jobs requested.
        # Copying the owner's entire npm cache duplicated unrelated downloads on every restart.
        if destination.is_symlink() or destination.resolve() != destination:
            raise DomainError('unsafe_package_cache', 'Package cache must be a real controller-owned directory')
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _ports_for_job(self, job_key: str) -> tuple[int, int]:
        # Released sockets can be selected again before a job starts listening.
        # Keep both assignments reserved across all commands and jobs on this executor.
        excluded = set(self.sandbox.protected_ports) | set(self._job_ports.values()) | set(self._job_secondary_ports.values())
        for assignments in (self._job_ports, self._job_secondary_ports):
            if job_key in assignments:
                continue
            for _ in range(128):
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                if 1024 <= port <= 65535 and port not in excluded:
                    assignments[job_key] = port
                    excluded.add(port)
                    break
            else:
                raise DomainError("local_test_port_unavailable", "Could not allocate a separate test service port")
        return self._job_ports[job_key], self._job_secondary_ports[job_key]

    async def execute(self, command, output_dir, limits, cancel=None, on_start=None):
        output_dir = Path(output_dir).resolve()
        workspace = output_dir.parent.parent / "workspace"
        if not command.cwd.resolve().is_relative_to(workspace):
            raise DomainError("local_execution_workspace_mismatch", "Local jobs must execute inside their frozen workspace")
        executable = shutil.which(command.argv[0])
        if not executable:
            raise DomainError("local_tool_missing", f"Required execution tool is unavailable: {command.argv[0]}")
        job_key = str(workspace)
        main_port, secondary_port = self._ports_for_job(job_key)
        environment = {**command.environment, "AGENTFLOW_WEB_PORT": str(main_port),
                       "AGENTFLOW_TEST_PORT": str(main_port),
                       "AGENTFLOW_TEST_SECONDARY_PORT": str(secondary_port)}
        reads = [workspace]
        browser = environment.get("AGENTFLOW_BROWSER_EXECUTABLE") or self.browser_executable or os.getenv("AGENTFLOW_BROWSER_EXECUTABLE")
        browser_bundle_id = None
        if browser:
            browser_path = Path(browser).resolve()
            bundle = next((parent for parent in browser_path.parents if parent.suffix == ".app"), browser_path.parent)
            reads.append(bundle)
            info = bundle / "Contents/Info.plist"
            if info.is_file():
                with info.open("rb") as stream:
                    browser_bundle_id = plistlib.load(stream).get("CFBundleIdentifier")
            elif browser_path.name == "chrome-headless-shell":
                browser_bundle_id = "com.google.chrome.for.testing"
        browser_cache = environment.get("PLAYWRIGHT_BROWSERS_PATH") or os.getenv("PLAYWRIGHT_BROWSERS_PATH")
        if browser_cache and browser_cache != "0":
            reads.append(Path(browser_cache).expanduser().resolve())
        package_download = Path(command.argv[0]).name == "npm" and len(command.argv) > 1 and command.argv[1] == "ci"
        private_temp = Path(tempfile.mkdtemp(prefix="af-node-", dir="/private/tmp" if platform.system() == "Darwin" else None)).resolve()
        environment.update(TMPDIR=str(private_temp), TMP=str(private_temp), TEMP=str(private_temp),
                           MAC_CHROMIUM_TMPDIR=str(private_temp), CFFIXED_USER_HOME=str(private_temp))
        writes = [workspace, output_dir, private_temp]
        if package_download:
            cache = self.sandbox.data_dir / "package_cache/npm"
            if not self._cache_seeded:
                await asyncio.to_thread(self._seed_cache, cache)
                self._cache_seeded = True
            writes.append(cache)
            environment.update(npm_config_cache=str(cache), npm_config_maxsockets="8",
                               npm_config_fetch_timeout=str(int(self.sandbox.package_fetch_timeout_seconds * 1000)),
                               npm_config_fetch_retry_mintimeout="1000", npm_config_fetch_retry_maxtimeout="5000",
                               npm_config_prefer_offline="true")
        try:
            prefix, evidence = await self.sandbox.prepare(Path(executable), read_roots=reads,
                write_roots=writes, evidence_dir=output_dir, allow_package_downloads=package_download,
                allowed_loopback_ports=[main_port, secondary_port], allow_browser_ipc=bool(browser),
                browser_bundle_id=browser_bundle_id)
        except BaseException:
            shutil.rmtree(private_temp)
            raise
        if evidence.get("package_proxy_port"):
            proxy = f"http://127.0.0.1:{evidence['package_proxy_port']}"
            environment.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy, NO_PROXY="", npm_config_https_proxy=proxy,
                               npm_config_proxy=proxy, npm_config_noproxy="", npm_config_registry="https://registry.npmjs.org")
        wrapped = CommandSpec(argv=tuple(prefix) + command.argv, cwd=command.cwd, label=command.label,
                              environment=environment)
        effective_cancel = cancel or asyncio.Event()
        identity = {}
        violations = []
        monitor_stop = asyncio.Event()

        async def started(pid, fingerprint):
            identity.update(pid=pid, created=psutil.Process(pid).create_time())
            if on_start:
                await on_start(pid, fingerprint)

        async def monitor():
            from ipaddress import ip_address
            while not monitor_stop.is_set():
                if identity:
                    try:
                        parent = psutil.Process(identity["pid"])
                        if parent.create_time() == identity["created"]:
                            for process in [parent, *parent.children(recursive=True)]:
                                try:
                                    for connection in process.net_connections("inet"):
                                        if connection.status == psutil.CONN_LISTEN and not ip_address(connection.laddr.ip).is_loopback:
                                            violations.append({"pid": process.pid, "address": connection.laddr.ip, "port": connection.laddr.port})
                                            effective_cancel.set()
                                except psutil.NoSuchProcess:
                                    continue
                    except psutil.NoSuchProcess:
                        pass
                    except psutil.AccessDenied:
                        violations.append({"reason": "listener_inspection_denied"})
                        effective_cancel.set()
                try:
                    await asyncio.wait_for(monitor_stop.wait(), timeout=.05)
                except TimeoutError:
                    pass
        watcher = asyncio.create_task(monitor())
        try:
            result = await super().execute(wrapped, output_dir, limits, effective_cancel, started)
        finally:
            monitor_stop.set()
            await watcher
            await self.sandbox.release(evidence)
        evidence.update(listen_address_enforcement="observed", non_loopback_listeners=violations)
        if violations:
            result.execution_status, result.reason = "error", "non_loopback_listener_or_inspection_unavailable"
        # Preserve controller-measured evidence after project processes have stopped.
        atomic_json(output_dir / "isolation.json", evidence)
        if result.cleanup_verified:
            shutil.rmtree(private_temp)
        return result

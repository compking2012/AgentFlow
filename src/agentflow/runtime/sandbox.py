"""Measured OS isolation. Unsupported hosts fail closed, never downgrade to cwd."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
import platform
import shutil
import socket
import sys
from pathlib import Path
from uuid import uuid4

from agentflow.common import DomainError, canonical_digest, utc_now

from .contracts import TaskEnvelope
from .launcher import atomic_json


class MacSeatbeltSandbox:
    PROBE_VERSION = 2
    DENIED_EXIT = 77

    def __init__(self, state_dir: Path, *, probe_timeout_seconds: float = 30):
        if (type(probe_timeout_seconds) not in {int, float} or not math.isfinite(probe_timeout_seconds)
                or not 0 < probe_timeout_seconds <= 120):
            raise ValueError('probe_timeout_seconds must be finite and between 0 and 120 seconds')
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.probe_timeout_seconds = float(probe_timeout_seconds)

    @staticmethod
    def _quote(value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise DomainError("unsafe_path", "Control characters cannot occur in sandbox paths", 422)
        return json.dumps(value, ensure_ascii=False)

    def policy(self, read_roots: list[Path], write_roots: list[Path], allowed_ports: list[int],
               web_addresses: list[str] | None = None) -> str:
        system = [Path(p) for p in ["/System", "/usr", "/bin", "/sbin", "/Library/Apple",
                  "/private/var/db/dyld", "/private/etc/ssl", "/private/etc/apache2/mime.types",
                  "/private/etc/mime.types", "/private/etc/hosts", "/private/etc/resolv.conf"]]
        # prepare() freezes canonical read roots and exact lexical write scopes.
        # Never resolve a later symlink into a new, wider authorization here.
        readers = sorted({os.path.abspath(p) for p in [*system, *read_roots, *write_roots]})
        writers = sorted({os.path.abspath(p) for p in write_roots})
        rules = [
            "(version 1)", "(deny default)", "(allow process*)", "(allow signal (target children))", "(allow sysctl-read)",
            "(allow mach-lookup)", "(allow file-read-metadata)",
            '(allow file-read-data (literal "/"))',
            "(allow file-read* " + " ".join(f"(subpath {self._quote(p)})" for p in readers) + ")",
            "(allow file-write* " + " ".join(f"(subpath {self._quote(p)})" for p in writers) + ")",
            '(allow file-read* file-write* (literal "/dev/null") (literal "/dev/random") (literal "/dev/urandom"))',
        ]
        for port in allowed_ports:
            if not 1 <= port <= 65535:
                raise DomainError("unsafe_network_policy", "Invalid sandbox port", 422)
            rules.append(f'(allow network-outbound (remote ip "localhost:{port}"))')
        for address in sorted(set(web_addresses or [])):
            parsed = ipaddress.ip_address(address)
            if not parsed.is_global:
                raise DomainError("unsafe_network_policy", "Research destinations must be global IPs", 403)
            authority = f"[{address}]" if parsed.version == 6 else address
            for port in (80, 443):
                rules.append(f'(allow network-outbound (remote ip "{authority}:{port}"))')
        return "\n".join(rules)

    async def resolve_web_hosts(self, task: TaskEnvelope) -> dict[str, list[str]]:
        # Public research is mediated by the authenticated controller broker.
        # No host/IP supplied by a role grants direct OS network egress, even
        # when loading a legacy task with a populated resolved_web_hosts map.
        return {}

    async def prepare(self, task: TaskEnvelope, executable: Path, private_home: Path, *,
                      runtime_read_roots: tuple[Path, ...] = ()) -> tuple[list[str], dict]:
        if platform.system() != "Darwin" or not shutil.which("sandbox-exec"):
            raise DomainError("isolation_unverified", "A verified controller sandbox is unavailable; execution blocked")
        from urllib.parse import urlsplit

        # Complete asynchronous host work before validating filesystem bindings.
        task.resolved_web_hosts = await self.resolve_web_hosts(task)
        task.assert_paths()
        task.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        reads = [task.workspace, task.artifact_dir, private_home, executable.parent,
                 Path(sys.prefix), Path(sys.base_prefix), *task.allowed_read_roots, *runtime_read_roots]
        reads = [path.resolve() for path in reads]
        writes = [task.artifact_dir.resolve(), private_home.resolve(), *task.allowed_write_roots]
        if task.allow_code_write:
            writes.append(task.workspace / ".git")
        for allowed in [*reads, *writes]:
            if any(allowed.resolve() == p.resolve() or p.resolve().is_relative_to(allowed.resolve())
                   or allowed.resolve().is_relative_to(p.resolve())
                   for p in task.protected_roots):
                raise DomainError("unsafe_sandbox_roots", "Sandbox allowed root includes protected state", 403)
        if task.allow_code_write:
            from .write_scope import prepare_write_parents
            prepare_write_parents(task.workspace, task.allowed_write_roots)
        parsed = urlsplit(task.proxy_base_url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        policy = self.policy(reads, writes, [port], [ip for values in task.resolved_web_hosts.values() for ip in values])
        identity = canonical_digest({"policy": policy, "executable": str(executable.resolve())})
        profile_path = self.state_dir / f"{identity.split(':')[1]}.sb"
        profile_path.write_text(policy)
        os.chmod(profile_path, 0o600)
        evidence = await self._probe(profile_path, task, private_home, reads, writes)
        binding = {'probe_version': self.PROBE_VERSION, 'phase': 'before_agent_launch',
            'attempt_id': task.attempt_id, 'run_id': task.run_id, 'iteration_id': task.iteration_id,
            'work_item_id': task.work_item_id, 'fencing_token': task.fencing_token,
            'input_fingerprint': task.input_fingerprint, 'policy_fingerprint': identity}
        evidence.update(**binding, probe_binding_fingerprint=canonical_digest(binding),
                        probe_timeout_seconds=self.probe_timeout_seconds,
                        checked_at=utc_now(), platform=platform.platform())
        atomic_json(profile_path.with_suffix(".probe.json"), evidence)
        if not evidence["verified"]:
            message = ('Filesystem/network isolation probe timed out; execution blocked'
                       if evidence['failure_code'] == 'isolation_probe_timeout'
                       else 'Filesystem/network isolation probe failed; execution blocked')
            raise DomainError(evidence['failure_code'], message, 409, evidence)
        return ["/usr/bin/sandbox-exec", "-f", str(profile_path)], evidence

    async def _run_probe(self, profile: Path, script: str) -> int:
        try:
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/sandbox-exec", "-f", str(profile), sys.executable, "-I", "-S", "-c", script,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                cwd="/", env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
        except OSError as error:
            raise DomainError('isolation_unverified', 'Sandbox probe could not start; execution blocked', 409,
                              {'outcome': 'start_error', 'error_type': type(error).__name__}) from error
        try:
            return await asyncio.wait_for(process.wait(), timeout=self.probe_timeout_seconds)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            raise DomainError('isolation_probe_timeout', 'Sandbox probe exceeded its bounded wait; execution blocked', 409,
                              {'outcome': 'timeout', 'timed_out': True,
                               'timeout_seconds': self.probe_timeout_seconds}) from None
        except asyncio.CancelledError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            raise

    @classmethod
    def _denial_probe(cls, operation):
        return ('import os\ntry:\n' + '\n'.join('    ' + line for line in operation.splitlines())
                + f'\nexcept PermissionError:\n    os._exit({cls.DENIED_EXIT})\nos._exit(0)')

    async def _probe(self, profile_path, task, private_home, reads, writes):
        suffix = uuid4().hex
        readable = task.artifact_dir / ('.agentflow-sandbox-probe-' + suffix)
        forbidden = self.state_dir / (profile_path.stem + '.' + suffix + '.forbidden')
        workspace_write = task.workspace / ('.agentflow-readonly-probe-' + suffix)
        results = {}
        sockets = []

        async def measure(name, script, expected, profile=profile_path):
            timed_out = False
            try:
                code = await self._run_probe(profile, script)
                outcome = 'permission_denied' if code == self.DENIED_EXIT else 'completed' if code == 0 else 'exit_error'
            except DomainError as error:
                timed_out = error.code == 'isolation_probe_timeout'
                code, outcome = (-1, 'timeout') if timed_out else (-2, 'start_error')
            results[name] = {'exit_code': code, 'expected_exit_code': expected, 'outcome': outcome,
                             'timed_out': timed_out, 'passed': not timed_out and code == expected}

        try:
            readable.write_text('probe')
            forbidden.write_text('probe')
            await measure('allowed_workspace_probe',
                f"import os\nf=os.open({str(readable)!r},os.O_RDWR);assert os.read(f,5)==b'probe';os.write(f,b'ok');os.close(f)", 0)
            await measure('protected_read_probe', self._denial_probe(
                f"f=os.open({str(forbidden)!r},os.O_RDONLY);os.read(f,1);os.close(f)"), self.DENIED_EXIT)
            await measure('protected_write_probe', self._denial_probe(
                f"f=os.open({str(forbidden)!r},os.O_WRONLY);os.close(f)"), self.DENIED_EXIT)
            # Read the real workspace, independently of the writable artifact directory.
            workspace_read = f"import os\nos.listdir({str(task.workspace)!r})"
            with os.scandir(task.workspace) as entries:
                source = next((entry.path for entry in entries if not entry.name.startswith('.')
                               and entry.is_file(follow_symlinks=False)), None)
            if source:
                workspace_read += f"\nf=os.open({source!r},os.O_RDONLY);os.read(f,1);os.close(f)"
            await measure('workspace_read_probe', workspace_read, 0)
            if task.allow_code_write:
                from .write_scope import coding_write_probe
                coding_profile = profile_path.with_name(profile_path.stem + '.' + suffix + '.coding.sb')
                try:
                    with coding_write_probe(task.allowed_write_roots, suffix) as (script, staging):
                        policy = profile_path.read_text()
                        if staging:
                            policy += '\n(allow file-read* file-write* ' + ' '.join(
                                f'(literal {self._quote(str(path))})' for path in staging) + ')'
                        coding_profile.write_text(policy)
                        await measure('coding_source_write_probe', script, 0, coding_profile)
                finally:
                    coding_profile.unlink(missing_ok=True)
            if not task.allow_code_write:
                operation = (f"f=os.open({source!r},os.O_WRONLY);os.close(f)" if source else
                    f"f=os.open({str(workspace_write)!r},os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(f)")
                await measure('readonly_workspace_write_probe', self._denial_probe(operation), self.DENIED_EXIT)
            # Local TCP listeners prove port-specific allow/deny without model requests.
            for _ in range(2):
                listener = socket.socket()
                listener.bind(('127.0.0.1', 0))
                listener.listen(4)
                sockets.append(listener)
            network_profile = profile_path.with_suffix(".network.sb")
            network_profile.write_text(self.policy(reads, writes, [sockets[0].getsockname()[1]]))
            for name, listener, expected in [('allowed_port_probe', sockets[0], 0),
                                              ('denied_port_probe', sockets[1], self.DENIED_EXIT)]:
                operation = f"import socket\ns=socket.create_connection(('127.0.0.1',{listener.getsockname()[1]}),timeout=5);s.close()"
                await measure(name, self._denial_probe(operation) if expected == self.DENIED_EXIT else operation,
                              expected, network_profile)
        finally:
            for listener in sockets:
                listener.close()
            readable.unlink(missing_ok=True)
            forbidden.unlink(missing_ok=True)
            try:
                workspace_write.lstat()
            except FileNotFoundError:
                pass
            else:
                workspace_write.unlink()
        verified = all(result['passed'] for result in results.values())
        return {
            'verified': verified,
            **{name: result['exit_code'] for name, result in results.items()},
            'readonly_workspace_write_probe': results.get('readonly_workspace_write_probe', {}).get('exit_code'),
            'probe_results': results,
            'failure_code': None if verified else 'isolation_probe_timeout'
                if any(result['timed_out'] for result in results.values()) else 'isolation_unverified',
            "filesystem": "hard", "network": "hard", "process_tree": "observed",
            "note": "POSIX process groups do not establish hard containment of arbitrary detached descendants.",
        }


def default_sandbox(state_dir: Path, *, probe_timeout_seconds: float = 30) -> MacSeatbeltSandbox:
    # A different controller sandbox must implement and pass the same probes;
    # unavailable Linux/Windows enforcement never becomes an unsandboxed fallback.
    return MacSeatbeltSandbox(state_dir, probe_timeout_seconds=probe_timeout_seconds)

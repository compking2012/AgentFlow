import asyncio
import json
import os
import platform
import shlex
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agentflow.adapters.codex import CodexExecAdapter
from agentflow.common import DomainError
from agentflow.runtime.coding_tools import discover_coding_tools
from agentflow.runtime.sandbox import MacSeatbeltSandbox


async def coding_launch(task, tmp_path):
    supervisor = SimpleNamespace(root=tmp_path / 'runtime' / 'supervisor',
                                 start=AsyncMock(side_effect=lambda launch: launch))
    sandbox = MacSeatbeltSandbox(tmp_path / 'sandbox')
    adapter = CodexExecAdapter(supervisor, sandbox, '/usr/bin/true')
    # Skip the external Codex CLI only; exercise adapter configuration and real OS isolation.
    adapter.probe = AsyncMock(return_value={'available': True, 'version': 'fixture'})
    adapter._help_text = AsyncMock(return_value='')
    writable = task.workspace / 'editable'
    writable.mkdir()
    task = task.model_copy(update={'allow_code_write': True, 'allowed_write_roots': [writable]})
    return await adapter.start(task)


def shell_environment(launch):
    overrides = {}
    for index, value in enumerate(launch.argv):
        if value == '-c':
            key, value = launch.argv[index + 1].split('=', 1)
            if key.startswith('shell_environment_policy.set.'):
                overrides[key.rsplit('.', 1)[1]] = json.loads(value)
    return overrides


async def run_shell(launch, command, *, shell=('/bin/sh', '-c')):
    process = await asyncio.create_subprocess_exec(
        *launch.argv[:3], *shell, command, cwd=launch.cwd,
        env=shell_environment(launch), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    return process.returncode, stdout.decode(), stderr.decode()


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Requires real macOS Seatbelt')
async def test_codex_shell_resolves_installed_tools_without_exposing_unrelated_path(task, tmp_path, monkeypatch):
    runtime = tmp_path / 'tool-installation'
    binaries = runtime / 'bin'
    binaries.mkdir(parents=True)
    links = tmp_path / 'local-bin'
    links.mkdir()
    for name in ('node', 'git'):
        executable = binaries / name
        executable.write_text(f'#!/bin/sh\nprintf "fixture-{name}\\n"\n')
        executable.chmod(0o755)
        (links / name).symlink_to(executable)
    protected = tmp_path / 'protected'
    protected.mkdir()
    (protected / 'private.txt').write_text('synthetic protected fixture')
    (links / 'private.txt').write_text('unrelated PATH directory fixture')
    (runtime / 'private.txt').write_text('unrelated runtime sibling fixture')
    library = runtime / 'lib'
    library.mkdir()
    (library / 'support.txt').write_text('runtime support fixture')
    monkeypatch.setenv('PATH', f'{links}:/usr/bin:/bin')
    launch = await coding_launch(task.model_copy(update={'protected_roots': [protected]}), tmp_path)
    status, stdout, stderr = await run_shell(launch, 'node --version && git --version')
    assert (status, stdout) == (0, 'fixture-node\nfixture-git\n'), stderr
    for variable in ('PATH', 'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL'):
        assert launch.environment[variable] == shell_environment(launch)[variable]
    for forbidden in (protected / 'private.txt', links / 'private.txt', runtime / 'private.txt'):
        status, _, _ = await run_shell(launch, f'/bin/cat {shlex.quote(str(forbidden))} >/dev/null')
        assert status != 0
    status, _, _ = await run_shell(launch, 'echo forbidden > original.py')
    assert status != 0
    assert (task.workspace / 'original.py').read_text() == 'ORIGINAL = True\n'
    status, stdout, stderr = await run_shell(launch, f'/bin/cat {shlex.quote(str(library / "support.txt"))}')
    assert (status, stdout) == (0, 'runtime support fixture'), stderr
    for readonly in (binaries / 'node', library / 'support.txt'):
        status, _, _ = await run_shell(launch, f'echo forbidden > {shlex.quote(str(readonly))}')
        assert status != 0
    assert (library / 'support.txt').read_text() == 'runtime support fixture'


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Requires real macOS Seatbelt')
@pytest.mark.parametrize('shell', [('/bin/sh', '-c'), ('/bin/zsh', '-lc')])
async def test_actual_node_syntax_checks_and_git_run_inside_codex_sandbox(task, tmp_path, shell):
    if not shutil.which('node') or not shutil.which('git'):
        pytest.skip('Requires installed Node and Git')
    (task.workspace / 'valid.js').write_text('const value = 1;\n')
    (task.workspace / 'invalid.js').write_text('const value = ;\n')
    launch = await coding_launch(task, tmp_path)
    status, _, stderr = await run_shell(launch, 'node --check valid.js', shell=shell)
    assert status == 0, stderr
    status, _, stderr = await run_shell(launch, 'node --check invalid.js', shell=shell)
    assert status != 0 and 'SyntaxError' in stderr
    status, stdout, stderr = await run_shell(launch, 'command -v git', shell=shell)
    assert (status, stdout.strip()) == (0, str(Path(shutil.which('git')).resolve())), stderr
    status, stdout, stderr = await run_shell(launch, 'git --version', shell=shell)
    assert status == 0 and stdout.startswith('git version '), stderr
    environment = shell_environment(launch)
    assert str(Path.home()) not in environment['PATH'].split(os.pathsep)
    profile = Path(launch.argv[2])
    evidence = json.loads(profile.with_suffix('.probe.json').read_text())
    assert evidence['verified']
    assert evidence['protected_read_probe'] == evidence['protected_write_probe'] == 77
    assert evidence['denied_port_probe'] == 77


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Requires real macOS Seatbelt')
async def test_zsh_multiline_edit_uses_authorized_private_temporary_directory(task, tmp_path):
    launch = await coding_launch(task, tmp_path)
    content = "const message = '你好';\n" + "// preserve 'quotes', $literal and \\\"text\\\"\n" * 1000
    command = "cat > editable/from-heredoc.mjs <<'AGENTFLOW_TEXT'\n" + content + 'AGENTFLOW_TEXT\n'
    status, _, stderr = await run_shell(launch, command, shell=('/bin/zsh', '-lc'))
    assert status == 0, stderr
    assert (task.workspace / 'editable/from-heredoc.mjs').read_text() == content
    environment = shell_environment(launch)
    assert environment['TMPPREFIX'] == launch.environment['TMPPREFIX']
    assert Path(environment['TMPPREFIX']).parent == Path(environment['TMPDIR'])
    status, _, _ = await run_shell(launch, 'echo forbidden > original.py', shell=('/bin/zsh', '-lc'))
    assert status != 0 and (task.workspace / 'original.py').read_text() == 'ORIGINAL = True\n'


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Requires macOS policy preparation')
async def test_coding_runtime_roots_cannot_overlap_protected_state(task, tmp_path, monkeypatch):
    installation = tmp_path / 'installation'
    binary = installation / 'bin' / 'node'
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\nexit 0\n')
    binary.chmod(0o755)
    protected = installation / 'lib' / 'controller'
    protected.mkdir(parents=True)
    monkeypatch.setenv('PATH', f'{binary.parent}:/usr/bin:/bin')
    with pytest.raises(DomainError) as error:
        await coding_launch(task.model_copy(update={'protected_roots': [protected]}), tmp_path)
    assert error.value.code == 'unsafe_sandbox_roots'


def test_coding_discovery_rejects_executable_directly_in_owner_home(tmp_path, monkeypatch):
    binary = tmp_path / 'node'
    binary.write_text('#!/bin/sh\nexit 0\n')
    binary.chmod(0o755)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('PATH', str(tmp_path))
    with pytest.raises(DomainError) as error:
        discover_coding_tools()
    assert error.value.code == 'unsafe_sandbox_roots'


def test_no_installed_coding_tools_retains_only_system_path(monkeypatch):
    monkeypatch.setenv('PATH', '')
    tools = discover_coding_tools()
    assert tools.path == '/usr/bin:/bin:/usr/sbin:/sbin'
    assert tools.read_roots == ()


def test_runtime_library_symlink_cannot_grant_the_owner_home(tmp_path, monkeypatch):
    home = tmp_path / 'owner-home'
    home.mkdir()
    installation = tmp_path / 'installation'
    binary = installation / 'bin' / 'node'
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\nexit 0\n')
    binary.chmod(0o755)
    (installation / 'lib').symlink_to(home, target_is_directory=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('PATH', str(binary.parent))
    with pytest.raises(DomainError) as error:
        discover_coding_tools()
    assert error.value.code == 'unsafe_sandbox_roots'


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Requires real macOS Seatbelt')
async def test_coding_context_environment_reads_exact_authorized_directory_and_stays_readonly(task, tmp_path, monkeypatch):
    context = tmp_path / 'stage context' / ('a1' * 32)
    context.mkdir(parents=True)
    document = context / 'input.json'
    document.write_text('{"test_cases":["case-1"]}\n')
    sibling = context.parent / 'unrelated.txt'
    sibling.write_text('unrelated owner data')
    monkeypatch.setenv('AGENTFLOW_CONTEXT_DIR', str(context.parent))
    scoped = task.model_copy(update={'context_directory': context, 'allowed_read_roots': [context]})
    launch = await coding_launch(scoped, tmp_path)
    status, stdout, stderr = await run_shell(launch, 'cat "$AGENTFLOW_CONTEXT_DIR/input.json"')
    assert (status, stdout) == (0, '{"test_cases":["case-1"]}\n'), stderr
    assert launch.environment['AGENTFLOW_CONTEXT_DIR'] == str(context)
    status, _, _ = await run_shell(launch, 'echo overwritten > "$AGENTFLOW_CONTEXT_DIR/input.json"')
    assert status != 0 and document.read_text() == '{"test_cases":["case-1"]}\n'
    status, _, _ = await run_shell(launch, 'cat "$AGENTFLOW_CONTEXT_DIR/../unrelated.txt"')
    assert status != 0

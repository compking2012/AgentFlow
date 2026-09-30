"""Coding file scopes must work before a paid Agent starts, without granting siblings."""
import asyncio
import os
import platform
import sys
from pathlib import Path

import pytest

from agentflow.common import DomainError
from agentflow.runtime.sandbox import MacSeatbeltSandbox


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Real macOS sandbox boundary')
async def test_new_nested_file_scopes_are_writable_but_sibling_modules_are_not(task, tmp_path):
    first = task.workspace / 'public/shell/api-client.mjs'
    second = task.workspace / 'public/apps/calculator.mjs'
    icons = task.workspace / 'public/apps/icons'
    coding = task.model_copy(update={'allow_code_write': True, 'allowed_write_roots': [first, second, icons]})
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator')
    prefix, evidence = await sandbox.prepare(coding, Path(sys.executable).resolve(), tmp_path / 'private')
    script = f'''from pathlib import Path
first, second, icons = map(Path, {[str(first), str(second), str(icons)]!r})
first.write_text('export const api = true;')
second.write_text('export const calculator = true;')
icons.mkdir()
(icons / 'calculator.svg').write_text('<svg/>')
for name in {[str(first.parent / 'other.mjs'), str(second.parent / 'other.mjs'), str(task.workspace / 'outside.txt')]!r}:
    try:
        Path(name).write_text('unauthorized')
        raise AssertionError('Sibling scope was writable')
    except PermissionError:
        pass
'''
    assert await sandbox._run_probe(Path(prefix[-1]), script) == 0
    assert evidence['probe_results']['coding_source_write_probe']['passed']
    assert first.read_text() == 'export const api = true;'
    assert not list(task.workspace.rglob('.agentflow-coding-probe-*'))


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Real macOS sandbox boundary')
async def test_broken_source_write_policy_fails_preflight_without_touching_existing_code(task, tmp_path, monkeypatch):
    source = task.workspace / 'original.py'
    coding = task.model_copy(update={'allow_code_write': True, 'allowed_write_roots': [source]})
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator')
    original = sandbox.policy
    def without_source(reads, writes, ports, web_addresses=None):
        return original(reads, [p for p in writes if p != source], ports, web_addresses)
    monkeypatch.setattr(sandbox, 'policy', without_source)
    with pytest.raises(DomainError) as error:
        await sandbox.prepare(coding, Path(sys.executable).resolve(), tmp_path / 'private')
    assert error.value.code == 'isolation_unverified'
    assert not error.value.details['probe_results']['coding_source_write_probe']['passed']
    assert source.read_text() == 'ORIGINAL = True\n'


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Real macOS sandbox boundary')
async def test_preparing_file_scope_rejects_symlinked_parent_without_creating_target_dirs(task, tmp_path):
    sibling = task.workspace / 'other-module'
    sibling.mkdir()
    (task.workspace / 'alias').symlink_to(sibling, target_is_directory=True)
    coding = task.model_copy(update={'allow_code_write': True,
        'allowed_write_roots': [task.workspace / 'alias/new-dir/module.mjs']})
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator')
    with pytest.raises(DomainError):
        await sandbox.prepare(coding, Path(sys.executable).resolve(), tmp_path / 'private')
    assert not (sibling / 'new-dir').exists()


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Real macOS sandbox boundary')
@pytest.mark.parametrize('scope_name', ['new.py', 'icons'])
@pytest.mark.parametrize('interruption', ['timeout', 'cancel'])
async def test_interrupted_coding_probe_removes_its_source_file(task, tmp_path, monkeypatch,
                                                              scope_name, interruption):
    source = task.workspace / 'nested' / scope_name
    original = task.workspace / 'original.py'
    coding = task.model_copy(update={'allow_code_write': True, 'allowed_write_roots': [original, source]})
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator', probe_timeout_seconds=0.5)
    run_probe = sandbox._run_probe

    async def block_fsync(profile, script):
        if str(source) in script and 'fsync' in script:
            script = 'import os, time\nos.fsync = lambda fd: time.sleep(10)\n' + script
        return await run_probe(profile, script)

    monkeypatch.setattr(sandbox, '_run_probe', block_fsync)
    preparing = asyncio.create_task(sandbox.prepare(coding, Path(sys.executable).resolve(), tmp_path / 'private'))
    if interruption == 'cancel':
        async with asyncio.timeout(3):
            while not source.exists():
                await asyncio.sleep(0.01)
        preparing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await preparing
    else:
        with pytest.raises(DomainError) as failure:
            await preparing
        assert failure.value.code == 'isolation_probe_timeout'
    assert not source.exists()
    assert original.read_text() == 'ORIGINAL = True\n'
    assert not list(task.workspace.rglob('.agentflow-coding-*'))


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Real macOS sandbox boundary')
async def test_path_swap_during_async_preflight_cannot_authorize_outside_workspace(task, tmp_path, monkeypatch):
    outside = tmp_path / 'outside'
    outside.mkdir()
    parent = task.workspace / 'module'
    parent.mkdir()
    source = parent / 'code.py'
    coding = task.model_copy(update={'allow_code_write': True, 'allowed_write_roots': [source]})
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator')

    async def replace_parent(_):
        parent.rmdir()
        parent.symlink_to(outside, target_is_directory=True)
        return {}

    monkeypatch.setattr(sandbox, 'resolve_web_hosts', replace_parent)
    with pytest.raises(DomainError):
        await sandbox.prepare(coding, Path(sys.executable).resolve(), tmp_path / 'private')
    assert not list(outside.iterdir())


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Real macOS sandbox boundary')
async def test_policy_does_not_resolve_a_scope_replaced_after_validation(task, tmp_path):
    source = task.workspace / 'original.py'
    outside = tmp_path / 'outside.py'
    outside.write_text('protected')
    source.unlink()
    source.symlink_to(outside)
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator')
    profile = tmp_path / 'frozen.sb'
    profile.write_text(sandbox.policy([task.workspace, Path(sys.prefix), Path(sys.base_prefix)], [source], []))
    assert await sandbox._run_probe(profile, f'from pathlib import Path\nPath({str(outside)!r}).write_text("escaped")') != 0
    assert outside.read_text() == 'protected'


def test_parent_renamed_outside_workspace_is_rejected_before_creating_descendants(task, tmp_path, monkeypatch):
    from agentflow.runtime.write_scope import prepare_write_parents

    parent = task.workspace / 'module'
    parent.mkdir()
    outside = tmp_path / 'moved'
    original_open = os.open
    moved = False

    def replace_after_open(path, flags, *args, **kwargs):
        nonlocal moved
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == 'module' and kwargs.get('dir_fd') is not None and not moved:
            parent.rename(outside)
            moved = True
        return descriptor

    monkeypatch.setattr(os, 'open', replace_after_open)
    with pytest.raises(DomainError):
        prepare_write_parents(task.workspace, [parent / 'new-dir' / 'code.py'])
    assert not (outside / 'new-dir').exists()


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Real macOS sandbox boundary')
@pytest.mark.parametrize('replacement', ['file', 'symlink', 'hardlink', 'changed_token'])
async def test_probe_cleanup_preserves_replaced_source(task, tmp_path, replacement):
    from agentflow.runtime.write_scope import coding_write_probe

    source = task.workspace / 'new.py'
    outside = tmp_path / 'user.py'
    outside.write_text('user code')
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator')
    with coding_write_probe([source], 'replacement') as (script, staging):
        profile = tmp_path / 'probe.sb'
        profile.write_text(sandbox.policy([task.workspace, Path(sys.prefix), Path(sys.base_prefix)],
                                          [source, *staging], []))
        assert await sandbox._run_probe(profile, script) == 0
        if replacement == 'changed_token':
            source.write_text('user code')
        else:
            source.unlink()
            if replacement == 'file':
                source.write_text('user code')
            elif replacement == 'symlink':
                source.symlink_to(outside)
            else:
                os.link(outside, source)
    assert source.read_text() == 'user code'
    assert outside.read_text() == 'user code'
    assert not list(task.workspace.glob('.agentflow-coding-*'))


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Real macOS sandbox boundary')
@pytest.mark.parametrize('fault', ['exit_after_link', 'cross_device'])
async def test_probe_create_fault_is_fail_closed_without_residue(task, tmp_path, monkeypatch, fault):
    source = task.workspace / 'new.py'
    original = task.workspace / 'original.py'
    coding = task.model_copy(update={'allow_code_write': True, 'allowed_write_roots': [source]})
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator')
    run_probe = sandbox._run_probe

    async def failed_link(profile, script):
        if str(source) in script and 'fsync' in script:
            if fault == 'exit_after_link':
                prefix = ('import os\nreal_link = os.link\n'
                          'def failed_link(*args, **kwargs):\n'
                          '    real_link(*args, **kwargs)\n    os._exit(6)\n')
            else:
                prefix = ('import os, errno\ndef failed_link(*args, **kwargs):\n'
                          '    raise OSError(errno.EXDEV, "Injected cross-device failure")\n')
            script = prefix + 'os.link = failed_link\n' + script
        return await run_probe(profile, script)

    monkeypatch.setattr(sandbox, '_run_probe', failed_link)
    with pytest.raises(DomainError) as failure:
        await sandbox.prepare(coding, Path(sys.executable).resolve(), tmp_path / 'private')
    assert failure.value.code == 'isolation_unverified'
    assert not source.exists()
    assert original.read_text() == 'ORIGINAL = True\n'
    assert not list(task.workspace.rglob('.agentflow-coding-*'))


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Real macOS sandbox boundary')
async def test_duplicate_scopes_remain_writable(task, tmp_path):
    source = task.workspace / 'new.py'
    coding = task.model_copy(update={'allow_code_write': True, 'allowed_write_roots': [source, source]})
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator')
    _, evidence = await sandbox.prepare(coding, Path(sys.executable).resolve(), tmp_path / 'private')
    assert evidence['verified']
    assert not source.exists()


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Real macOS sandbox boundary')
async def test_failed_cleanup_blocks_launch_instead_of_accepting_probe_residue(task, tmp_path, monkeypatch):
    source = task.workspace / 'new.py'
    coding = task.model_copy(update={'allow_code_write': True, 'allowed_write_roots': [source]})
    sandbox = MacSeatbeltSandbox(tmp_path / 'isolator')
    unlink = os.unlink

    def deny_probe_removal(path, *args, **kwargs):
        if path == source.name and kwargs.get('dir_fd') is not None:
            raise PermissionError('Injected delete denial')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', deny_probe_removal)
    with pytest.raises(DomainError) as failure:
        await sandbox.prepare(coding, Path(sys.executable).resolve(), tmp_path / 'private')
    assert failure.value.code == 'coding_workspace_unwritable'
    assert not list(task.workspace.glob('.agentflow-coding-*'))

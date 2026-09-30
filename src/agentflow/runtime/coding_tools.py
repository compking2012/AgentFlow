"""Expose selected local coding runtimes without inheriting the owner's PATH."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from agentflow.common import DomainError

SYSTEM_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'


@dataclass(frozen=True)
class CodingTools:
    path: str
    read_roots: tuple[Path, ...]


def discover_coding_tools() -> CodingTools:
    """Resolve only supported tools, including symlinked user installations.

    Runtime resources sit beside bin in lib/libexec/share. Homebrew libraries
    may depend on other formulae, so its package store is readable, while its
    configuration directory and the owner's home are never added as roots.
    All these roots still pass the sandbox's protected-state overlap checks.
    """
    directories: list[str] = []
    roots: set[Path] = set()
    owner_home = Path.home().resolve()
    for name in ('node', 'git'):
        found = shutil.which(name)
        if found is None:
            continue
        executable = Path(found).resolve(strict=True)
        directory = executable.parent
        directories.append(str(directory))
        roots.add(directory)
        if directory.name == 'bin':
            for resource in ('lib', 'libexec', 'share'):
                support = directory.parent / resource
                if support.is_dir():
                    roots.add(support.resolve())
        for cellar in (Path('/opt/homebrew/Cellar'), Path('/usr/local/Cellar')):
            if executable.is_relative_to(cellar.resolve()):
                roots.add(cellar.resolve())
    if any(root == owner_home or root in owner_home.parents for root in roots):
        raise DomainError('unsafe_sandbox_roots', 'Coding tool root would expose an owner directory', 403)
    path = os.pathsep.join(dict.fromkeys([*directories, *SYSTEM_PATH.split(os.pathsep)]))
    return CodingTools(path=path, read_roots=tuple(sorted(roots)))

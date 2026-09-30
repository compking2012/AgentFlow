"""Reuse only exact public tarball bytes already present in local npm caches.

The real isolated npm ci still validates the lock and installs every package.
No node_modules directory, command result, test result or quality state is copied.
"""
import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit


def seed_locked_npm_cache(destination: Path, source_caches: list[Path], lockfiles: list[Path]):
    copied = []
    for lock in lockfiles:
        for name, package in json.loads(lock.read_text()).get('packages', {}).items():
            url = urlsplit(package.get('resolved', ''))
            integrity = package.get('integrity', '')
            if (url.scheme != 'https' or url.hostname != 'registry.npmjs.org'
                    or url.username or url.password or url.query or not integrity.startswith('sha512-')):
                continue
            try:
                checksum = base64.b64decode(integrity[7:], validate=True)
            except ValueError:
                continue
            if len(checksum) != 64:
                continue
            hexadecimal = checksum.hex()
            relative = Path('_cacache/content-v2/sha512') / hexadecimal[:2] / hexadecimal[2:4] / hexadecimal[4:]
            target = destination / relative
            if target.exists() or target.is_symlink():
                continue
            for cache in source_caches:
                source = cache / relative
                if not source.is_file() or source.is_symlink() or source.stat().st_size > 32 * 1024 * 1024:
                    continue
                data = source.read_bytes()
                if hashlib.sha512(data).digest() != checksum:
                    continue
                target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                with target.open('xb') as file:
                    file.write(data)
                copied.append(name)
                break
    return copied

"""Read-only structural verification of additive changes to existing test cases.

This guard authenticates the transformation, not coverage quality or path ownership.
The caller binds paths to the original test author and supplies independent review.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from agentflow.control.test_migration_guard import TestMigrationGuard


class TestCoverageGuard(TestMigrationGuard):
    __test__ = False

    @staticmethod
    def _files(root: Path) -> dict[str, tuple[str, bytes]]:
        # Migration's snapshots distinguish executable files; coverage also binds
        # every permission bit, including changes that leave a file non-executable.
        files = TestMigrationGuard._files(root)
        return {name: (f'{kind}:{stat.S_IMODE(os.lstat(root / name).st_mode):04o}', content)
                for name, (kind, content) in files.items()}

    def verify(self, before_repo: str | Path, after_repo: str | Path, paths: list[str]) -> dict[str, Any]:
        report: dict[str, Any] = {'ok': False, 'errors': [], 'evidence': [], 'affected_case_ids': [],
                                  'requires_independent_review': True}
        try:
            before_root, after_root = Path(before_repo), Path(after_repo)
            if not before_root.is_dir() or not after_root.is_dir():
                raise ValueError('Both repository roots must exist')
            before, after = self._files(before_root), self._files(after_root)
            report.update(before_digest=self._digest(before), after_digest=self._digest(after))
            if before.keys() != after.keys():
                raise ValueError('File additions/deletions are forbidden in test coverage extension')
            if not isinstance(paths, list) or not paths:
                raise ValueError('At least one authorized existing test path is required')
            allowed = set()
            for path in paths:
                if (not isinstance(path, str) or not path or '\\' in path or Path(path).is_absolute()
                        or '..' in Path(path).parts or Path(path).as_posix() != path):
                    raise ValueError('Coverage path must be a canonical repository-relative file')
                if path in allowed:
                    raise ValueError(f'Duplicate coverage path: {path}')
                if path not in before or before[path][0].startswith('symlink:'):
                    raise ValueError(f'Coverage file is missing or a symlink: {path}')
                allowed.add(path)
            for path, old_entry in before.items():
                if old_entry[0] != after[path][0]:
                    raise ValueError(f'File mode or symlink change forbidden: {path}')
                if path not in allowed and old_entry != after[path]:
                    raise ValueError(f'Unrelated file change forbidden: {path}')
            for path in sorted(allowed):
                evidence = self._parse(operation='coverage', file=path,
                                       before=before[path][1].decode('utf-8'),
                                       after=after[path][1].decode('utf-8'))
                if not evidence.get('ok'):
                    report['errors'].extend(f'{path}: {error}' for error in evidence.get('errors', []))
                    if not evidence.get('errors'):
                        report['errors'].append(f'{path}: Coverage parser rejected the transformation')
                    continue
                report['evidence'].append(evidence['evidence'])
                if before[path] != after[path]:
                    report['affected_case_ids'].extend(evidence['evidence']['case_ids'])
            report['affected_case_ids'] = sorted(set(report['affected_case_ids']))
            report['semantic_protection'] = ['standard_parser', 'file_set_and_modes_preserved',
                'unrelated_file_bytes_preserved', 'original_test_inventory_preserved',
                'original_statements_preserved', 'recursive_block_tail_additions_only',
                'original_top_level_order_preserved', 'fresh_inert_top_level_declarations_only',
                'protected_bindings_and_test_configuration_preserved']
            report['ok'] = not report['errors']
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            report['errors'].append(str(exc))
        return report

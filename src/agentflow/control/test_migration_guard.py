"""Read-only JavaScript test manifest and exact, action-bound migration verification.

The vendored Babel bundle parses but never imports or executes the inspected source.
Actions contain JavaScript source strings for old_expected/new_expected. Everything
outside the single bound first expected argument is protected byte-for-byte.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


class TestMigrationGuard:
    __test__ = False
    SAFE_MATCHERS = frozenset({'toBe', 'toEqual', 'toStrictEqual', 'toContainEqual', 'toHaveText', 'toHaveValue',
                              'node:assert.strictEqual', 'node:assert.deepStrictEqual'})

    def __init__(self, node_path: str | Path | None = None) -> None:
        self.node_path = str(node_path or os.environ.get('AGENTFLOW_NODE_PATH') or shutil.which('node') or 'node')
        self.parser = Path(__file__).resolve().parents[1] / 'resources/test_migration_guard/parser.bundle.cjs'

    def _parse(self, **payload: Any) -> dict[str, Any]:
        result = subprocess.run([self.node_path, str(self.parser)], input=json.dumps(payload),
                                capture_output=True, text=True, timeout=30, check=False)
        try:
            data = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise ValueError('JavaScript parser unavailable or returned invalid output') from exc
        if result.returncode or data.get('error'):
            raise ValueError(f"JavaScript parse rejected: {data.get('error', result.stderr[:500])}")
        return data

    def inspect(self, path: str | Path, source: str | None = None) -> dict[str, Any]:
        return self._parse(file=Path(path).as_posix(), source=source if source is not None else Path(path).read_bytes().decode('utf-8'))

    def validate_plan(self, actions: list[dict]) -> dict[str, Any]:
        errors = []
        for action in actions:
            try:
                matcher = action['matcher'].removeprefix('resolves.').removeprefix('rejects.')
                if matcher not in self.SAFE_MATCHERS:
                    raise ValueError('Matcher requires production repair or an explicit unsupported-adapter decision')
                old = self._parse(operation='literal', source=action['old_expected'])
                new = self._parse(operation='literal', source=action['new_expected'])
                if not old['safe'] or not new['safe'] or not self._preserves_shape(old['value'], new['value']):
                    raise ValueError('Expectation must remain a static literal with all original structural constraints')
            except (KeyError, TypeError, ValueError) as error:
                errors.append({'path': action.get('path'), 'assertion_id': action.get('assertion_id'), 'message': str(error)})
        return {'ok': not errors, 'errors': errors}

    @staticmethod
    def _files(root: Path) -> dict[str, tuple[str, bytes]]:
        result = {}
        for directory, names, files in os.walk(root, followlinks=False):
            base = Path(directory)
            if base == root and '.git' in names:
                names.remove('.git')
            for name in list(names):
                path = base / name
                if path.is_symlink():
                    result[path.relative_to(root).as_posix()] = ('symlink', os.readlink(path).encode())
                    names.remove(name)
            for name in files:
                path = base / name
                if base == root and name == '.git':
                    continue
                kind = 'symlink' if path.is_symlink() else ('executable' if path.stat().st_mode & 0o111 else 'file')
                result[path.relative_to(root).as_posix()] = (kind, os.readlink(path).encode() if path.is_symlink() else path.read_bytes())
        return result

    @staticmethod
    def _digest(files: dict) -> str:
        h = hashlib.sha256()
        for name, (kind, content) in sorted(files.items()):
            h.update(json.dumps([name, kind, hashlib.sha256(content).hexdigest()]).encode())
        return h.hexdigest()

    @staticmethod
    def _preserves_shape(old: Any, new: Any) -> bool:
        # Removing object constraints or array members weakens subset matchers.
        if type(old) is not type(new):
            return False
        if isinstance(old, dict):
            return old.keys() <= new.keys() and all(TestMigrationGuard._preserves_shape(v, new[k]) for k, v in old.items())
        if isinstance(old, list):
            return len(old) == len(new) and all(TestMigrationGuard._preserves_shape(a, b) for a, b in zip(old, new))
        return True

    def verify(self, before_repo: str | Path, after_repo: str | Path, actions: list[dict]) -> dict[str, Any]:
        report: dict[str, Any] = {'ok': False, 'errors': [], 'affected_case_ids': [], 'evidence': []}
        try:
            before_root, after_root = Path(before_repo), Path(after_repo)
            if not before_root.is_dir() or not after_root.is_dir():
                raise ValueError('Both repository roots must exist')
            before, after = self._files(before_root), self._files(after_root)
            report.update(before_digest=self._digest(before), after_digest=self._digest(after))
            if before.keys() != after.keys():
                raise ValueError('File additions/deletions are forbidden in test migration')
            if not actions:
                raise ValueError('At least one approved migration action is required')
            patches: dict[str, list[tuple[int, int, str]]] = {}
            manifests: dict[str, dict] = {}
            seen = set()
            for action in actions:
                path = action.get('file', action.get('path'))
                if not isinstance(path, str) or Path(path).is_absolute() or '..' in Path(path).parts or Path(path).as_posix() != path:
                    raise ValueError('Action path must be a canonical repository-relative file')
                if action.get('file', path) != action.get('path', path):
                    raise ValueError('Conflicting action file and path')
                if path not in before or before[path][0] == 'symlink':
                    raise ValueError(f'Action file is missing or a symlink: {path}')
                if not isinstance(action.get('requirement_refs'), list) or not action['requirement_refs'] or not all(
                    (isinstance(x, str) and x.strip()) or
                    (isinstance(x, dict) and all(isinstance(x.get(k), str) and x[k].strip()
                     for k in ('artifact_id', 'requirement_id', 'quote'))) for x in action['requirement_refs']):
                    raise ValueError('Migration must bind approved requirement references')
                original = before[path][1].decode('utf-8')
                if path not in manifests:
                    manifests[path] = self.inspect(path, original)
                case = next((c for c in manifests[path]['cases'] if c['case_id'] == action.get('case_id')), None)
                assertion = next((a for a in case['assertions'] if a['assertion_id'] == action.get('assertion_id')), None) if case else None
                if not assertion or assertion['matcher'] != action.get('matcher'):
                    raise ValueError('Action case/assertion/matcher does not match original AST')
                matcher = assertion['matcher']
                if matcher.removeprefix('resolves.').removeprefix('rejects.') not in self.SAFE_MATCHERS:
                    raise ValueError('Matcher cannot be safely migrated without weakening its contract')
                key = (path, action['assertion_id'])
                if key in seen:
                    raise ValueError('Duplicate action binding')
                seen.add(key)
                old, new = action.get('old_expected'), action.get('new_expected')
                if not isinstance(old, str) or old != assertion['expected_source']:
                    raise ValueError('old_expected must exactly match original AST source')
                if not isinstance(new, str) or old == new:
                    raise ValueError('new_expected must be a changed JavaScript source string')
                if not assertion['static_expected']:
                    raise ValueError('Original expected expression is not a static literal')
                parsed = self._parse(operation='literal', source=new)
                if not parsed['safe'] or not self._preserves_shape(assertion['expected_value'], parsed['value']):
                    raise ValueError('Expected update is executable or weakens expected structure')
                start, end = assertion['expected_range']
                patches.setdefault(path, []).append((start, end, new))
                report['affected_case_ids'].append(case['case_id'])
                report['evidence'].append({'file': path, 'case_id': case['case_id'], 'assertion_id': assertion['assertion_id'],
                    'matcher': assertion['matcher'], 'actual_expression': assertion['actual_expression'], 'old_expected': old,
                    'new_expected': new, 'expected_range': [start, end], 'requirement_refs': action['requirement_refs']})
            for path, old_entry in before.items():
                if old_entry[0] != after[path][0]:
                    raise ValueError(f'File mode or symlink change forbidden: {path}')
                if path not in patches:
                    if old_entry != after[path]:
                        raise ValueError(f'Unrelated file change forbidden: {path}')
                    continue
                planned = old_entry[1].decode('utf-8')
                last_start = len(planned) + 1
                for start, end, replacement in sorted(patches[path], reverse=True):
                    if end > last_start:
                        raise ValueError('Overlapping assertion edits are forbidden')
                    planned = planned[:start] + replacement + planned[end:]
                    last_start = start
                if planned.encode('utf-8') != after[path][1]:
                    raise ValueError(f'Changes outside approved expected arguments: {path}')
                updated = self.inspect(path, planned)
                def identity(manifest):
                    return [(c['case_id'], c['name'], c['framework_operation'],
                             [(a['assertion_id'], a['matcher'], a['actual_expression'], a['argument_count']) for a in c['assertions']]) for c in manifest['cases']]
                if identity(updated) != identity(manifests[path]):
                    raise ValueError(f'Test or assertion identity changed: {path}')
            report['affected_case_ids'] = sorted(set(report['affected_case_ids']))
            report['semantic_protection'] = ['standard_parser', 'static_literal_only', 'expected_structure_preserved',
                'exact_old_source', 'action_bound_replacement', 'all_other_bytes_preserved', 'case_assertion_identity_preserved']
            report['ok'] = True
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            report['errors'].append(str(exc))
        return report

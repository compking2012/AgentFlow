"""Retire disposable read-only clones after their execution and accounting have closed."""
from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from uuid import uuid4
from weakref import WeakValueDictionary

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.domain.planning import CODING_STEPS, STEPS
from agentflow.runtime.maintenance import STOPPED, _guard, _Skip, _transaction_state, _verify_stopped

REFERENCE_KINDS = ('code_snapshot', 'candidate', 'project', 'project_intent', 'delivery_intent')
_LOCKS = WeakValueDictionary()


def _references(records):
    paths = []
    for kind in REFERENCE_KINDS:
        for row in records.get(kind, []):
            record = row.get('payload', {}) if kind == 'project_intent' else row
            paths.extend(record[field] for field in ('repository_path', 'source_repository',
                'target_repository', 'local_path') if isinstance(record.get(field), str))
    return paths


def _referenced(workspace, paths):
    workspace = Path(workspace)
    for value in paths:
        path = Path(value)
        root_parts = tuple(part.casefold() for part in workspace.parts)
        path_parts = tuple(part.casefold() for part in path.parts)
        if path_parts[:len(root_parts)] == root_parts:
            return True
        if (path == workspace or path.is_relative_to(workspace)
                or path.resolve() == workspace or path.resolve().is_relative_to(workspace)):
            return True
        # Filesystem aliases include case variants on the default macOS volume.
        for parent in (path, *path.parents):
            try:
                if parent.samefile(workspace):
                    return True
            except FileNotFoundError:
                continue
    return False


class ReadonlyWorkspaceRetention:
    def __init__(self, store, workspaces, maintenance):
        self.store, self.workspaces, self.maintenance = store, workspaces, maintenance
        self._lock = _LOCKS.setdefault(str(workspaces.metadata.resolve()), asyncio.Lock())

    async def sweep(self):
        async with self._lock:
            return await self._sweep()

    async def _sweep(self):
        counts = Counter()
        attempts = await self.store.list('attempt')
        if any(a.get('status') not in STOPPED for a in attempts):
            return {'retired': 0, 'skipped': {'execution_active_or_unknown': 1}}
        done = {r['id'] for r in await self.store.list('workspace_retirement')}
        references = _references({kind: await self.store.list(kind) for kind in REFERENCE_KINDS})
        retired = 0
        for attempt in attempts:
            identity = attempt['id']
            if identity in done or attempt.get('status') != 'completed':
                continue
            context = await self.store.read('dispatch_context', identity)
            task = (context or {}).get('task', {})
            if (task.get('step') not in STEPS or task.get('step') in CODING_STEPS
                    or task.get('role') == 'development' or task.get('allowed_write_paths') != []):
                continue
            reservation = None
            defer_remaining = False
            try:
                target = {'kind': 'ephemeral_home', 'folder': 'openhands_homes', 'attempt_id': identity}
                state = await self.maintenance._state(target)
                _guard(state, target)
                for supervised in state['supervised_attempt']:
                    await asyncio.to_thread(_verify_stopped, self.maintenance.data_dir, supervised)
                pending = await self.store.read('workspace_retirement_intent', identity)
                workspace = (pending['path'] if pending and pending.get('state') == 'retiring'
                             else self.workspaces.registration(identity)['path'])
                trash = str(Path(workspace).with_name('.retired-' + canonical_digest(identity).split(':')[1]))
                observed = canonical_digest(state)
                reservation_id = str(uuid4())
                def reserve(tx):
                    current = _transaction_state(tx, target)
                    if canonical_digest(current) != observed:
                        raise DomainError('retirement_state_changed', '工作区执行状态已变化，暂不清理。')
                    _guard(current, target)
                    if current['attempt'][0].get('status') != 'completed':
                        raise DomainError('retirement_state_changed', '该工作已不再是成功完成状态，暂不清理。')
                    if any(a.get('status') not in STOPPED for a in tx.list('attempt')):
                        raise DomainError('retirement_state_changed', '有新执行开始，暂不清理工作区。')
                    current_references = _references({kind: tx.list(kind) for kind in REFERENCE_KINDS})
                    if _referenced(workspace, current_references) or _referenced(trash, current_references):
                        raise DomainError('workspace_referenced', '工作区仍被项目或源码引用，已保留。')
                    previous = tx.get('workspace_retirement_intent', identity)
                    return tx.put('workspace_retirement_intent', identity,
                        {'attempt_id': identity, 'path': workspace, 'trash_path': trash,
                         'reservation_id': reservation_id, 'state': 'retiring'},
                        previous['revision'] if previous else None)
                reservation = await self.store.command('workspace.retirement.reserve', str(uuid4()), {'attempt_id': identity}, reserve)
                result = await self.workspaces.retire(identity, protected_paths=references)
                def complete(tx):
                    intent = tx.get('workspace_retirement_intent', identity)
                    if not intent or intent.get('reservation_id') != reservation['reservation_id']:
                        raise DomainError('retirement_state_changed', '工作区回收操作的身份已变化。')
                    tx.put('workspace_retirement_intent', identity, {**intent, 'state': 'retired'}, intent['revision'])
                    return tx.put('workspace_retirement', identity,
                        {'attempt_id': identity, 'retired_at': utc_now(), 'result': result})
                await self.store.command('workspace.retirement', identity, {'attempt_id': identity},
                    complete)
                retired += 1
            except _Skip as error:
                counts[str(error)] += 1
            except DomainError as error:
                counts[error.code] += 1
                if error.code == 'retirement_state_changed':
                    # New execution or changed references take priority over
                    # scanning the rest of the historical workspace backlog.
                    defer_remaining = True
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                counts['retirement_deferred'] += 1
            # A live retiring journal must keep the admission fence across retries.
            name = canonical_digest(identity).split(':')[1]
            if reservation and not any((self.workspaces.metadata / folder / (name + '.json')).exists()
                                       for folder in ('retiring', 'retired')):
                def release(tx):
                    intent = tx.get('workspace_retirement_intent', identity)
                    if (intent and intent.get('state') == 'retiring'
                            and intent.get('reservation_id') == reservation['reservation_id']):
                        return tx.put('workspace_retirement_intent', identity, {**intent, 'state': 'released'}, intent['revision'])
                    return {}
                await self.store.command('workspace.retirement.release', str(uuid4()), {'attempt_id': identity}, release)
            if defer_remaining:
                break
        return {'retired': retired, 'skipped': dict(counts)}

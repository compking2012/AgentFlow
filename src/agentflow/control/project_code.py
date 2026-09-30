"""A visible development checkout, separate from reviewed delivery references."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.domain.planning import CODING_STEPS, STEPS
from agentflow.repository import RepositoryAdapter
from agentflow.repository.development_checkout import checkout_development


class ProjectCodeService:
    def __init__(self, store, repository=None):
        self.store = store
        self.repository = repository or RepositoryAdapter()
        self._locks = {}

    async def reconcile(self):
        results = []
        for product in await self.store.list('product'):
            if (product.get('run_id') and not product.get('deleted_at') and not product.get('needs_restart')
                    and not product.get('restore_reconciliation_required')):
                results.append(await self.sync(product['run_id']))
        return results

    async def sync(self, run_id):
        run = await self.store.read('run', run_id)
        if not run or not run.get('project_id'):
            return None
        async with self._locks.setdefault(run['project_id'], asyncio.Lock()):
            return await self._sync(run_id)

    async def _sync(self, run_id):
        run = await self.store.read('run', run_id)
        project = await self.store.read('project', run['project_id'])
        if not project:
            return None
        products = [p for p in await self.store.list('product') if p.get('project_id') == project['id']]
        if products and not any(p.get('run_id') == run_id and not p.get('deleted_at')
                                and not p.get('needs_restart') and not p.get('restore_reconciliation_required')
                                for p in products):
            return None
        items = [w for w in await self.store.list('work_item') if w.get('run_id') == run_id and not w.get('archived')]
        contracts = {row['id']: row for row in await self.store.list('review_contract_repair')
                     if row.get('run_id') == run_id}
        binding = None
        by_id = {row['id']: row for row in items}
        if contracts:
            from agentflow.control.review_contract_binding import REVIEW_BINDING_KINDS, repair_batch
            rows = await asyncio.gather(*(self.store.list(kind) for kind in REVIEW_BINDING_KINDS))
            records = dict(zip(REVIEW_BINDING_KINDS, rows, strict=True))
            class Projection:
                def get(self, kind, key):
                    return next((row for row in records.get(kind, []) if row['id'] == key), None)
                def list(self, kind):
                    return records.get(kind, [])
            binding = Projection()
            by_id = {row['id']: row for row in binding.list('work_item')}
        ranks = {step: index for index, step in enumerate(STEPS)}
        def logical_rank(work, seen=frozenset()):
            if not work or work['id'] in seen:
                return None
            batch_id = work.get('payload', {}).get('review_contract_task')
            if not batch_id:
                return ranks.get(work.get('step'))
            batch = contracts.get(batch_id)
            if (not batch or batch.get('actor') != 'controller'
                    or batch.get('assembly_work_item_id') != work['id']):
                return None
            source = batch.get('context', {}).get('snapshot', {})
            return logical_rank(by_id.get(source.get('work_item_id')), seen | {work['id']})
        selected, superseded = [], set()
        for work in items:
            if (work.get('step') not in CODING_STEPS or work.get('kind') == 'stage_child'
                    or work.get('status') not in {'completed', 'waiting_approval'} or not work.get('attempt_id')):
                continue
            batch_id = work.get('payload', {}).get('review_contract_task')
            rank = logical_rank(work)
            if rank is None:
                continue  # Internal actions have no independent visible source phase.
            if batch_id:
                batch = contracts.get(batch_id)
                if (not batch or work.get('kind') != 'aggregation' or work.get('status') != 'completed'
                        or work.get('payload', {}).get('review_contract_kind') != 'assembly'
                        or not batch.get('repair_work_item_ids')
                        or set(work.get('dependencies', [])) != set(batch['repair_work_item_ids'])):
                    continue
                try:
                    repair_batch(binding, work)
                except DomainError:
                    continue  # A malformed historical projection must not stop controller startup.
                complete = True
                for child_id in batch['repair_work_item_ids']:
                    child = by_id.get(child_id)
                    source = binding.get('code_snapshot', (child or {}).get('attempt_id'))
                    if (not child or child.get('status') != 'completed'
                            or child.get('quality_result') in {'failed', 'inconclusive'}
                            or not source or source.get('stale') or source.get('run_id') != run_id
                            or source.get('work_item_id') != child_id or source.get('generation') != child['generation']):
                        complete = False
                        break
                if not complete:
                    continue
            snapshot = await self.store.read('code_snapshot', work['attempt_id'])
            if (snapshot and not snapshot.get('stale') and snapshot.get('work_item_id') == work['id']
                    and snapshot.get('run_id') == run_id and snapshot.get('generation') == work['generation']):
                selected.append((work, snapshot, rank))
                if batch_id:
                    ancestor = contracts[batch_id]['context']['snapshot']
                    visited = set()
                    while ancestor.get('id') and ancestor['id'] not in visited:
                        visited.add(ancestor['id'])
                        superseded.add(ancestor['id'])
                        ancestor_work = by_id.get(ancestor.get('work_item_id'), {})
                        ancestor_batch = contracts.get(ancestor_work.get('payload', {}).get('review_contract_task'))
                        if not ancestor_batch or ancestor_batch.get('assembly_work_item_id') != ancestor_work.get('id'):
                            break
                        ancestor = ancestor_batch.get('context', {}).get('snapshot', {})
        if not selected:
            return None
        latest = max(rank for _, _, rank in selected)
        selected = [(work, snapshot) for work, snapshot, rank in selected
                    if rank == latest and snapshot['id'] not in superseded]
        if len(selected) != 1:
            return None  # Parallel contributions need their accepted aggregation first.
        work, snapshot = selected[0]
        identity = canonical_digest({'project': project['id'], 'snapshot': snapshot['id'],
                                     'commit': snapshot['commit_oid'], 'generation': work['generation']})
        previous = await self.store.read('project_code', project['id'])
        operation = await self.store.read('project_code_sync', identity)
        if operation is None:
            applied_commit = (previous or {}).get('last_applied_commit')
            applied_ref = (previous or {}).get('last_applied_ref')
            if previous and previous.get('state') == 'ready':
                applied_commit = applied_commit or previous.get('commit_oid')
                applied_ref = applied_ref or previous.get('development_ref')
            operation = await self.store.command('project.code.prepare', identity, {'snapshot': snapshot['id']},
                lambda tx: tx.put('project_code_sync', identity, {
                    'project_id': project['id'], 'run_id': run_id, 'work_item_id': work['id'],
                    'generation': work['generation'], 'snapshot_id': snapshot['id'],
                    'commit_oid': snapshot['commit_oid'], 'state': 'prepared',
                    'previous_commit': applied_commit,
                    'previous_ref': applied_ref, 'created_at': utc_now()}))
        try:
            checked = await asyncio.to_thread(self._checkout, project, run, snapshot, operation)
            status, error = 'ready', None
        except (DomainError, OSError) as exc:
            checked = {'path': project['local_path'], 'commit_oid': snapshot['commit_oid']}
            status = 'blocked'
            error = exc.message if isinstance(exc, DomainError) else '项目代码目录暂时无法同步，已保留原文件。'
        if (previous and previous.get('state') == status and previous.get('error') == error
                and previous.get('snapshot_id') == snapshot['id']
                and all(previous.get(k) == v for k, v in checked.items())):
            return previous
        def finish(tx):
            current = tx.get('work_item', work['id'])
            stored = tx.get('code_snapshot', snapshot['id'])
            current_run = tx.get('run', run_id)
            valid = (current and current.get('generation') == work['generation']
                     and current.get('attempt_id') == work['attempt_id'] and stored == snapshot
                     and current_run and current_run['input_fingerprint'] == run['input_fingerprint'])
            record = {'project_id': project['id'], 'run_id': run_id, 'work_item_id': work['id'],
                'generation': work['generation'], 'snapshot_id': snapshot['id'], **checked,
                'state': status if valid else 'stale', 'error': error, 'operation_id': identity,
                'last_applied_commit': checked['commit_oid'] if status == 'ready' else (
                    (previous or {}).get('last_applied_commit') or operation.get('previous_commit')),
                'last_applied_ref': checked.get('development_ref') if status == 'ready' else (
                    (previous or {}).get('last_applied_ref') or operation.get('previous_ref'))}
            prior = tx.get('project_code', project['id'])
            if prior and all(prior.get(k) == v for k, v in record.items()):
                return prior
            result = tx.put('project_code', project['id'], record, prior['revision'] if prior else None)
            op = tx.get('project_code_sync', identity)
            tx.put('project_code_sync', identity, {**op, 'state': result['state']}, op['revision'])
            tx.event('project.code_updated', {'project_id': project['id'], 'snapshot_id': snapshot['id'],
                'state': result['state'], 'path': project['local_path']}, run_id=run_id)
            return result
        # A fresh receipt also records a user edit detected after an earlier successful sync.
        from uuid import uuid4
        return await self.store.command('project.code.finish', str(uuid4()), {'operation_id': identity}, finish)

    def _git(self, root, args, *, check=True):
        # Project-local filters are arbitrary programs; they must not run during synchronization.
        keys = self.repository._run(root, ['config', '--includes', '--name-only', '--get-regexp',
            r'^filter\..*\.(clean|smudge|process|required)$'], check=False).decode().splitlines()
        overrides = ['-c', 'core.autocrlf=false']
        for key in keys:
            overrides += ['-c', key + ('=false' if key.endswith('.required') else '=')]
        return self.repository._run(root, overrides + args, check=check)

    def _checkout(self, project, run, snapshot, operation):
        root = Path(project['local_path'])
        if root.is_symlink() or root.resolve() != root:
            raise DomainError('unsafe_project_code', '项目代码目录归属发生变化，未写入源码。')
        self.repository._repo(root, worktree=True)
        commit = self.repository._oid(snapshot['commit_oid'])
        source = Path(snapshot['repository_path'])
        if self.repository._integrity(source, commit) != snapshot['tree_oid']:
            raise DomainError('code_snapshot_changed', '源码快照校验失败，未写入项目。')
        names = self.repository._run(source, ['ls-tree', '-r', '--name-only', '-z', commit]).split(b'\0')
        if any(name.lower() == b'.agentflow' or name.lower().startswith(b'.agentflow/') for name in names):
            raise DomainError('reserved_project_path', '源码不能占用项目中的 .agentflow 运行目录。')
        head = self.repository._commit(root, 'HEAD')
        current_ref = self._git(root, ['symbolic-ref', '-q', 'HEAD'], check=False).decode().strip()
        branch = 'refs/heads/agentflow/development/' + canonical_digest(run['id']).split(':')[1][:24]
        base = run.get('base_commit', project['base_commit'])
        allowed = {(base, run.get('base_ref', project['base_ref'])),
                   (operation.get('previous_commit'), operation.get('previous_ref')),
                   (commit, branch), (commit, '')}
        if (head, current_ref) not in allowed:
            raise DomainError('project_checkout_changed', '项目当前分支或提交已由外部修改，未切换或覆盖。')
        existing = self._git(root, ['show-ref', '--verify', '--hash', branch], check=False).decode().strip()
        if existing and existing not in {commit, operation.get('previous_commit')}:
            raise DomainError('development_ref_changed', '开发分支已变化，未覆盖已有提交。')
        if head != commit:
            with tempfile.TemporaryDirectory(prefix='agentflow-project-code-') as temporary:
                bundle = Path(temporary).resolve() / 'source.bundle'
                info = self.repository._prepare_bundle(source, commit, bundle)
                self.repository._import_bundle(root, bundle, commit, info['sha256'])
        return checkout_development(self.repository, root, candidate=commit, branch=branch,
            expected_head=head, expected_ref=current_ref, expected_branch=existing or None)

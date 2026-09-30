"""Version-bound workflow, readable artifacts and quality projections for the owner."""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from agentflow.common import DomainError, canonical_digest
from agentflow.control.documents_projection import (
    copy_digest,
    copy_matches,
    legacy_copies,
    projection_state,
    prune_legacy_copies,
    render_group_document,
    write_current_copy,
)
from agentflow.control.failure_messages import failure_display
from agentflow.control.project_documents import ProjectDocumentService
from agentflow.control.project_workflow import compose_product_workflow
from agentflow.control.readable import document_sources, output_contract, render_document, safe_filename
from agentflow.control.workflow_stages import group_workflow_stages, logical_stage_key, stage_contexts
from agentflow.execution.manifests import file_digest
from agentflow.execution.transport import ArtifactTransport
from agentflow.models.budget import account_id
from agentflow.runtime.failures import (
    known_failure_reason,
    read_frozen_codex_failure,
    read_role_failure,
    refine_codex_failure,
)
from agentflow.runtime.prelaunch import prelaunch_failure_code


class _WorkflowReadStore:
    """Reuse historical scans within one view; identity reads and writes stay live."""
    _KINDS = frozenset({'work_item', 'work_revision', 'artifact', 'product',
        'product_test_runtime_repair', 'product_test_repair', 'review_repair',
        'review_source_repair', 'failure_analysis'})

    def __init__(self, store):
        self._store = store
        self._lists = {}

    def __getattr__(self, name):
        return getattr(self._store, name)

    async def list(self, kind):
        if kind not in self._KINDS:
            return await self._store.list(kind)
        if kind not in self._lists:
            self._lists[kind] = await self._store.list(kind)
        return self._lists[kind]


class WorkflowViewRequests:
    """Coalesce overlapping reads only; never retain a completed view."""
    def __init__(self, store, artifacts, settings):
        self.store, self.artifacts, self.settings = store, artifacts, settings
        self._pending = {}

    async def workflow(self, identity):
        task = self._pending.get(identity)
        if task is None or task.done():
            task = asyncio.create_task(
                RunPresentationService(self.store, self.artifacts, self.settings).workflow(identity))
            self._pending[identity] = task

            def finished(completed):
                if self._pending.get(identity) is completed:
                    self._pending.pop(identity, None)
                if not completed.cancelled():
                    completed.exception()  # A disconnected viewer must not leak an unobserved error.

            task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def close(self):
        pending = list(self._pending.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._pending.clear()


class RunPresentationService:
    def __init__(self, store, artifacts, settings):
        self.store, self.artifacts, self.settings = store, artifacts, settings

    async def _run(self, identity):
        run = await self.store.read('run', identity)
        if not run:
            raise DomainError('not_found', 'Unknown run', 404)
        return run

    async def _sources(self, work):
        found = []
        for identity in work.get('artifact_ids', []):
            item = await self.store.read('artifact', identity)
            if (item and not item.get('stale') and item.get('work_item_id') == work['id']
                    and item.get('run_id') == work['run_id']
                    and item.get('generation', work['generation']) == work['generation']):
                found.append(item)
        return found

    def _document_root(self, run, plan, products):
        product_id = (plan or {}).get('product_contract', {}).get('product_id')
        product = next((product for product in products if product['id'] == product_id), None)
        if not product:
            product = next((p for p in products
                            if p.get('project_id') == run['project_id']), None)
        if product:
            output = Path(product['output_directory'])
            marker = output / '.agentflow-product.json'
            if (not output.is_absolute() or output.resolve() != output or output.is_symlink()
                    or marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096
                    or json.loads(marker.read_text()).get('product_id') != product['id']):
                raise DomainError('output_ownership_changed', '产品工作目录归属无法核验，未写入文档。')
            root = output / 'documents'
        else:
            root = self.settings.data_dir / 'workspace_documents'
        return root

    async def _directory(self, work):
        run = await self._run(work['run_id'])
        plan = await self.store.read('plan', run['plan_id']) if run.get('plan_id') else None
        root = self._document_root(run, plan, await self.store.list('product'))
        directory = root / safe_filename(work['run_id']) / safe_filename(work.get('logical_stage_key', work['step']))
        for path in [directory, *directory.parents]:
            if path.is_symlink():
                raise DomainError('unsafe_document_path', '文档目录不能是符号链接。')
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if directory.resolve() != directory:
            raise DomainError('unsafe_document_path', '文档目录归属发生变化。')
        return directory

    @staticmethod
    def _write_copy(path, content, known_digests=()):
        write_current_copy(path, content, known_digests)

    @staticmethod
    def _current_group(tx, run_id, key):
        run = tx.get('run', run_id)
        plan = tx.get('plan', run['plan_id']) if run and run.get('plan_id') else None
        items = [w for w in tx.list('work_item') if w.get('run_id') == run_id and not w.get('archived')]
        groups = group_workflow_stages(items, plan, tx.list('product_test_runtime_repair'), tx.list('review_repair'))
        return next((group for group in groups if group['key'] == key), None), items

    def _validate_group(self, tx, run_id, key, state):
        latest, current_items = self._current_group(tx, run_id, key)
        if not latest or projection_state(latest, current_items, tx.list('work_revision')) != state:
            raise DomainError('stale_artifact', '文档来源已更新。')

    @staticmethod
    def _source_version_matches(source, version):
        return (source is not None and source.get('digest') == version['digest']
                and source.get('generation') == version['generation']
                and bool(source.get('stale')) == version.get('stale', False))

    @staticmethod
    def _legacy_candidates(root, group, records, all_items, revisions, repairs, review_repairs=()):
        run_id = group['work']['run_id']
        all_items = [item for item in all_items if item.get('run_id') == run_id]
        all_items += [row['snapshot'] for row in revisions if row.get('snapshot', {}).get('run_id') == run_id]
        by_id = {item['id']: item for item in all_items}
        roots = {member['id'] for member in group['members']} | {
            item['id'] for item in all_items if not item.get('parent_stage_id')
            and logical_stage_key(item, repairs, by_id, review_repairs) == group['key']}
        related = [item for item in all_items if item['id'] in roots or item.get('parent_stage_id') in roots]
        return list(legacy_copies(root, records, related))

    async def _publish_group_document(self, work, group, state, content=None, *, identity=None, projection_key=None):
        path, storage_error = None, None
        try:
            path = await self._directory(work) / safe_filename(output_contract(work)['artifact_name'] + '.md')
            run = await self._run(work['run_id'])
            plan = await self.store.read('plan', run['plan_id']) if run.get('plan_id') else None
            root = self._document_root(run, plan, await self.store.list('product'))
            all_items = await self.store.list('work_item')
            revisions = await self.store.list('work_revision')
            repairs = await self.store.list('product_test_runtime_repair')
            review_repairs = await self.store.list('review_repair')
            records = await self.store.list('readable_artifact')
            items = [item for item in all_items if item.get('run_id') == run['id'] and not item.get('archived')]
            current = next((item for item in group_workflow_stages(items, plan, repairs, review_repairs)
                            if item['key'] == group['key']), None)
            if not current or projection_state(current, items, revisions) != state:
                raise DomainError('stale_artifact', '文档来源已更新。')
            if root != path.parents[2]:
                raise DomainError('output_ownership_changed', '产品工作目录归属无法核验，未写入文档。')
            candidates = self._legacy_candidates(root, group, records, all_items, revisions, repairs, review_repairs)
            cleanup = any(copy_matches(old, digest) for old, digest in candidates)
            known = {row['digest'] for row in records if row.get('projection_key') == projection_key}
            if content is None:
                if not cleanup:
                    return None, None
            else:
                existing = copy_digest(path)
                expected = next(row['digest'] for row in records if row['id'] == identity)
                if existing == expected and not cleanup:
                    return path, None
                if existing and existing not in known:
                    raise DomainError('document_modified', '工作目录中的文档已修改，原文件已保留。')

            def publish(tx):
                self._validate_group(tx, work['run_id'], group['key'], state)
                if identity:
                    registered = tx.get('readable_artifact', identity)
                    if not registered or any(not self._source_version_matches(tx.get('artifact', version['id']), version)
                                             for version in registered['source_versions']):
                        raise DomainError('stale_artifact', '文档来源已更新。')
                run = tx.get('run', work['run_id'])
                plan = tx.get('plan', run['plan_id']) if run.get('plan_id') else None
                root = self._document_root(run, plan, tx.list('product'))
                if root != path.parents[2]:
                    raise DomainError('output_ownership_changed', '产品工作目录归属无法核验，未写入文档。')
                records = tx.list('readable_artifact')
                if content is not None:
                    known = {row['digest'] for row in records if row.get('projection_key') == projection_key}
                    self._write_copy(path, content, known)
                candidates = self._legacy_candidates(root, group, records, tx.list('work_item'),
                    tx.list('work_revision'), tx.list('product_test_runtime_repair'), tx.list('review_repair'))
                prune_legacy_copies(candidates)
                return {'id': identity, 'path': str(path) if content is not None else None}
            # The store's single writer serializes publication with generation changes.
            await self.store.command('document.publish', str(uuid4()), {'id': identity}, publish)
        except (DomainError, OSError, ValueError) as error:
            path = None
            storage_error = error.message if isinstance(error, DomainError) else '工作目录暂时无法保存文档，可查看已归档版本。'
        return path, storage_error

    async def group_document(self, group, items):
        run = await self._run(group['work']['run_id'])
        plan = await self.store.read('plan', run['plan_id']) if run.get('plan_id') else None
        product = ProjectDocumentService.product_for(run, plan, await self.store.list('product'))
        if product:
            return await ProjectDocumentService(self.store, self.artifacts, self.settings).document(
                self, product, run, plan, group, items)
        language = (plan or {}).get('product_contract', {}).get('language', 'zh-CN')
        work = {**group['work'], 'logical_stage_key': group['key'], 'language': language}
        revisions = await self.store.list('work_revision')
        rendered = await render_group_document(group, items, self.artifacts, await self.store.list('artifact'),
                                               revisions, language=language)
        state = projection_state(group, items, revisions)
        if rendered is None:
            await self._publish_group_document(work, group, state)
            return None
        projection_key = str(uuid5(NAMESPACE_URL, 'agentflow:document:' + run['id'] + ':' + group['key']))
        blob = await self.artifacts.put_bytes(rendered['content'])
        identity = str(uuid5(NAMESPACE_URL, 'agentflow:readable:current:' + canonical_digest({
            'key': projection_key, 'state': state, 'digest': blob['id'], 'sources': rendered['source_versions']})))
        name = output_contract(work)['artifact_name'] + '.md'

        def register(tx):
            self._validate_group(tx, run['id'], group['key'], state)
            if any(not self._source_version_matches(tx.get('artifact', version['id']), version)
                   for version in rendered['source_versions']):
                raise DomainError('stale_artifact', '文档来源已更新。')
            return tx.get('readable_artifact', identity) or tx.put('readable_artifact', identity, {
                'run_id': run['id'], 'work_item_id': work['id'], 'generation': work['generation'],
                'digest': blob['id'], 'source_versions': rendered['source_versions'], 'name': name,
                'media_type': 'text/markdown', 'projection_key': projection_key,
                'logical_stage_key': group['key'], 'projection_state': state})
        # A fresh command key keeps validation active even when the immutable record already exists.
        record = await self.store.read('readable_artifact', identity)
        if record is None:
            record = await self.store.command('readable.register', str(uuid4()), {'id': identity}, register)
        path, storage_error = await self._publish_group_document(work, group, state, rendered['content'],
                                                               identity=identity, projection_key=projection_key)
        return await self._document_descriptor(work, record, path, storage_error, source_work=rendered['source_work'])

    async def document(self, work, *, materialize=True):
        run = await self._run(work['run_id'])
        plan = await self.store.read('plan', run['plan_id']) if run.get('plan_id') else None
        persisted = await self.store.read('work_item', work['id'])
        if (not persisted or persisted.get('archived') or persisted['generation'] != work['generation']
                or persisted.get('artifact_ids') != work.get('artifact_ids')):
            raise DomainError('stale_artifact', '该文档属于旧的工作版本。')
        if materialize and not work.get('parent_stage_id'):
            items = [w for w in await self.store.list('work_item') if w.get('run_id') == run['id'] and not w.get('archived')]
            groups = group_workflow_stages(items, plan, await self.store.list('product_test_runtime_repair'),
                                           await self.store.list('review_repair'))
            group = next((group for group in groups if any(w['id'] == work['id'] for w in group['members'])), None)
            return await self.group_document(group, items) if group else None
        work = {**work, 'language': (plan or {}).get('product_contract', {}).get('language', 'zh-CN')}
        sources = await self._sources(work)
        if not sources:
            return None
        readable = next((a for a in sources if a.get('readable')), None)
        contract = output_contract(work)
        name = (contract['name'] + (' Summary' if work['language'] == 'en' else '汇总')
                if work.get('kind') == 'aggregation' else contract['artifact_name']) + '.md'
        if work.get('parent_stage_id'):
            subject = str(work.get('payload', {}).get('goal') or work.get('key') or '子任务').splitlines()[0][:32]
            name = contract['artifact_name'] + ' - ' + safe_filename(subject) + '.md'
        if readable:
            await self.artifacts.verify(readable['digest'])
            digest = readable['digest']
        else:
            content = render_document(work, await document_sources(self.artifacts, sources)).encode('utf-8')
            digest = (await self.artifacts.put_bytes(content))['id']
        source_versions = [{'id': a['id'], 'digest': a['digest'], 'generation': a.get('generation')} for a in sources]
        identity = str(uuid5(NAMESPACE_URL, 'agentflow:readable:' + canonical_digest({
            'work': work['id'], 'generation': work['generation'], 'sources': source_versions})))

        def register(tx):
            current = tx.get('work_item', work['id'])
            if (not current or current['generation'] != work['generation']
                    or current.get('artifact_ids') != work.get('artifact_ids')):
                raise DomainError('stale_artifact', '工作产物版本已更新。')
            return tx.get('readable_artifact', identity) or tx.put('readable_artifact', identity, {
                'run_id': work['run_id'], 'work_item_id': work['id'], 'generation': work['generation'],
                'digest': digest, 'source_versions': source_versions, 'name': name, 'media_type': 'text/markdown'})
        record = await self.store.read('readable_artifact', identity)
        if record is None:
            record = await self.store.command('readable.register', str(uuid4()), {'id': identity}, register)
        return await self._document_descriptor(work, record, None, None)

    async def _document_descriptor(self, work, record, path, storage_error, *, source_work=None, include_code_paths=True):
        source_work = source_work or work
        descriptor = {'artifact_id': record['id'], 'name': record['name'], 'path': str(path) if path else None,
            'media_type': 'text/markdown', 'revision': work['generation'], 'digest': record['digest'],
            'kind': 'document', 'preview_url': '/api/v1/readable_artifacts/' + record['id'],
            'download_url': '/api/v1/readable_artifacts/' + record['id'] + '?download=true',
            'storage_error': storage_error}
        if not include_code_paths:
            return descriptor
        contract = output_contract(work)
        snapshot = await self.store.read('code_snapshot', source_work['attempt_id']) if source_work.get('attempt_id') else None
        if (contract['kind'] != 'document' and snapshot and not snapshot.get('stale')
                and snapshot.get('work_item_id') == source_work['id'] and snapshot.get('generation') == source_work['generation']):
            repository = snapshot.get('repository_path')
            if repository:
                source = Path(repository)
                run = await self._run(work['run_id'])
                development = await self.store.read('project_code', run['project_id'])
                if (development and development.get('run_id') == run['id']
                        and development.get('snapshot_id') == snapshot['id']):
                    if development.get('state') == 'ready':
                        source = Path(development['path'])
                    elif development.get('error'):
                        descriptor['storage_error'] = development['error']
                selected = source / 'tests' if contract['kind'] == 'test_code' and (source / 'tests').is_dir() else source
                descriptor.update(kind=contract['kind'], document_path=descriptor['path'], path=str(selected),
                                  name=contract['artifact_name'], source_commit=snapshot.get('commit_oid'))
        return descriptor

    async def readable(self, identity, *, preview=True):
        record = await self.store.read('readable_artifact', identity)
        if not record:
            raise DomainError('not_found', 'Unknown document', 404)
        if record.get('project_document'):
            verified = await self.artifacts.verify(record['digest'])
            if preview and verified['size'] > 1024 * 1024:
                raise DomainError('preview_too_large', '文档较大，请下载后阅读。', 413)
            return record, verified
        work = await self.store.read('work_item', record['work_item_id'])
        if not work or work.get('archived') or work['generation'] != record['generation']:
            raise DomainError('stale_artifact', '该文档属于旧的工作版本。')
        if record.get('projection_key'):
            run = await self._run(work['run_id'])
            plan = await self.store.read('plan', run['plan_id']) if run.get('plan_id') else None
            items = [w for w in await self.store.list('work_item') if w.get('run_id') == run['id'] and not w.get('archived')]
            groups = group_workflow_stages(items, plan, await self.store.list('product_test_runtime_repair'),
                                           await self.store.list('review_repair'))
            group = next((group for group in groups if group['key'] == record['logical_stage_key']), None)
            if not group or projection_state(group, items, await self.store.list('work_revision')) != record['projection_state']:
                raise DomainError('stale_artifact', '文档来源已更新。')
            for version in record['source_versions']:
                source = await self.store.read('artifact', version['id'])
                if not self._source_version_matches(source, version):
                    raise DomainError('stale_artifact', '文档来源已更新。')
        else:
            sources = await self._sources(work)
            if {a['id']: a['digest'] for a in sources} != {a['id']: a['digest'] for a in record['source_versions']}:
                raise DomainError('stale_artifact', '文档来源已更新。')
        verified = await self.artifacts.verify(record['digest'])
        if preview and verified['size'] > 1024 * 1024:
            raise DomainError('preview_too_large', '文档较大，请下载后阅读。', 413)
        return record, verified

    async def workflow(self, identity):
        # Each request gets fresh history. Publishing still uses the real store's
        # transaction and its generation/source/ownership checks.
        presenter = RunPresentationService(_WorkflowReadStore(self.store), self.artifacts, self.settings)
        return await presenter._workflow(identity)

    async def _workflow(self, identity):
        run = await self._run(identity)
        items = [w for w in await self.store.list('work_item') if w.get('run_id') == identity and not w.get('archived')]
        plan = await self.store.read('plan', run['plan_id']) if run.get('plan_id') else None
        repairs = [r for r in await self.store.list('product_test_runtime_repair') if r.get('run_id') == identity]
        groups = group_workflow_stages(items, plan, repairs, await self.store.list('review_repair'))
        from agentflow.control.review_contract_view import review_contract_view
        repair_views = await review_contract_view(self.store, identity)
        repair_events = [{**row, 'repair_kind': kind}
            for kind in ('product_test_repair', 'product_test_runtime_repair', 'review_source_repair', 'review_repair')
            for row in await self.store.list(kind) if row.get('run_id') == identity]
        contexts = stage_contexts(groups, items, repair_events, await self.store.list('artifact'),
                                  await self.store.list('work_revision'))
        failure_analyses = None
        stages = []
        for group in groups:
            work = group['work']
            grouped, parents = [], set()
            for member in group['members']:
                children = sorted((w for w in items if w.get('parent_stage_id') == member['id']),
                                  key=lambda w: (w.get('key', ''), w['id']))
                if children:
                    parents.add(member['id'])
                grouped.extend(children)
                grouped.append({**member, 'logical_stage_key': group['key']})
            tasks = []
            for i, task in enumerate(grouped):
                contract = output_contract(task)
                label = (contract['name'] + '汇总' if task['id'] in parents else
                         str(task.get('payload', {}).get('goal') or task.get('key') or contract['name'])[:100]
                         if task.get('parent_stage_id') else contract['name'])
                root_id = task.get('parent_stage_id') or task['id']
                historical = root_id not in group['current_ids']
                artifact_notice = None
                try:
                    output = await self.document(task, materialize=False)
                except DomainError as error:
                    if error.code != 'stale_artifact':
                        raise
                    output = None
                    artifact_notice = '任务版本已更新，产物将在下次刷新时重新读取。'
                reason = None
                if task['status'] in {'failed', 'blocked', 'execution_unknown'} or (
                        task['step'] in {'unit_test_execution', 'integration_test_execution'}
                        and task['status'] == 'completed' and task.get('quality_result') != 'passed'):
                    attempt = await self.store.read('attempt', task['attempt_id']) if task.get('attempt_id') else None
                    code = known_failure_reason(task.get('blocking_reason'))
                    attempt_matches = (attempt and attempt.get('work_item_id') == task['id']
                            and attempt.get('run_id') == task['run_id']
                            and all(attempt.get(field) == task.get(field) and task.get(field) is not None
                                    for field in ('generation', 'fencing_token', 'input_fingerprint')))
                    if attempt_matches:
                        code = known_failure_reason(attempt.get('runtime_failure_code')) or code or known_failure_reason(attempt.get('summary'))
                        supervised = await self.store.read('supervised_attempt', task['attempt_id'])
                        if supervised is None and code in {None, 'isolation_unverified'}:
                            code = await prelaunch_failure_code(self.store, self.settings.data_dir, task['attempt_id']) or code
                        if (not code and supervised and supervised.get('attempt_id') == task['attempt_id']
                                and supervised.get('run_id') == task['run_id']
                                and all(supervised.get(field) == task.get(field) for field in
                                        ('fencing_token', 'input_fingerprint'))):
                            if supervised.get('backend') == 'codex_exec':
                                code = await read_frozen_codex_failure(self.store, self.settings.data_dir, task['attempt_id'],
                                    run_id=task['run_id'], work_item_id=task['id'], fencing_token=task['fencing_token'],
                                    input_fingerprint=task['input_fingerprint'])
                            elif supervised.get('backend') == 'openhands_role':
                                code = read_role_failure(self.settings.data_dir, task['attempt_id'])
                        if code == 'model_output_limit':
                            code = await refine_codex_failure(self.store, self.settings.data_dir, task['attempt_id'],
                                fencing_token=task['fencing_token'], input_fingerprint=task['input_fingerprint'], fallback=code)
                        if code == 'model_rate_limited':
                            account = await self.store.read('budget_account', account_id('run', task['run_id']))
                            if (account and account.get('owner_id') == task['run_id']
                                    and type(account.get('max_requests')) is int and account['max_requests'] > 0
                                    and account.get('request_count', 0) >= account.get('max_requests', 1)):
                                code = 'model_request_limit_reached'
                    if task['status'] == 'execution_unknown':
                        code = 'execution_unconfirmed'
                    code = code or ('test_failed' if task['status'] == 'completed'
                                    else 'worker_exited' if task['status'] == 'failed' else 'work_blocked')
                    code, reason = await failure_display(self.store, self.settings, task,
                                                         attempt if attempt_matches else None, code)
                    if attempt_matches:
                        from agentflow.control.failure_remediation import failure_signature
                        if failure_analyses is None:
                            failure_analyses = await self.store.list('failure_analysis')
                        signature = failure_signature(task, attempt)
                        matching = [row for row in failure_analyses
                            if row.get('actor') == 'controller' and row.get('phase') == 'analysis'
                            and row.get('run_id') == task['run_id'] and row.get('work_item_id') == task['id']
                            and row.get('generation') == task['generation'] and row.get('attempt_id') == task['attempt_id']
                            and row.get('failure_signature') == signature]
                        latest = max(matching, default=None, key=lambda row: (
                            str(row.get('analyzed_at') or row.get('created_at') or ''), row['revision'], row['id']))
                        if latest and latest.get('status') == 'blocked':
                            entries = latest.get('blockers')
                            blockers = [row for row in (entries if isinstance(entries, list) else []) if isinstance(row, dict)
                                and isinstance(row.get('message'), str) and row['message'].strip()]
                            blocker = next((row for row in blockers if row.get('code') in {
                                'automatic_repair_limit', 'automatic_timeout_retry_limit', 'review_repair_limit'}),
                                next(iter(blockers), None))
                            if blocker:
                                message = ' '.join(blocker['message'].split())
                                if message not in reason:
                                    reason += '\n自动恢复未继续：' + message[:400] + ('…' if len(message) > 400 else '')
                tasks.append({'id': task['id'], 'name': label, 'role': task['role'], 'status': task['status'],
                    'quality_result': task.get('quality_result', 'unknown'), 'generation': task['generation'],
                    'expected_artifact': {'name': contract['artifact_name'], 'kind': contract['kind'], 'description': contract['purpose']},
                    'artifacts': [output] if output else [], 'blocking_reason': reason,
                    **({'artifact_notice': artifact_notice} if artifact_notice else {}),
                    'is_aggregation': task['id'] in parents, 'is_history': historical})
            current_tasks = [t for t in tasks if not t['is_history']]
            status = work['status']
            if any(t['status'] in {'failed', 'execution_unknown'} for t in current_tasks):
                status = 'failed'
            elif any(t['status'] in {'running', 'waiting_execution', 'cancel_requested'} for t in current_tasks):
                status = 'waiting_execution' if status == 'waiting_execution' else 'running'
            elif any(t['status'] == 'blocked' for t in current_tasks):
                status = 'blocked'
            elif any(t['status'] == 'waiting_approval' for t in current_tasks):
                status = 'waiting_approval'
            elif any(t['status'] == 'cancelled' for t in current_tasks):
                status = 'cancelled'
            elif any(t['status'] != 'completed' for t in current_tasks):
                status = 'pending'
            contract = output_contract(work)
            quality = ('failed' if any(t['quality_result'] == 'failed' for t in current_tasks) else
                       'passed' if current_tasks and all(t['quality_result'] == 'passed' for t in current_tasks) else 'unknown')
            current_roots = [member for member in group['members'] if member['id'] in group['current_ids']]
            retry_rank = {'failed': 0, 'execution_unknown': 0, 'blocked': 1, 'cancelled': 2,
                          'waiting_approval': 3, 'pending': 4, 'queued': 4, 'running': 5, 'waiting_execution': 5}
            retry_work = min(current_roots, key=lambda member: (
                0 if member.get('quality_result') in {'failed', 'inconclusive'} else retry_rank.get(member['status'], 9),
                member['id'] != work['id'], member['id']))
            artifact_notice = None
            try:
                if any(task.get('artifact_notice') for task in tasks):
                    raise DomainError('stale_artifact', '阶段任务版本已更新。')
                output = await self.group_document(group, items)
            except DomainError as error:
                if error.code != 'stale_artifact':
                    raise
                output = None
                artifact_notice = '阶段版本已更新，产物将在下次刷新时重新读取。'
            stages.append({'id': group['id'], 'key': group['key'], 'work_item_id': retry_work['id'],
                **({'repair_context': repair_views[group['id']]} if group['id'] in repair_views else {}),
                'step': work['step'], 'name': contract['name'], 'status': status,
                'quality_result': quality, 'expected_artifact': {
                    'name': contract['artifact_name'], 'kind': contract['kind'], 'description': contract['purpose']},
                'output': output, 'tasks': tasks,
                **({'artifact_notice': artifact_notice} if artifact_notice else {}),
                'dependencies': group['dependencies'],
                **({'context': contexts[group['key']]} if group['key'] in contexts else {})})
        return await compose_product_workflow(self.store, run, plan,
            {'run_id': identity, 'input_fingerprint': run['input_fingerprint'], 'stages': stages})

    async def _verified_reports(self, run):
        current = [c for c in await self.store.list('candidate') if c.get('run_id') == run['id']
                   and c.get('run_input_fingerprint') == run.get('input_fingerprint') and not c.get('stale')]
        if len(current) != 1:
            return None, []
        candidate = current[0]
        matrix = await self.store.read('target_matrix', candidate['id'])
        if (not matrix or matrix.get('state') != 'bound_to_platform_manifest'
                or matrix.get('candidate_fingerprint') != candidate.get('fingerprint')):
            return candidate, []
        reports = []
        transport = ArtifactTransport(self.store, self.settings.data_dir / 'nodes/artifacts')
        for check in await self.store.list('check'):
            if (check.get('run_id') != run['id'] or check.get('candidate_fingerprint') != candidate.get('fingerprint')
                    or not check.get('evidence_verified') or check.get('stale')):
                continue
            if not check.get('node_result_id') or not check.get('raw_report_artifact_id'):
                continue
            result = await self.store.read('node_result', check['node_result_id'])
            raw = await self.store.read('node_artifact', check['raw_report_artifact_id'])
            entry = candidate.get('matrix_mappings', {}).get(check.get('matrix_entry_id'))
            if not result or result.get('assessment_state') != 'validated' or not raw or not entry:
                continue
            path = transport.object_path(raw['digest'])
            if raw.get('state') != 'complete' or path.is_symlink() or not path.is_file() or file_digest(path) != raw['digest']:
                continue
            verified = next((v for v in result.get('verified_checks', [])
                if v.get('matrix_entry_id') == check.get('matrix_entry_id')
                and v.get('raw_report_artifact_version_id') == raw['id']), None)
            if not verified or not isinstance(verified.get('normalized_report'), dict):
                continue
            reports.append({'phase': entry['phase'], 'target_config_id': entry.get('target_config_id'),
                'matrix_entry_id': check['matrix_entry_id'], 'source_artifact_id': raw['id'],
                'report': verified['normalized_report']})
        return candidate, reports

    async def quality(self, identity):
        run = await self._run(identity)
        flow = await self.workflow(identity)
        candidate, reports = await self._verified_reports(run)
        plan = await self.store.read('plan', run['plan_id']) if run.get('plan_id') else None
        matrix = await self.store.read('target_matrix', candidate['id']) if candidate else None
        target_types = {entry['target_config_id']: entry['app_target'] for entry in
            [*(plan or {}).get('target_configs', []), *((matrix or {}).get('plan') or {}).get('entries', [])]
            if entry.get('target_config_id') and entry.get('app_target')}
        declared_targets = set((plan or {}).get('app_targets', []))
        matrix_entries = ((matrix or {}).get('plan') or {}).get('entries', [])
        mappings = (candidate or {}).get('matrix_mappings', {})
        planned_targets = {entry.get('app_target') for entry in matrix_entries
            if (mapping := mappings.get(entry.get('matrix_entry_id')))
            and mapping.get('target_config_id') == entry.get('target_config_id')
            and mapping.get('phase') in {'unit', 'integration'}}
        metrics = []
        phases = {}
        for phase, field in [('unit', 'unit_tests'), ('integration', 'integration_tests')]:
            selected = [r for r in reports if r['phase'] == phase]
            cases = {}
            for value in selected:
                for case in value['report'].get('cases', []):
                    # Shared raw executions are counted once; independent reports
                    # with the same case name must never overwrite a failure.
                    key = (value['target_config_id'], value['source_artifact_id'], case['case_id'], case.get('attempt', 0))
                    ranks = {'passed': 0, 'skipped': 1, 'unknown': 2, 'failed': 3, 'error': 4}
                    if key not in cases or ranks.get(case.get('status'), 2) > ranks.get(cases[key].get('status'), 2):
                        cases[key] = case
                for metric in value['report'].get('performance_metrics', []):
                    if (isinstance(metric, dict) and type(metric.get('value')) in {int, float}
                            and math.isfinite(metric['value'])):
                        item = {**metric, 'source_artifact_id': value['source_artifact_id']}
                        if item not in metrics:
                            metrics.append(item)
            counts = {key: sum(c.get('status') == key for c in cases.values()) for key in ('passed', 'failed', 'error', 'skipped', 'unknown')}
            required = {key for key, value in (candidate or {}).get('matrix_mappings', {}).items() if value.get('phase') == phase}
            complete = (bool(required) and required <= {r['matrix_entry_id'] for r in selected}
                        and all(not r['report'].get('missing_case_ids') for r in selected))
            if declared_targets:
                # The accepted matrix may share unit tests while assigning separate
                # integration tests to each platform. Each phase needs its own rows;
                # every declared platform still needs a bound test plan entry.
                covered_targets = planned_targets if matrix_entries else {
                    target_types.get(r['target_config_id']) for r in selected}
                complete = complete and declared_targets <= covered_targets
            valid = bool(selected) and all(r['report'].get('execution_status') == 'completed' for r in selected)
            report_failed = any(r['report'].get('quality_result') == 'failed' for r in selected)
            total = len(cases)
            phases[field] = {'status': 'not_run' if not selected else 'failed' if counts['failed'] or counts['error'] or report_failed else
                'passed' if complete and valid and not counts['skipped'] and not counts['unknown'] else 'incomplete',
                'verified': bool(selected), 'coverage_complete': complete, 'total': total, 'passed': counts['passed'],
                'failed': counts['failed'], 'errors': counts['error'], 'skipped': counts['skipped'], 'unknown': counts['unknown'],
                'pass_rate': counts['passed'] / total if total and complete and valid else None,
                'duration_seconds': sum(float(c.get('duration_seconds', 0)) for c in cases.values()
                    if type(c.get('duration_seconds')) in {int, float} and math.isfinite(c['duration_seconds'])) if cases else None}
        works = {w['id']: w for w in await self.store.list('work_item') if w.get('run_id') == identity and not w.get('archived')}
        issues, reviewed = [], False
        for review in await self.store.list('review'):
            work = works.get(review.get('work_item_id'))
            if (not work or review.get('stale') or review.get('run_id') not in {None, identity} or work.get('parent_stage_id') or work['generation'] != review.get('generation')
                    or work['status'] not in {'completed', 'waiting_approval'}):
                continue
            reviewed = True
            source_records = await self._sources(work)
            bodies = await document_sources(self.artifacts, source_records)
            bodies = [(meta, body.get('result', body) if isinstance(body, dict) else body) for meta, body in bodies]
            findings = next((body.get('findings', []) for _, body in bodies
                             if isinstance(body, dict) and 'findings' in body), review.get('blocking_findings', []))
            for finding in findings:
                issues.append({'title': str(finding.get('description', '未说明的问题'))[:4000],
                    'severity': finding.get('severity', 'warning'), 'category': finding.get('category', 'unclassified'),
                    'status': 'open', 'path': finding.get('path', ''),
                    'source_artifact_id': source_records[0]['id'] if source_records else None})
        artifacts = [stage['output'] for stage in flow['stages'] if stage['output']]
        project = await self.store.read('project', run['project_id'])
        return {'run_id': identity, 'input_fingerprint': run['input_fingerprint'],
            'candidate_fingerprint': (candidate or {}).get('fingerprint'), **phases,
            'bugs': {'status': 'reviewed' if reviewed else 'not_run', 'open': len(issues), 'resolved': None,
                     'resolution_tracking': False,
                     'by_category': {kind: sum(item['category'] == kind for item in issues)
                                     for kind in ('bug', 'security', 'style', 'performance', 'maintainability', 'unclassified')},
                     'total': len(issues), 'items': issues},
            'performance': {'status': 'measured' if metrics else 'not_measured', 'metrics': metrics},
            'paths': {'project_directory': (candidate or {}).get('source_repository') or next(
                (a['path'] for a in artifacts if a['kind'] == 'code' and a.get('path')), (project or {}).get('local_path')),
                'test_directories': list(dict.fromkeys(a['path'] for a in artifacts if a['kind'] == 'test_code' and a.get('path')))},
            'artifacts': artifacts}

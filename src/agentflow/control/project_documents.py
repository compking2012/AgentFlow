"""Project stage heads backed by immutable, run-bound document snapshots.

The SQLite writer fences publication against both the current product change and
its work generation. Files are replaceable projections; artifact bytes and each
run's snapshot records remain immutable.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.documents_projection import (
    copy_digest,
    projection_state,
    render_group_document,
    write_current_copy,
)
from agentflow.control.readable import output_contract, safe_filename
from agentflow.control.workflow_stages import group_workflow_stages

_HISTORY_START = '<!-- agentflow:change-history -->'
_HISTORY_END = '<!-- /agentflow:change-history -->'


def _identity(product_id, stage):
    return str(uuid5(NAMESPACE_URL, 'agentflow:project-document:' + product_id + ':' + stage))


def _plain(content):
    text = content.decode('utf-8') if isinstance(content, bytes) else content
    return re.sub(re.escape(_HISTORY_START) + r'.*?' + re.escape(_HISTORY_END) + r'\s*', '', text,
                  flags=re.S).lstrip()


def _brief(value, limit=240):
    return ' '.join(str(value or '').split())[:limit].replace('|', '\\|')


def _sections(body):
    result, key = {}, '正文'
    for line in _plain(body).splitlines():
        if re.match(r'^#{1,6}\s+', line):
            key = re.sub(r'^#+\s+', '', line)
        result.setdefault(key, []).append(line)
    return {key: '\n'.join(lines) for key, lines in result.items()}


def _history_body(body, history, language):
    heading = 'Change History' if language == 'en' else '变更记录'
    columns = '| Date | Change | Updated sections / summary |' if language == 'en' else '| 日期 | 需求变更 | 更新章节／摘要 |'
    rows = [f"| {_brief(row['date'])} | {_brief(row['title'])} | {_brief(row['summary'], 500)} |" for row in history]
    return (_HISTORY_START + '\n## ' + heading + '\n\n' + columns + '\n| --- | --- | --- |\n'
            + '\n'.join(rows) + '\n' + _HISTORY_END + '\n\n' + _plain(body)).encode('utf-8')


class ProjectDocumentService:
    def __init__(self, store, artifacts, settings):
        self.store, self.artifacts, self.settings = store, artifacts, settings

    @staticmethod
    def product_for(run, plan, products):
        product_id = (plan or {}).get('product_contract', {}).get('product_id')
        if not product_id:
            return None
        product = next((row for row in products if row['id'] == product_id), None)
        if not product or product.get('project_id') != run.get('project_id'):
            raise DomainError('document_product_mismatch', '文档与项目归属不一致。')
        return product

    @staticmethod
    def root(product):
        output = Path(product['output_directory'])
        marker = output / '.agentflow-product.json'
        try:
            valid = (output.is_absolute() and output.resolve() == output and not output.is_symlink()
                and not marker.is_symlink() and marker.is_file() and marker.stat().st_size <= 4096
                and json.loads(marker.read_text()).get('product_id') == product['id'])
        except (OSError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise DomainError('output_ownership_changed', '产品工作目录归属无法核验，未写入文档。')
        return output / 'documents'

    async def _directory(self, product, stage):
        directory = self.root(product) / safe_filename(stage)
        for path in [directory, *directory.parents]:
            if path.is_symlink():
                raise DomainError('unsafe_document_path', '文档目录不能是符号链接。')
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if directory.resolve() != directory:
            raise DomainError('unsafe_document_path', '文档目录归属发生变化。')
        return directory

    @staticmethod
    def _current(product, run, plan):
        return (product.get('run_id') == run['id'] and not product.get('deleted_at')
                and product.get('current_change_id') == (plan or {}).get('product_contract', {}).get('change_id'))

    @staticmethod
    def _ref(record):
        return {'artifact_id': record['id'], **{key: record.get(key) for key in (
            'digest', 'name', 'run_id', 'work_item_id', 'generation', 'logical_stage_key', 'history', 'source_versions')}}

    async def _baseline(self, product, run, plan, stage):
        contract = (plan or {}).get('product_contract', {})
        baseline = contract.get('document_baseline')
        if baseline is None and contract.get('change_id'):
            change = await self.store.read('product_change', contract['change_id'])
            baseline = (change or {}).get('document_baseline')
        if baseline is not None:
            ref = baseline.get('documents', {}).get(stage)
            if not ref:
                return None
            record = await self.store.read('readable_artifact', ref['artifact_id'])
            if (not record or not record.get('project_document') or record.get('product_id') != product['id']
                    or record.get('logical_stage_key') != stage or record['digest'] != ref['digest']):
                raise DomainError('document_baseline_mismatch', '冻结文档基线无法核验。')
            await self.artifacts.verify(record['digest'])
            return record
        # Legacy runs have no explicit baseline. Only earlier runs of this same
        # product can contribute; a historical view never borrows a future head.
        run_ids = product.get('run_ids', [])
        preceding = run_ids[:run_ids.index(run['id'])] if run['id'] in run_ids else []
        records = [row for row in await self.store.list('readable_artifact')
                   if row.get('project_document') and row.get('product_id') == product['id']
                   and row.get('logical_stage_key') == stage and row.get('run_id') in preceding]
        return max(records, key=lambda row: (preceding.index(row['run_id']), row.get('created_at', ''), row['id'])) if records else None

    async def document(self, presenter, product, run, plan, group, items, *, materialize=True, include_code_paths=True):
        stage = group['key']
        # Fence against the head observed before baseline discovery or rendering.
        expected_head = await self.store.read('project_document_head', _identity(product['id'], stage))
        language = (plan or {}).get('product_contract', {}).get('language', 'zh-CN')
        work = {**group['work'], 'logical_stage_key': stage, 'language': language}
        revisions = await self.store.list('work_revision')
        state = projection_state(group, items, revisions)
        records = [row for row in await self.store.list('readable_artifact') if row.get('project_document')
                   and row.get('product_id') == product['id'] and row.get('run_id') == run['id']
                   and row.get('logical_stage_key') == stage]
        baseline = await self._baseline(product, run, plan, stage)
        baseline_id = baseline['id'] if baseline else None
        current = self._current(product, run, plan)
        record = next((row for row in records if row.get('projection_state') == state
                       and (not current or row.get('baseline_artifact_id') == baseline_id)), None)
        if not current and records:
            record = max(records, key=lambda row: (row.get('created_at', ''), row['id']))
        rendered = None
        if record is None:
            rendered = await render_group_document(group, items, self.artifacts, await self.store.list('artifact'),
                                                   revisions, language=language)
            if rendered is None or (rendered['pending'] and baseline and not records):
                if baseline:
                    return await presenter._document_descriptor(work, baseline, None, None,
                                                                 include_code_paths=include_code_paths)
                return None
            prior = max(records, key=lambda row: (row.get('created_at', ''), row['id'])) if records else baseline
            history = list((baseline or prior or {}).get('history', []))
            change_id = (plan or {}).get('product_contract', {}).get('change_id')
            change = await self.store.read('product_change', change_id) if change_id else None
            comparison = baseline or prior
            old_sections = _sections(await self.artifacts.read(comparison['digest'])) if comparison else {}
            sections = _sections(rendered['content'])
            changed = [key for key in dict.fromkeys([*sections, *old_sections]) if sections.get(key) != old_sections.get(key)]
            title = (change or {}).get('title') or ('Initial version' if language == 'en' else '初始版本')
            date = ((change or {}).get('created_at') or run.get('created_at') or utc_now())[:10]
            summary = '、'.join(changed[:12]) or ('Reviewed existing content' if language == 'en' else '复核已有内容')
            description = (change or {}).get('description')
            if description:
                summary += '；' + _brief(description)
            row = {'change_id': change_id, 'run_id': run['id'], 'date': date, 'title': title, 'summary': summary}
            # Regeneration revises the same change entry instead of nesting the
            # entire previous document or repeatedly appending its history.
            history = [entry for entry in history if entry['run_id'] != run['id']] + [row]
            content = _history_body(rendered['content'], history, language)
            blob = await self.artifacts.put_bytes(content)
            identity = str(uuid5(NAMESPACE_URL, 'agentflow:project-document-version:' + canonical_digest({
                'product': product['id'], 'run': run['id'], 'stage': stage, 'state': state, 'digest': blob['id'], 'baseline': baseline_id})))
            name = (expected_head or prior or {}).get('name') or output_contract(work)['artifact_name'] + '.md'
            def register(tx):
                presenter._validate_group(tx, run['id'], stage, state)
                if any(not presenter._source_version_matches(tx.get('artifact', version['id']), version)
                       for version in rendered['source_versions']):
                    raise DomainError('stale_artifact', '文档来源已更新。')
                return tx.get('readable_artifact', identity) or tx.put('readable_artifact', identity, {
                    'project_document': True, 'product_id': product['id'], 'run_id': run['id'],
                    'work_item_id': work['id'], 'generation': work['generation'], 'logical_stage_key': stage,
                    'projection_state': state, 'digest': blob['id'], 'name': name, 'media_type': 'text/markdown',
                    'source_versions': rendered['source_versions'], 'history': history,
                    'previous_artifact_id': prior['id'] if prior else None, 'baseline_artifact_id': baseline_id,
                    'created_at': utc_now()})
            record = await self.store.command('project.document.snapshot', str(uuid4()), {'id': identity}, register)
        await self.artifacts.verify(record['digest'])
        path, error = (None, None)
        if current and materialize:
            path, error = await self._publish(presenter, product, run, plan, group, state, record, expected_head)
        return await presenter._document_descriptor(work, record, path, error,
            source_work=rendered['source_work'] if rendered else None, include_code_paths=include_code_paths)

    async def _publish(self, presenter, product, run, plan, group, state, record, expected_head):
        try:
            directory = await self._directory(product, group['key'])
            path = directory / safe_filename(record['name'])
            content = await self.artifacts.read(record['digest'])
            # Even the idempotent fast path rechecks ownership and the product
            # identity after asynchronous preflight work.
            latest = await self.store.read('product', product['id'])
            if not self._current(latest, run, plan):
                return None, None
            if (self.root(latest) != directory.parent):
                raise DomainError('output_ownership_changed', '产品工作目录归属发生变化。')
            existing = copy_digest(path)
            if expected_head and expected_head.get('artifact_id') == record['id'] and existing == record['digest']:
                return path, None
            def publish(tx):
                latest = tx.get('product', product['id'])
                current_run = tx.get('run', run['id'])
                current_plan = tx.get('plan', current_run['plan_id'])
                if not self._current(latest, current_run, current_plan):
                    raise DomainError('stale_artifact', '当前需求已变化，历史快照已保留。')
                presenter._validate_group(tx, run['id'], group['key'], state)
                if self.root(latest) != directory.parent:
                    raise DomainError('output_ownership_changed', '产品工作目录归属发生变化。')
                head = tx.get('project_document_head', _identity(product['id'], group['key']))
                if (head or {}).get('revision') != (expected_head or {}).get('revision'):
                    raise DomainError('stale_artifact', '项目文档已更新，请重新读取。')
                registered = tx.get('readable_artifact', record['id'])
                if not registered or any(not presenter._source_version_matches(tx.get('artifact', row['id']), row)
                                          for row in registered['source_versions']):
                    raise DomainError('stale_artifact', '文档来源已更新。')
                # All validation precedes filesystem replacement. Only the
                # presently owned head can authorize replacing existing bytes.
                updated = tx.put('project_document_head', _identity(product['id'], group['key']), {
                    'product_id': product['id'], 'logical_stage_key': group['key'], 'run_id': run['id'],
                    'artifact_id': record['id'], 'digest': record['digest'], 'name': record['name'], 'path': str(path)},
                    head['revision'] if head else None)
                write_current_copy(path, content, [head['digest']] if head else [])
                return updated
            await self.store.command('project.document.publish', str(uuid4()), {'id': record['id']}, publish)
            return path, None
        except (DomainError, OSError, ValueError) as exc:
            return None, exc.message if isinstance(exc, DomainError) else '工作目录暂时无法保存文档，可查看已归档版本。'

    async def freeze(self, product_id, run_id=None, *, materialize=True):
        """Return verified immutable stage refs for a change or delivery snapshot."""
        from agentflow.control.presentation import RunPresentationService
        product = await self.store.read('product', product_id)
        if not product:
            raise DomainError('not_found', 'Unknown product', 404)
        run_id = run_id or product.get('run_id')
        if not run_id:
            return {'product_id': product_id, 'run_id': None, 'documents': {}}
        run = await self.store.read('run', run_id)
        plan = await self.store.read('plan', run['plan_id']) if run and run.get('plan_id') else None
        self.product_for(run or {}, plan, [product])
        if not plan or plan.get('product_contract', {}).get('product_id') != product_id:
            raise DomainError('document_product_mismatch', '文档与项目归属不一致。')
        documents = {}
        current = self._current(product, run, plan)
        if current and materialize:
            root = self.root(product)
            for head in await self.store.list('project_document_head'):
                if head.get('product_id') != product_id:
                    continue
                path = root / safe_filename(head['logical_stage_key']) / safe_filename(head['name'])
                if path.exists() or path.is_symlink():
                    if copy_digest(path) != head['digest']:
                        raise DomainError('document_modified', '工作目录中的文档已修改，原文件已保留。')
        baseline = plan.get('product_contract', {}).get('document_baseline')
        if baseline is None:
            run_ids = product.get('run_ids', [])
            previous = run_ids[:run_ids.index(run_id)] if run_id in run_ids else []
            if previous:
                inherited = await self.freeze(product_id, previous[-1], materialize=False)
                documents.update(inherited['documents'])
        baseline = baseline or {}
        for stage in baseline.get('documents', {}):
            prior = await self._baseline(product, run, plan, stage)
            if prior:
                documents[stage] = self._ref(prior)
        items = [row for row in await self.store.list('work_item') if row.get('run_id') == run_id and not row.get('archived')]
        groups = group_workflow_stages(items, plan, await self.store.list('product_test_runtime_repair'),
                                      await self.store.list('review_repair'))
        presenter = RunPresentationService(self.store, self.artifacts, self.settings)
        for group in groups:
            descriptor = await self.document(presenter, product, run, plan, group, items,
                                             materialize=materialize, include_code_paths=False)
            if not descriptor:
                continue
            if descriptor.get('storage_error'):
                raise DomainError('document_modified', descriptor['storage_error'])
            record = await self.store.read('readable_artifact', descriptor['artifact_id'])
            documents[group['key']] = self._ref(record)
        if current and materialize:
            actual = {group['key'] for group in groups}
            for stage, ref in documents.items():
                if stage not in actual or ref.get('run_id') != run_id:
                    await self._publish_inherited(product, run, plan, stage, ref)
        return {'product_id': product_id, 'run_id': run_id, 'documents': documents}

    async def _publish_inherited(self, product, run, plan, stage, ref):
        directory = await self._directory(product, stage)
        path = directory / safe_filename(ref['name'])
        head_id = _identity(product['id'], stage)
        head = await self.store.read('project_document_head', head_id)
        content = await self.artifacts.read(ref['digest'])
        if head and head['artifact_id'] == ref['artifact_id'] and copy_digest(path) == ref['digest']:
            return
        def publish(tx):
            latest = tx.get('product', product['id'])
            current_run = tx.get('run', run['id'])
            current_plan = tx.get('plan', current_run['plan_id'])
            if not self._current(latest, current_run, current_plan) or self.root(latest) != directory.parent:
                raise DomainError('stale_artifact', '当前需求或文档归属已变化。')
            prior = tx.get('project_document_head', head_id)
            if (prior or {}).get('revision') != (head or {}).get('revision'):
                raise DomainError('stale_artifact', '项目文档已更新，请重新读取。')
            record = tx.get('readable_artifact', ref['artifact_id'])
            if (not record or record.get('product_id') != product['id'] or record['digest'] != ref['digest']
                    or record.get('logical_stage_key') != stage or not record.get('project_document')):
                raise DomainError('document_baseline_mismatch', '冻结文档基线无法核验。')
            result = tx.put('project_document_head', head_id, {'product_id': product['id'],
                'logical_stage_key': stage, 'run_id': record['run_id'], 'artifact_id': record['id'],
                'digest': record['digest'], 'name': record['name'], 'path': str(path)}, prior['revision'] if prior else None)
            write_current_copy(path, content, [prior['digest']] if prior else [])
            return result
        await self.store.command('project.document.inherit', str(uuid4()), {'id': ref['artifact_id']}, publish)

    @staticmethod
    def _proven_legacy_source(record, source, version, revisions):
        """The oldest readable schema omitted stale; a completed revision proves its identity."""
        if (record.get('projection_key') or 'stale' in version or not source
                or source.get('digest') != version.get('digest')
                or source.get('generation') != version.get('generation')
                or source.get('generation') != record.get('generation')
                or source.get('work_item_id') != record.get('work_item_id')
                or source.get('run_id') != record.get('run_id')):
            return False
        return any(row.get('work_item_id') == record['work_item_id']
            and (snapshot := row.get('snapshot', {})).get('id') == record['work_item_id']
            and snapshot.get('run_id') == record['run_id']
            and snapshot.get('generation') == record['generation']
            and snapshot.get('status') == 'completed'
            and version['id'] in snapshot.get('artifact_ids', []) for row in revisions)

    async def migrate(self, product_id):
        """Flatten only verified generated legacy copies; conflicts preserve all files.

        Run explicitly during authenticated product maintenance. Read-only views
        never invoke migration. Unknown files, links, edits, and invalid source
        identities are reported before publishing or removing any legacy copy.
        """
        import os

        from agentflow.control.documents_projection import (
            finder_metadata_digest,
            legacy_copies,
            prune_empty_legacy_directories,
            prune_legacy_copies,
        )
        from agentflow.control.presentation import RunPresentationService
        product = await self.store.read('product', product_id)
        if not product:
            raise DomainError('not_found', 'Unknown product', 404)
        root = self.root(product)
        runs = {row['id']: row for row in await self.store.list('run')}
        plans = {row['id']: row for row in await self.store.list('plan')}
        run_ids = [identity for identity in product.get('run_ids', []) if identity in runs]
        if product.get('run_id') in runs and product['run_id'] not in run_ids:
            run_ids.append(product['run_id'])
        works = await self.store.list('work_item')
        revisions = await self.store.list('work_revision')
        work_by_id = {row['id']: row for row in works}
        sources = {row['id']: row for row in await self.store.list('artifact')}
        records = [row for row in await self.store.list('readable_artifact') if row.get('run_id') in run_ids
                   and not row.get('project_document')]
        candidates, conflicts, imports, by_path = {}, [], [], {}
        for record in records:
            run = runs[record['run_id']]
            plan = plans.get(run.get('plan_id'), {})
            if (run.get('project_id') != product.get('project_id')
                    or plan.get('product_contract', {}).get('product_id') != product_id):
                continue
            work = work_by_id.get(record.get('work_item_id'))
            if not work:
                continue
            stage = record.get('logical_stage_key') or work.get('step')
            paths = ([root / safe_filename(run['id']) / safe_filename(stage) / safe_filename(record['name'])]
                     if record.get('projection_key') else
                     [path for path, _ in legacy_copies(root, [record], [work])])
            found = False
            for path in paths:
                if not path.exists() and not path.is_symlink():
                    continue
                found = True
                by_path.setdefault(path, []).append(record)
            if found and not work.get('parent_stage_id'):
                imports.append((record, work, stage))
        # One mutable legacy filename can have many registered immutable
        # versions. Its bytes need to match one proven version, not every one.
        matched, historical_only = set(), set()
        for path, versions in by_path.items():
            try:
                digest = copy_digest(path)
                record = next((row for row in versions if row['digest'] == digest), None)
                if not record:
                    raise DomainError('document_modified', '已登记旧文档被修改，已保留原文件。')
                await self.artifacts.verify(digest)
                for version in record.get('source_versions', []):
                    source = sources.get(version['id'])
                    if not RunPresentationService._source_version_matches(source, version):
                        if not self._proven_legacy_source(record, source, version, revisions):
                            raise DomainError('stale_artifact', '旧文档来源无法核验，已保留原文件。')
                        historical_only.add(record['id'])
                candidates[path] = digest
                matched.add(record['id'])
            except (DomainError, OSError, ValueError) as exc:
                conflicts.append({'path': str(path), 'reason': exc.message if isinstance(exc, DomainError) else '旧文档路径无法安全核验。'})
        for identity in run_ids:
            directory = root / safe_filename(identity)
            if directory.is_symlink():
                conflicts.append({'path': str(directory), 'reason': '旧文档目录不能是符号链接。'})
                continue
            if not directory.is_dir():
                continue
            for base, directories, names in os.walk(directory, followlinks=False):
                for name in directories + names:
                    path = Path(base) / name
                    if name == '.DS_Store' and path not in candidates:
                        metadata_digest = finder_metadata_digest(path)
                        if metadata_digest:
                            candidates[path] = metadata_digest
                    if path.is_symlink() or (path.is_file() and path not in candidates):
                        conflicts.append({'path': str(path), 'reason': '文件来源无法核验，已保留原文件。'})
        if conflicts:
            return {'product_id': product_id, 'migrated': 0, 'removed': [], 'conflicts': conflicts}
        presenter = RunPresentationService(self.store, self.artifacts, self.settings)
        imports.sort(key=lambda item: (run_ids.index(item[0]['run_id']), item[0].get('generation', 0),
                                      item[0]['id'] in matched, item[0]['id']))
        prior_by_stage = {}
        for record, work, stage in imports:
            run = runs[record['run_id']]
            plan = plans[run['plan_id']]
            contract = plan.get('product_contract', {})
            language = contract.get('language', 'zh-CN')
            change = await self.store.read('product_change', contract['change_id']) if contract.get('change_id') else None
            prior = prior_by_stage.get(stage)
            history = [row for row in (prior or {}).get('history', []) if row['run_id'] != run['id']]
            await self.artifacts.verify(record['digest'])
            body = await self.artifacts.read(record['digest'])
            old_sections = _sections(await self.artifacts.read(prior['digest'])) if prior else {}
            changed = [key for key, value in _sections(body).items() if value != old_sections.get(key)]
            history.append({'run_id': run['id'], 'change_id': contract.get('change_id'),
                'date': ((change or {}).get('created_at') or run.get('created_at') or utc_now())[:10],
                'title': (change or {}).get('title') or ('Initial version' if language == 'en' else '初始版本'),
                'summary': '、'.join(changed[:12]) or '复核已有内容'})
            blob = await self.artifacts.put_bytes(_history_body(body, history, language))
            identity = str(uuid5(NAMESPACE_URL, 'agentflow:migrated-document:' + product_id + ':' + record['id']))
            def register(tx):
                existing = tx.get('readable_artifact', identity)
                if existing:
                    return existing
                source = tx.get('readable_artifact', record['id'])
                if source != record:
                    raise DomainError('stale_artifact', '旧文档来源已更新。')
                if record['id'] in historical_only:
                    for version in record.get('source_versions', []):
                        artifact = tx.get('artifact', version['id'])
                        if (not presenter._source_version_matches(artifact, version)
                                and not self._proven_legacy_source(record, artifact, version, tx.list('work_revision'))):
                            raise DomainError('stale_artifact', '旧文档历史身份已变化，已保留原文件。')
                group, current_items = presenter._current_group(tx, run['id'], stage)
                active_sources = set((group or {}).get('work', {}).get('artifact_ids', []))
                active = (record['id'] not in historical_only and group
                          and record['generation'] == group['work']['generation']
                          and any(version['id'] in active_sources for version in record.get('source_versions', [])))
                state = projection_state(group, current_items, tx.list('work_revision')) if active else 'legacy:' + record['id']
                return tx.put('readable_artifact', identity, {'project_document': True, 'product_id': product_id,
                    'run_id': run['id'], 'work_item_id': work['id'], 'generation': record['generation'],
                    'logical_stage_key': stage, 'projection_state': state, 'digest': blob['id'],
                    'name': (prior or {}).get('name') or record['name'], 'media_type': 'text/markdown',
                    'source_versions': record.get('source_versions', []), 'history': history,
                    'previous_artifact_id': prior['id'] if prior else None, 'legacy_artifact_id': record['id'],
                    'created_at': utc_now()})
            prior_by_stage[stage] = await self.store.command('project.document.migrate-snapshot', str(uuid4()), {'id': identity}, register)
        # Freeze chooses the current run and inherited stages without changing
        # any historical project's filesystem. It also detects canonical edits.
        try:
            frozen = await self.freeze(product_id)
            def cleanup(tx):
                latest = tx.get('product', product_id)
                if latest.get('run_id') != product.get('run_id') or latest.get('current_change_id') != product.get('current_change_id'):
                    raise DomainError('stale_artifact', '当前需求已变化。')
                if self.root(latest) != root:
                    raise DomainError('output_ownership_changed', '产品工作目录归属发生变化。')
                for stage, ref in frozen['documents'].items():
                    head = tx.get('project_document_head', _identity(product_id, stage))
                    flat = root / safe_filename(stage) / safe_filename(ref['name'])
                    if not head or head.get('digest') != ref['digest'] or copy_digest(flat) != ref['digest']:
                        raise DomainError('document_not_published', '当前文档尚未安全发布，已保留旧目录。')
                if candidates and not frozen['documents']:
                    raise DomainError('document_not_published', '当前文档尚未安全发布，已保留旧目录。')
                for path, digest in candidates.items():
                    if copy_digest(path) != digest:
                        raise DomainError('document_modified', '旧文档被修改，已保留原文件。')
                prune_legacy_copies(candidates.items())
                return {'removed': [str(path) for path in candidates if not path.exists()],
                        'conflicts': [{'path': str(path), 'reason': '旧文档在清理前发生变化，已保留原文件。'}
                                      for path in candidates if path.exists() or path.is_symlink()]}
            removed = await self.store.command('project.document.migrate-cleanup', str(uuid4()), {'product_id': product_id}, cleanup) if candidates else {'removed': [], 'conflicts': []}
        except (DomainError, OSError, ValueError) as exc:
            return {'product_id': product_id, 'migrated': len(imports), 'removed': [],
                    'conflicts': [{'path': str(root), 'reason': str(exc)}]}
        for identity in run_ids:
            prune_empty_legacy_directories(root / safe_filename(identity))
        return {'product_id': product_id, 'migrated': len(imports), 'removed': removed['removed'], 'conflicts': removed['conflicts']}

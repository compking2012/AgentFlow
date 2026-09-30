"""Export confirmed Web/API deliveries as independently runnable local packages."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.documents_projection import render_group_document
from agentflow.control.product_management import frozen_product_version
from agentflow.control.project_documents import ProjectDocumentService
from agentflow.control.readable import (
    output_contract,
    safe_filename,
    test_report_markdown,
)
from agentflow.control.workflow_stages import group_workflow_stages
from agentflow.execution.manifests import file_digest, tree_digest
from agentflow.execution.transport import safe_extract_tar
from agentflow.repository import RepositoryAdapter
from agentflow.runtime.launcher import atomic_json


class ProductExporter:
    def __init__(self, store, artifacts, nodes, data_dir):
        self.store, self.artifacts, self.nodes = store, artifacts, nodes
        self.root = Path(data_dir) / 'product_exports'
        self.repository = RepositoryAdapter()

    async def verify_product(self, product):
        persisted = await self.store.read('product', product['id'])
        if (not persisted or persisted.get('state') != 'completed' or not persisted.get('delivery')
                or any(persisted.get(key) != product.get(key) for key in ('run_id', 'output_directory', 'delivery'))):
            raise DomainError('delivery_identity_mismatch', 'Product does not reference its current confirmed delivery')
        run = await self.store.read('run', product['run_id'])
        if not run or not run.get('delivery_ids'):
            raise DomainError('delivery_identity_mismatch', 'Product has no confirmed delivery record')
        delivery = await self.store.read('delivery', run['delivery_ids'][-1])
        if not delivery:
            raise DomainError('delivery_identity_mismatch', 'The confirmed delivery record is missing')
        receipt = await self.export(product, delivery)
        if receipt != product['delivery']:
            raise DomainError('delivery_receipt_mismatch', 'Product receipt differs from the verified export')
        return receipt

    async def verify_version(self, product, run_id):
        run = await self.store.read('run', run_id)
        plan = await self.store.read('plan', run['plan_id']) if run and run.get('plan_id') else None
        contract = (plan or {}).get('product_contract', {})
        if (not run or not plan or contract.get('product_id') != product['id']
                or run.get('project_id') != product.get('project_id') or not run.get('delivery_ids')):
            raise DomainError('delivery_identity_mismatch', '该交付版本不属于当前产品。')
        delivery = await self.store.read('delivery', run['delivery_ids'][-1])
        version = frozen_product_version(product, run, plan)
        return await self.export(version, delivery or {})

    async def export(self, product: dict, delivery: dict) -> dict:
        persisted = await self.store.read('delivery', delivery.get('id', 'missing'))
        candidate = await self.store.read('candidate', delivery.get('candidate_id', 'missing'))
        run = await self.store.read('run', product['run_id'])
        if (not run or not persisted or persisted != delivery or delivery['id'] not in run.get('delivery_ids', [])
                or run.get('execution_state') != 'completed' or run.get('quality_result') != 'passed'
                or not delivery.get('confirmed_at') or not candidate or candidate['run_id'] != run['id']
                or candidate['source_commit'] != delivery['commit_oid']
                or candidate.get('tree_oid') != delivery.get('tree_oid')
                or candidate['run_input_fingerprint'] != run['input_fingerprint']
                or candidate['fingerprint'] != delivery['candidate_fingerprint']):
            raise DomainError('delivery_identity_mismatch', 'Only the confirmed current product candidate can be exported')
        plan = await self.store.read('plan', run['plan_id']) if run.get('plan_id') else None
        if plan:
            product = frozen_product_version(product, run, plan)
        output = self._owned_output(product)
        stage = self.root / product['id']
        if product.get('current_change_id'):
            stage = stage / 'runs' / run['id']
        stage.mkdir(parents=True, mode=0o700, exist_ok=True)
        release = output / 'releases' / run['id'] if product.get('current_change_id') else output / 'release'
        if release.parent.is_symlink() or release.parent.resolve() != release.parent:
            raise DomainError('unsafe_export', 'Delivery versions directory cannot be a symbolic link')
        release.parent.mkdir(parents=True, exist_ok=True)
        if release.is_symlink():
            raise DomainError('unsafe_export', 'Delivery output cannot be a symbolic link')
        receipt_path = stage / 'receipt.json'
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            manifest = await asyncio.to_thread(self.verify, receipt)
            if (Path(receipt['path']) != release or Path(receipt['archive_path']) != stage / 'product.zip'
                    or any(manifest.get(key) != expected for key, expected in {
                        'product_id': product['id'], 'run_id': run['id'], 'source_commit': candidate['source_commit'],
                        'candidate_fingerprint': candidate['fingerprint']}.items())):
                raise DomainError('delivery_receipt_mismatch', 'Cached export belongs to a different product or candidate')
            return receipt
        components = [a for a in candidate['platform_manifest']['artifacts']
                      if a['app_target'] == product['target'] and a['component_role'] == 'product']
        if len(components) != 1:
            raise DomainError('product_package_missing', 'The delivery needs exactly one verified runnable product package')
        component = components[0]
        node_artifact = await self.store.read('node_artifact', component['artifact_version_id'])
        if (not component.get('verified_upload') or not node_artifact or node_artifact.get('state') != 'complete'
                or node_artifact.get('digest') != component['digest']):
            raise DomainError('product_package_unverified', 'The runnable product must reference a verified complete upload')
        archive = self.nodes.artifacts.object_path(node_artifact['digest'])
        if file_digest(archive) != component['digest']:
            raise DomainError('product_package_corrupt', 'Built product package failed its delivery digest check')
        pending = stage / 'pending'
        if pending.exists():
            shutil.rmtree(pending)
        pending.mkdir(mode=0o700)
        materialized = stage / 'materialized'
        if materialized.exists():
            shutil.rmtree(materialized)
        await asyncio.to_thread(safe_extract_tar, archive, materialized)
        relative = component.get('metadata', {}).get('relative_path')
        built = (materialized / (relative or '')).resolve()
        if not built.is_relative_to(materialized.resolve()) or not built.is_dir():
            raise DomainError('invalid_product_layout', 'Built product path must be a directory inside its package')
        if tree_digest(built) != component.get('metadata', {}).get('content_digest'):
            raise DomainError('product_content_mismatch', 'Extracted product differs from the tested bytes')
        await asyncio.to_thread(shutil.copytree, built, pending / 'product')
        if not (pending / 'product/server.mjs').is_file():
            raise DomainError('product_entry_missing', 'The tested Web/API package must provide server.mjs')
        source = pending / 'source'
        source.mkdir()
        await asyncio.to_thread(self._write_source, Path(candidate['source_repository']), candidate['source_commit'], source)
        await self.repository.prepare_bundle(Path(candidate['source_repository']), candidate['source_commit'], pending / 'repository.bundle')
        docs = pending / 'documents'
        docs.mkdir()
        await self._write_documents(docs, product, run, plan)
        language = (plan or {}).get('product_contract', {}).get('language', 'zh-CN')
        reports = pending / 'reports'
        reports.mkdir()
        raw_reports = pending / 'evidence/raw_reports'
        raw_reports.mkdir(parents=True)
        for check in await self.store.list('check'):
            if check.get('run_id') != run['id'] or check.get('candidate_fingerprint') != candidate['fingerprint']:
                continue
            raw = await self.store.read('node_artifact', check['raw_report_artifact_id'])
            origin = self.nodes.artifacts.object_path(raw['digest'])
            if file_digest(origin) != raw['digest']:
                raise DomainError('test_report_corrupt', 'A delivery test report is corrupt')
            shutil.copyfile(origin, raw_reports / (check['id'] + '.report'))
            result = await self.store.read('node_result', check.get('node_result_id', ''))
            normalized = [v['normalized_report'] for v in (result or {}).get('verified_checks', [])
                          if v.get('matrix_entry_id') == check.get('matrix_entry_id') and v.get('normalized_report')]
            entry = candidate.get('matrix_mappings', {}).get(check.get('matrix_entry_id'), {})
            title = (('Unit Test Report' if entry.get('phase') == 'unit' else 'Integration Test Report')
                     if language == 'en' else ('单元测试报告' if entry.get('phase') == 'unit' else '集成测试报告'))
            (reports / (title + '-' + check['id'][:8] + '.md')).write_text(
                test_report_markdown(title, normalized, language=language), encoding='utf-8')
        instructions = (
            "Requires Node.js 22.13 or later; no third-party installation is needed at runtime.\n\n"
            "Run `./start.sh` here and open the local URL printed in the terminal. PORT defaults to 3000.\n"
            "Managed versions share runtime/data under the product directory. A separately extracted release uses "
            "<extraction-directory>.runtime/data, isolated from other copies.\n"
            "source contains the exact delivered code; documents and reports contain development and test evidence.\n"
            f"\nSource commit: `{candidate['source_commit']}`\nLocal Git reference: `{delivery['delivery_ref']}`\n"
            if language == 'en' else
            "需要 Node.js 22.13 或以上。产品运行时无需安装第三方依赖。\n\n"
            "在本目录执行 `./start.sh`，打开终端显示的本地地址。可用 PORT 指定端口，默认 3000。\n"
            "平台内的各版本共享产品目录下的 runtime/data；独立解压运行使用“解压目录名.runtime/data”，不同解压目录相互隔离。\n"
            "source 是精确交付源码，documents 和 reports 保留研发与测试证据。\n"
            f"\n源码提交：`{candidate['source_commit']}`\n本地 Git 引用：`{delivery['delivery_ref']}`\n")
        (pending / 'README.md').write_text(f"# {product['name']}\n\n{product['goal']}\n\n" + instructions)
        (pending / 'runtime-path.cjs').write_text('''const fs = require('node:fs');
const path = require('node:path');
const release = path.resolve(process.argv[2]);
let runtime = release + '.runtime';
try {
  const manifest = JSON.parse(fs.readFileSync(path.join(release, 'delivery-manifest.json'), 'utf8'));
  const parent = path.dirname(release);
  const root = path.basename(parent) === 'releases' && path.basename(release) === manifest.run_id
    ? path.dirname(parent) : path.basename(release) === 'release' ? parent : null;
  if (root) {
    const markerPath = path.join(root, '.agentflow-product.json');
    const stat = fs.lstatSync(markerPath);
    if (stat.isFile() && !stat.isSymbolicLink() && stat.size <= 4096) {
      const marker = JSON.parse(fs.readFileSync(markerPath, 'utf8'));
      if (marker.product_id === manifest.product_id) runtime = path.join(root, 'runtime');
    }
  }
} catch (_) { /* A standalone package owns only its adjacent runtime directory. */ }
process.stdout.write(runtime);
''', encoding='utf-8')
        (pending / 'start.sh').write_text('#!/bin/sh\nset -eu\nrelease=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
            'runtime=$(node "$release/runtime-path.cjs" "$release")\n'
            'mkdir -p "$runtime/data" "$runtime/home"\n'
            'exec env -i PATH="$PATH" HOME="$runtime/home" HOST=127.0.0.1 PORT="${PORT:-3000}" '
            'AGENTFLOW_DATA_DIR="$runtime/data" node "$release/product/server.mjs"\n')
        (pending / 'start.sh').chmod(0o755)
        files = self._inventory(pending)
        manifest = {'product_id': product['id'], 'run_id': run['id'], 'source_commit': candidate['source_commit'],
                    'candidate_fingerprint': candidate['fingerprint'], 'files': files}
        (pending / 'delivery-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        if release.exists():
            existing = release / 'delivery-manifest.json'
            if not existing.is_file() or json.loads(existing.read_text()) != manifest or self._inventory(release) != files:
                raise DomainError('delivery_directory_changed', 'Output release directory exists with different content')
            shutil.rmtree(pending)
        else:
            # Copy onto the destination filesystem before atomically publishing the directory.
            destination_stage = output / ('.release-' + product['id'])
            if destination_stage.exists():
                shutil.rmtree(destination_stage)
            shutil.copytree(pending, destination_stage)
            destination_stage.rename(release)
            shutil.rmtree(pending)
        zip_path = stage / 'product.zip'
        temp_zip = stage / 'product.zip.tmp'
        # Frozen tar entries intentionally use Unix epoch timestamps. ZIP starts
        # at 1980; clamp ZIP metadata without touching the tested product bytes.
        with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED, strict_timestamps=False) as zipped:
            for path in sorted(release.rglob('*')):
                if path.is_file():
                    zipped.write(path, path.relative_to(release))
        temp_zip.replace(zip_path)
        receipt = {'product_id': product['id'], 'run_id': run['id'], 'candidate_fingerprint': candidate['fingerprint'],
            'source_commit': candidate['source_commit'], 'path': str(release),
            'archive_path': str(zip_path), 'archive_digest': file_digest(zip_path),
            'archive_download_url': f"/api/v1/products/{product['id']}/download?run_id={run['id']}",
            'launch_command': './start.sh', 'working_directory': str(release),
            'manifest_digest': canonical_digest(manifest), 'exported_at': utc_now()}
        atomic_json(receipt_path, receipt)
        await asyncio.to_thread(self.verify, receipt)
        return receipt

    async def _write_documents(self, docs, product, run, plan):
        if (plan or {}).get('product_contract', {}).get('product_id') == product['id']:
            snapshots = await ProjectDocumentService(self.store, self.artifacts,
                SimpleNamespace(data_dir=self.root.parent)).freeze(product['id'], run['id'], materialize=False)
            for stage, ref in snapshots['documents'].items():
                await self.artifacts.verify(ref['digest'])
                directory = docs / safe_filename(stage)
                directory.mkdir(exist_ok=True)
                (directory / safe_filename(ref['name'])).write_bytes(await self.artifacts.read(ref['digest']))
            return
        all_artifacts = await self.store.list('artifact')
        language = (plan or {}).get('product_contract', {}).get('language', 'zh-CN')
        items = [work for work in await self.store.list('work_item')
                 if work.get('run_id') == run['id'] and not work.get('archived')]
        revisions = await self.store.list('work_revision')
        groups = group_workflow_stages(items, plan, await self.store.list('product_test_runtime_repair'),
                                       await self.store.list('review_repair'))
        for group in groups:
            rendered = await render_group_document(group, items, self.artifacts, all_artifacts,
                                                   revisions, language=language)
            if not rendered:
                continue
            name = safe_filename(output_contract(group['work'], language=language)['artifact_name']) + '.md'
            (docs / name).write_bytes(rendered['content'])

    @staticmethod
    def _owned_output(product):
        output = Path(product['output_directory'])
        marker = output / '.agentflow-product.json'
        if (not output.is_absolute() or output.is_symlink() or output.resolve() != output or not output.is_dir()
                or marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096):
            raise DomainError('unsafe_export', 'Product output ownership can no longer be verified')
        try:
            valid = json.loads(marker.read_text()).get('product_id') == product['id']
        except (OSError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise DomainError('unsafe_export', 'Output directory belongs to a different product')
        return output

    @staticmethod
    def _inventory(directory):
        rows = []
        for path in sorted(directory.rglob('*')):
            if path.is_symlink():
                raise DomainError('unsafe_export', 'Delivery files cannot be linked')
            if path.is_file() and path.relative_to(directory).as_posix() != 'delivery-manifest.json':
                rows.append({'path': path.relative_to(directory).as_posix(), 'digest': file_digest(path),
                             'executable': bool(path.stat().st_mode & 0o111)})
        return rows

    def verify(self, receipt):
        root = Path(receipt['path'])
        path = root / 'delivery-manifest.json'
        if (not root.is_absolute() or root.resolve() != root or root.is_symlink()
                or path.is_symlink() or not path.is_file()):
            raise DomainError('delivery_missing', 'The verified release directory is missing')
        manifest = json.loads(path.read_text())
        if (canonical_digest(manifest) != receipt['manifest_digest'] or self._inventory(root) != manifest['files']
                or manifest.get('product_id') != receipt.get('product_id')
                or manifest.get('source_commit') != receipt.get('source_commit')
                or manifest.get('run_id') != receipt.get('run_id')
                or manifest.get('candidate_fingerprint') != receipt.get('candidate_fingerprint')):
            raise DomainError('delivery_changed', 'Delivered files changed after verification')
        if file_digest(Path(receipt['archive_path'])) != receipt['archive_digest']:
            raise DomainError('delivery_archive_changed', 'Delivery download archive failed verification')
        try:
            with zipfile.ZipFile(receipt['archive_path']) as archive:
                rows = manifest['files']
                expected = {row['path']: row for row in rows}
                names = archive.namelist()
                if len(names) != len(set(names)) or set(names) != set(expected) | {'delivery-manifest.json'}:
                    raise ValueError('Archive files do not match the delivered inventory')
                for name, row in expected.items():
                    with archive.open(name) as stream:
                        if 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest() != row['digest']:
                            raise ValueError('Archive bytes do not match the tested release')
                    if bool((archive.getinfo(name).external_attr >> 16) & 0o111) != row['executable']:
                        raise ValueError('Archive executable mode does not match the release')
                if json.loads(archive.read('delivery-manifest.json')) != manifest:
                    raise ValueError('Archive manifest differs from the release')
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            raise DomainError('delivery_archive_changed', 'Download contents differ from the verified release') from exc
        return manifest

    def _write_source(self, repository, commit, destination):
        for row in self.repository._run(repository, ['ls-tree', '-r', '-z', commit]).split(b'\0'):
            if not row:
                continue
            head, raw_path = row.split(b'\t', 1)
            mode, kind, oid = head.split()
            name = Path(raw_path.decode())
            if name.is_absolute() or '..' in name.parts or kind != b'blob' or mode not in {b'100644', b'100755'}:
                raise DomainError('unsupported_source_entry', 'Source export requires regular relative files')
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.repository._run(repository, ['cat-file', 'blob', oid.decode()]))
            path.chmod(0o755 if mode == b'100755' else 0o644)

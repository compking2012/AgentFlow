"""Export boundary tests with actual Git, tar, CAS and ZIP bytes.

Confirmed run/build rows are injected protocol fixtures; they do not establish
that an Agent generated, reviewed, built or tested the fixture application.
"""
import json
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from agentflow.common import DomainError, canonical_digest
from agentflow.control.product_exports import ProductExporter
from agentflow.execution.manifests import file_digest, tree_digest
from agentflow.execution.service import NodeService
from agentflow.repository import RepositoryAdapter
from agentflow.storage import LocalArtifactStore, Store


@pytest_asyncio.fixture
async def exported(tmp_path, request):
    store = Store(tmp_path / 'data')
    await store.start()
    try:
        artifacts = LocalArtifactStore(tmp_path / 'data/artifacts')
        nodes = NodeService(store, tmp_path / 'data', 'https://127.0.0.1:9443')
        repo = tmp_path / 'source'
        repo.mkdir()
        git = RepositoryAdapter()
        git._run(repo, ['init'])
        (repo / 'server.mjs').write_text('// Export protocol fixture. Never executed.\n')
        git._run(repo, ['add', '--all'])
        git._run(repo, ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '-m', 'Fixture'])
        commit = git._run(repo, ['rev-parse', 'HEAD']).decode().strip()
        tree = git._run(repo, ['rev-parse', 'HEAD^{tree}']).decode().strip()
        built = tmp_path / 'built'
        built.mkdir()
        (built / 'server.mjs').write_text((repo / 'server.mjs').read_text())
        package = tmp_path / 'package.tar'
        def frozen_time(info):
            info.mtime = 0
            return info
        with tarfile.open(package, 'w') as archive:
            archive.add(built, arcname='app', filter=frozen_time)
        component = await nodes.import_input(package.read_bytes(), 'package.tar', 'run')
        output = tmp_path / 'output'
        output.mkdir()
        (output / '.agentflow-product.json').write_text(json.dumps({'product_id': 'product'}))

        def records(tx):
            tx.put('run', 'run', {'execution_state': 'completed', 'quality_result': 'passed',
                'input_fingerprint': 'input', 'delivery_ids': ['delivery']})
            tx.put('candidate', 'candidate', {'run_id': 'run', 'source_commit': commit, 'source_repository': str(repo),
                'tree_oid': tree, 'run_input_fingerprint': 'input', 'fingerprint': 'candidate-fingerprint',
                'platform_manifest': {'artifacts': [{'app_target': 'web', 'component_role': 'product',
                    'artifact_version_id': component['id'], 'digest': component['digest'], 'verified_upload': True,
                    'metadata': {'relative_path': 'app', 'content_digest': tree_digest(built)}}]}})
            delivery = tx.put('delivery', 'delivery', {'candidate_id': 'candidate', 'commit_oid': commit, 'tree_oid': tree,
                'candidate_fingerprint': 'candidate-fingerprint', 'confirmed_at': 'fixture-confirmed',
                'delivery_ref': 'refs/agentflow/delivered/fixture'})
            product = tx.put('product', 'product', {'name': 'Fixture', 'goal': 'Export boundary protocol fixture',
                'run_id': 'run', 'target': 'web', 'state': 'completed', 'output_directory': str(output)})
            return {'product': product, 'delivery': delivery}
        seeded = await store.command('fixture.export', 'seed', {}, records)
        product, delivery = seeded['product'], seeded['delivery']
        if getattr(request, 'param', None) == 'document_rework':
            document_rows = []
            for identity, key, dependencies, body, quality in [
                ('old-review', 'code_review', [], {'content': 'First review', 'findings': [
                    {'description': 'Missing owner check', 'severity': 'blocking', 'path': 'access.mjs'}]}, 'failed'),
                ('new-review', 'repair:review', ['old-review'], {'content': 'Current review approved', 'findings': []}, 'passed'),
                ('old-unit', 'unit_test_implementation:review', [], {'content': 'Initial unit review', 'findings': []}, 'passed'),
                ('new-unit', 'runtime:review', ['old-unit'], {'content': 'Current unit review approved', 'findings': []}, 'passed'),
            ]:
                blob = await artifacts.put_bytes(json.dumps(body).encode())
                document_rows.append((identity, key, dependencies, blob, quality))
            def documents(tx):
                for identity, key, dependencies, blob, quality in document_rows:
                    tx.put('artifact', identity + '-source', {'run_id': 'run', 'work_item_id': identity,
                        'generation': 1, 'digest': blob['id'], 'name': 'openhands_final.json', 'media_type': 'application/json'})
                    tx.put('work_item', identity, {'run_id': 'run', 'key': key, 'step': 'code_review',
                        'generation': 1, 'status': 'completed', 'quality_result': quality,
                        'dependencies': dependencies, 'artifact_ids': [identity + '-source']})
                tx.put('product_test_runtime_repair', 'runtime-repair', {
                    'run_id': 'run', 'phase': 'unit', 'review_work_item_id': 'new-unit'})
                return {}
            await store.command('fixture.export', 'documents', {}, documents)
        if getattr(request, 'param', None) == 'late_test_owner':
            blobs = {key: await artifacts.put_bytes(json.dumps({'content': value}).encode()) for key, value in {
                'code_review': 'Initial production review approved',
                'unit_test_implementation:review': 'Initial unit review approved',
                'late-review': 'Latest repaired unit code approved',
            }.items()}
            def rebound_documents(tx):
                tx.put('work_item', 'unit-owner', {'run_id': 'run', 'step': 'unit_test_implementation',
                    'key': 'unit_test_implementation', 'generation': 2, 'status': 'completed',
                    'quality_result': 'unknown', 'dependencies': ['code_review'], 'artifact_ids': []})
                for key, blob in blobs.items():
                    tx.put('artifact', key + '-source', {'run_id': 'run', 'work_item_id': key,
                        'generation': 1, 'digest': blob['id'], 'name': 'openhands_final.json', 'media_type': 'application/json'})
                    tx.put('work_item', key, {'run_id': 'run', 'step': 'code_review', 'key': key,
                        'generation': 1, 'status': 'completed', 'quality_result': 'passed',
                        'dependencies': [] if key == 'code_review' else ['unit-owner'], 'artifact_ids': [key + '-source']})
                tx.put('review_repair', 'late-attempt', {'run_id': 'run', 'mode': 'late_test_owner',
                    'review_work_item_id': 'late-review', 'owner_stage_work_item_ids': ['unit-owner'],
                    'review_bindings': {'late-review': {'dependencies': ['unit-owner'], 'minimum_generation': 1}},
                    'created_at': '2026-09-03T00:00:00+00:00'})
                return {}
            await store.command('fixture.export', 'rebound-documents', {}, rebound_documents)
        exporter = ProductExporter(store, artifacts, nodes, tmp_path / 'data')
        receipt = await exporter.export(product, delivery)
        def finish(tx):
            return tx.put('product', 'product', {**product, 'delivery': receipt}, product['revision'])
        product = await store.command('fixture.export', 'receipt', {}, finish)
        yield SimpleNamespace(store=store, exporter=exporter, product=product, delivery=delivery,
                              receipt=receipt, output=output, repo=repo, component=component)
    finally:
        await store.close()


async def test_export_reuses_only_exact_verified_receipt_and_preserves_executable_mode(exported):
    env = exported
    assert await env.exporter.export(env.product, env.delivery) == env.receipt
    assert await env.exporter.verify_product(env.product) == env.receipt
    release = Path(env.receipt['path'])
    assert (release / 'source/server.mjs').read_bytes() == (env.repo / 'server.mjs').read_bytes()
    assert (release / 'repository.bundle').stat().st_size > 0
    with zipfile.ZipFile(env.receipt['archive_path']) as archive:
        assert archive.read('source/server.mjs') == (env.repo / 'server.mjs').read_bytes()
        assert (archive.getinfo('start.sh').external_attr >> 16) & 0o111
        assert archive.getinfo('product/server.mjs').date_time == (1980, 1, 1, 0, 0, 0)
    assert (release / 'product/server.mjs').stat().st_mtime == 0


@pytest.mark.parametrize('exported', ['document_rework'], indirect=True)
async def test_export_has_one_current_document_per_logical_stage_with_issue_history(exported):
    docs = Path(exported.receipt['path']) / 'documents'
    assert sorted(path.name for path in docs.glob('*.md')) == ['代码审查报告.md', '单元测试代码审查报告.md']
    review = (docs / '代码审查报告.md').read_text()
    assert 'Current review approved' in review and 'First review' not in review
    assert 'Missing owner check' in review and '当前审查通过' in review
    assert 'Current unit review approved' in (docs / '单元测试代码审查报告.md').read_text()
    with zipfile.ZipFile(exported.receipt['archive_path']) as archive:
        assert len([name for name in archive.namelist() if name.startswith('documents/')]) == 2


@pytest.mark.parametrize('exported', ['late_test_owner'], indirect=True)
async def test_export_places_rebound_gate_report_with_unit_review_without_replacing_source_review(exported):
    docs = Path(exported.receipt['path']) / 'documents'
    assert sorted(path.name for path in docs.glob('*.md')) == ['代码审查报告.md', '单元测试代码审查报告.md']
    assert 'Initial production review approved' in (docs / '代码审查报告.md').read_text()
    assert 'Latest repaired unit code approved' in (docs / '单元测试代码审查报告.md').read_text()


async def test_historical_export_survives_product_rename_goal_change_and_soft_deletion(exported):
    env = exported
    before = Path(env.receipt['archive_path']).read_bytes()
    snapshot = {key: env.product[key] for key in ('name', 'goal', 'target')}
    def history(tx):
        tx.put('plan', 'historical-plan', {'product_contract': {'product_id': 'product',
            'product_snapshot': snapshot, 'target': 'web', 'config_revision': 1}})
        run = tx.get('run', 'run')
        tx.put('run', 'run', {**run, 'plan_id': 'historical-plan', 'project_id': 'source-project'}, run['revision'])
        product = tx.get('product', 'product')
        return tx.put('product', 'product', {**product, 'name': 'New name', 'goal': 'A different goal',
            'target': 'api', 'targets': ['api'], 'config_revision': 2, 'needs_restart': True,
            'deleted_at': 'fixture-deleted', 'project_id': 'source-project'}, product['revision'])
    edited = await env.store.command('fixture.export', 'historical-metadata', {}, history)
    assert await env.exporter.verify_version(edited, 'run') == env.receipt
    assert Path(env.receipt['archive_path']).read_bytes() == before
    assert env.repo.is_dir() and Path(env.receipt['path']).is_dir()


async def test_requirement_iteration_exports_a_version_without_overwriting_previous_delivery(exported):
    env = exported
    old_archive = Path(env.receipt['archive_path']).read_bytes()
    old_manifest = Path(env.receipt['path']) / 'delivery-manifest.json'
    old_bytes = old_manifest.read_bytes()
    old_candidate = await env.store.read('candidate', 'candidate')
    def next_version(tx):
        old_run = tx.get('run', 'run')
        tx.put('plan', 'old-plan', {'product_contract': {'product_id': 'product', 'target': 'web'}})
        tx.put('run', 'run', {**old_run, 'project_id': 'project', 'plan_id': 'old-plan'}, old_run['revision'])
        tx.put('plan', 'new-plan', {'product_contract': {'product_id': 'product', 'target': 'web', 'change_id': 'change-two'}})
        tx.put('run', 'run-two', {'project_id': 'project', 'plan_id': 'new-plan', 'execution_state': 'completed',
            'quality_result': 'passed', 'input_fingerprint': 'new-input', 'delivery_ids': ['delivery-two']})
        tx.put('candidate', 'candidate-two', {**{k: v for k, v in old_candidate.items() if k not in {'id', 'revision'}}, 'run_id': 'run-two',
            'run_input_fingerprint': 'new-input', 'fingerprint': 'new-candidate'})
        delivery = tx.put('delivery', 'delivery-two', {**{k: v for k, v in env.delivery.items() if k not in {'id', 'revision'}}, 'run_id': 'run-two',
            'candidate_id': 'candidate-two', 'candidate_fingerprint': 'new-candidate'})
        product = tx.get('product', 'product')
        product = tx.put('product', 'product', {**product, 'project_id': 'project', 'run_id': 'run-two',
            'current_change_id': 'change-two'}, product['revision'])
        return {'product': product, 'delivery': delivery}
    values = await env.store.command('fixture.next-version', 'next', {}, next_version)
    receipt = await env.exporter.export(values['product'], values['delivery'])
    assert receipt['path'] == str(env.output / 'releases/run-two')
    assert '?run_id=run-two' in receipt['archive_download_url']
    assert Path(env.receipt['archive_path']).read_bytes() == old_archive and old_manifest.read_bytes() == old_bytes
    assert await env.exporter.verify_version(values['product'], 'run') == env.receipt
    release = Path(receipt['path'])
    def runtime_path(directory):
        return subprocess.check_output(['node', str(directory / 'runtime-path.cjs'), str(directory)], text=True).strip()
    assert runtime_path(release) == str(env.output / 'runtime')
    assert runtime_path(Path(env.receipt['path'])) == str(env.output / 'runtime')
    portable_a, portable_b = env.output.parent / 'portable-a', env.output.parent / 'portable-b'
    shutil.copytree(release, portable_a)
    shutil.copytree(release, portable_b)
    assert runtime_path(portable_a) == str(portable_a) + '.runtime'
    assert runtime_path(portable_b) == str(portable_b) + '.runtime'
    with pytest.raises(DomainError):
        await env.exporter.verify_version({**values['product'], 'id': 'another-product'}, 'run')


@pytest.mark.parametrize('change', ['unconfirmed', 'wrong_source', 'wrong_candidate', 'stale_run', 'foreign_product'])
async def test_export_rejects_noncurrent_delivery_identity(exported, change):
    env = exported
    delivery = dict(env.delivery)
    if change == 'unconfirmed':
        delivery['confirmed_at'] = None
    elif change == 'wrong_source':
        delivery['commit_oid'] = 'f' * 40
    elif change == 'wrong_candidate':
        delivery['candidate_fingerprint'] = 'wrong'
    elif change == 'stale_run':
        def changed(tx):
            run = tx.get('run', 'run')
            return tx.put('run', 'run', {**run, 'input_fingerprint': 'new-input'}, run['revision'])
        await env.store.command('fixture.change', change, {}, changed)
    else:
        with pytest.raises(DomainError):
            await env.exporter.verify_product({**env.product, 'id': 'foreign'})
        return
    with pytest.raises(DomainError, match='confirmed current product candidate'):
        await env.exporter.export(env.product, delivery)


@pytest.mark.parametrize('change', ['marker', 'symlink', 'receipt_identity', 'zip_bytes', 'zip_mode'])
async def test_export_rejects_changed_ownership_receipt_or_archive_content(exported, change):
    env = exported
    if change == 'marker':
        (env.output / '.agentflow-product.json').write_text(json.dumps({'product_id': 'other'}))
    elif change == 'symlink':
        moved = env.output.with_name('moved-output')
        env.output.rename(moved)
        env.output.symlink_to(moved, target_is_directory=True)
    elif change == 'receipt_identity':
        receipt_path = env.exporter.root / 'product/receipt.json'
        receipt = json.loads(receipt_path.read_text())
        receipt['source_commit'] = 'e' * 40
        receipt_path.write_text(json.dumps(receipt))
    else:
        archive_path = Path(env.receipt['archive_path'])
        with zipfile.ZipFile(archive_path) as archive:
            files = [(entry, archive.read(entry)) for entry in archive.infolist()]
        with zipfile.ZipFile(archive_path, 'w', strict_timestamps=False) as archive:
            for entry, content in files:
                if change == 'zip_bytes' and entry.filename == 'source/server.mjs':
                    content = b'// Replaced archive source\n'
                if change == 'zip_mode' and entry.filename == 'start.sh':
                    entry.external_attr = 0o100644 << 16
                archive.writestr(entry, content)
        receipt_path = env.exporter.root / 'product/receipt.json'
        receipt = json.loads(receipt_path.read_text())
        receipt['archive_digest'] = file_digest(archive_path)
        receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(DomainError):
        await env.exporter.export(env.product, env.delivery)


async def test_even_internally_consistent_cached_manifest_must_match_current_candidate(exported):
    env = exported
    manifest_path = Path(env.receipt['path']) / 'delivery-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['candidate_fingerprint'] = 'another-candidate'
    manifest_path.write_text(json.dumps(manifest))
    archive_path = Path(env.receipt['archive_path'])
    with zipfile.ZipFile(archive_path, 'w', strict_timestamps=False) as archive:
        for path in sorted(manifest_path.parent.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(manifest_path.parent))
    receipt = {**env.receipt, 'candidate_fingerprint': 'another-candidate',
               'manifest_digest': canonical_digest(manifest), 'archive_digest': file_digest(archive_path)}
    (env.exporter.root / 'product/receipt.json').write_text(json.dumps(receipt))
    assert env.exporter.verify(receipt)['candidate_fingerprint'] == 'another-candidate'
    with pytest.raises(DomainError, match='different product or candidate'):
        await env.exporter.export(env.product, env.delivery)

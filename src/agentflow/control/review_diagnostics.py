"""Frozen-source review diagnostics using normal authenticated NodeService jobs.

Only review_diagnostic and node execution records are written. No formal candidate,
check, delivery, target_matrix, or downstream workflow transition is created.
"""
from __future__ import annotations

import asyncio
import hashlib
import posixpath
import tempfile
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.control.execution_pipeline import ExecutionPipeline, ProjectExecutionSpec
from agentflow.execution.manifests import (
    BuildArtifact,
    MatrixPlan,
    MatrixPlanEntry,
    SourceManifest,
    bind_matrix,
    freeze_platform_manifest,
)
from agentflow.execution.models import TargetConfig
from agentflow.testing.reports import NormalizedReport


class _DiagnosticAdvanced(Exception):
    """Another poll committed progress; return its state instead of overwriting it."""


class ReviewDiagnostics:
    def __init__(self, store, workflow, nodes):
        self.store, self.workflow, self.nodes = store, workflow, nodes
        self.pipeline = ExecutionPipeline(store, workflow, nodes, None)

    async def _unit_manifests(self, repository, commit, guard):
        """Discover static Node unit modules from the frozen entry, without importing them."""
        raw = await asyncio.to_thread(self.pipeline.repository._run, repository, ['ls-tree', '-r', '-z', commit, '--', 'tests'])
        files = {}
        for row in filter(None, raw.split(b'\0')):
            header, name = row.split(b'\t', 1)
            files[name.decode()] = header.split()[0]
        entry = 'tests/unit.test.mjs'
        if entry not in files:
            return []
        manifests, visited, pending = [], set(), [entry]
        while pending:
            path = pending.pop(0)
            if path in visited:
                continue
            visited.add(path)
            if files.get(path) not in {b'100644', b'100755'}:
                raise ValueError('builtin_unit_module_missing_or_linked:' + path)
            content = await asyncio.to_thread(self.pipeline.repository._run, repository, ['show', f'{commit}:{path}'])
            if path == entry and content == (Path(__file__).resolve().parents[1] / 'resources/web_api_starter' / entry).read_bytes():
                return []  # Deferred starter work is not an existing regression suite.
            manifest = await asyncio.to_thread(guard.inspect, path, content.decode('utf-8'))
            manifests.append(manifest)
            for module in manifest.get('imports', []):
                if module.startswith('.'):
                    dependency = posixpath.normpath(posixpath.join(posixpath.dirname(path), module))
                    if dependency.startswith('tests/') and not dependency.endswith('.json'):
                        pending.append(dependency)
        ids = ['test::' + case['title_path'][-1] for manifest in manifests for case in manifest['cases']]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError('builtin_unit_suite_empty_or_ambiguous')
        return manifests

    async def prepare_builtin_suite(self, run, source, *, node_path=None):
        """Verify built-in support and derive full old Playwright suite identities.

        This read-only diagnostic recipe source does not create or impersonate an
        accepted downstream test plan. The unchanged built-in configuration fixes
        testDir, testMatch, and the empty Playwright project name.
        """
        from agentflow.control.starter_execution import STARTER_SUPPORT_FILES
        from agentflow.control.test_migration_guard import TestMigrationGuard

        plan = await self.store.read('plan', run['plan_id'])
        configs = [TargetConfig.model_validate(c) for c in plan['target_configs']]
        if (plan.get('product_contract', {}).get('stack') != 'node_web_api'
                or not configs or any(c.app_target not in {'api', 'web'} for c in configs)):
            raise ValueError('builtin_diagnostic_requires_node_web_api')
        repository = Path(source.get('repository', source.get('repository_path', '')))
        commit = source.get('commit', source.get('commit_oid'))
        tree = (await asyncio.to_thread(self.pipeline.repository._run, repository,
                                       ['rev-parse', f'{commit}^{{tree}}'])).decode().strip()
        if tree != source.get('tree_oid'):
            raise ValueError('frozen_source_mismatch')
        template = Path(__file__).resolve().parents[1] / 'resources/web_api_starter'
        support = []
        for path in STARTER_SUPPORT_FILES:
            row = await asyncio.to_thread(self.pipeline.repository._run, repository, ['ls-tree', '-z', commit, '--', path])
            if not row or row.split(b' ', 1)[0] not in {b'100644', b'100755'}:
                raise ValueError('builtin_diagnostic_support_file_missing_or_linked')
            raw = await asyncio.to_thread(self.pipeline.repository._run, repository, ['show', f'{commit}:{path}'])
            if raw != (template / path).read_bytes():
                raise ValueError('builtin_diagnostic_support_changed')
            support.append({'path': path, 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()})
        guard = TestMigrationGuard(node_path=node_path)
        cases, targets, manifests = [], [], []
        units = await self._unit_manifests(repository, commit, guard)
        manifests.extend(units)
        for config in configs:
            kind = config.app_target.value
            path = f'tests/{kind}.spec.mjs'
            raw = await asyncio.to_thread(self.pipeline.repository._run, repository, ['show', f'{commit}:{path}'])
            manifest = await asyncio.to_thread(guard.inspect, path, raw.decode('utf-8'))
            manifests.append(manifest)
            mapped = []
            for case in manifest['cases']:
                framework_id = '::'.join([Path(path).name, *case['title_path'], ''])
                mapped.append(framework_id)
                cases.append({'case_id': case['case_id'], 'path': path, 'name': case['name'],
                    'target_config_id': config.target_config_id, 'phase': 'integration',
                    'framework_case_ids': [framework_id]})
            if not mapped or len(set(mapped)) != len(mapped):
                raise ValueError('builtin_diagnostic_suite_empty_or_ambiguous')
            target = {'target_config_id': config.target_config_id,
                'build': {'adapter': kind, 'project_path': '.', 'output_paths': {'product': 'build/product', 'test': 'build/tests'}},
                'integration': {'adapter': kind, 'test_kind': 'api' if kind == 'api' else 'integration',
                    'test_project_path': 'build/tests', 'product_path': 'build/product',
                    'framework_config': f'build/tests/playwright.{kind}.config.mjs',
                    'report_path': f'reports/{kind}-integration.json', 'expected_case_ids': mapped}}
            if units:
                expected = []
                for unit in units:
                    for case in unit['cases']:
                        framework_id = 'test::' + case['title_path'][-1]
                        expected.append(framework_id)
                        cases.append({'case_id': case['case_id'], 'path': unit['file'], 'name': case['name'],
                            'target_config_id': config.target_config_id, 'phase': 'unit', 'framework_case_ids': [framework_id]})
                target['unit'] = {'adapter': kind, 'test_kind': 'unit', 'test_project_path': 'build/tests',
                    'product_path': 'build/product', 'unit_project': 'build/tests/unit.test.mjs',
                    'report_path': f'reports/{kind}-unit.xml', 'expected_case_ids': expected}
            targets.append(target)
        spec = ProjectExecutionSpec.model_validate({'schema_version': 1, 'targets': targets, 'cross_scenarios': []})
        return {'execution_spec': spec.model_dump(mode='json'), 'cases': cases, 'manifests': manifests,
            'provenance': {'kind': 'verified_builtin_existing_suite_diagnostic', 'source_commit': commit,
                'source_tree_oid': tree, 'support_files': support, 'spec_fingerprint': canonical_digest(spec.model_dump(mode='json'))}}

    async def _save(self, record, **changes):
        identity = record['id']
        def update(tx):
            old = tx.get('review_diagnostic', identity)
            if old['revision'] != record['revision']:
                raise _DiagnosticAdvanced()
            frozen_fields = ('created_at', 'source_manifest', 'recipes', 'matrix_plan', 'matrix_mappings',
                             'platform_manifest', 'matrix_binding', 'build_job_ids', 'test_job_ids')
            for field in frozen_fields:
                if old.get(field) and field in changes and changes[field] != old[field]:
                    raise DomainError('diagnostic_frozen_input_changed', f'Diagnostic frozen field cannot change: {field}')
            if old['state'] in {'passed', 'failed', 'blocked'} or all(old.get(k) == v for k, v in changes.items()):
                return old
            return tx.put('review_diagnostic', identity, {**old, **changes}, old['revision'])
        await self.store.command('review.diagnostic.update',
            f"{identity}:{record['revision']}:{canonical_digest(changes)}", changes, update)
        # An idempotency receipt may contain an earlier revision. Always resume
        # from the durable current state, including after another instance runs.
        return await self.store.read('review_diagnostic', identity)

    async def start_or_poll(self, run, batch, source, affected_cases, *, target_configs=None,
                            execution_spec=None):
        """Poll without holding an agent slot.

        source = {repository, commit, tree_oid[, snapshot_id]}.
        affected_cases = [{case_id, target_config_id, phase, framework_case_ids}].
        execution_spec, when supplied, is a controller-approved frozen recipe
        specification; otherwise read agentflow.project.json from the exact commit.
        """
        batch_id = batch['id'] if isinstance(batch, dict) else batch
        identity = str(uuid5(NAMESPACE_URL, f"review-diagnostic:{run['id']}:{batch_id}"))
        request = {'run_id': run['id'], 'run_input_fingerprint': run['input_fingerprint'],
                   'batch_id': batch_id, 'source': source, 'affected_cases': affected_cases,
                   'target_configs': [c.model_dump(mode='json') if isinstance(c, TargetConfig) else c for c in target_configs] if target_configs is not None else None,
                   'execution_spec': execution_spec}
        fingerprint = canonical_digest(request)
        record = await self.store.read('review_diagnostic', identity)
        if record and record['request_fingerprint'] != fingerprint:
            return {**record, 'state': 'blocked', 'blockers': ['diagnostic_request_changed']}
        if not record:
            def create(tx):
                return tx.put('review_diagnostic', identity, {
                    **request, 'request_fingerprint': fingerprint, 'created_at': utc_now(), 'state': 'waiting', 'stage': 'preparing',
                    'jobs': [], 'build_job_ids': [], 'test_job_ids': [], 'reports': [], 'blockers': [],
                    'affected_case_ids': sorted({c['case_id'] for c in affected_cases}),
                    'source_manifest': None, 'platform_manifest': None})
            await self.store.command('review.diagnostic.create', identity, request, create)
            record = await self.store.read('review_diagnostic', identity)
        if record['state'] in {'passed', 'failed', 'blocked'}:
            return record
        try:
            if not record.get('created_at'):
                record = await self._save(record, created_at=utc_now())
            if not self.nodes:
                raise ValueError('executor_not_configured')
            if record['stage'] == 'preparing':
                record = await self._prepare(run, batch, record, target_configs, execution_spec)
            if record['state'] in {'passed', 'failed', 'blocked'}:
                return record
            return await self._poll(run, batch, record)
        except _DiagnosticAdvanced:
            return await self.store.read('review_diagnostic', identity)
        except (DomainError, OSError, ValueError, KeyError, TypeError) as exc:
            current = await self.store.read('review_diagnostic', identity)
            try:
                return await self._save(current, state='blocked', blockers=[getattr(exc, 'code', str(exc))])
            except _DiagnosticAdvanced:
                return await self.store.read('review_diagnostic', identity)

    async def _prepare(self, run, batch, record, target_configs, execution_spec):
        source, affected = record['source'], record['affected_cases']
        repository = Path(source.get('repository', source.get('repository_path', '')))
        commit = source.get('commit', source.get('commit_oid'))
        if not commit or not source.get('tree_oid') or not affected:
            raise ValueError('frozen_source_and_affected_cases_required')
        resolved = (await asyncio.to_thread(self.pipeline.repository._run, repository, ['rev-parse', f'{commit}^{{commit}}'])).decode().strip()
        tree = (await asyncio.to_thread(self.pipeline.repository._run, repository, ['rev-parse', f'{commit}^{{tree}}'])).decode().strip()
        if resolved != commit or tree != source['tree_oid']:
            raise ValueError('frozen_source_mismatch')
        plan = await self.store.read('plan', run['plan_id'])
        configs = [TargetConfig.model_validate(c) for c in (target_configs if target_configs is not None else plan['target_configs'])]
        approved = {c['target_config_id']: TargetConfig.model_validate(c).fingerprint for c in plan['target_configs']}
        if any(approved.get(c.target_config_id) != c.fingerprint for c in configs):
            raise ValueError('target_config_not_approved')
        if execution_spec is None:
            raw = await asyncio.to_thread(self.pipeline.repository._run, repository, ['show', f'{commit}:agentflow.project.json'])
            spec = ProjectExecutionSpec.model_validate_json(raw)
        else:
            spec = ProjectExecutionSpec.model_validate(execution_spec)
        selected_keys = {(c['target_config_id'], c['phase']) for c in affected}
        targets = {t.target_config_id: t for t in spec.targets}
        by_id = {c.target_config_id: c for c in configs}
        entries, mappings = [], []
        for target_id, phase in sorted(selected_keys):
            if target_id not in targets or target_id not in by_id or phase not in {'unit', 'integration'}:
                raise ValueError('affected_case_target_or_phase_missing')
            target, config = targets[target_id], by_id[target_id]
            recipe = getattr(target, phase)
            if recipe is None or not recipe.expected_case_ids:
                raise ValueError('diagnostic_suite_empty')
            if any(r.adapter != config.app_target for r in [target.build, recipe]):
                raise ValueError('diagnostic_adapter_mismatch')
            if target.install:
                raise ValueError('diagnostic_native_install_not_supported')
            for case in [c for c in affected if (c['target_config_id'], c['phase']) == (target_id, phase)]:
                if not case.get('case_id') or not case.get('framework_case_ids') or not set(case['framework_case_ids']) <= set(recipe.expected_case_ids):
                    raise ValueError('affected_case_not_in_frozen_suite')
            entry_id = str(uuid5(NAMESPACE_URL, f"{record['id']}:{target_id}:{phase}"))
            entries.append(MatrixPlanEntry(matrix_entry_id=entry_id, test_case_id=entry_id,
                app_target=config.app_target, component_roles=['product', 'test'],
                target_config_id=target_id, target_config_revision=config.revision))
            mappings.append({'matrix_entry_id': entry_id, 'test_case_id': entry_id, 'phase': phase,
                'target_config_id': target_id, 'framework_case_ids': recipe.expected_case_ids})
        selected_ids = {key[0] for key in selected_keys}
        configs = [c for c in configs if c.target_config_id in selected_ids]
        matrix = MatrixPlan(matrix_id=record['id'], required_app_targets=sorted({c.app_target for c in configs}),
                            target_configs=configs, entries=entries)
        spec = spec.model_copy(update={'targets': [t for t in spec.targets if t.target_config_id in selected_ids], 'cross_scenarios': []})
        directory = self.workflow.settings.data_dir / 'review-diagnostics' / record['id']
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='prepare-', dir=directory) as temporary:
            archive = Path(temporary) / 'source.tar'
            await asyncio.to_thread(self.pipeline._archive, repository, commit, archive)
            blob = await self.nodes.import_input(archive, 'source.tar', run['id'])
        build = await self.nodes.import_input(spec.model_dump_json().encode(), 'diagnostic-execution-plan.json', run['id'])
        manifest = SourceManifest(manifest_id=str(uuid5(NAMESPACE_URL, f"review-diagnostic:{record['id']}:source")),
            source_commit=commit, source_tree_oid=tree,
            source_bundle_artifact_version_id=blob['id'], source_bundle_digest=blob['digest'],
            test_package_artifact_version_id=blob['id'], test_package_digest=blob['digest'],
            build_plan_artifact_version_id=build['id'], build_plan_digest=build['digest'],
            target_matrix_fingerprint=matrix.fingerprint, required_app_targets=tuple(matrix.required_app_targets))
        return await self._save(record, stage='building', recipes=spec.model_dump(mode='json'),
            matrix_plan=matrix.model_dump(mode='json'), matrix_mappings=mappings,
            source_manifest={**manifest.model_dump(mode='json'), 'fingerprint': manifest.fingerprint})

    async def _receipt(self, job, record):
        if job.get('source_manifest') != record['source_manifest']:
            raise ValueError('diagnostic_job_source_mismatch')
        if job['kind'] == 'test' and job.get('platform_artifact_manifest') != record['platform_manifest']:
            raise ValueError('diagnostic_job_platform_mismatch')
        result = await self.store.read('node_result', job.get('result_id')) if job.get('result_id') else None
        if not result or result.get('job_id') != job['id'] or result.get('assessment_state') != 'validated':
            raise ValueError('diagnostic_unverified_result')
        return result

    async def _poll(self, run, batch, record):
        spec = ProjectExecutionSpec.model_validate(record['recipes'])
        matrix = MatrixPlan.model_validate(record['matrix_plan'])
        configs = {c.target_config_id: c for c in matrix.target_configs}
        parent = batch.get('validation_work_item_id') if isinstance(batch, dict) else None
        if not record['build_job_ids']:
            jobs = []
            for target in spec.targets:
                config = configs[target.target_config_id]
                job = await self.nodes.enqueue_job(run['id'], kind='build', target_config=config,
                    source_manifest=record['source_manifest'], recipe=target.build, limits=self.pipeline._limits(),
                    required_resource_ids=config.required_resource_ids, parent_work_item_id=parent,
                    idempotency_key=f"review-diagnostic:{record['id']}:build:{config.target_config_id}")
                jobs.append(job['id'])
            record = await self._save(record, build_job_ids=jobs, jobs=jobs)
        builds = [await self.store.read('node_job', j) for j in record['build_job_ids']]
        if await self.pipeline._drain_failed_group(builds):
            return record
        if any(j['state'] in {'failed', 'cancelled', 'execution_unknown'} for j in builds):
            return await self._save(record, state='failed', blockers=['diagnostic_build_not_passed'])
        if any(j['state'] != 'completed' for j in builds):
            return record
        if not record['platform_manifest']:
            source = SourceManifest.model_validate({k: v for k, v in record['source_manifest'].items() if k != 'fingerprint'})
            artifacts = []
            for job in builds:
                result = await self._receipt(job, record)
                if job.get('quality_result') != 'passed':
                    raise ValueError('diagnostic_build_not_passed')
                for claim in result.get('verified_build_artifacts', []):
                    if claim.get('source_manifest_fingerprint') != source.fingerprint:
                        raise ValueError('diagnostic_build_source_mismatch')
                    artifacts.append(BuildArtifact(artifact_id=claim['component_id'], artifact_version_id=claim['artifact_version_id'],
                        app_target=job['app_target'], target_config_id=job['target_config']['target_config_id'],
                        component_role=claim['component_role'],
                        kind={'application': 'product', 'test_runner': 'test', 'api_service': 'service', 'test_data': 'data'}[claim['kind']],
                        digest=claim['digest'], source_manifest_fingerprint=source.fingerprint,
                        toolchain_fingerprint=claim['environment_fingerprint'], verified_upload=True,
                        metadata={'relative_path': claim['relative_path'], 'content_digest': claim['content_digest']}))
            platform = freeze_platform_manifest(source, matrix, artifacts).model_copy(update={
                'manifest_id': str(uuid5(NAMESPACE_URL, f"review-diagnostic:{record['id']}:platform")),
                'frozen_at': record['created_at']})
            binding = bind_matrix(matrix, source, platform, {e.matrix_entry_id: {} for e in matrix.entries})
            record = await self._save(record, platform_manifest={**platform.model_dump(mode='json'), 'fingerprint': platform.fingerprint},
                                      matrix_binding=binding, stage='testing')
        if not record['test_job_ids']:
            jobs = []
            for mapping in record['matrix_mappings']:
                config = configs[mapping['target_config_id']]
                target = next(t for t in spec.targets if t.target_config_id == config.target_config_id)
                job = await self.nodes.enqueue_job(run['id'], kind='test', target_config=config,
                    source_manifest=record['source_manifest'], platform_manifest=record['platform_manifest'],
                    recipe=getattr(target, mapping['phase']), matrix_entry_ids=[mapping['matrix_entry_id']],
                    matrix_entries=[mapping], matrix_plan_fingerprint=matrix.fingerprint,
                    matrix_binding_fingerprint=record['matrix_binding']['binding_fingerprint'],
                    limits=self.pipeline._limits(), required_resource_ids=config.required_resource_ids,
                    parent_work_item_id=parent,
                    idempotency_key=f"review-diagnostic:{record['id']}:test:{mapping['matrix_entry_id']}")
                jobs.append(job['id'])
            record = await self._save(record, test_job_ids=jobs, jobs=record['build_job_ids'] + jobs)
        jobs = [await self.store.read('node_job', j) for j in record['test_job_ids']]
        if await self.pipeline._drain_failed_group(jobs):
            return record
        if any(j['state'] in {'cancelled', 'execution_unknown'} for j in jobs):
            return await self._save(record, state='blocked', blockers=['diagnostic_test_execution_unknown'])
        if any(j['state'] not in {'completed', 'failed'} for j in jobs):
            return record
        reports, blockers = [], []
        for job in jobs:
            result = await self._receipt(job, record)
            verified = result.get('verified_checks', [])
            if {c['matrix_entry_id'] for c in verified} != set(job['matrix_entry_ids']) or len(verified) != len(job['matrix_entry_ids']):
                raise ValueError('diagnostic_missing_or_duplicate_reports')
            for check in verified:
                mapping = next(m for m in record['matrix_mappings'] if m['matrix_entry_id'] == check['matrix_entry_id'])
                normalized = NormalizedReport.model_validate(check['normalized_report'])
                cases = normalized.cases
                reports.append({'job_id': job['id'], 'node_result_id': result['id'],
                    'raw_report_artifact_version_id': check['raw_report_artifact_version_id'],
                    'matrix_entry_id': mapping['matrix_entry_id'], 'normalized_report': normalized.model_dump(mode='json')})
                if (normalized.execution_status != 'completed' or normalized.quality_result != 'passed'
                        or normalized.errors or normalized.missing_case_ids or not cases
                        or not set(mapping['framework_case_ids']) <= {c.case_id for c in cases}
                        or any(c.status != 'passed' for c in cases)
                        or len({(c.case_id, c.attempt) for c in cases}) != len(cases)):
                    blockers.append(f"diagnostic_suite_not_passed:{mapping['target_config_id']}:{mapping['phase']}")
            if job['state'] != 'completed' or job.get('quality_result') != 'passed':
                blockers.append(f"diagnostic_job_not_passed:{job['id']}")
        return await self._save(record, state='failed' if blockers else 'passed', stage='finished',
                                reports=reports, blockers=blockers)

"""Repair product code after known test failures without regenerating its tests."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from uuid import uuid4

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.domain.planning import EXECUTION_STEPS, descendants
from agentflow.execution.manifests import file_digest
from agentflow.models.budget import account_id


class ProductTestRepair:
    def __init__(self, store, workflow, *, limit=None, nodes=None):
        self.store, self.workflow, self.limit, self.nodes = store, workflow, limit, nodes

    def _automatic_allowed(self, tx, product_id):
        limit = self.workflow.settings.auto_test_repair_limit if self.limit is None else self.limit
        count = sum(row.get('product_id') == product_id for row in tx.list('product_test_repair'))
        count += sum(row.get('product_id') == product_id and row.get('actor') == 'system'
                     for row in tx.list('product_test_runtime_repair'))
        return type(limit) is int and (limit == -1 or limit > 0 and count < limit)

    @staticmethod
    def _automatic_runtime_job(tx, candidate):
        """Only an isolated, explicit Playwright route lifecycle failure qualifies.

        Assertions, generic network failures and mixed target failures retain
        their existing analysis path; message similarity does not grant scope.
        Raw report bytes are independently parsed by the shared repair entry.
        """
        failed = [job for job in tx.list('node_job') if job.get('run_id') == candidate['run_id']
                  and job.get('quality_result') == 'failed'
                  and (job.get('platform_artifact_manifest') or {}).get('fingerprint') == candidate['fingerprint']]
        if len(failed) != 1:
            return None
        job = failed[0]
        if (job.get('state') != 'completed' or not job.get('result_id')
                or job['id'] not in candidate.get('phase_jobs', {}).get('integration', [])
                or job.get('target_config', {}).get('app_target') != 'web'):
            return None
        result = tx.get('node_result', job['result_id'])
        if not result or result.get('assessment_state') != 'validated' or result.get('errors'):
            return None
        cases = []
        for check in result.get('verified_checks', []):
            report = check.get('normalized_report', {})
            if report.get('execution_status') != 'completed' or report.get('errors') or report.get('missing_case_ids'):
                return None
            cases.extend(case for case in report.get('cases', []) if case.get('status') in {'failed', 'error'})
        if cases and all(re.search(r'(?:^|\n)(?:Error: )?route\.(?:continue|fulfill|abort|fallback): Route is already handled!?',
                                   case.get('message', ''))
                         and re.search(r'[/\\]web\.spec\.mjs:\d+', case.get('message', '')) for case in cases):
            return job['id']
        return None

    async def _failure_evidence(self, run_id):
        verified = {}
        for job in await self.store.list('node_job'):
            if job.get('run_id') != run_id or job.get('quality_result') != 'failed' or not job.get('result_id'):
                continue
            result = await self.store.read('node_result', job['result_id'])
            if not result or result.get('assessment_state') != 'validated':
                continue
            for check in result.get('verified_checks', []):
                report = check.get('normalized_report', {})
                if report.get('quality_result') != 'failed':
                    continue
                artifact = await self.store.read('node_artifact', check.get('raw_report_artifact_version_id', 'missing'))
                if (not artifact or artifact.get('state') != 'complete'
                        or report.get('execution_status') != 'completed' or report.get('errors') or report.get('missing_case_ids')
                        or not any(case.get('status') in {'failed', 'error'} for case in report.get('cases', []))
                        or not re.fullmatch(r'sha256:[0-9a-f]{64}', artifact.get('digest', ''))
                        or report.get('raw_digest') != artifact['digest']):
                    return None
                path = (self.nodes.artifacts.object_path(artifact['digest']) if self.nodes
                        else Path(self.workflow.settings.data_dir) / 'nodes/artifacts/objects' / artifact['digest'][7:])
                try:
                    if await asyncio.to_thread(file_digest, path, maximum_bytes=32 * 1024 * 1024) != artifact['digest']:
                        return None
                except (OSError, DomainError):
                    return None
                verified[(job['id'], result['id'], artifact['id'])] = {
                    'report_digest': canonical_digest(report), 'artifact_digest': artifact['digest'],
                    'versions': [('node_job', job['id'], job['revision']), ('node_result', result['id'], result['revision']),
                                 ('node_artifact', artifact['id'], artifact['revision'])]}
        return verified

    async def attempt(self, product):
        verified = await self._failure_evidence(product['run_id'])
        if not verified:
            return {'scheduled': False}
        def repair(tx):
            run = tx.get('run', product['run_id'])
            if not run or run['execution_state'] != 'running' or run.get('delivery_ids'):
                return {'scheduled': False}
            plan = tx.get('plan', run['plan_id'])
            contract = plan.get('product_contract', {})
            if (run.get('purpose') != 'code_delivery' or contract.get('stack') != 'node_web_api'
                    or contract.get('product_id') != product['id']):
                return {'scheduled': False}
            previous = [r for r in tx.list('product_test_repair') if r['product_id'] == product['id']]
            if not self._automatic_allowed(tx, product['id']):
                return {'scheduled': False}
            items = [w for w in tx.list('work_item') if w['run_id'] == run['id'] and not w.get('archived')]
            if any(w['status'] in {'running', 'waiting_execution', 'execution_unknown', 'cancel_requested', 'waiting_approval'} for w in items):
                return {'scheduled': False}
            from agentflow.models.uncertainty import (
                acknowledged_invocation_ids,
                acknowledgment_state,
                invocation_blocks,
            )
            acknowledged = acknowledged_invocation_ids(acknowledgment_state(tx))
            if any(i.get('run_id') == run['id'] and invocation_blocks(i, acknowledged) for i in tx.list('model_invocation')):
                return {'scheduled': False}
            budget = run.get('budget_limit')
            if not budget:
                return {'scheduled': False}
            for kind, owner in [('run', run['id']), ('iteration', run['iteration_id'])]:
                account = tx.get('budget_account', account_id(kind, owner))
                if (not account or account.get('restore_uncertain') or account.get('uncertain_micros', 0)
                        or type(account.get('max_requests')) is not int or account['max_requests'] < 0
                        or type(account.get('request_count')) is not int or account['request_count'] < 0
                        or (account['max_requests'] > 0 and account.get('request_count', 0) >= account['max_requests'])
                        or (budget.get('cost_mode') != 'request_limited'
                            and account['settled_micros'] + account['reserved_micros'] >= account['limit_micros'])):
                    return {'scheduled': False}
            candidates = [c for c in tx.list('candidate') if c['run_id'] == run['id'] and c['run_input_fingerprint'] == run['input_fingerprint']]
            if len(candidates) != 1:
                return {'scheduled': False}
            candidate = candidates[0]
            if any(r['candidate_id'] == candidate['id'] for r in previous):
                return {'scheduled': False}
            jobs = [j for j in tx.list('node_job') if j.get('parent_work_item_id') in {w['id'] for w in items}
                    and (j.get('platform_artifact_manifest') or {}).get('fingerprint') == candidate['fingerprint']]
            if any(j['state'] not in {'completed', 'failed', 'cancelled'} for j in jobs):
                return {'scheduled': False}
            failures = []
            for job in jobs:
                if job.get('quality_result') != 'failed' or not job.get('result_id'):
                    continue
                result = tx.get('node_result', job['result_id'])
                if not result or result.get('assessment_state') != 'validated':
                    continue
                for check in result.get('verified_checks', []):
                    report = check.get('normalized_report', {})
                    if report.get('quality_result') == 'failed':
                        evidence = verified.get((job['id'], result['id'], check.get('raw_report_artifact_version_id')))
                        if not evidence or evidence['report_digest'] != canonical_digest(report):
                            return {'scheduled': False}
                        for kind, identity, revision in evidence['versions']:
                            current = tx.get(kind, identity)
                            if not current or current['revision'] != revision:
                                return {'scheduled': False}
                        artifact = tx.get('node_artifact', check['raw_report_artifact_version_id'])
                        if artifact['digest'] != evidence['artifact_digest']:
                            return {'scheduled': False}
                        failures.append({'target_config_id': job['target_config']['target_config_id'],
                            'report_artifact_id': check['raw_report_artifact_version_id'], 'report': report})
            if not failures:
                return {'scheduled': False}
            runtime_job = self._automatic_runtime_job(tx, candidate)
            if runtime_job:
                current_product = tx.get('product', product['id'])
                return {'scheduled': False, '_runtime_request': {
                    'expected_revision': current_product['revision'], 'expected_run_revision': run['revision'],
                    'candidate_id': candidate['id'], 'failed_job_id': runtime_job,
                    'reason': ('Verified Playwright route lifecycle failure in tests/web.spec.mjs. '
                        'Release the held request and await its route handler completion before removing the route '
                        '(or use unrouteAll with behavior=wait). Preserve real requests and all assertions. '
                        'Do not swallow route errors, change product code, or fabricate responses to pass.')}}
            if 'implementation' not in plan.get('authorized_rework_steps', []):
                return {'scheduled': False}
            if any(re.search(r'\blisten\b', case.get('message', ''), re.IGNORECASE)
                   and re.search(r'\b(?:EPERM|EACCES)\b', case.get('message', ''), re.IGNORECASE)
                   for failure in failures for case in failure['report'].get('cases', [])
                   if case.get('status') in {'failed', 'error'}):
                message = '测试监听端口被隔离策略拒绝，需要由所有者启动测试运行前提修复；不会自动修改产品源码。'
                notice = {'code': 'test_runtime_repair_required', 'candidate_id': candidate['id'], 'message': message}
                current_product = tx.get('product', product['id'])
                if current_product and current_product.get('test_runtime_repair_required') != notice:
                    tx.put('product', product['id'], {**current_product, 'test_runtime_repair_required': notice},
                           current_product['revision'])
                return {'scheduled': False, 'reason_code': notice['code'], 'message': message}
            phases = [w for w in items if w['step'] in EXECUTION_STEPS]
            unit = next((w for w in phases if w['step'] == 'unit_test_execution'), None)
            if not unit:
                return {'scheduled': False}
            roots = {unit['id']}
            affected = descendants(items, roots)
            # All existing implementation/review/test-plan/code assets remain intact.
            baseline_dependencies = list(unit['dependencies'])
            repair_id, review_id, snapshot_id = str(uuid4()), str(uuid4()), str(uuid4())
            reason = ('Fix the product source so the existing frozen tests pass. Do not edit tests, test plans, '
                      'build tooling, lockfiles or execution recipes. All original assertions must stay unchanged. '
                      'Failure evidence:\n' + json.dumps(failures, ensure_ascii=False))
            self.workflow._invalidate(tx, items, roots, reason)
            approval_steps = tx.get('plan', run['plan_id']).get('approval_steps', [])
            common = {'run_id': run['id'], 'project_id': run['project_id'], 'generation': 1, 'fencing_token': 0,
                'status': 'pending', 'quality_result': 'unknown', 'required': True, 'attempt_id': None,
                'artifact_ids': [], 'input_fingerprint': run['input_fingerprint'], 'policy_fingerprint': unit['policy_fingerprint']}
            tx.put('code_snapshot', snapshot_id, {'run_id': run['id'], 'work_item_id': repair_id, 'generation': 0,
                'repository_path': candidate['source_repository'], 'commit_oid': candidate['source_commit'],
                'tree_oid': candidate['tree_oid'], 'base_oid': candidate['source_commit'], 'stale': False})
            tx.put('work_item', repair_id, {**common, 'key': 'product-repair-' + repair_id, 'step': 'implementation',
                'role': 'development', 'dependencies': baseline_dependencies, 'write_paths': ['src', 'public'],
                'approval_required': 'implementation' in approval_steps,
                'payload': {'repair_base_snapshot_id': snapshot_id, 'change_expectation': reason,
                    'product_frozen_repair': True}})
            tx.put('work_item', review_id, {**common, 'key': 'product-repair-review-' + review_id,
                'step': 'code_review', 'role': 'review', 'dependencies': [repair_id], 'write_paths': [],
                'approval_required': 'code_review' in approval_steps,
                'payload': {'change_expectation': 'Independently review the repair and verify tests/acceptance were not weakened.'}})
            current_unit = tx.get('work_item', unit['id'])
            tx.put('work_item', unit['id'], {**current_unit, 'dependencies': [*baseline_dependencies, review_id]}, current_unit['revision'])
            tx.put('run', run['id'], {**run, 'input_fingerprint': canonical_digest({'prior': run['input_fingerprint'],
                'repair_id': repair_id}), 'blocking_reasons': [], 'quality_result': 'unknown'}, run['revision'])
            record = tx.put('product_test_repair', repair_id, {'product_id': product['id'], 'run_id': run['id'],
                'candidate_id': candidate['id'], 'preserved_test_source': candidate['source_commit'],
                'repair_work_item_id': repair_id, 'review_work_item_id': review_id,
                'affected_work_item_ids': sorted(affected), 'ordinal': len(previous) + 1, 'created_at': utc_now()})
            tx.event('product.test_repair_scheduled', record, run_id=run['id'])
            return {'scheduled': True, 'repair_id': repair_id}
        outcome = await self.store.command('product.test_repair', str(uuid4()), {'product_id': product['id'],
            'verified_evidence': sorted((list(key), value) for key, value in verified.items())}, repair)
        request = outcome.pop('_runtime_request', None)
        if request is None:
            return outcome
        from agentflow.control.product_models import ProductTestRuntimeRepairRequest
        key = 'automatic-route-lifecycle:' + request['candidate_id'] + ':' + request['failed_job_id']
        try:
            result = await self._repair_runtime(product['id'], ProductTestRuntimeRepairRequest.model_validate(request),
                                                key, automatic=True)
        except DomainError as error:
            # A known fixture error must not fall back to editing product code
            # when scope, accounting, approval or concurrent state prevents repair.
            return {'scheduled': False, 'reason_code': 'test_runtime_repair_required', 'message': error.message}
        return {'scheduled': True, 'repair_id': result['repair_work_item_id'], 'kind': 'test_runtime_repair'}

    @staticmethod
    def _runtime_error():
        return DomainError('test_runtime_repair_invalid', '测试修复的候选、失败证据或停止状态无法核验；未修改原产物。')

    def _runtime_context(self, tx, product_id, request, *, automatic=False):
        from agentflow.control.product_management import guard_product_run
        from agentflow.control.service import ensure_revision
        from agentflow.models.uncertainty import (
            acknowledged_invocation_ids,
            acknowledgment_state,
            attempt_uncertainty_blocks,
            invocation_blocks,
        )
        product = tx.get('product', product_id)
        if not product:
            raise DomainError('not_found', 'Unknown product', 404)
        ensure_revision(product, request.expected_revision)
        run = tx.get('run', product.get('run_id'))
        if not run:
            raise self._runtime_error()
        ensure_revision(run, request.expected_run_revision)
        guard_product_run(tx, run)
        plan = tx.get('plan', run.get('plan_id'))
        contract = (plan or {}).get('product_contract', {})
        expected_state = 'running' if automatic else 'paused'
        if (run.get('execution_state') != expected_state or run.get('delivery_ids')
                or run.get('purpose') != 'code_delivery' or contract.get('stack') != 'node_web_api'
                or contract.get('product_id') != product_id or product.get('project_id') != run.get('project_id')):
            raise self._runtime_error()
        items = [w for w in tx.list('work_item') if w.get('run_id') == run['id'] and not w.get('archived')]
        attempts = [a for a in tx.list('attempt') if a.get('run_id') == run['id']]
        attempt_ids = {a['id'] for a in attempts}
        supervisors = [p for p in tx.list('supervised_attempt') if p.get('run_id') == run['id'] or p['id'] in attempt_ids]
        jobs = [j for j in tx.list('node_job') if j.get('run_id') == run['id']]
        calls = [i for i in tx.list('model_invocation') if i.get('run_id') == run['id']]
        acknowledged = acknowledged_invocation_ids(acknowledgment_state(tx))
        if (any(w.get('status') not in {'pending', 'completed', 'failed', 'blocked', 'cancelled', 'superseded'} for w in items)
                or any(a.get('status') not in {'completed', 'failed', 'blocked', 'cancelled'} for a in attempts)
                or any(p.get('state') not in {'completed', 'failed', 'cancelled'} for p in supervisors)
                or any(j.get('state') not in {'completed', 'failed', 'cancelled'} for j in jobs)
                or any(invocation_blocks(i, acknowledged) for i in calls)
                or any(attempt_uncertainty_blocks(b, calls, acknowledged)
                       for b in tx.list('model_attempt_budget') if b['id'] in attempt_ids)
                or any(s.get('run_id') == run['id'] and s.get('status') not in {'completed', 'failed', 'cancelled'}
                       for s in tx.list('cross_scenario'))
                or any(d.get('run_id') == run['id'] for d in tx.list('delivery_intent'))
                or any(row.get(flag) for row in [product, run, plan, *items, *attempts, *supervisors, *jobs, *calls]
                       for flag in ('restore_uncertain', 'restore_reconciliation_required', 'restore_revalidation_required'))):
            raise self._runtime_error()
        for kind, owner in [('run', run['id']), ('iteration', run['iteration_id'])]:
            account = tx.get('budget_account', account_id(kind, owner))
            if (not account or account.get('restore_uncertain') or account.get('reserved_micros')
                    or account.get('uncertain_micros') or account.get('owner_kind') != kind or account.get('owner_id') != owner
                    or any(type(account.get(f)) is not int or account[f] < 0
                           for f in ('request_count', 'max_requests', 'settled_micros', 'limit_micros'))
                    or (account['max_requests'] and account['request_count'] >= account['max_requests'])
                    or (run.get('budget_limit', {}).get('cost_mode') != 'request_limited'
                        and account['settled_micros'] >= account['limit_micros'])):
                raise self._runtime_error()
        candidate = tx.get('candidate', request.candidate_id)
        job = tx.get('node_job', request.failed_job_id)
        if (not candidate or candidate.get('run_id') != run['id'] or candidate.get('stale')
                or not job or job.get('run_id') != run['id'] or job.get('quality_result') != 'failed'
                or job.get('state') != 'completed' or not job.get('result_id')
                or job.get('target_config') not in candidate.get('matrix_plan', {}).get('target_configs', [])
                or (job.get('platform_artifact_manifest') or {}).get('fingerprint') != candidate.get('fingerprint')
                or (job.get('source_manifest') or {}).get('fingerprint') != candidate.get('source_manifest', {}).get('fingerprint')):
            raise self._runtime_error()
        from agentflow.execution.manifests import SourceManifest
        try:
            manifest = SourceManifest.model_validate({k: v for k, v in candidate.get('source_manifest', {}).items()
                                                     if k != 'fingerprint'})
        except ValueError as error:
            raise self._runtime_error() from error
        if (manifest.fingerprint != candidate['source_manifest'].get('fingerprint')
                or manifest.source_commit != candidate.get('source_commit')
                or manifest.source_tree_oid != candidate.get('tree_oid')
                or job.get('source_manifest') != candidate['source_manifest']):
            raise self._runtime_error()
        phases = [phase for phase in ('unit', 'integration') if job['id'] in candidate.get('phase_jobs', {}).get(phase, [])]
        target = job.get('target_config', {}).get('app_target')
        if len(phases) != 1 or target not in {'api', 'web'}:
            raise self._runtime_error()
        phase = phases[0]
        step = 'unit_test_implementation' if phase == 'unit' else 'integration_test_implementation'
        path = 'tests/unit.test.mjs' if phase == 'unit' else f'tests/{target}.spec.mjs'
        if automatic and (request.replace_repair_id is not None or not self._automatic_allowed(tx, product_id)
                          or self._automatic_runtime_job(tx, candidate) != job['id']):
            raise self._runtime_error()
        parent = tx.get('work_item', job.get('parent_work_item_id'))
        units = [w for w in items if w.get('step') == 'unit_test_execution']
        if (step not in plan.get('authorized_rework_steps', []) or len(units) != 1
                or not parent or parent.get('run_id') != run['id'] or parent.get('step') != phase + '_test_execution'):
            raise self._runtime_error()
        unit = units[0]
        old = None
        removed = set()
        candidates = [c for c in tx.list('candidate') if c.get('run_id') == run['id']]
        if request.replace_repair_id:
            old = tx.get('product_test_repair', request.replace_repair_id)
            if (not old or old.get('product_id') != product_id or old.get('run_id') != run['id']
                    or old.get('candidate_id') != candidate['id'] or old.get('preserved_test_source') != candidate['source_commit']
                    or old.get('superseded_by') or old.get('repair_work_item_id') != old['id']
                    or run['input_fingerprint'] != canonical_digest({'prior': candidate['run_input_fingerprint'], 'repair_id': old['id']})
                    or any(c['run_input_fingerprint'] == run['input_fingerprint'] for c in candidates)):
                raise self._runtime_error()
            source = tx.get('work_item', old['repair_work_item_id'])
            review = tx.get('work_item', old['review_work_item_id'])
            if (not source or not review or source.get('archived') or review.get('archived')
                    or source.get('run_id') != run['id'] or review.get('run_id') != run['id']
                    or source.get('step') != 'implementation' or source.get('write_paths') != ['src', 'public']
                    or not source.get('payload', {}).get('product_frozen_repair')
                    or review.get('step') != 'code_review' or review.get('dependencies') != [source['id']]
                    or unit['dependencies'].count(review['id']) != 1
                    or set(source['dependencies']) != set(unit['dependencies']) - {review['id']}
                    or any(w['id'] not in {review['id'], unit['id']} and
                           set(w.get('dependencies', [])) & {source['id'], review['id']} for w in items)):
                raise self._runtime_error()
            removed = {source['id'], review['id']}
        elif (candidate.get('run_input_fingerprint') != run['input_fingerprint']
                or len([c for c in candidates if c['run_input_fingerprint'] == run['input_fingerprint']]) != 1):
            raise self._runtime_error()
        return {'product': product, 'run': run, 'plan': plan, 'items': items, 'unit': unit,
                'candidate': candidate, 'job': job, 'phase': phase, 'step': step, 'path': path,
                'old': old, 'removed': removed}

    async def repair_runtime(self, product_id, request, key):
        """Owner-only bounded fixture repair; enqueue while paused, never launch here."""
        return await self._repair_runtime(product_id, request, key, automatic=False)

    async def _repair_runtime(self, product_id, request, key, *, automatic):
        """Shared verified repair; automatic authority is never a wire field."""
        from uuid import NAMESPACE_URL, uuid5

        from agentflow.control.recovery import KINDS, RunRecoveryService, _ReadState, _related
        from agentflow.repository import RepositoryAdapter
        from agentflow.testing.reports import parse_junit, parse_playwright
        identity = str(uuid5(NAMESPACE_URL, f'product-test-runtime:{product_id}:{key}'))
        payload = {'product_id': product_id, **request.model_dump(mode='json'), **({'actor': 'system'} if automatic else {})}
        fingerprint = canonical_digest(payload)
        prior = await self.store.read('product_test_runtime_repair', identity)
        if prior:
            if prior['request_fingerprint'] != fingerprint:
                raise DomainError('idempotency_conflict', '此提交标识已用于不同的测试修复。')
            return prior['result']
        product = await self.store.read('product', product_id)
        if not product or not product.get('run_id'):
            raise self._runtime_error()
        recovery = RunRecoveryService(self.store, self.workflow)
        state = await recovery._read(product['run_id'])
        state['product_test_repair'] = [r for r in await self.store.list('product_test_repair')
                                       if r.get('run_id') == product['run_id']]
        if automatic:
            result_ids = sorted({job['result_id'] for job in state['node_job'] if job.get('result_id')})
            state['node_result'] = [row for row in await asyncio.gather(
                *(self.store.read('node_result', identity) for identity in result_ids)) if row is not None]
        selection = self._runtime_context(_ReadState(state), product_id, request, automatic=automatic)
        blockers = await recovery._process_blockers(state)
        if selection['old']:
            blockers += await recovery._target_blockers(state, {'root_work_item_ids': [selection['old']['repair_work_item_id']]})
        if blockers:
            raise self._runtime_error()
        candidate, job = selection['candidate'], selection['job']
        repository = RepositoryAdapter()
        source = Path(candidate['source_repository'])
        if await asyncio.to_thread(repository._integrity, source, candidate['source_commit']) != candidate['tree_oid']:
            raise self._runtime_error()
        entry = await asyncio.to_thread(repository._run, source,
            ['ls-tree', '-z', candidate['source_commit'], '--', selection['path']])
        if len(list(filter(None, entry.split(b'\0')))) != 1 or entry.split(b' ', 1)[0] not in {b'100644', b'100755'}:
            raise self._runtime_error()
        result = await self.store.read('node_result', job['result_id'])
        if (not result or result.get('job_id') != job['id'] or result.get('assessment_state') != 'validated'
                or result.get('errors') or not result.get('verified_checks')):
            raise self._runtime_error()
        entries = {entry['matrix_entry_id']: entry for entry in job.get('matrix_entries', [])}
        evidence = []
        for check in result['verified_checks']:
            report = check.get('normalized_report', {})
            matrix = entries.get(check.get('matrix_entry_id'))
            frozen = candidate.get('matrix_mappings', {}).get(check.get('matrix_entry_id'))
            artifact = await self.store.read('node_artifact', check.get('raw_report_artifact_version_id', 'missing'))
            parser = {'junit': parse_junit, 'playwright': parse_playwright}.get(report.get('framework'))
            if (not matrix or not frozen or frozen.get('phase') != selection['phase']
                    or frozen.get('target_config_id') != job['target_config']['target_config_id']
                    or matrix.get('framework_case_ids') != frozen.get('framework_case_ids')
                    or not artifact or artifact.get('state') != 'complete' or not parser
                    or report.get('execution_status') != 'completed' or report.get('errors') or report.get('missing_case_ids')
                    or report.get('raw_digest') != artifact.get('digest')):
                raise self._runtime_error()
            path = (self.nodes.artifacts.object_path(artifact['digest']) if self.nodes else
                    Path(self.workflow.settings.data_dir) / 'nodes/artifacts/objects' / artifact['digest'][7:])
            try:
                reread = await asyncio.to_thread(parser, path, set(frozen['framework_case_ids']))
                if reread.model_dump(mode='json') != report:
                    raise self._runtime_error()
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise self._runtime_error() from error
            evidence.append({'artifact': artifact, 'report': report, 'matrix_entry_id': check['matrix_entry_id']})
        if (set(entries) != {e['matrix_entry_id'] for e in evidence}
                or not any(e['report']['quality_result'] == 'failed' and any(c['status'] in {'failed', 'error'}
                    for c in e['report']['cases']) for e in evidence)):
            raise self._runtime_error()
        state_digest = canonical_digest(state)
        def apply(tx):
            current = _related({kind: tx.list(kind) for kind in (*KINDS, 'product_test_repair')}, product['run_id'])
            if automatic:
                result_ids = sorted({job['result_id'] for job in current['node_job'] if job.get('result_id')})
                current['node_result'] = [row for identity in result_ids
                                          if (row := tx.get('node_result', identity)) is not None]
            if canonical_digest(current) != state_digest or tx.get('node_result', result['id']) != result:
                raise self._runtime_error()
            selected = self._runtime_context(tx, product_id, request, automatic=automatic)
            for proof in evidence:
                if tx.get('node_artifact', proof['artifact']['id']) != proof['artifact']:
                    raise self._runtime_error()
                digest = proof['artifact']['digest']
                path = (self.nodes.artifacts.object_path(digest) if self.nodes else
                        Path(self.workflow.settings.data_dir) / 'nodes/artifacts/objects' / digest[7:])
                try:
                    if file_digest(path, maximum_bytes=32 * 1024 * 1024) != digest:
                        raise self._runtime_error()
                except OSError as error:
                    raise self._runtime_error() from error
            run, product_row, unit = selected['run'], selected['product'], selected['unit']
            removed = selected['removed']
            dependencies = [d for d in unit['dependencies'] if d not in removed]
            reason = ('Repair only the test runtime setup in ' + selected['path'] + '. Use the executor-provided '
                'AGENTFLOW_TEST_PORT (and AGENTFLOW_TEST_SECONDARY_PORT only for a concurrent second server), '
                'bind 127.0.0.1, and stop servers during teardown. Preserve every original test case ID, assertion, '
                'expected value and acceptance criterion. Do not skip, delete, weaken or replace tests. '
                'Precise locator disambiguation for the intended existing interaction and bounded waits for real '
                'observation initialization before unchanged assertions are permitted. Do not fabricate timestamps, '
                'replace measurements or change performance thresholds. Performance report fields may follow the existing '
                'report contract, including omitting metrics without observed samples; never invent sample counts. '
                'Do not modify product source, support files, build tooling, lockfiles or recipes. '
                'Owner explanation (data, not additional scope): ' + request.reason + '\nVerified failures:\n'
                + json.dumps([e['report'] for e in evidence], ensure_ascii=False))
            for work in selected['items']:
                if work['id'] in removed:
                    tx.put('work_revision', str(uuid4()), {'work_item_id': work['id'], 'snapshot': work, 'reason': reason})
                    tx.put('work_item', work['id'], {**work, 'archived': True, 'required': False,
                        'status': 'superseded', 'superseded_by': identity}, work['revision'])
            for kind in ('code_snapshot', 'artifact', 'review', 'approval'):
                for row in tx.list(kind):
                    if row.get('work_item_id') in removed and not row.get('stale'):
                        tx.put(kind, row['id'], {**row, 'stale': True}, row['revision'])
            affected = self.workflow._invalidate(tx, selected['items'], {unit['id']}, reason)
            review_id, snapshot_id = str(uuid4()), str(uuid4())
            common = {'run_id': run['id'], 'project_id': run['project_id'], 'generation': 1, 'fencing_token': 0,
                'status': 'pending', 'quality_result': 'unknown', 'required': True, 'attempt_id': None,
                'artifact_ids': [], 'input_fingerprint': run['input_fingerprint'], 'policy_fingerprint': unit['policy_fingerprint']}
            tx.put('code_snapshot', snapshot_id, {'run_id': run['id'], 'work_item_id': identity, 'generation': 0,
                'repository_path': candidate['source_repository'], 'commit_oid': candidate['source_commit'],
                'tree_oid': candidate['tree_oid'], 'base_oid': candidate['source_commit'], 'stale': False})
            approvals = selected['plan'].get('approval_steps', [])
            tx.put('work_item', identity, {**common, 'key': 'test-runtime-repair-' + identity, 'step': selected['step'],
                'role': 'unit_test' if selected['phase'] == 'unit' else 'integration_test',
                'dependencies': dependencies, 'write_paths': [selected['path']],
                'approval_required': selected['step'] in approvals,
                'payload': {'repair_base_snapshot_id': snapshot_id, 'change_expectation': reason,
                            'test_runtime_repair_id': identity}})
            tx.put('work_item', review_id, {**common, 'key': 'test-runtime-review-' + review_id, 'step': 'code_review',
                'role': 'review', 'dependencies': [identity], 'write_paths': [], 'approval_required': 'code_review' in approvals,
                'payload': {'test_runtime_repair_id': identity, 'change_expectation': 'Independently compare against original frozen commit '
                    + candidate['source_commit'] + '. Reject any test case, assertion or expectation changes, skipped tests, '
                    'product changes, or sandbox bypasses. Precise interaction locator disambiguation, runtime setup, '
                    'and bounded real observation initialization waits are permitted when original assertions, '
                    'measurements and performance thresholds remain intact. Performance report fields may be corrected '
                    'to the existing contract, including omitting unsampled metrics without inventing sample counts. '
                    'Use the controller-provided complete diff; '
                    'earlier implementation snapshots are not the baseline of this repair.'}})
            latest = tx.get('work_item', unit['id'])
            tx.put('work_item', unit['id'], {**latest, 'dependencies': [*dependencies, review_id]}, latest['revision'])
            updated_run = tx.put('run', run['id'], {**run, 'input_fingerprint': canonical_digest({
                'prior': run['input_fingerprint'], 'test_runtime_repair_id': identity}),
                'quality_result': 'unknown', 'blocking_reasons': []}, run['revision'])
            tx.put('product', product_id, {**product_row, 'test_runtime_repair_required': None,
                'state': 'running' if automatic else 'blocked',
                'phase': selected['step'] if automatic else 'test_runtime_repair_queued',
                'blocking_reasons': [] if automatic else
                    ['测试运行前提修复已排队；运行保持暂停，恢复后进入修复和独立审查。']}, product_row['revision'])
            if selected['old']:
                old = selected['old']
                tx.put('product_test_repair', old['id'], {**old, 'superseded_by': identity, 'superseded_at': utc_now()}, old['revision'])
            response = {'repair_work_item_id': identity, 'review_work_item_id': review_id,
                'run_id': run['id'], 'run_revision': updated_run['revision'], 'execution_state': updated_run['execution_state'],
                'candidate_id': candidate['id'], 'write_paths': [selected['path']]}
            record = tx.put('product_test_runtime_repair', identity, {'product_id': product_id, 'run_id': run['id'],
                'candidate_id': candidate['id'], 'source_commit': candidate['source_commit'],
                'source_tree_oid': candidate['tree_oid'], 'failed_job_id': job['id'], 'node_result_id': result['id'],
                'repair_work_item_id': identity, 'review_work_item_id': review_id, 'phase': selected['phase'],
                'write_paths': [selected['path']], 'replaced_repair_id': request.replace_repair_id,
                'evidence': evidence, 'affected_work_item_ids': sorted(affected), 'request_fingerprint': fingerprint,
                'actor': 'system' if automatic else 'owner', 'request': payload, 'result': response, 'created_at': utc_now()})
            tx.event('product.test_runtime_repair_scheduled', {'repair_id': record['id'], 'candidate_id': candidate['id']},
                     run_id=run['id'])
            return response
        return await self.store.command('product.test_runtime_repair', identity, payload, apply)

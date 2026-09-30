"""Owner fixture repair over real frozen Git and verified reports; no worker execution."""
import json
from types import SimpleNamespace
from uuid import uuid4
from xml.sax.saxutils import quoteattr

import httpx
import pytest
from test_execution_pipeline import fixture

from agentflow.common import DomainError
from agentflow.control.api import create_app
from agentflow.control.product_repair import ProductTestRepair
from agentflow.control.product_routes import product_router
from agentflow.control.scheduler import Scheduler
from agentflow.models.budget import BudgetLedger
from agentflow.testing.reports import parse_junit


async def patch(env, kind, identity, **changes):
    def apply(tx):
        old = tx.get(kind, identity)
        return tx.put(kind, identity, {**(old or {}), **changes}, old['revision'] if old else None)
    return await env.store.command('fixture.test-runtime', str(uuid4()), {}, apply)


async def failed_candidate(env, *, phase='unit', environment_error=True, failure_message=None, missing_case=False):
    tests = env.source / 'tests'
    tests.mkdir()
    for name in ('unit.test.mjs', 'api.spec.mjs', 'web.spec.mjs'):
        (tests / name).write_text('import assert from "node:assert/strict";\nassert.equal(1, 1);\n')
    if missing_case:
        (tests / 'unit.test.mjs').write_text('import assert from "node:assert/strict";\n'
            'import test from "node:test";\n'
            "test('target-0::unit::denied', () => { assert.equal(1, 1); });\n")
    frozen = await env.repository.freeze_workspace(env.source, env.snapshot['commit_oid'], 'runtime fixture source')
    await patch(env, 'code_snapshot', 'code-snapshot', commit_oid=frozen['commit_oid'], tree_oid=frozen['tree_oid'])
    async def source(_run, _work):
        return env.source, frozen['commit_oid']
    env.pipeline.source_resolver = source
    plan = await env.store.read('plan', 'plan')
    await patch(env, 'plan', 'plan', product_contract={'stack': 'node_web_api', 'product_id': 'product'},
        authorized_rework_steps=['implementation', 'unit_test_implementation', 'integration_test_implementation'])
    await patch(env, 'run', 'run', purpose='code_delivery', budget_limit={
        'currency': 'USD', 'limit_micros': 0, 'max_model_requests': 20, 'cost_mode': 'request_limited'})
    await BudgetLedger(env.store).setup_accounts('run', 'iteration', 0, 0, run_max_requests=20, iteration_max_requests=20)
    output = env.settings.data_dir / 'product-output'
    output.mkdir()
    (output / '.agentflow-product.json').write_text(json.dumps({'product_id': 'product'}))
    await patch(env, 'product', 'product', run_id='run', run_ids=['run'], project_id=plan['project_id'],
                state='blocked', config_revision=1, needs_restart=False, output_directory=str(output))
    await env.pipeline.begin(await env.claim())
    await env.finish_builds()
    if phase == 'integration':
        for identity in (await env.candidate())['phase_jobs']['unit']:
            await env.test_receipt(identity)
        await env.pipeline.reconcile()
        await env.pipeline.begin(await env.claim())
    candidate = await env.candidate()
    for identity in candidate['phase_jobs'][phase]:
        job = await env.store.read('node_job', identity)
        checks = []
        for entry in job['matrix_entries']:
            message = failure_message or ('listen EPERM: operation not permitted 127.0.0.1'
                                           if environment_error else 'expected 1 to equal 2')
            cases = ''.join(f'<testcase name={quoteattr(case)} fullname={quoteattr(case)}>'
                + (f'<failure>{message}</failure>' if index == 0 and not missing_case else '') + '</testcase>'
                for index, case in enumerate(entry['framework_case_ids']) if not missing_case or index != 0)
            artifact = await env.nodes.import_input(f'<testsuite>{cases}</testsuite>'.encode(), 'junit.xml', 'run')
            parsed = parse_junit(env.nodes.artifacts.object_path(artifact['digest']), set(entry['framework_case_ids']))
            checks.append({'matrix_entry_id': entry['matrix_entry_id'], 'raw_report_artifact_version_id': artifact['id'],
                           'normalized_report': parsed.model_dump(mode='json')})
        await env._receipt(identity, 'completed', 'unknown' if missing_case else 'failed', 'validated', checks=checks)
    await env.pipeline.reconcile()
    await patch(env, 'run', 'run', execution_state='paused')
    return {'expected_revision': (await env.store.read('product', 'product'))['revision'],
            'expected_run_revision': (await env.store.read('run', 'run'))['revision'],
            'candidate_id': candidate['id'], 'failed_job_id': candidate['phase_jobs'][phase][0],
            'reason': 'Use executor-provided loopback port without changing test assertions.'}


async def request_repair(env, payload, key='runtime-repair'):
    from agentflow.control.product_models import ProductTestRuntimeRepairRequest
    return await ProductTestRepair(env.store, env.workflow, nodes=env.nodes).repair_runtime(
        'product', ProductTestRuntimeRepairRequest.model_validate(payload), key)


async def test_missing_required_cases_authorize_only_scoped_test_completion(tmp_path):
    async with fixture(tmp_path, app_targets=('api',)) as env:
        payload = await failed_candidate(env, missing_case=True)
        await patch(env, 'node_job', payload['failed_job_id'], state='failed')
        candidate = await env.store.read('candidate', payload['candidate_id'])
        result = await request_repair(env, payload)
        work = await env.store.read('work_item', result['repair_work_item_id'])
        assert work['write_paths'] == ['tests/unit.test.mjs']
        assert 'keep all existing IDs and assertions unchanged' in work['payload']['change_expectation']
        assert await env.store.read('candidate', candidate['id']) == candidate


@pytest.mark.parametrize('quality,expected', [('unknown', True), ('inconclusive', True), ('passed', False)])
def test_completed_execution_requires_passed_quality(quality, expected):
    from agentflow.control.products import ProductService
    assert ProductService._failed_work({'step': 'unit_test_execution', 'status': 'completed',
                                       'quality_result': quality}) is expected
    assert not ProductService._failed_work({'step': 'implementation', 'status': 'completed',
                                           'quality_result': 'unknown'})


@pytest.mark.parametrize('changes', [{'errors': ['missing_required_cases', 'runner_error']},
    {'missing_case_ids': []}, {'cases': [{'status': 'skipped'}]}, {'execution_status': 'completed'}])
def test_missing_case_authorization_rejects_other_incomplete_evidence(changes):
    report = {'execution_status': 'error', 'quality_result': 'unknown',
              'errors': ['missing_required_cases'], 'missing_case_ids': ['test::missing'],
              'cases': [{'status': 'passed'}]}
    assert not ProductTestRepair._missing_case_report({**report, **changes})


@pytest.mark.parametrize(('phase', 'target', 'path', 'step'), [
    ('unit', 'api', 'tests/unit.test.mjs', 'unit_test_implementation'),
    ('integration', 'api', 'tests/api.spec.mjs', 'integration_test_implementation'),
    ('integration', 'web', 'tests/web.spec.mjs', 'integration_test_implementation')])
async def test_owner_fixture_repair_preserves_upstream_and_failure(tmp_path, phase, target, path, step):
    async with fixture(tmp_path, app_targets=(target,)) as env:
        payload = await failed_candidate(env, phase=phase)
        before = {kind: await env.store.list(kind) for kind in ('candidate', 'node_job', 'node_result',
            'model_invocation', 'budget_account', 'plan')}
        code = await env.store.read('work_item', 'code-work')
        plan_artifact = await env.store.read('artifact', 'unit-plan-artifact')
        result = await request_repair(env, payload)
        work = await env.store.read('work_item', result['repair_work_item_id'])
        review = await env.store.read('work_item', result['review_work_item_id'])
        assert work['write_paths'] == [path] and work['step'] == step
        assert review['dependencies'] == [work['id']] and review['step'] == 'code_review'
        assert (await env.store.read('work_item', 'unit-work'))['dependencies'][-1] == review['id']
        assert (await env.store.read('run', 'run'))['execution_state'] == 'paused'
        assert (await env.store.read('work_item', 'unit-work'))['generation'] == 2
        assert (await env.store.read('work_item', 'integration-work'))['generation'] == 2
        assert await env.store.read('work_item', 'code-work') == code
        assert await env.store.read('artifact', 'unit-plan-artifact') == plan_artifact
        for kind, rows in before.items():
            assert await env.store.list(kind) == rows
        repository, commit = await Scheduler(env.workflow, env.store, None, None, env.settings)._source(
            await env.store.read('run', 'run'), work)
        assert str(repository) == before['candidate'][0]['source_repository']
        assert commit == before['candidate'][0]['source_commit']
        assert await request_repair(env, payload) == result
        with pytest.raises(DomainError):
            await request_repair(env, {**payload, 'reason': 'different request'})


@pytest.mark.parametrize('problem', ['run_live', 'stale_revision', 'candidate_wrong_run', 'job_wrong_candidate',
    'job_wrong_run', 'job_active', 'attempt_unknown', 'work_waiting_approval', 'process_running',
    'model_reserved', 'restore_uncertain', 'bad_raw', 'missing_raw', 'bad_report', 'source_changed', 'scope_not_authorized'])
async def test_invalid_or_unstopped_evidence_cannot_authorize_test_changes(tmp_path, problem):
    async with fixture(tmp_path, app_targets=('api',)) as env:
        payload = await failed_candidate(env)
        job = await env.store.read('node_job', payload['failed_job_id'])
        if problem == 'run_live':
            await patch(env, 'run', 'run', execution_state='running')
        elif problem == 'stale_revision':
            payload['expected_revision'] += 1
        elif problem == 'candidate_wrong_run':
            await patch(env, 'candidate', payload['candidate_id'], run_id='other')
        elif problem == 'job_wrong_candidate':
            await patch(env, 'node_job', job['id'], platform_artifact_manifest={'fingerprint': 'other'})
        elif problem == 'job_wrong_run':
            await patch(env, 'node_job', job['id'], run_id='other')
        elif problem == 'job_active':
            await patch(env, 'node_job', job['id'], state='running')
        elif problem == 'attempt_unknown':
            await patch(env, 'attempt', 'code-snapshot', status='execution_unknown')
        elif problem == 'work_waiting_approval':
            await patch(env, 'work_item', 'code-work', status='waiting_approval')
        elif problem == 'process_running':
            await patch(env, 'supervised_attempt', 'code-snapshot', run_id='run', state='running')
        elif problem == 'model_reserved':
            await patch(env, 'model_invocation', 'pending-call', run_id='run', state='reserved')
        elif problem == 'restore_uncertain':
            await patch(env, 'run', 'run', restore_reconciliation_required=True)
        elif problem == 'source_changed':
            await patch(env, 'candidate', payload['candidate_id'], tree_oid='f' * 40)
        elif problem == 'scope_not_authorized':
            await patch(env, 'plan', 'plan', authorized_rework_steps=['implementation'])
        else:
            result = await env.store.read('node_result', job['result_id'])
            check = result['verified_checks'][0]
            artifact = await env.store.read('node_artifact', check['raw_report_artifact_version_id'])
            if problem == 'bad_report':
                check['normalized_report']['raw_digest'] = 'sha256:' + '0' * 64
                await patch(env, 'node_result', result['id'], verified_checks=result['verified_checks'])
            elif problem == 'missing_raw':
                env.nodes.artifacts.object_path(artifact['digest']).unlink()
            else:
                path = env.nodes.artifacts.object_path(artifact['digest'])
                path.chmod(0o600)
                path.write_bytes(b'corrupted')
        with pytest.raises(DomainError):
            await request_repair(env, payload)
        assert not await env.store.list('product_test_runtime_repair')
        assert (await env.store.read('work_item', 'unit-work'))['generation'] == 1


async def test_explicit_replacement_removes_only_stopped_misdirected_repair(tmp_path):
    async with fixture(tmp_path, app_targets=('api',)) as env:
        payload = await failed_candidate(env, environment_error=False)
        await patch(env, 'run', 'run', execution_state='running')
        old = await ProductTestRepair(env.store, env.workflow, nodes=env.nodes).attempt({'id': 'product', 'run_id': 'run'})
        assert old['scheduled']
        old_record = await env.store.read('product_test_repair', old['repair_id'])
        await patch(env, 'work_item', old['repair_id'], status='cancelled')
        await patch(env, 'run', 'run', execution_state='paused')
        payload.update(expected_run_revision=(await env.store.read('run', 'run'))['revision'], replace_repair_id=old['repair_id'])
        result = await request_repair(env, payload)
        for identity in (old['repair_id'], old_record['review_work_item_id']):
            work = await env.store.read('work_item', identity)
            assert work['archived'] and not work['required']
        assert (await env.store.read('product_test_repair', old['repair_id']))['superseded_by'] == result['repair_work_item_id']
        unit = await env.store.read('work_item', 'unit-work')
        assert old_record['review_work_item_id'] not in unit['dependencies']
        work = await env.store.read('work_item', result['repair_work_item_id'])
        assert old_record['review_work_item_id'] not in work['dependencies']
        base = await env.store.read('code_snapshot', work['payload']['repair_base_snapshot_id'])
        assert base['commit_oid'] == (await env.store.read('candidate', payload['candidate_id']))['source_commit']


async def test_listen_permission_failure_never_schedules_product_source_repair(tmp_path):
    async with fixture(tmp_path, app_targets=('api',)) as env:
        await failed_candidate(env)
        await patch(env, 'run', 'run', execution_state='running')
        result = await ProductTestRepair(env.store, env.workflow, nodes=env.nodes).attempt({'id': 'product', 'run_id': 'run'})
        assert not result['scheduled'] and result['reason_code'] == 'test_runtime_repair_required'
        product = await env.store.read('product', 'product')
        assert product['test_runtime_repair_required']['code'] == 'test_runtime_repair_required'
        assert not await env.store.list('product_test_repair')


async def test_owner_endpoint_authentication_and_strict_request(tmp_path):
    async with fixture(tmp_path, app_targets=('api',)) as env:
        payload = await failed_candidate(env)
        app = create_app(env.settings, store=env.store, artifacts=env.workflow.artifacts)
        service = SimpleNamespace(test_repair=ProductTestRepair(env.store, env.workflow, nodes=env.nodes))
        app.include_router(product_router(service))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=env.settings.origin) as client:
            url = '/api/v1/products/product/test-runtime-repair'
            assert (await client.post(url, json=payload)).status_code in {401, 403}
            session = await client.post('/api/v1/session', json={'bootstrap_token': app.state.tokens.bootstrap_code},
                headers={'Origin': env.settings.origin, 'Idempotency-Key': 'session'})
            headers = {'Origin': env.settings.origin, 'Authorization': 'Bearer ' + session.json()['owner_token'],
                       'Idempotency-Key': 'owner-runtime'}
            for change in ({'write_paths': ['.']}, {'expected_revision': True}, {'candidate_id': ''}, {'automatic': True}):
                assert (await client.post(url, json={**payload, **change}, headers=headers)).status_code == 422
            response = await client.post(url, json=payload, headers=headers)
            assert response.status_code == 202, response.text
            assert response.json()['repair_work_item_id']


async def test_runtime_hint_survives_product_refresh_and_clears_after_owner_request(tmp_path):
    from agentflow.control.products import ProductService
    async with fixture(tmp_path, app_targets=('api',)) as env:
        payload = await failed_candidate(env)
        service = ProductService(env.store, env.workflow, None, None, SimpleNamespace(nodes=env.nodes), None, env.settings)
        await patch(env, 'run', 'run', execution_state='running')
        for _ in range(2):
            await service._advance(await env.store.read('product', 'product'))
            product = await service.detail('product')
            assert product['phase'] == 'test_runtime_repair_required'
            assert '测试运行前提修复' in product['blocking_reasons'][0]
        await patch(env, 'run', 'run', execution_state='paused')
        payload.update(expected_revision=(await env.store.read('product', 'product'))['revision'],
                       expected_run_revision=(await env.store.read('run', 'run'))['revision'])
        await request_repair(env, payload)
        await patch(env, 'run', 'run', execution_state='running')
        await service._advance(await env.store.read('product', 'product'))
        product = await service.detail('product')
        assert product['state'] == 'running' and not product['test_runtime_repair_required']


async def test_report_changed_after_parse_is_rejected_inside_writer(tmp_path, monkeypatch):
    from agentflow.testing import reports
    async with fixture(tmp_path, app_targets=('api',)) as env:
        payload = await failed_candidate(env)
        parse = reports.parse_junit
        def changed(path, *args):
            result = parse(path, *args)
            path.chmod(0o600)
            path.write_bytes(b'changed after parsing')
            return result
        monkeypatch.setattr(reports, 'parse_junit', changed)
        with pytest.raises(DomainError):
            await request_repair(env, payload)
        assert not await env.store.list('product_test_runtime_repair')


async def test_job_target_cannot_select_another_test_file(tmp_path):
    async with fixture(tmp_path, app_targets=('api',)) as env:
        payload = await failed_candidate(env, phase='integration')
        job = await env.store.read('node_job', payload['failed_job_id'])
        await patch(env, 'node_job', job['id'], target_config={**job['target_config'], 'app_target': 'web'})
        with pytest.raises(DomainError):
            await request_repair(env, payload)


@pytest.mark.parametrize('problem', ['live_repair', 'wrong_product', 'wrong_source', 'later_candidate', 'changed_dependency'])
async def test_replacement_cannot_discard_an_unrelated_live_or_already_consumed_repair(tmp_path, problem):
    async with fixture(tmp_path, app_targets=('api',)) as env:
        payload = await failed_candidate(env, environment_error=False)
        await patch(env, 'run', 'run', execution_state='running')
        old = await ProductTestRepair(env.store, env.workflow, nodes=env.nodes).attempt({'id': 'product', 'run_id': 'run'})
        await patch(env, 'work_item', old['repair_id'], status='running' if problem == 'live_repair' else 'cancelled')
        await patch(env, 'run', 'run', execution_state='paused')
        payload.update(expected_run_revision=(await env.store.read('run', 'run'))['revision'], replace_repair_id=old['repair_id'])
        if problem == 'wrong_product':
            await patch(env, 'product_test_repair', old['repair_id'], product_id='other')
        elif problem == 'wrong_source':
            await patch(env, 'product_test_repair', old['repair_id'], preserved_test_source='f' * 40)
        elif problem == 'later_candidate':
            await patch(env, 'candidate', 'new-candidate', run_id='run',
                        run_input_fingerprint=(await env.store.read('run', 'run'))['input_fingerprint'])
        elif problem == 'changed_dependency':
            await patch(env, 'work_item', 'unit-work', dependencies=['code-work'])
        with pytest.raises(DomainError):
            await request_repair(env, payload)
        assert not (await env.store.read('work_item', old['repair_id'])).get('archived')


async def historical_unknown(env):
    from datetime import UTC, datetime, timedelta

    from test_model_uncertainty_acknowledgment import acknowledgment

    from agentflow.common import canonical_digest
    from agentflow.control.model_uncertainty import ModelUncertaintyService
    from agentflow.models.profiles import AttemptContext
    from agentflow.runtime.launcher import atomic_json
    run = await env.store.read('run', 'run')
    await patch(env, 'iteration', 'iteration', budget_limit=run['budget_limit'])
    fingerprint = canonical_digest('historical stopped invocation')
    await patch(env, 'work_item', 'historic', run_id='run', project_id=run['project_id'], step='implementation',
        status='completed', generation=1, fencing_token=1, input_fingerprint=fingerprint,
        attempt_id='historic-attempt', dependencies=[], required=False, quality_result='unknown')
    await patch(env, 'attempt', 'historic-attempt', run_id='run', iteration_id='iteration', work_item_id='historic',
        generation=1, fencing_token=1, input_fingerprint=fingerprint, status='failed')
    directory = env.settings.data_dir / 'supervisor' / canonical_digest({'attempt_id': 'historic-attempt'}).split(':')[1]
    directory.mkdir(parents=True, mode=0o700)
    process = {'attempt_id': 'historic-attempt', 'operation_id': 'historic-attempt', 'run_id': 'run',
        'state': 'failed', 'reason': 'timeout', 'directory': str(directory), 'nonce': 'fixture-nonce',
        'pid': 2147483647, 'process_started_at': 1.0, 'boot_fingerprint': canonical_digest({'boot_time': 0}),
        'fencing_token': 1, 'input_fingerprint': fingerprint}
    atomic_json(directory / 'result.json', {**process, 'execution_status': 'failed'})
    await patch(env, 'supervised_attempt', 'historic-attempt', **process)
    ledger = BudgetLedger(env.store)
    context = AttemptContext(attempt_id='historic-attempt', run_id='run', iteration_id='iteration',
        model_profile_id='fixture', fencing_token=1, input_fingerprint=fingerprint,
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(), max_model_requests=20,
        max_output_tokens=1024, cost_mode='request_limited')
    invocation = await ledger.reserve(context, protocol='responses', request_fingerprint=fingerprint,
        profile_revision=1, amount_micros=0, currency='USD', idempotency_key='historical')
    await ledger.dispatch(invocation['id'])
    await ledger.uncertain(invocation['id'], 'consumer_disconnected')
    service = ModelUncertaintyService(env.store, env.workflow, SimpleNamespace(_active={}))
    view = await service.view('run')
    row = next(row for row in view['items'] if row['invocation_id'] == invocation['id'])
    assert row['eligible'], row['blockers']
    await service.acknowledge('run', invocation['id'], acknowledgment(row), 'historical-ack')
    return invocation['id']


@pytest.mark.parametrize('problem', [None, 'ack_tampered', 'process_changed'])
async def test_counted_unknown_history_requires_a_current_valid_acknowledgment(tmp_path, problem):
    from agentflow.models.uncertainty import ACK_KIND
    async with fixture(tmp_path, app_targets=('api',)) as env:
        payload = await failed_candidate(env)
        invocation_id = await historical_unknown(env)
        payload['expected_run_revision'] = (await env.store.read('run', 'run'))['revision']
        before = {kind: await env.store.list(kind) for kind in ('model_invocation', 'model_attempt_budget', 'budget_account')}
        if problem == 'ack_tampered':
            ack = (await env.store.list(ACK_KIND))[0]
            await patch(env, ACK_KIND, ack['id'], basis={})
        elif problem == 'process_changed':
            await patch(env, 'supervised_attempt', 'historic-attempt', nonce='changed')
        if problem:
            with pytest.raises(DomainError):
                await request_repair(env, payload)
        else:
            assert (await request_repair(env, payload))['repair_work_item_id']
            assert (await env.store.read('model_invocation', invocation_id))['state'] == 'uncertain'
        for kind, rows in before.items():
            assert await env.store.list(kind) == rows


async def test_repair_cannot_change_the_failed_candidate_source_identity(tmp_path):
    async with fixture(tmp_path, app_targets=('api',)) as env:
        payload = await failed_candidate(env)
        candidate = await env.store.read('candidate', payload['candidate_id'])
        (env.source / 'feature.txt').write_text('a different real source tree\n')
        other = await env.repository.freeze_workspace(env.source, candidate['source_commit'], 'different source')
        await patch(env, 'candidate', candidate['id'], source_commit=other['commit_oid'], tree_oid=other['tree_oid'])
        with pytest.raises(DomainError):
            await request_repair(env, payload)
        assert not await env.store.list('product_test_runtime_repair')


@pytest.mark.parametrize('budget_state', ['unknown', 'known', 'malformed'])
async def test_replacing_source_repair_cannot_abandon_unknown_coding_usage(tmp_path, budget_state):
    from agentflow.control.coding_steps import CodingSteps
    async with fixture(tmp_path, app_targets=('api',)) as env:
        payload = await failed_candidate(env, environment_error=False)
        await patch(env, 'run', 'run', execution_state='running')
        old = await ProductTestRepair(env.store, env.workflow, nodes=env.nodes).attempt({'id': 'product', 'run_id': 'run'})
        await patch(env, 'work_item', old['repair_id'], status='cancelled')
        fields = {'run_id': 'run', 'work_item_id': old['repair_id'], 'uncertain': budget_state != 'known'}
        if budget_state != 'malformed':
            fields.update(max_steps=8, step_count=0, max_tool_calls=20, observed_tool_calls=0,
                          max_active_seconds=300, active_seconds=0)
        budget = await patch(env, 'coding_work_budget', CodingSteps.budget_id('run', old['repair_id']), **fields)
        await patch(env, 'run', 'run', execution_state='paused')
        payload.update(expected_run_revision=(await env.store.read('run', 'run'))['revision'], replace_repair_id=old['repair_id'])
        if budget_state == 'known':
            assert (await request_repair(env, payload))['repair_work_item_id']
        else:
            with pytest.raises(DomainError):
                await request_repair(env, payload)
            assert not (await env.store.read('work_item', old['repair_id'])).get('archived')
            assert not await env.store.list('product_test_runtime_repair')
        assert await env.store.read('coding_work_budget', budget['id']) == budget

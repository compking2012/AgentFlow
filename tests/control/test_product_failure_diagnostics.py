"""Current-attempt failure diagnostics reach products without publishing raw errors."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_products

from agentflow.common import canonical_digest
from agentflow.control.coding_steps import CodingSteps
from agentflow.control.scheduler import Scheduler
from agentflow.models.budget import account_id
from agentflow.product_cli import print_product
from agentflow.repository import RepositoryAdapter
from agentflow.runtime.failures import runtime_failure_message
from agentflow.runtime.trace import ExecutionTrace

product_api = test_products.product_api
FINGERPRINT = 'sha256:' + 'a' * 64


async def failed_product(env, *, code=None, summary='Role did not complete', status='failed', reason=None, supervised=True):
    project = await env.service.workflow.create_project({'name': 'Failure diagnostics fixture',
        'local_path': str(env.settings.data_dir.parent / 'diagnostic-project'),
        'import_mode': 'initialize_managed', 'dirty_worktree_policy': 'require_clean'}, 'diagnostic-project')
    output = env.settings.data_dir.parent / 'diagnostic-product'
    output.mkdir()
    (output / '.agentflow-product.json').write_text(json.dumps({'product_id': 'product'}))
    def seed(tx):
        product = tx.put('product', 'product', {'name': 'Example', 'run_id': 'run', 'state': 'blocked',
            'project_id': project['id'], 'output_directory': str(output),
            'phase': 'goal', 'blocking_reasons': ['goal 尚未通过'], 'delivery': None})
        tx.put('run', 'run', {'execution_state': 'running', 'delivery_ids': [], 'quality_result': 'unknown',
                             'project_id': project['id'], 'input_fingerprint': FINGERPRINT})
        item = tx.put('work_item', 'work', {'run_id': 'run', 'attempt_id': 'attempt', 'step': 'goal',
            'project_id': project['id'],
            'status': status, 'quality_result': 'unknown', 'required': True, 'generation': 1, 'role': 'product',
            'fencing_token': 2, 'input_fingerprint': FINGERPRINT, 'policy_fingerprint': FINGERPRINT,
            'artifact_ids': [], 'approval_required': False})
        tx.put('attempt', 'attempt', {'run_id': 'run', 'work_item_id': 'work', 'generation': 1,
            'fencing_token': 2, 'input_fingerprint': FINGERPRINT, 'status': status, 'summary': summary,
            'runtime_failure_code': code})
        if supervised:
            tx.put('supervised_attempt', 'attempt', {'attempt_id': 'attempt', 'run_id': 'run', 'state': status,
                'fencing_token': 2, 'input_fingerprint': FINGERPRINT, 'backend': 'openhands_role',
                'reason': reason, 'exit_code': 1, 'directory': '/must-not-be-read/arbitrary-log-path'})
        return {'product': product, 'item': item}
    seeded = await env.store.command('fixture', 'seed-failure', {}, seed)
    return seeded['product'], seeded['item']


def role_error(env, *, error_type='ConversationRunError', message=None):
    path = env.settings.data_dir / 'attempt_artifacts' / canonical_digest('attempt').split(':')[1] / 'role_error.json'
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps({'type': error_type, 'message': message or (
        "'PromptTokensDetailsWrapper' object has no attribute 'cache_creation_tokens'; "
        'api_key=private-provider-key Authorization: Bearer private-scoped-token')}))
    path.chmod(0o600)
    return path


def collector(env, execute_task):
    scheduler = Scheduler.__new__(Scheduler)
    scheduler._wake = asyncio.Event()
    scheduler.store, scheduler.settings, scheduler.workflow = env.store, env.settings, env.service.workflow
    scheduler.repository = RepositoryAdapter()
    scheduler.coding_steps = CodingSteps(env.store, env.settings, scheduler.repository)
    scheduler.traces = ExecutionTrace(env.store)
    scheduler.runtime = SimpleNamespace(execute_task=execute_task)
    return scheduler


async def test_legacy_failure_is_enriched_read_only_for_api_list_detail_and_cli(product_api, capsys):
    env = product_api
    original, _ = await failed_product(env)
    role_error(env)
    before = {kind: await env.store.list(kind) for kind in ['product', 'work_item', 'attempt', 'supervised_attempt']}
    expected = '目标分析：' + runtime_failure_message('response_usage_incompatible')
    for url in ['/api/v1/products/product', '/api/v1/products']:
        response = await env.client.get(url, headers=env.headers)
        assert response.status_code == 200
        product = response.json()['items'][0] if url.endswith('products') else response.json()
        assert product['blocking_reasons'] == [expected]
        assert product['state'] == 'blocked' and product['id'] == original['id']
        assert not any(secret in response.text for secret in ['private-provider-key', 'private-scoped-token',
                                                            'cache_creation_tokens', '/must-not-be-read'])
    print_product(product)
    assert expected in capsys.readouterr().out
    for kind, records in before.items():
        assert await env.store.list(kind) == records, 'Reading failure details cannot modify an existing run'
    assert not await env.store.list('model_invocation')


async def test_planning_diagnostic_shows_all_invalid_scopes_without_generic_blocker(product_api):
    env = product_api
    await failed_product(env, code='planning_validation_failed', status='blocked')
    issues = [{'code': 'role_write_scope', 'stage_key': 'code_review', 'child_key': str(i),
        'path': f'parallel_work/1/children/{i}/write_paths', 'message': 'Read-only review has writes'} for i in range(5)]
    diagnostic = {'code': 'planning_validation_failed', 'message': 'Validation failed',
        'details': {'origin': 'planning_validation', 'phase': 'plan_validation',
                    'category': 'correctable_output', 'issues': issues}}
    def attach(tx):
        row = tx.get('attempt', 'attempt')
        return tx.put('attempt', 'attempt', {**row, 'failure_diagnostic': diagnostic}, row['revision'])
    await env.store.command('fixture', 'planning-diagnostic', {}, attach)
    response = await env.client.get('/api/v1/products/product', headers=env.headers)
    message = response.json()['blocking_reasons'][0]
    assert '5' in message and '写权限' in message and 'inspection_paths' in message
    assert '缺少执行前提' not in message


async def test_unknown_controller_diagnostic_keeps_original_code_but_redacts_secret(product_api):
    env = product_api
    await failed_product(env, code='controller_validation_failed', status='blocked')
    def attach(tx):
        row = tx.get('attempt', 'attempt')
        return tx.put('attempt', 'attempt', {**row, 'failure_diagnostic': {
            'code': 'unexpected_stage_contract', 'message': 'Stage is missing. api_key=private-value', 'details': None}}, row['revision'])
    await env.store.command('fixture', 'unknown-diagnostic', {}, attach)
    response = await env.client.get('/api/v1/products/product', headers=env.headers)
    assert 'unexpected_stage_contract' in response.text and 'Stage is missing' in response.text
    assert 'private-value' not in response.text and '缺少执行前提' not in response.text


@pytest.mark.parametrize('used,maximum,expected', [(199, 200, 'model_rate_limited'),
    (200, 200, 'model_request_limit_reached'), (0, 0, 'model_rate_limited'), (9000, 0, 'model_rate_limited')])
async def test_current_run_limit_is_distinguished_from_provider_throttling(product_api, used, maximum, expected):
    from agentflow.control.presentation import RunPresentationService

    env = product_api
    _, item = await failed_product(env, code='model_rate_limited')
    await env.store.command('fixture', 'request-cap', {}, lambda tx: tx.put('budget_account', account_id('run', 'run'),
        {'owner_id': 'run', 'request_count': used, 'max_requests': maximum}))
    before = await env.store.list('budget_account')
    detail = await env.service.detail('product')
    assert detail['blocking_reasons'] == ['目标分析：' + runtime_failure_message(expected)]
    workflow = await RunPresentationService(env.store, env.service.workflow.artifacts, env.settings).workflow('run')
    assert workflow['stages'][0]['tasks'][0]['blocking_reason'] == runtime_failure_message(expected)
    assert await env.store.list('budget_account') == before
    assert (await env.store.read('work_item', item['id']))['status'] == 'failed'


async def test_scheduler_persists_safe_failure_code_and_product_blocks_with_specific_reason(product_api):
    env = product_api
    product, item = await failed_product(env, status='running')
    result = {'execution_status': 'failed', 'quality_result': 'unknown', 'artifacts': [],
              'summary': runtime_failure_message('response_usage_incompatible'),
              'runtime_failure_code': 'response_usage_incompatible'}
    scheduler = collector(env, AsyncMock(return_value=result))
    await scheduler._execute_existing({'attempt_id': 'attempt', 'work_item_id': 'work', 'run_id': 'run',
        'step': 'goal', 'input_fingerprint': FINGERPRINT, 'fencing_token': 2})
    attempt = await env.store.read('attempt', 'attempt')
    assert attempt['runtime_failure_code'] == 'response_usage_incompatible'
    assert attempt['status'] == 'failed' and attempt['quality_result'] == 'unknown'
    env.service.test_repair = SimpleNamespace(attempt=AsyncMock(return_value={'scheduled': False}))
    await env.service._advance(product)
    stored = await env.store.read('product', 'product')
    assert stored['blocking_reasons'] == ['目标分析：' + result['summary']]
    assert stored['state'] == 'blocked'
    assert (await env.store.read('work_item', item['id']))['status'] == 'failed'
    assert not await env.store.list('model_invocation')


async def test_verified_reasoning_limit_reaches_product_and_workflow_without_rewriting_history(product_api, monkeypatch):
    import agentflow.control.presentation as presentation
    import agentflow.control.products as products

    env = product_api
    await failed_product(env, code='model_output_limit')
    refine = AsyncMock(return_value='reasoning_output_limit')
    monkeypatch.setattr(products, 'refine_codex_failure', refine)
    monkeypatch.setattr(presentation, 'refine_codex_failure', refine)
    before = await env.store.list('attempt')
    detail = await env.service.detail('product')
    assert runtime_failure_message('reasoning_output_limit') in detail['blocking_reasons'][0]
    flow = await presentation.RunPresentationService(env.store, env.service.workflow.artifacts, env.settings).workflow('run')
    assert flow['stages'][0]['tasks'][0]['blocking_reason'] == runtime_failure_message('reasoning_output_limit')
    assert await env.store.list('attempt') == before
    assert refine.await_count == 2
    for call in refine.await_args_list:
        assert call.kwargs == {'fencing_token': 2, 'input_fingerprint': FINGERPRINT, 'fallback': 'model_output_limit'}


async def test_collected_truncation_persists_verified_reasoning_diagnostic(product_api, monkeypatch):
    import agentflow.control.scheduler as scheduler_module

    env = product_api
    await failed_product(env, status='running')
    refine = AsyncMock(return_value='reasoning_output_limit')
    monkeypatch.setattr(scheduler_module, 'refine_codex_failure', refine)
    scheduler = collector(env, AsyncMock(return_value={
        'execution_status': 'failed', 'quality_result': 'unknown', 'artifacts': [],
        'runtime_failure_code': 'model_output_limit', 'summary': 'generic limit'}))
    await scheduler._execute_existing({'attempt_id': 'attempt', 'work_item_id': 'work', 'run_id': 'run',
        'step': 'implementation', 'input_fingerprint': FINGERPRINT, 'fencing_token': 2})
    result = await env.store.read('attempt', 'attempt')
    assert result['status'] == 'failed' and result['runtime_failure_code'] == 'reasoning_output_limit'
    assert result['summary'] == runtime_failure_message('reasoning_output_limit')


async def test_prelaunch_timeout_is_persisted_as_a_specific_block_without_raw_error(product_api):
    from agentflow.common import DomainError

    env = product_api
    await failed_product(env, status='running', supervised=False)
    scheduler = collector(env, AsyncMock(side_effect=DomainError(
        'isolation_probe_timeout', 'sensitive-provider-detail=fixture-private-value')))
    await scheduler._execute_existing({'attempt_id': 'attempt', 'work_item_id': 'work', 'run_id': 'run',
        'step': 'code_review', 'fencing_token': 2, 'input_fingerprint': FINGERPRINT})
    attempt = await env.store.read('attempt', 'attempt')
    assert attempt['status'] == 'blocked' and attempt['runtime_failure_code'] == 'isolation_probe_timeout'
    assert attempt['summary'] == runtime_failure_message('isolation_probe_timeout')
    assert 'fixture-private-value' not in json.dumps(attempt)
    assert not await env.store.list('supervised_attempt')
    assert not await env.store.list('model_invocation')


async def test_legacy_prelaunch_proof_refines_product_and_workflow_without_changing_history(product_api, monkeypatch):
    from agentflow.control import presentation, products

    env = product_api
    await failed_product(env, status='blocked', supervised=False,
        summary='Filesystem/network isolation probe failed; execution blocked')
    proof = AsyncMock(return_value='isolation_probe_timeout')
    monkeypatch.setattr(products, 'prelaunch_failure_code', proof)
    monkeypatch.setattr(presentation, 'prelaunch_failure_code', proof)
    before = await env.store.list('attempt')
    detail = await env.service.detail('product')
    assert runtime_failure_message('isolation_probe_timeout') in detail['blocking_reasons'][0]
    flow = await presentation.RunPresentationService(env.store, env.service.workflow.artifacts, env.settings).workflow('run')
    assert flow['stages'][0]['tasks'][0]['blocking_reason'] == runtime_failure_message('isolation_probe_timeout')
    assert proof.await_count == 2 and await env.store.list('attempt') == before


async def test_legacy_block_command_identity_is_unchanged_without_a_failure_code(product_api):
    env = product_api
    await failed_product(env, status='running', supervised=False)
    first = await env.service.workflow.block_attempt('attempt', 'existing reason', 'old-block')
    assert await env.service.workflow.block_attempt('attempt', 'existing reason', 'old-block', failure_code=None) == first


@pytest.mark.parametrize('field,value', [
    ('generation', 0), ('fencing_token', 1), ('input_fingerprint', 'sha256:' + 'b' * 64),
    ('run_id', 'different-run'), ('work_item_id', 'different-work'),
])
async def test_stale_attempt_metadata_cannot_supply_a_new_failure_reason(product_api, monkeypatch, field, value):
    env = product_api
    await failed_product(env, code='response_usage_incompatible')
    def change(tx):
        attempt = tx.get('attempt', 'attempt')
        return tx.put('attempt', 'attempt', {**attempt, field: value}, attempt['revision'])
    await env.store.command('fixture', 'stale', {}, change)
    def forbidden(*_):
        raise AssertionError('A stale attempt cannot cause an artifact read')
    monkeypatch.setattr('agentflow.control.products.read_role_failure', forbidden)
    detail = await env.service.detail('product')
    assert detail['blocking_reasons'] == ['目标分析：Agent 执行进程异常退出。']


async def test_unknown_error_text_and_untrusted_error_code_are_never_published(product_api):
    env = product_api
    await failed_product(env, code='Bearer private-scoped-token', summary='api_key=private-provider-key',
                         reason='private-unknown-reason')
    role_error(env, error_type='private-sensitive-type', message='private-scoped-token private-provider-key')
    detail = await env.service.detail('product')
    assert detail['blocking_reasons'] == ['目标分析：' + runtime_failure_message('worker_internal_error')]
    assert 'private-' not in json.dumps(detail)


@pytest.mark.parametrize('kind', ['linked_file', 'linked_directory', 'hardlink', 'public_file', 'oversized', 'malformed', 'fifo'])
async def test_unsafe_or_missing_old_evidence_falls_back_to_safe_exit_information(product_api, tmp_path, kind):
    import os
    env = product_api
    await failed_product(env)
    path = role_error(env)
    if kind == 'linked_file':
        other = tmp_path / 'private-error'
        path.rename(other)
        path.symlink_to(other)
    elif kind == 'linked_directory':
        other = tmp_path / 'private-directory'
        path.parent.rename(other)
        path.parent.symlink_to(other)
    elif kind == 'hardlink':
        os.link(path, tmp_path / 'second-link')
    elif kind == 'public_file':
        path.chmod(0o644)
    elif kind == 'oversized':
        path.write_text('x' * 65537)
    elif kind == 'malformed':
        path.write_text('private-scoped-token: not JSON')
    else:
        path.unlink()
        os.mkfifo(path, 0o600)
    detail = await env.service.detail('product')
    assert detail['blocking_reasons'] == ['目标分析：Agent 执行进程异常退出（退出码 1）。']
    assert 'private-' not in json.dumps(detail)


async def test_execution_uncertainty_is_not_hidden_by_an_old_worker_error(product_api):
    env = product_api
    await failed_product(env, status='execution_unknown')
    role_error(env)
    detail = await env.service.detail('product')
    assert detail['blocking_reasons'] == ['目标分析：' + runtime_failure_message('execution_unconfirmed')]


async def test_supervisor_fence_mismatch_cannot_authorize_legacy_error_file_read(product_api, monkeypatch):
    env = product_api
    await failed_product(env)
    def change(tx):
        supervised = tx.get('supervised_attempt', 'attempt')
        return tx.put('supervised_attempt', 'attempt', {**supervised, 'fencing_token': 1}, supervised['revision'])
    await env.store.command('fixture', 'wrong-supervisor-fence', {}, change)
    def forbidden(*_):
        raise AssertionError('A mismatched supervisor cannot authorize an artifact read')
    monkeypatch.setattr('agentflow.control.products.read_role_failure', forbidden)
    detail = await env.service.detail('product')
    assert detail['blocking_reasons'] == ['目标分析：Agent 执行进程异常退出。']


async def test_export_or_restore_blocking_reasons_are_preserved(product_api):
    env = product_api
    await failed_product(env, code='response_usage_incompatible')
    for flag in ['finalization_error', 'restore_reconciliation_required']:
        def change(tx):
            product = tx.get('product', 'product')
            return tx.put('product', 'product', {**product, flag: True,
                          'blocking_reasons': ['Existing recovery requires reconciliation']}, product['revision'])
        await env.store.command('fixture', flag, {}, change)
        detail = await env.service.detail('product')
        assert detail['blocking_reasons'] == ['Existing recovery requires reconciliation']


async def test_controller_collector_drops_unknown_failure_codes(product_api):
    env = product_api
    await failed_product(env, status='running')
    await env.service.workflow.finish_attempt('attempt', {'execution_status': 'failed', 'quality_result': 'unknown',
        'input_fingerprint': FINGERPRINT, 'fencing_token': 2, 'summary': 'Role did not complete',
        'runtime_failure_code': 'arbitrary-private-code'}, 'finish', verified_artifacts=[])
    assert (await env.store.read('attempt', 'attempt'))['runtime_failure_code'] is None


async def test_known_supervisor_limits_are_shown_without_raw_attempt_summary(product_api):
    env = product_api
    await failed_product(env, summary='private-scoped-token', reason='timeout')
    detail = await env.service.detail('product')
    assert detail['blocking_reasons'] == ['目标分析：' + runtime_failure_message('worker_timeout')]

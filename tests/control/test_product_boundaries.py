"""Product lifecycle coordination with real Store and protocol-only preparation.

The injected workflow below tests durable identities, not generated software,
model execution, sandbox acceptance, or delivery quality.
"""
import asyncio
import json
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
import test_products

from agentflow.common import DomainError
from agentflow.control.product_models import ProductRequest

product_api = test_products.product_api
configure = test_products.configure


async def submit(env, tmp_path, key='product', **changes):
    await configure(env)
    request = ProductRequest(name='Boundary product', goal='Build a persistent list of useful books.',
        output_directory=str(tmp_path / key), **changes)
    return await env.service.submit(request, key), request


async def ready_local(env):
    async def prepare(**_):
        return {'state': 'ready', 'target_configs': [{'app_target': 'web'}, {'app_target': 'api'}]}
    env.service.local.prepare = prepare


async def protocol_workflow(env, *, failure=None):
    """Persist only the preparation protocol; deliberately never create code."""
    calls = SimpleNamespace(starts=0)

    async def create_project(payload, key):
        return await env.store.command('fixture.project', key, payload,
            lambda tx: tx.put('project', 'project', payload))

    async def create_plan(payload, key):
        return await env.store.command('fixture.plan', key, payload,
            lambda tx: tx.put('plan', 'plan', {**payload, 'state': 'ready'}))

    async def start_run(payload, key):
        calls.starts += 1
        if failure == 'before_commit' and calls.starts == 1:
            raise OSError('Injected failure before run.start commit')
        def commit(tx):
            plan = tx.get('plan', 'plan')
            tx.put('plan', 'plan', {**plan, 'state': 'started', 'started_run_id': 'run'}, plan['revision'])
            return tx.put('run', 'run', {'plan_id': 'plan', 'project_id': 'project', 'execution_state': 'running'})
        result = await env.store.command('fixture.run.start', key, payload, commit)
        if failure == 'after_commit' and calls.starts == 1:
            raise OSError('Injected lost acknowledgement after run.start commit')
        return result

    async def seed(*_):
        pass

    env.service.workflow = SimpleNamespace(create_project=create_project, create_plan=create_plan, start_run=start_run)
    env.service._seed_starter = seed
    await ready_local(env)
    return calls


async def test_product_identity_and_idempotency_survive_changed_global_model_bindings(product_api, tmp_path):
    env = product_api
    first, request = await submit(env, tmp_path)
    assert first['id'] == str(uuid5(NAMESPACE_URL, 'agentflow:product:product'))
    assert first['id'] not in first['model_bindings'].values()
    await configure(env, key='new-default', model='different-model')
    await env.service._change(first['id'], state='blocked', blocking_reasons=['fixture failure'])
    replay = await env.service.submit(request, 'product')
    assert replay['id'] == first['id'] and replay['state'] == 'blocked'
    assert replay['model_bindings'] == first['model_bindings']
    assert len(await env.store.list('product')) == 1
    with pytest.raises(DomainError, match='不同的产品请求'):
        await env.service.submit(request.model_copy(update={'goal': 'An entirely different product goal'}), 'product')
    second = await env.service.submit(request.model_copy(update={'output_directory': str(tmp_path / 'second')}), 'second')
    assert second['id'] != first['id'] and second['model_bindings'] != first['model_bindings']


async def test_local_setup_status_normalizes_state_and_explanation(product_api):
    async def status(**_):
        return {'state': 'not_prepared', 'message': 'Local setup has not run'}
    product_api.service.local.status = status
    product_api.service.local.prepare = status
    assert (await product_api.service.setup_status())['local_execution'] == {
        'state': 'unprepared', 'message': 'Local setup has not run', 'detail': 'Local setup has not run'}
    assert (await product_api.service.prepare_local())['state'] == 'unprepared'


async def test_output_filled_after_submission_is_preserved_and_preparation_blocks(product_api, tmp_path):
    env = product_api
    product, _ = await submit(env, tmp_path)
    output = tmp_path / 'product'
    output.mkdir()
    (output / 'user.txt').write_text('Keep this user-owned content')
    await ready_local(env)
    await env.service._prepare_product(product['id'])
    assert (await env.service.detail(product['id']))['state'] == 'blocked'
    assert (output / 'user.txt').read_text() == 'Keep this user-owned content'
    assert not (output / '.agentflow-product.json').exists()
    assert not await env.store.list('project')
    with pytest.raises(DomainError, match='输出目录被占用'):
        await env.service.retry_prepare(product['id'], 'retry')


@pytest.mark.parametrize('reason', ['running', 'has_run', 'active', 'restored', 'symlink', 'marker'])
async def test_preparation_retry_requires_known_unstarted_owned_output(product_api, tmp_path, reason):
    env = product_api
    product, _ = await submit(env, tmp_path)
    changes = {'state': 'blocked'}
    if reason == 'running':
        changes['state'] = 'running'
    elif reason == 'has_run':
        changes['run_id'] = 'existing-run'
    elif reason == 'restored':
        changes['restore_reconciliation_required'] = True
    elif reason == 'symlink':
        target = tmp_path / 'other'
        target.mkdir()
        (tmp_path / 'product').symlink_to(target, target_is_directory=True)
    elif reason == 'marker':
        (tmp_path / 'product').mkdir()
        (tmp_path / 'product/.agentflow-product.json').write_text(json.dumps({'product_id': 'other'}))
    await env.service._change(product['id'], **changes)
    task = None
    if reason == 'active':
        task = asyncio.create_task(asyncio.Event().wait())
        env.service._tasks[product['id']] = task
    try:
        with pytest.raises(DomainError):
            await env.service.retry_prepare(product['id'], 'retry')
        assert not await env.store.list('run')
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('recovery', ['retry', 'reconcile'])
async def test_lost_run_start_ack_reattaches_original_run_without_second_start(product_api, tmp_path, recovery):
    env = product_api
    product, _ = await submit(env, tmp_path)
    calls = await protocol_workflow(env, failure='after_commit')
    await env.service._prepare_product(product['id'])
    blocked = await env.service.detail(product['id'])
    assert blocked['state'] == 'blocked' and not blocked['run_id']
    assert len(await env.store.list('run')) == 1
    result = (await env.service.retry_prepare(product['id'], 'retry') if recovery == 'retry'
              else await env.service._recover_prepared_run(product['id']))
    assert result['run_id'] == 'run' and result['state'] == 'running'
    assert calls.starts == 1 and len(await env.store.list('run')) == 1
    await env.service._prepare_product(product['id'])
    assert calls.starts == 1


async def test_before_commit_preparation_failure_can_be_retried_once(product_api, tmp_path):
    env = product_api
    product, _ = await submit(env, tmp_path)
    calls = await protocol_workflow(env, failure='before_commit')
    await env.service._prepare_product(product['id'])
    assert not await env.store.list('run')
    response = await env.client.post(f"/api/v1/products/{product['id']}/retry", json={},
        headers={**env.headers, 'Idempotency-Key': 'retry'})
    assert response.status_code == 202 and response.json()['state'] == 'preparing'
    await env.service._prepare_product(product['id'])
    assert (await env.service.detail(product['id']))['run_id'] == 'run'
    assert calls.starts == 2 and len(await env.store.list('run')) == 1
    replay = await env.client.post(f"/api/v1/products/{product['id']}/retry", json={},
        headers={**env.headers, 'Idempotency-Key': 'retry'})
    assert replay.status_code == 202 and calls.starts == 2


async def test_restored_product_never_recovers_or_advances_automatically(product_api, tmp_path):
    env = product_api
    product, _ = await submit(env, tmp_path)
    calls = await protocol_workflow(env, failure='after_commit')
    await env.service._prepare_product(product['id'])
    product = await env.service._change(product['id'], restore_reconciliation_required=True)
    assert await env.service._recover_prepared_run(product['id']) is None
    await env.service._prepare_product(product['id'])
    await env.service._advance(product)
    assert (await env.service.detail(product['id']))['state'] == 'blocked'
    assert calls.starts == 1


async def test_export_failure_stays_blocked_until_explicit_retry_without_starting_another_run(product_api, tmp_path):
    """Finalization coordination fixture; never substitutes for real export E2E."""
    env = product_api
    product, _ = await submit(env, tmp_path)
    calls = await protocol_workflow(env)
    await env.service._prepare_product(product['id'])
    def delivered(tx):
        run = tx.get('run', 'run')
        tx.put('run', 'run', {**run, 'execution_state': 'completed', 'quality_result': 'passed',
                             'delivery_ids': ['delivery']}, run['revision'])
        return tx.put('delivery', 'delivery', {'confirmed_at': 'fixture-confirmed', 'run_id': 'run'})
    delivery = await env.store.command('fixture.delivered', 'confirmed', {}, delivered)
    async def run_detail(identity):
        return await env.store.read('run', identity)
    env.service.workflow.run_detail = run_detail
    attempts = []
    async def export(current_product, current_delivery):
        attempts.append((current_product['id'], current_delivery['id']))
        if len(attempts) == 1:
            raise ValueError('Injected export failure after confirmed Git delivery')
        return {'source_commit': 'protocol-fixture', 'path': str(tmp_path / 'product/release')}
    env.service.exporter = SimpleNamespace(export=export)
    await env.service._advance(await env.service.detail(product['id']))
    blocked = await env.service.detail(product['id'])
    assert blocked['state'] == 'blocked' and blocked['phase'] == 'export' and blocked['finalization_error']
    assert blocked['run_id'] == 'run' and len(attempts) == 1
    await env.service.start()
    try:
        await asyncio.sleep(.6)
        assert len(attempts) == 1, 'Export failure must not enter an automatic hot retry loop'
    finally:
        await env.service.close()
    retried = await env.client.post(f"/api/v1/products/{product['id']}/retry", json={},
        headers={**env.headers, 'Idempotency-Key': 'retry-export'})
    assert retried.status_code == 202, retried.text
    assert retried.json()['state'] == 'running' and not retried.json()['finalization_error']
    assert retried.json()['run_id'] == 'run'
    await env.service._advance(retried.json())
    result = await env.service.detail(product['id'])
    assert result['state'] == 'completed' and len(attempts) == 2
    assert calls.starts == 1 and len(await env.store.list('run')) == 1
    assert await env.store.read('delivery', 'delivery') == delivery
    assert not await env.store.list('model_invocation')

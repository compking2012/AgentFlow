"""Configurable test-driven source repair retains all original authority and accounting."""
import tomllib

import pytest
from pydantic import ValidationError
from test_execution_pipeline import fixture
from test_execution_settings import execution_config as execution_config
from test_execution_settings import request
from test_product_test_runtime_repair import failed_candidate, patch

from agentflow.common import DomainError
from agentflow.control.product_repair import ProductTestRepair
from agentflow.models.budget import account_id
from agentflow.settings import Settings


async def failing_product(env, previous_count=2):
    await failed_candidate(env, environment_error=False)
    await patch(env, 'run', 'run', execution_state='running')
    for index in range(previous_count):
        await patch(env, 'product_test_repair', 'old-repair-' + str(index), product_id='product', run_id='run',
                    candidate_id='old-candidate-' + str(index), ordinal=index + 1)
    return {'id': 'product', 'run_id': 'run'}


async def test_existing_coordinator_reads_changed_limit_after_two_repairs_without_reset(tmp_path):
    async with fixture(tmp_path, app_targets=('api',)) as env:
        product = await failing_product(env)
        env.workflow.settings = env.settings.model_copy(update={'auto_test_repair_limit': 2})
        coordinator = ProductTestRepair(env.store, env.workflow, nodes=env.nodes)
        previous = await env.store.list('product_test_repair')
        budgets = await env.store.list('budget_account')
        code = await env.store.read('work_item', 'code-work')
        assert (await coordinator.attempt(product))['scheduled'] is False
        env.workflow.settings = env.settings.model_copy(update={'auto_test_repair_limit': -1})
        result = await coordinator.attempt(product)
        assert result['scheduled'] is True
        repair = await env.store.read('product_test_repair', result['repair_id'])
        assert repair['ordinal'] == 3
        assert [await env.store.read('product_test_repair', row['id']) for row in previous] == previous
        assert await env.store.list('budget_account') == budgets
        assert await env.store.read('work_item', 'code-work') == code
        assert (await env.store.read('work_item', result['repair_id']))['payload']['product_frozen_repair'] is True


@pytest.mark.parametrize('limit,count,scheduled', [(0, 0, False), (1, 0, True), (1, 1, False),
    (2, 2, False), (3, 2, True), (3, 3, False), (-1, 2, True), (-1, 20, True)])
async def test_disabled_positive_and_continuous_test_repair_limits(tmp_path, limit, count, scheduled):
    async with fixture(tmp_path, app_targets=('api',)) as env:
        product = await failing_product(env, count)
        env.workflow.settings = env.settings.model_copy(update={'auto_test_repair_limit': limit})
        result = await ProductTestRepair(env.store, env.workflow, nodes=env.nodes).attempt(product)
        assert result['scheduled'] is scheduled
        assert len(await env.store.list('product_test_repair')) == count + int(scheduled)


@pytest.mark.parametrize('explicit,configured,count,scheduled', [(0, -1, 0, False), (3, 0, 2, True),
    (-1, 0, 2, True), (2, -1, 2, False)])
async def test_explicit_constructor_override_remains_authoritative(tmp_path, explicit, configured, count, scheduled):
    async with fixture(tmp_path, app_targets=('api',)) as env:
        product = await failing_product(env, count)
        env.workflow.settings = env.settings.model_copy(update={'auto_test_repair_limit': configured})
        result = await ProductTestRepair(env.store, env.workflow, nodes=env.nodes, limit=explicit).attempt(product)
        assert result['scheduled'] is scheduled


@pytest.mark.parametrize('stop', ['active', 'approval', 'unknown_model', 'run_budget', 'iteration_budget', 'scope'])
async def test_continuous_test_repair_cannot_bypass_existing_gates(tmp_path, stop):
    async with fixture(tmp_path, app_targets=('api',)) as env:
        product = await failing_product(env)
        env.workflow.settings = env.settings.model_copy(update={'auto_test_repair_limit': -1})
        if stop in {'active', 'approval'}:
            await patch(env, 'work_item', 'unit-work', status='running' if stop == 'active' else 'waiting_approval')
        elif stop == 'unknown_model':
            await patch(env, 'model_invocation', 'unknown', run_id='run', state='uncertain')
        elif stop in {'run_budget', 'iteration_budget'}:
            kind = stop.removesuffix('_budget')
            await patch(env, 'budget_account', account_id(kind, kind), request_count=20)
        else:
            await patch(env, 'plan', 'plan', authorized_rework_steps=[])
        before = {kind: await env.store.list(kind) for kind in ('budget_account', 'work_item', 'product_test_repair', 'model_invocation')}
        assert (await ProductTestRepair(env.store, env.workflow, nodes=env.nodes).attempt(product))['scheduled'] is False
        assert {kind: await env.store.list(kind) for kind in before} == before


@pytest.mark.parametrize('limit', [-1, 0, 3, 12, 100])
async def test_test_repair_setting_roundtrips_in_execution_api_and_fixed_file(execution_config, limit):
    env = execution_config
    fields = {row['key']: row for row in env.configuration.execution_settings()['fields']}
    assert 'app.auto_test_repair_limit' in fields
    field = fields['app.auto_test_repair_limit']
    assert field['minimum'] == -1 and field['default_value'] == 100
    before = tomllib.loads(env.path.read_text())
    result = await env.configuration.update_execution_settings(request(env.configuration, **{'app.auto_test_repair_limit': limit}),
                                                               'save-test-policy', env.store)
    after = tomllib.loads(env.path.read_text())
    assert after['app']['auto_test_repair_limit'] == limit
    assert after['models'] == before['models'] and after['product'] == before['product']
    assert result['loaded_values']['app.auto_test_repair_limit'] == 100
    assert result['saved_values']['app.auto_test_repair_limit'] == limit
    assert env.configuration.reload().settings.auto_test_repair_limit == limit
    assert not await env.store.list('product_test_repair') and not await env.store.list('model_invocation')


@pytest.mark.parametrize('value', [-2, True, 1.5, '3'])
async def test_test_repair_limit_rejects_noninteger_and_invalid_negative_configuration(execution_config, value):
    with pytest.raises(ValidationError):
        Settings(auto_test_repair_limit=value)
    with pytest.raises((DomainError, ValidationError)):
        await execution_config.configuration.update_execution_settings(
            request(execution_config.configuration, **{'app.auto_test_repair_limit': value}), 'invalid-test-limit', execution_config.store)


async def test_owner_api_persists_test_repair_limit_without_reauthorizing_work(execution_config):
    from types import SimpleNamespace

    import httpx

    from agentflow.control.api import create_app
    from agentflow.control.product_routes import product_router
    env = execution_config
    app = create_app(env.configuration.settings, store=env.store)
    app.include_router(product_router(SimpleNamespace(configuration=env.configuration, store=env.store)))
    owner = app.state.tokens.exchange(app.state.tokens.bootstrap_code)
    path = '/api/v1/settings/execution'
    payload = request(env.configuration, **{'app.auto_test_repair_limit': -1})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=env.configuration.settings.origin) as client:
        headers = {'Origin': env.configuration.settings.origin, 'Idempotency-Key': 'test-policy'}
        assert (await client.post(path, headers=headers, json=payload)).status_code == 401
        headers['Authorization'] = 'Bearer ' + owner
        response = await client.post(path, headers=headers, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()['saved_values']['app.auto_test_repair_limit'] == -1
        assert response.json()['loaded_values']['app.auto_test_repair_limit'] == 100
        assert response.json()['restart_required'] is True
        assert (await client.post(path, headers=headers, json=payload)).json() == response.json()
    assert tomllib.loads(env.path.read_text())['app']['auto_test_repair_limit'] == -1
    assert not await env.store.list('product_test_repair') and not await env.store.list('model_invocation')


def test_generated_fixed_configuration_uses_one_hundred_round_default():
    from agentflow.configuration import _template
    assert tomllib.loads(_template())['app']['auto_test_repair_limit'] == 100

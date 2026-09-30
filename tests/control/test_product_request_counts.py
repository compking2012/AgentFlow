"""Strict zero-as-unlimited product inputs; no agents or paid requests execute."""
import pytest
import test_product_language
import test_product_lifecycle
import test_products
from pydantic import ValidationError

from agentflow.common import DomainError
from agentflow.configuration import ProductDefaults, load_configuration
from agentflow.control.product_models import ProductRequest

product_api = test_products.product_api


@pytest.mark.parametrize('limit', [0, 1, 9, 10, 200, 2000])
def test_request_count_accepts_only_nonnegative_integer_limits(limit):
    request = ProductRequest(name='Product', goal='Build a persistent reading list.', max_model_requests=limit)
    assert request.max_model_requests == ProductDefaults(max_model_requests=limit).max_model_requests == limit
    assert type(request.max_model_requests) is int


@pytest.mark.parametrize('value', [-1, 2001, True, False, 0.0, 1.0, 0.5, '0', '1', None, [], {}])
def test_request_count_never_coerces_invalid_values_to_unlimited(value):
    with pytest.raises(ValidationError):
        ProductRequest(name='Product', goal='Build a persistent reading list.', max_model_requests=value)
    with pytest.raises(ValidationError):
        ProductDefaults(max_model_requests=value)


@pytest.mark.parametrize('value', ['-1', '2001', 'true', 'false', '0.0', '0.5', '"0"'])
def test_toml_rejects_invalid_unlimited_spellings(tmp_path, monkeypatch, value):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    configuration = load_configuration(create=True)
    configuration.config_path.write_text('[product]\nmax_model_requests = ' + value + '\n')
    with pytest.raises(DomainError, match='product.max_model_requests'):
        configuration.reload()


def test_toml_default_remains_200_and_zero_does_not_change_other_limits(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    configuration = load_configuration(create=True)
    assert configuration.product.max_model_requests == ProductRequest(name='P', goal='A sufficiently clear goal').max_model_requests == 200
    configuration.config_path.write_text(configuration.config_path.read_text().replace('max_model_requests = 200', 'max_model_requests = 0'))
    unlimited = configuration.reload()
    assert unlimited.product.max_model_requests == 0
    assert unlimited.product.max_tool_calls == configuration.product.max_tool_calls == 100
    assert unlimited.product.max_active_seconds == configuration.product.max_active_seconds == 1800
    assert unlimited.models.roles.max_output_tokens == configuration.models.roles.max_output_tokens == 8192


async def test_new_product_inherits_zero_and_freezes_it_after_config_default_changes(product_api, tmp_path, monkeypatch):
    env = product_api
    config = test_products.attach_file_configuration(env, monkeypatch, tmp_path,
        'max_model_requests = 0\nmax_tool_calls = 13\nmax_active_seconds = 777\n')
    await test_products.configure(env)
    body = {'name': 'Unlimited API', 'goal': 'Create a persistent and editable reading list API.',
            'targets': ['api'], 'output_directory': str(tmp_path / 'release')}
    headers = {**env.headers, 'Idempotency-Key': 'unlimited-product'}
    response = await env.client.post('/api/v1/products', json=body, headers=headers)
    assert response.status_code == 202, response.text
    product = response.json()
    assert product['max_model_requests'] == 0
    config.config_path.write_text(config.config_path.read_text().replace('max_model_requests = 0', 'max_model_requests = 7'))
    env.service.configuration = config.reload()
    replay = await env.client.post('/api/v1/products', json=body, headers=headers)
    assert replay.json()['max_model_requests'] == 0 and replay.json()['id'] == product['id']
    explicit = await env.client.post('/api/v1/products', json={**body, 'name': 'Explicit zero',
        'output_directory': str(tmp_path / 'explicit'), 'max_model_requests': 0},
        headers={**env.headers, 'Idempotency-Key': 'explicit-zero'})
    assert explicit.status_code == 202 and explicit.json()['max_model_requests'] == 0
    limited = await env.client.post('/api/v1/products', json={**body, 'name': 'Finite default',
        'output_directory': str(tmp_path / 'finite')}, headers={**env.headers, 'Idempotency-Key': 'finite'})
    assert limited.status_code == 202 and limited.json()['max_model_requests'] == 7
    test_product_language.planning_target(env)
    await env.service._prepare_product(product['id'])
    saved = await env.store.read('product', product['id'])
    assert saved['state'] == 'running', saved
    plan = await env.store.read('plan', saved['plan_id'])
    assert plan['budget_limit']['max_model_requests'] == 0
    assert plan['budget_limit']['max_tool_calls'] == 13 and plan['budget_limit']['max_active_seconds'] == 777
    assert not await env.store.list('attempt') and not await env.store.list('model_invocation')


@pytest.mark.parametrize('keep_configuration', [True, False])
async def test_import_and_requirement_keep_zero_from_configuration_or_product(product_api, tmp_path, monkeypatch, keep_configuration):
    env = product_api
    test_products.attach_file_configuration(env, monkeypatch, tmp_path, 'max_model_requests = 0\n')
    source = await test_product_lifecycle.repository(env, tmp_path / 'existing')
    original = test_product_lifecycle.source_bytes(source)
    product = await test_product_lifecycle.import_product(env, source, tmp_path / 'release')
    assert product['max_model_requests'] == 0 and product['state'] == 'registered'
    if not keep_configuration:
        env.service.configuration = None
    await test_products.configure(env)
    response = await env.client.post(f"/api/v1/products/{product['id']}/changes", json={
        'description': 'Add filters while preserving every saved book.', 'expected_revision': product['revision']},
        headers={**env.headers, 'Idempotency-Key': 'unlimited-change'})
    assert response.status_code == 202, response.text
    change = response.json()
    assert change['max_model_requests'] == 0
    test_product_language.planning_target(env)
    await env.service.lifecycle.prepare_change(change['id'])
    saved = await env.store.read('product_change', change['id'])
    assert saved['state'] == 'running', saved
    plan = await env.store.read('plan', saved['plan_id'])
    assert plan['budget_limit']['max_model_requests'] == 0
    assert test_product_lifecycle.source_bytes(source) == original
    assert not await env.store.list('attempt') and not await env.store.list('model_invocation')


@pytest.mark.parametrize('limit', [1, 9, 2000])
async def test_positive_limits_are_accepted_by_real_product_api(product_api, tmp_path, limit):
    env = product_api
    await test_products.configure(env)
    response = await env.client.post('/api/v1/products', json={'name': 'Finite product',
        'goal': 'Build a persistent reading list.', 'max_model_requests': limit,
        'output_directory': str(tmp_path / 'product')}, headers={**env.headers, 'Idempotency-Key': 'finite'})
    assert response.status_code == 202, response.text
    assert response.json()['max_model_requests'] == limit


@pytest.mark.parametrize('value', [-1, 2001, True, False, 0.0, 1.0, 0.5, '0', '200'])
async def test_real_product_api_rejects_invalid_count_before_any_work(product_api, value):
    response = await product_api.client.post('/api/v1/products', json={'name': 'Invalid product',
        'goal': 'Build a persistent reading list.', 'max_model_requests': value},
        headers={**product_api.headers, 'Idempotency-Key': 'invalid'})
    assert response.status_code == 422, response.text
    assert not await product_api.store.list('product') and not await product_api.store.list('model_invocation')

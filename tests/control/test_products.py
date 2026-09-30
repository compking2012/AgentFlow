"""Owner product input/configuration contracts; execution is covered by the full E2E suite."""
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from agentflow.control.api import create_app
from agentflow.control.product_routes import product_router
from agentflow.control.products import ProductService
from agentflow.execution.service import NodeService
from agentflow.models.secrets import LocalSecretStore
from agentflow.models.service import ModelService
from agentflow.settings import Settings
from agentflow.storage import LocalArtifactStore, Store


@pytest_asyncio.fixture
async def product_api(tmp_path):
    settings = Settings(data_dir=tmp_path / 'control')
    store = Store(settings.data_dir)
    await store.start()
    secrets = LocalSecretStore(settings.data_dir)
    async def authorize(*_):
        raise AssertionError('Configuration/submit tests must not dispatch a model call')
    models = ModelService(store, settings.data_dir, authorize, lambda p: secrets.read(p.credential_reference))
    nodes = NodeService(store, settings.data_dir, 'https://127.0.0.1:9443')
    async def status():
        return {'state': 'unprepared', 'target_configs': []}
    async def probe():
        return [{'backend': 'fixture-inventory', 'available': True}]
    local = SimpleNamespace(nodes=nodes, status=status)
    runtime = SimpleNamespace(probe=probe)
    app = create_app(settings, store=store, artifacts=LocalArtifactStore(settings.data_dir / 'artifacts'), models=models)
    service = ProductService(store, app.state.workflow, models, runtime, local, secrets, settings)
    app.include_router(product_router(service))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.origin) as client:
        response = await client.post('/api/v1/session', json={'bootstrap_token': app.state.tokens.bootstrap_code},
            headers={'Origin': settings.origin, 'Idempotency-Key': 'session'})
        headers = {'Authorization': 'Bearer ' + response.json()['owner_token'], 'Origin': settings.origin}
        yield SimpleNamespace(client=client, headers=headers, store=store, service=service, secrets=secrets, settings=settings)
    await models.close()
    await store.close()


async def configure(env, key='model', **changes):
    return await env.client.post('/api/v1/product_setup/models', json={'role': 'both', 'provider': 'openai_compatible',
        'base_url': 'https://models.example.invalid/v1', 'model': 'explicit-owner-model',
        'api_key': 'secret-fixture-value-never-persisted-to-db', **changes},
        headers={**env.headers, 'Idempotency-Key': key})


async def test_configuration_keeps_credentials_private_and_binds_both_model_roles(product_api):
    env = product_api
    before = (await env.client.get('/api/v1/product_setup', headers=env.headers)).json()
    assert not before['models_ready'] and not before['ready']
    response = await configure(env)
    assert response.status_code == 200, response.text
    assert (await configure(env)).json() == response.json()
    after = (await env.client.get('/api/v1/product_setup', headers=env.headers)).json()
    assert after['models_ready'] and not after['ready']
    assert after['model_bindings']['role_model_profile_id'] == after['model_bindings']['coding_model_profile_id']
    assert 'secret-fixture' not in json.dumps(after) + response.text
    assert env.secrets.read('local:' + response.json()['profile_id']).startswith('secret-fixture')
    for path in (env.settings.data_dir / 'state').glob('*'):
        if path.is_file():
            assert b'secret-fixture-value' not in path.read_bytes()
    assert await env.store.list('model_invocation') == []


@pytest.mark.parametrize('role', ['roles', 'coding', 'both'])
async def test_model_settings_api_accepts_output_above_the_former_platform_ceiling(product_api, role):
    env = product_api
    response = await configure(env, role=role, max_output_tokens=393216)
    assert response.status_code == 200, response.text
    profile = await env.store.read('model_profile', response.json()['profile_id'])
    assert profile['max_output_tokens'] == 393216
    setup = (await env.client.get('/api/v1/product_setup', headers=env.headers)).json()
    assert any(p['max_output_tokens'] == 393216 for p in setup['profiles'])
    assert await env.store.list('model_invocation') == []


async def test_goal_submission_is_idempotent_and_never_overwrites_existing_output(product_api, tmp_path):
    env = product_api
    await configure(env)
    payload = {'name': 'Reading list', 'goal': 'Build a reading list with persistent books and read status.',
               'target': 'web', 'output_directory': str(tmp_path / 'product'), 'review_mode': 'auto'}
    first = await env.client.post('/api/v1/products', json=payload, headers={**env.headers, 'Idempotency-Key': 'same'})
    replay = await env.client.post('/api/v1/products', json=payload, headers={**env.headers, 'Idempotency-Key': 'same'})
    assert first.status_code == 202, first.text
    assert first.json() == replay.json() and len(await env.store.list('product')) == 1
    assert first.json()['state'] == 'preparing' and first.json()['run_id'] is None
    occupied = tmp_path / 'occupied'
    occupied.mkdir()
    (occupied / 'user.txt').write_text('preserve this file')
    response = await env.client.post('/api/v1/products', json={**payload, 'output_directory': str(occupied)},
        headers={**env.headers, 'Idempotency-Key': 'occupied'})
    assert response.status_code == 409
    assert (occupied / 'user.txt').read_text() == 'preserve this file'
    changed = await env.client.post('/api/v1/products', json={**payload, 'goal': 'A different product goal'},
        headers={**env.headers, 'Idempotency-Key': 'same'})
    assert changed.status_code == 409


async def test_missing_model_and_delivery_evidence_cannot_look_usable(product_api, tmp_path):
    env = product_api
    payload = {'name': 'Empty product', 'goal': 'Build a product with real behavior', 'output_directory': str(tmp_path / 'out')}
    response = await env.client.post('/api/v1/products', json=payload, headers={**env.headers, 'Idempotency-Key': 'missing'})
    assert response.status_code == 409 and response.json()['error']['code'] == 'model_setup_required'
    assert not (tmp_path / 'out').exists()
    await configure(env)
    response = await env.client.post('/api/v1/products', json=payload, headers={**env.headers, 'Idempotency-Key': 'prepared'})
    identity = response.json()['id']
    download = await env.client.get('/api/v1/products/' + identity + '/download', headers=env.headers)
    assert download.status_code == 409 and not response.json().get('delivery')
    anonymous = await env.client.get('/api/v1/products/' + identity)
    assert anonymous.status_code == 401


@pytest.mark.parametrize('change', [{'base_url': 'http://example.com'}, {'base_url': 'https://user:key@example.com'},
                                   {'api_key': None}, {'api_key': 'bad\nkey'}, {'api_key': '   '}, {'role': 'operator'}])
async def test_unsafe_or_incomplete_model_setup_is_rejected(product_api, change):
    response = await configure(product_api, **change)
    assert response.status_code == 422
    assert not await product_api.store.list('model_profile')


async def test_blank_product_goal_is_rejected_before_any_model_or_filesystem_work(product_api):
    response = await product_api.client.post('/api/v1/products', json={'name': '  ', 'goal': '        '},
        headers={**product_api.headers, 'Idempotency-Key': 'blank'})
    assert response.status_code == 422
    assert not await product_api.store.list('product')
    assert not await product_api.store.list('model_invocation')


def attach_file_configuration(env, monkeypatch, tmp_path, product_defaults=''):
    from agentflow.configuration import load_configuration
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    config = load_configuration(create=True)
    config.config_path.write_text('[app]\ndata_dir = ' + json.dumps(str(env.settings.data_dir)) +
        '\n[product]\n' + product_defaults)
    env.service.configuration = config.reload()
    return env.service.configuration


async def test_fixed_file_drives_api_defaults_and_freezes_limits(product_api, monkeypatch, tmp_path):
    env = product_api
    config = attach_file_configuration(env, monkeypatch, tmp_path,
        'target = "api"\nreview_mode = "milestones"\nmax_model_requests = 77\nmax_tool_calls = 11\n'
        'max_active_seconds = 1200\noutput_root = ' + json.dumps(str(tmp_path / 'deliveries')) + '\n')
    configured = await configure(env)
    assert configured.status_code == 200, configured.text
    assert config.reload().models.roles.enabled and config.reload().models.coding.enabled
    setup = (await env.client.get('/api/v1/product_setup', headers=env.headers)).json()
    assert setup['configuration_path'] == str(config.config_path)
    assert setup['product_defaults']['target'] == 'api' and not setup['restart_required']
    payload = {'name': 'Saved API', 'goal': 'Create persistent editable reading list API.'}
    response = await env.client.post('/api/v1/products', json=payload,
        headers={**env.headers, 'Idempotency-Key': 'file-default'})
    assert response.status_code == 202, response.text
    product = response.json()
    assert product['target'] == 'api' and product['review_mode'] == 'milestones'
    assert product['max_model_requests'] == 77 and product['max_tool_calls'] == 11
    assert product['max_active_seconds'] == 1200
    assert product['output_directory'].startswith(str(tmp_path / 'deliveries') + '/')
    config.config_path.write_text(config.config_path.read_text().replace('max_tool_calls = 11', 'max_tool_calls = 22'))
    status = (await env.client.get('/api/v1/product_setup', headers=env.headers)).json()
    assert status['restart_required'] and not status['ready']
    replay = await env.client.post('/api/v1/products', json=payload,
        headers={**env.headers, 'Idempotency-Key': 'file-default'})
    assert replay.json() == product, 'Lost ACK recovery must work even after file changes'
    fresh = await env.client.post('/api/v1/products', json={**payload, 'name': 'Another API'},
        headers={**env.headers, 'Idempotency-Key': 'fresh'})
    assert fresh.status_code == 409 and fresh.json()['error']['code'] == 'configuration_restart_required'
    assert len(await env.store.list('product')) == 1
    assert (await env.store.read('product', product['id']))['max_tool_calls'] == 11


async def test_submit_rejects_route_edit_during_setup_probe(product_api, monkeypatch, tmp_path):
    env = product_api
    config = attach_file_configuration(env, monkeypatch, tmp_path)
    configured = await configure(env)
    assert configured.status_code == 200, configured.text
    assert not config.restart_required()
    probes = []

    async def edit_route_during_probe():
        probes.append('setup')
        assert not await env.store.list('product')
        text = config.config_path.read_text()
        assert 'explicit-owner-model' in text
        config.config_path.write_text(text.replace('explicit-owner-model', 'edited-owner-model'))
        return [{'backend': 'fixture-inventory', 'available': True}]

    monkeypatch.setattr(env.service.runtime, 'probe', edit_route_during_probe)
    env.service._runtime_cache = None
    output = tmp_path / 'rejected-product'
    response = await env.client.post('/api/v1/products', json={
        'name': 'Route edit during submit', 'goal': 'Build a persistent reading list API.',
        'output_directory': str(output)}, headers={**env.headers, 'Idempotency-Key': 'route-edit-during-setup'})

    assert probes == ['setup']
    assert response.status_code == 409 and response.json()['error']['code'] == 'configuration_restart_required'
    assert config.models.coding.model == 'explicit-owner-model'
    assert config.reload().models.coding.model == 'edited-owner-model'
    assert not output.exists()
    assert not await env.store.list('product')
    assert not await env.store.list('run')
    assert not await env.store.list('model_invocation')


async def test_explicit_product_choices_override_file_defaults(product_api, monkeypatch, tmp_path):
    env = product_api
    attach_file_configuration(env, monkeypatch, tmp_path, 'target = "api"\nreview_mode = "milestones"\n')
    await configure(env)
    response = await env.client.post('/api/v1/products', json={'name': 'Explicit Web',
        'goal': 'Build a browser based reading list application.', 'target': 'web', 'review_mode': 'auto'},
        headers={**env.headers, 'Idempotency-Key': 'override'})
    assert response.status_code == 202, response.text
    assert response.json()['target'] == 'web' and response.json()['review_mode'] == 'auto'


async def test_config_directory_cannot_be_a_product_output(product_api, monkeypatch, tmp_path):
    env = product_api
    config = attach_file_configuration(env, monkeypatch, tmp_path)
    await configure(env)
    response = await env.client.post('/api/v1/products', json={'name': 'Private',
        'goal': 'Build an application in a safe location.', 'output_directory': str(config.config_path.parent / 'product')},
        headers={**env.headers, 'Idempotency-Key': 'private'})
    assert response.status_code == 422 and response.json()['error']['code'] == 'protected_output_directory'
    assert not await env.store.list('product')


async def test_empty_file_model_sections_do_not_silently_use_old_database_defaults(product_api, monkeypatch, tmp_path):
    env = product_api
    configured = await configure(env)
    profile_id = configured.json()['profile_id']
    attach_file_configuration(env, monkeypatch, tmp_path)
    setup = (await env.client.get('/api/v1/product_setup', headers=env.headers)).json()
    assert not setup['models_ready']
    assert set(setup['model_bindings'].values()) == {None}
    # Existing workflows may still reference this immutable profile.
    assert (await env.service.models.registry.get(profile_id)).acceptance_status == 'accepted'


async def test_omitted_and_explicit_defaults_are_distinct_submission_intents(product_api, monkeypatch, tmp_path):
    env = product_api
    attach_file_configuration(env, monkeypatch, tmp_path, 'target = "api"\n')
    await configure(env)
    payload = {'name': 'Default API', 'goal': 'Build a saved reading list product.'}
    headers = {**env.headers, 'Idempotency-Key': 'intent'}
    first = await env.client.post('/api/v1/products', json=payload, headers=headers)
    assert first.status_code == 202 and first.json()['target'] == 'api'
    second = await env.client.post('/api/v1/products', json={**payload, 'target': 'web'}, headers=headers)
    assert second.status_code == 409 and second.json()['error']['code'] == 'idempotency_conflict'


async def test_concurrent_idempotent_requests_cannot_merge_distinct_human_review_choices(product_api, monkeypatch, tmp_path):
    import asyncio

    from agentflow.common import DomainError
    from agentflow.control.product_models import ProductRequest
    env = product_api
    attach_file_configuration(env, monkeypatch, tmp_path, 'review_mode = "every_step"\n')
    await configure(env)
    original_read = env.store.read
    both_read = asyncio.Event()
    arrivals = 0
    async def synchronized_read(kind, identity):
        nonlocal arrivals
        value = await original_read(kind, identity)
        if kind == 'product' and value is None and arrivals < 2:
            arrivals += 1
            if arrivals == 2:
                both_read.set()
            await both_read.wait()
        return value
    monkeypatch.setattr(env.store, 'read', synchronized_read)
    payload = {'name': 'Review required', 'goal': 'Build a product respecting configured human review.'}
    results = await asyncio.wait_for(asyncio.gather(
        env.service.submit(ProductRequest(**payload), 'parallel-intent'),
        env.service.submit(ProductRequest(**payload, review_mode='auto'), 'parallel-intent'),
        return_exceptions=True), 5)
    assert sum(isinstance(result, dict) for result in results) == 1
    for result, expected in zip(results, ['every_step', 'auto'], strict=True):
        if isinstance(result, DomainError):
            assert result.code == 'idempotency_conflict'
        else:
            assert result['review_mode'] == expected
    assert len(await env.store.list('product')) == 1

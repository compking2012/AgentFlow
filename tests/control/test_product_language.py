"""Language is a product default and an immutable accepted-iteration setting."""
import asyncio

import pytest
import test_product_lifecycle
import test_products

from agentflow.common import DomainError, canonical_digest
from agentflow.control.product_models import ProductRequest, product_identity
from agentflow.execution.models import TargetConfig, ToolRequirement

product_api = test_products.product_api


async def new_product(env, tmp_path, *, key='new', **values):
    await test_products.configure(env)
    request = {'name': 'Language product', 'goal': 'Build a persistent reading list for personal use.',
               'output_directory': str(tmp_path / key), 'targets': ['api'], **values}
    response = await env.client.post('/api/v1/products', json=request,
                                    headers={**env.headers, 'Idempotency-Key': key})
    assert response.status_code == 202, response.text
    return response.json(), request


async def set_language(env, product, language, key='language'):
    return await env.client.post(f"/api/v1/products/{product['id']}/language",
        json={'language': language, 'expected_revision': product['revision']},
        headers={**env.headers, 'Idempotency-Key': key})


async def idle_product(env, product):
    # A preparation failure with no active work allows future-default edits.
    return await env.service._change(product['id'], state='blocked')


def planning_target(env):
    # Supply target metadata only; no agent, build, or test execution is claimed.
    target = TargetConfig(app_target='api', os_name='Darwin', os_version_constraint='*', cpu_architecture='arm64',
        required_display_protocol='not_required', required_device_mode='not_required',
        build_backend=ToolRequirement(name='npm', version_constraint='>=10'),
        test_backend=ToolRequirement(name='node', version_constraint='>=22.13'))
    async def prepare(**_):
        return {'state': 'ready', 'target_configs': [target.model_dump(mode='json')]}
    env.service.local.prepare = prepare


async def test_product_language_defaults_to_chinese_and_file_default_can_be_overridden(product_api, tmp_path, monkeypatch):
    env = product_api
    config = test_products.attach_file_configuration(env, monkeypatch, tmp_path, 'language = "en"\n')
    english, _ = await new_product(env, tmp_path)
    assert english['language'] == english['initial_language'] == 'en'
    chinese, _ = await new_product(env, tmp_path, key='explicit', language='zh-CN')
    assert chinese['language'] == chinese['initial_language'] == 'zh-CN'
    setup = (await env.client.get('/api/v1/product_setup', headers=env.headers)).json()
    assert setup['product_defaults']['language'] == config.product.language == 'en'
    assert not await env.store.list('model_invocation')


async def test_import_uses_file_language_without_model_configuration(product_api, tmp_path, monkeypatch):
    env = product_api
    test_products.attach_file_configuration(env, monkeypatch, tmp_path, 'language = "en"\n')
    directory = await test_product_lifecycle.repository(env, tmp_path / 'existing')
    original = test_product_lifecycle.source_bytes(directory)
    product = await test_product_lifecycle.import_product(env, directory, tmp_path / 'release')
    assert product['language'] == product['initial_language'] == 'en'
    assert product['state'] == 'registered'
    assert test_product_lifecycle.source_bytes(directory) == original
    assert not await env.store.list('model_invocation') and not await env.store.list('run')


async def test_language_update_is_revision_checked_idempotent_and_does_not_rewrite_other_records(product_api, tmp_path):
    env = product_api
    product, request = await new_product(env, tmp_path)
    product = await idle_product(env, product)
    assert product['language'] == product['initial_language'] == 'zh-CN'
    before = {kind: await env.store.list(kind) for kind in ('run', 'plan', 'artifact', 'work_item', 'product_change')}
    changed = await set_language(env, product, 'en', 'first')
    assert changed.status_code == 200, changed.text
    english = changed.json()
    assert english['language'] == 'en' and english['initial_language'] == 'zh-CN'
    assert english['goal'] == product['goal'] and english['state'] == product['state']
    latest = (await set_language(env, english, 'zh-CN', 'second')).json()
    replay = await set_language(env, product, 'en', 'first')
    assert replay.json() == english
    assert (await env.store.read('product', product['id']))['language'] == 'zh-CN'
    assert (await set_language(env, product, 'en', 'stale')).status_code == 409
    assert (await set_language(env, latest, 'en', 'first')).status_code == 409
    for kind, values in before.items():
        assert await env.store.list(kind) == values
    recovery = await env.client.post('/api/v1/products', json=request,
                                    headers={**env.headers, 'Idempotency-Key': 'new'})
    assert recovery.status_code == 202 and recovery.json()['id'] == product['id']
    assert recovery.json()['initial_language'] == 'zh-CN'


async def test_concurrent_language_updates_require_a_fresh_product_revision(product_api, tmp_path):
    env = product_api
    product, _ = await new_product(env, tmp_path)
    product = await idle_product(env, product)
    replies = await asyncio.gather(set_language(env, product, 'en', 'one'), set_language(env, product, 'en', 'two'))
    assert sorted(reply.status_code for reply in replies) == [200, 409]
    assert (await env.store.read('product', product['id']))['revision'] == product['revision'] + 1


async def test_initial_plan_keeps_the_language_accepted_before_default_was_changed(product_api, tmp_path):
    env = product_api
    product, _ = await new_product(env, tmp_path, language='en')
    product = await idle_product(env, product)
    updated = await set_language(env, product, 'zh-CN')
    assert updated.status_code == 200
    planning_target(env)
    await env.service._prepare_product(product['id'])
    current = await env.store.read('product', product['id'])
    assert current['state'] == 'running', current
    plan = await env.store.read('plan', current['plan_id'])
    assert plan['product_contract']['language'] == 'en'
    assert current['language'] == 'zh-CN' and current['goal'] == product['goal']
    before = {kind: await env.store.list(kind) for kind in ('run', 'plan', 'artifact', 'work_item')}
    assert (await set_language(env, current, 'en', 'during-run')).status_code == 409
    for kind, values in before.items():
        assert await env.store.list(kind) == values
    assert not await env.store.list('attempt') and not await env.store.list('model_invocation')


@pytest.mark.parametrize('kind', ['initial', 'change'])
async def test_legacy_ready_plan_replays_without_changing_its_accepted_request(product_api, tmp_path, monkeypatch, kind):
    env = product_api
    await test_products.configure(env)
    planning_target(env)
    if kind == 'initial':
        product, _ = await new_product(env, tmp_path)
        async def prepare():
            await env.service._prepare_product(product['id'])
    else:
        path = await test_product_lifecycle.repository(env, tmp_path / 'source')
        product = await test_product_lifecycle.import_product(env, path, tmp_path / 'release')
        response = await env.client.post(f"/api/v1/products/{product['id']}/changes", json={
            'description': 'Add saved filters to the reading list.', 'expected_revision': product['revision']},
            headers={**env.headers, 'Idempotency-Key': 'change'})
        change = response.json()
        async def prepare():
            await env.service.lifecycle.prepare_change(change['id'])
    create_plan, start_run = env.service.workflow.create_plan, env.service.workflow.start_run
    async def old_plan(payload, key):
        # Simulate a prior binary saving its real immutable plan and command.
        return await create_plan({**payload, 'product_contract': {
            name: value for name, value in payload['product_contract'].items() if name != 'language'}}, key)
    async def interrupted(*_):
        raise asyncio.CancelledError()
    monkeypatch.setattr(env.service.workflow, 'create_plan', old_plan)
    monkeypatch.setattr(env.service.workflow, 'start_run', interrupted)
    with pytest.raises(asyncio.CancelledError):
        await prepare()
    plans = await env.store.list('plan')
    assert len(plans) == 1 and plans[0]['state'] == 'ready' and 'language' not in plans[0]['product_contract']
    current = await env.store.read('product', product['id'])
    if kind == 'change':
        await env.service.lifecycle._update_change(change['id'], state='blocked')
    current = await idle_product(env, current)
    assert (await set_language(env, current, 'en', 'future-only')).status_code == 200
    monkeypatch.setattr(env.service.workflow, 'create_plan', create_plan)
    monkeypatch.setattr(env.service.workflow, 'start_run', start_run)
    await prepare()
    assert len(await env.store.list('plan')) == 1
    assert len(await env.store.list('run')) == 1
    unchanged = await env.store.read('plan', plans[0]['id'])
    assert unchanged['product_contract'] == plans[0]['product_contract']
    assert (await env.store.read('product', product['id']))['language'] == 'en'
    assert not await env.store.list('attempt') and not await env.store.list('model_invocation')


@pytest.mark.parametrize('legacy', [False, True])
async def test_change_language_is_frozen_for_recovery_and_prd_planning(product_api, tmp_path, legacy):
    env = product_api
    directory = await test_product_lifecycle.repository(env, tmp_path / 'existing')
    initial = 'zh-CN' if legacy else 'en'
    future = 'en' if legacy else 'zh-CN'
    product = await test_product_lifecycle.import_product(env, directory, tmp_path / 'release', language=initial)
    await test_products.configure(env)
    body = {'description': 'Add filtering for completed and unread books.', 'expected_revision': product['revision']}
    endpoint = f"/api/v1/products/{product['id']}/changes"
    headers = {**env.headers, 'Idempotency-Key': 'change'}
    created = await env.client.post(endpoint, json=body, headers=headers)
    assert created.status_code == 202, created.text
    change = created.json()
    assert change['language'] == initial
    if legacy:
        def old_change(tx):
            current = tx.get('product_change', change['id'])
            body = {key: value for key, value in current.items() if key != 'language'}
            return tx.put('product_change', change['id'], body, current['revision'])
        await env.store.command('fixture', 'old-language-less-change', {}, old_change)
    current = await env.store.read('product', product['id'])
    assert (await set_language(env, current, future, 'preparing-denied')).status_code == 409
    await env.service.lifecycle._update_change(change['id'], state='blocked')
    current = await idle_product(env, current)
    assert (await set_language(env, current, future)).status_code == 200
    replay = await env.client.post(endpoint, json=body, headers=headers)
    assert replay.status_code == 202 and replay.json()['id'] == change['id']
    assert replay.json().get('language', 'zh-CN') == initial
    changed = await env.client.post(endpoint, json={**body, 'language': future}, headers=headers)
    assert changed.status_code == 409
    planning_target(env)
    await env.service.lifecycle.prepare_change(change['id'])
    accepted = await env.store.read('product_change', change['id'])
    assert accepted['state'] == 'running', accepted
    plan = await env.store.read('plan', accepted['plan_id'])
    assert plan['product_contract']['language'] == initial
    assert plan['actual_steps'][0] == 'prd'
    assert (await env.store.read('product', product['id']))['language'] == future
    assert not await env.store.list('attempt') and not await env.store.list('model_invocation')


@pytest.mark.parametrize('override', [None, 'zh-CN'])
async def test_updated_default_is_inherited_or_overridden_only_for_the_new_iteration(product_api, tmp_path, override):
    env = product_api
    path = await test_product_lifecycle.repository(env, tmp_path / 'existing')
    product = await test_product_lifecycle.import_product(env, path, tmp_path / 'release')
    updated = await set_language(env, product, 'en')
    assert updated.status_code == 200
    product = updated.json()
    await test_products.configure(env)
    response = await env.client.post(f"/api/v1/products/{product['id']}/changes", json={
        'description': 'Add a filter while preserving existing reading list behavior.',
        **({'language': override} if override else {}),
        'expected_revision': product['revision']}, headers={**env.headers, 'Idempotency-Key': 'english-change'})
    assert response.status_code == 202, response.text
    assert response.json()['language'] == (override or 'en')
    assert (await env.store.read('product', product['id']))['language'] == 'en'


@pytest.mark.parametrize('version', [None, 1, 2])
async def test_old_products_and_submission_fingerprints_remain_recoverable(product_api, tmp_path, version):
    env = product_api
    wire = {'name': 'Older product', 'goal': 'Build a persistent personal reading list.', 'output_directory': str(tmp_path / 'old')}
    request = ProductRequest(**wire)
    legacy = {field: request.model_dump(mode='json')[field] for field in ('name', 'goal', 'output_directory', 'target',
        'review_mode', 'max_model_requests', 'role_model_profile_id', 'coding_model_profile_id')}
    value = {**legacy, 'state': 'blocked', 'blocking_reasons': [], 'run_id': None, 'project_id': None, 'delivery': None}
    if version:
        value['request_fingerprint'] = canonical_digest(legacy) if version == 1 else canonical_digest({'version': 2, 'request': wire})
        if version == 2:
            value['request_fingerprint_version'] = 2
    product = await env.store.command('fixture', 'legacy-product', {},
        lambda tx: tx.put('product', product_identity('old-key'), value))
    detail = await env.service.detail(product['id'])
    assert detail['language'] == 'zh-CN'
    assert (await set_language(env, product, 'en')).status_code == 200
    replay = await env.service.submit(request, 'old-key')
    assert replay['id'] == product['id'] and replay['initial_language'] == 'zh-CN'
    with pytest.raises(DomainError, match='不同的产品'):
        await env.service.submit(request.model_copy(update={'language': 'en'}), 'old-key')
    assert len(await env.store.list('product')) == 1


@pytest.mark.parametrize('language', ['zh', 'EN', 'ja', '', None, ['en']])
async def test_invalid_language_is_rejected_before_product_work(product_api, language):
    env = product_api
    response = await env.client.post('/api/v1/products', json={'name': 'Invalid language',
        'goal': 'Build a persistent reading list.', 'language': language},
        headers={**env.headers, 'Idempotency-Key': 'invalid'})
    assert response.status_code == 422
    changed = await env.client.post('/api/v1/products/unknown/language', json={'language': language, 'expected_revision': 1},
        headers={**env.headers, 'Idempotency-Key': 'invalid-update'})
    assert changed.status_code == 422
    assert not await env.store.list('product') and not await env.store.list('model_invocation')


def test_fixed_toml_language_is_validated_and_defaults_to_chinese(tmp_path, monkeypatch):
    from agentflow.configuration import load_configuration
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    configuration = load_configuration(create=True)
    assert configuration.product.language == 'zh-CN'
    assert 'language = "zh-CN"' in configuration.config_path.read_text()
    configuration.config_path.write_text(configuration.config_path.read_text().replace('language = "zh-CN"', 'language = "en"'))
    assert configuration.reload().product.language == 'en'
    configuration.config_path.write_text(configuration.config_path.read_text().replace('language = "en"', 'language = "invalid"'))
    with pytest.raises(DomainError, match='product.language'):
        configuration.reload()

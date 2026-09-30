"""Private execution settings persist safely without reauthorizing existing work."""
import asyncio
import json
import stat
import tomllib
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

from agentflow.common import DomainError
from agentflow.configuration import load_configuration
from agentflow.configuration_execution import EXECUTION_FIELDS
from agentflow.control.api import create_app
from agentflow.control.product_models import ModelSetupRequest
from agentflow.control.product_routes import product_router
from agentflow.models.profiles import ModelRegistry
from agentflow.models.secrets import LocalSecretStore
from agentflow.storage import Store


@pytest_asyncio.fixture
async def execution_config(tmp_path, monkeypatch):
    path = tmp_path / 'private/config.toml'
    data = tmp_path / 'state'
    path.parent.mkdir(mode=0o700)
    path.write_text('[app]\ndata_dir = ' + json.dumps(str(data)) + '\nmax_body_bytes = 1048576\n'
        '[product]\nlanguage = "zh-CN"\n[models.roles]\n'
        'provider = "openai_compatible"\nbase_url = "https://private-model.example.invalid/v1"\n'
        'model = "owner-selected"\napi_key = "PRIVATE_EXECUTION_SETTINGS_KEY"\n'
        'max_output_tokens = 262144\nreasoning_effort = "high"\n')
    path.chmod(0o600)
    monkeypatch.setattr('agentflow.configuration.configuration_path', lambda: path)
    configuration = load_configuration()
    store = Store(data)
    await store.start()
    env = SimpleNamespace(configuration=configuration, path=path, store=store)
    try:
        yield env
    finally:
        await store.close()


def request(configuration, **values):
    return {'expected_configuration_revision': configuration.execution_settings()['configuration_revision'],
            'values': values or {'product.max_tool_calls': 500}}


async def test_metadata_reads_saved_loaded_defaults_and_model_sources_without_secrets(execution_config):
    env = execution_config
    view = env.configuration.execution_settings()
    assert view['configuration_path'] == str(env.path) and view['configuration_revision'].startswith('sha256:')
    assert not view['restart_required'] and not view['current_runs_changed']
    fields = {row['key']: row for row in view['fields']}
    assert set(fields) == {row[0] for row in EXECUTION_FIELDS}
    assert fields['product.max_tool_calls']['default_value'] == 100
    assert fields['product.max_tool_calls']['source'] == 'application_default'
    assert fields['product.max_active_seconds']['exclusive_minimum'] == 0
    assert fields['app.max_role_iterations']['default_value'] == 1000
    assert fields['app.max_coding_steps']['saved_value'] == 32
    assert fields['product.max_model_requests']['zero_meaning'] == '不限制请求次数'
    assert all(row['effect_scope'] and row['unit'] and row['restart_on_change'] for row in fields.values())
    assert view['model_parameters']['roles'] == {'source': 'models.roles', 'model': 'owner-selected',
        'max_output_tokens': 262144, 'configured': True}
    assert 'PRIVATE_EXECUTION_SETTINGS_KEY' not in json.dumps(view)
    assert 'private-model.example.invalid' not in json.dumps(view)
    assert await env.store.list('configuration_execution_update') == []


async def test_save_preserves_other_toml_fields_credentials_live_settings_and_work_authorization(execution_config):
    env = execution_config
    configuration = env.configuration
    protected = ('run', 'work_item', 'attempt', 'coding_work_budget', 'coding_step_usage', 'budget_account', 'authorization')
    for kind in protected:
        await env.store.command('fixture', kind, {}, lambda tx, kind=kind: tx.put(kind, 'fixture', {'used': 120, 'limit': 100}))
    before_state = {kind: await env.store.list(kind) for kind in protected}
    before_file = tomllib.loads(env.path.read_text())
    payload = request(configuration, **{'product.max_tool_calls': 500, 'app.agent_concurrency': 6,
        'app.max_role_iterations': 1200, 'product.max_model_requests': 0})
    result = await configuration.update_execution_settings(payload, 'save', env.store)
    saved = configuration.reload()
    assert saved.product.max_tool_calls == 500 and saved.settings.agent_concurrency == 6
    assert saved.settings.max_role_iterations == 1200 and saved.product.max_model_requests == 0
    assert configuration.product.max_tool_calls == 100 and configuration.settings.agent_concurrency == 3
    assert result['restart_required'] and not result['current_runs_changed']
    assert result['saved_values']['product.max_tool_calls'] == 500 and result['loaded_values']['product.max_tool_calls'] == 100
    assert result['configuration_revision'] == saved.execution_settings()['configuration_revision']
    assert not saved.execution_settings()['restart_required']
    after_file = tomllib.loads(env.path.read_text())
    assert after_file['models'] == before_file['models']
    assert after_file['app']['max_body_bytes'] == before_file['app']['max_body_bytes']
    assert after_file['product']['language'] == before_file['product']['language']
    assert stat.S_IMODE(env.path.stat().st_mode) == 0o600
    assert {kind: await env.store.list(kind) for kind in protected} == before_state
    assert not await env.store.list('model_invocation') and not await env.store.list('run_recovery')
    for path in (configuration.settings.data_dir / 'state').iterdir():
        if path.is_file():
            assert b'PRIVATE_EXECUTION_SETTINGS_KEY' not in path.read_bytes()


async def test_timeout_retry_policy_is_visible_editable_and_does_not_start_work(execution_config):
    env = execution_config
    view = env.configuration.execution_settings()
    field = next((row for row in view['fields'] if row['key'] == 'app.auto_timeout_retry_limit'), None)
    assert field is not None, 'The automatic timeout policy must be owner-configurable'
    assert field['minimum'] == 0 and field['zero_meaning']
    before = env.path.read_bytes()
    await env.configuration.update_execution_settings(request(env.configuration, **{'app.auto_timeout_retry_limit': 0}),
                                                      'disable-timeout-retry', env.store)
    assert env.configuration.reload().settings.auto_timeout_retry_limit == 0
    assert env.path.read_bytes() != before
    assert not await env.store.list('timeout_recovery')
    assert not await env.store.list('model_invocation')
    with pytest.raises((ValidationError, DomainError)):
        await env.configuration.update_execution_settings(request(env.configuration, **{'app.auto_timeout_retry_limit': -1}),
                                                          'invalid-timeout-retry', env.store)


@pytest.mark.parametrize('values', [
    {}, {'product.max_tool_calls': -1}, {'product.max_tool_calls': True}, {'product.max_tool_calls': '500'},
    {'product.max_tool_calls': 1.5}, {'product.max_active_seconds': 0}, {'app.agent_concurrency': 0},
    {'app.max_coding_steps': 0}, {'app.max_role_iterations': 0}, {'app.auto_failure_run_limit': -1},
    {'models.roles.max_output_tokens': 10}, {'models.roles.api_key': 123}, {'product.max_tool_calls': float('inf')},
    {'product.max_tool_calls': 10**400},
])
async def test_invalid_or_non_execution_fields_cannot_change_the_file(execution_config, values):
    env = execution_config
    payload = {**request(env.configuration), 'values': values}
    before = env.path.read_bytes()
    with pytest.raises((ValidationError, DomainError)):
        await env.configuration.update_execution_settings(payload, 'invalid', env.store)
    assert env.path.read_bytes() == before
    assert not await env.store.list('configuration_execution_update')


async def test_manual_file_changes_are_visible_and_stale_versions_are_rejected(execution_config):
    env = execution_config
    payload = request(env.configuration)
    env.path.write_text(env.path.read_text().replace('[product]', '[product]\nmax_tool_calls = 700'))
    before = env.path.read_bytes()
    view = env.configuration.execution_settings()
    assert view['saved_values']['product.max_tool_calls'] == 700
    assert view['loaded_values']['product.max_tool_calls'] == 100 and view['restart_required']
    with pytest.raises(DomainError) as error:
        await env.configuration.update_execution_settings(payload, 'stale', env.store)
    assert error.value.code == 'configuration_changed' and env.path.read_bytes() == before
    assert not await env.store.list('configuration_execution_update')


async def test_existing_settings_table_alias_is_preserved_when_app_fields_are_saved(execution_config):
    env = execution_config
    env.path.write_text(env.path.read_text().replace('[app]', '[settings]\nagent_concurrency = 4'))
    view = env.configuration.execution_settings()
    field = next(row for row in view['fields'] if row['key'] == 'app.agent_concurrency')
    assert field['source'] == 'configuration_file' and field['saved_value'] == 4
    await env.configuration.update_execution_settings(request(env.configuration, **{'app.agent_concurrency': 5}), 'alias', env.store)
    document = tomllib.loads(env.path.read_text())
    assert document['settings']['agent_concurrency'] == 5 and 'app' not in document
    assert env.configuration.reload().settings.agent_concurrency == 5


async def test_concurrent_same_key_and_old_receipts_never_repeat_or_revert_later_saves(execution_config):
    env = execution_config
    configuration = env.configuration
    payload = request(configuration)
    results = await asyncio.gather(*(configuration.update_execution_settings(payload, 'same', env.store) for _ in range(4)))
    assert all(result == results[0] for result in results)
    assert len(await env.store.list('configuration_execution_update')) == 1
    await configuration.update_execution_settings(request(configuration, **{'product.max_tool_calls': 800}), 'next', env.store)
    before = env.path.read_bytes()
    assert await configuration.update_execution_settings(payload, 'same', env.store) == results[0]
    assert env.path.read_bytes() == before
    with pytest.raises(DomainError) as error:
        await configuration.update_execution_settings({**payload, 'values': {'product.max_tool_calls': 900}}, 'same', env.store)
    assert error.value.code == 'idempotency_conflict'
    with pytest.raises(DomainError) as error:
        await configuration.update_execution_settings({**payload, 'expected_configuration_revision': 'sha256:' + '0' * 64}, 'same', env.store)
    assert error.value.code == 'idempotency_conflict'
    env.path.unlink()
    assert await configuration.update_execution_settings(payload, 'same', env.store) == results[0]


async def test_different_keys_with_one_base_version_only_accept_one_save(execution_config):
    env = execution_config
    first = request(env.configuration)
    second = {**first, 'values': {'product.max_tool_calls': 800}}
    results = await asyncio.gather(env.configuration.update_execution_settings(first, 'first', env.store),
        env.configuration.update_execution_settings(second, 'second', env.store), return_exceptions=True)
    assert len([result for result in results if isinstance(result, dict)]) == 1
    conflicts = [result for result in results if isinstance(result, DomainError)]
    assert len(conflicts) == 1 and conflicts[0].code == 'configuration_changed'
    assert len(await env.store.list('configuration_execution_update')) == 1


async def test_cancelled_waiter_leaves_prepared_save_resumable_without_implicit_execution(execution_config, monkeypatch):
    env = execution_config
    original = env.store.command
    prepared = asyncio.Event()
    async def wait_after_prepare(scope, key, payload, handler):
        result = await original(scope, key, payload, handler)
        if scope == 'configuration.execution.prepare':
            prepared.set()
            await asyncio.Event().wait()
        return result
    monkeypatch.setattr(env.store, 'command', wait_after_prepare)
    payload = request(env.configuration)
    before = env.path.read_bytes()
    task = asyncio.create_task(env.configuration.update_execution_settings(payload, 'cancelled', env.store))
    await asyncio.wait_for(prepared.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.path.read_bytes() == before
    monkeypatch.setattr(env.store, 'command', original)
    result = await env.configuration.update_execution_settings(payload, 'cancelled', env.store)
    assert result['saved_values']['product.max_tool_calls'] == 500


async def test_pending_pre_write_failure_can_resume_with_original_version(execution_config, monkeypatch):
    env = execution_config
    import agentflow.configuration_execution as module
    original = module._write
    def fail(*_args, **_kwargs):
        raise DomainError('simulated_write_failure', 'fixture failure')
    monkeypatch.setattr(module, '_write', fail)
    payload = request(env.configuration)
    before = env.path.read_bytes()
    with pytest.raises(DomainError, match='fixture failure'):
        await env.configuration.update_execution_settings(payload, 'pre-write', env.store)
    assert env.path.read_bytes() == before
    monkeypatch.setattr(module, '_write', original)
    result = await env.configuration.update_execution_settings(payload, 'pre-write', env.store)
    assert result['saved_values']['product.max_tool_calls'] == 500
    assert (await env.store.list('configuration_execution_update'))[0]['state'] == 'completed'


@pytest.mark.parametrize('completed', [False, True])
async def test_lost_ack_after_file_or_receipt_commit_recovers_without_rewriting(execution_config, monkeypatch, completed):
    env = execution_config
    import agentflow.configuration_execution as module
    original = env.store.command
    async def fail(scope, key, payload, handler):
        if scope == 'configuration.execution.complete':
            if completed:
                await original(scope, key, payload, handler)
            raise RuntimeError('lost acknowledgment')
        return await original(scope, key, payload, handler)
    payload = request(env.configuration)
    monkeypatch.setattr(env.store, 'command', fail)
    with pytest.raises(RuntimeError, match='lost acknowledgment'):
        await env.configuration.update_execution_settings(payload, 'lost-ack', env.store)
    before = env.path.read_bytes()
    assert env.configuration.reload().product.max_tool_calls == 500
    monkeypatch.setattr(env.store, 'command', original)
    writes = Mock(side_effect=AssertionError('A recognized saved version must not be rewritten'))
    monkeypatch.setattr(module, '_write', writes)
    result = await env.configuration.update_execution_settings(payload, 'lost-ack', env.store)
    assert result['saved_values']['product.max_tool_calls'] == 500 and env.path.read_bytes() == before
    writes.assert_not_called()


async def test_unconfirmed_write_never_overwrites_a_third_file_version(execution_config, monkeypatch):
    env = execution_config
    original = env.store.command
    async def fail(scope, key, payload, handler):
        if scope == 'configuration.execution.complete':
            raise RuntimeError('completion interrupted')
        return await original(scope, key, payload, handler)
    payload = request(env.configuration)
    monkeypatch.setattr(env.store, 'command', fail)
    with pytest.raises(RuntimeError):
        await env.configuration.update_execution_settings(payload, 'interrupted', env.store)
    env.path.write_text(env.path.read_text().replace('max_tool_calls = 500', 'max_tool_calls = 700'))
    before = env.path.read_bytes()
    monkeypatch.setattr(env.store, 'command', original)
    with pytest.raises(DomainError) as error:
        await env.configuration.update_execution_settings(payload, 'interrupted', env.store)
    assert error.value.code == 'execution_settings_save_unconfirmed'
    assert error.value.details['requires_refresh'] and env.path.read_bytes() == before
    assert (await env.store.list('configuration_execution_update'))[0]['state'] == 'pending'


async def test_model_and_execution_saves_share_a_lock_and_preserve_each_others_fields(execution_config):
    env = execution_config
    registry = ModelRegistry(env.store)
    secrets = LocalSecretStore(env.configuration.settings.data_dir)
    model = ModelSetupRequest.model_validate({'role': 'coding', 'provider': 'openai_compatible',
        'base_url': 'https://model.example.invalid/v1', 'model': 'new-owner-model', 'api_key': 'PRIVATE_NEW_MODEL_KEY'})
    await asyncio.gather(env.configuration.update_execution_settings(request(env.configuration), 'execution', env.store),
                         env.configuration.update_model(model, 'model', registry, secrets))
    saved = env.configuration.reload()
    assert saved.product.max_tool_calls == 500 and saved.models.coding.model == 'new-owner-model'
    assert saved.models.roles.api_key.get_secret_value() == 'PRIVATE_EXECUTION_SETTINGS_KEY'
    assert not await env.store.list('model_invocation')


async def test_owner_api_saves_without_waking_agents_and_rejects_scoped_or_unversioned_requests(execution_config):
    env = execution_config
    scheduler = SimpleNamespace(wake=Mock())
    app = create_app(env.configuration.settings, store=env.store, scheduler=scheduler)
    app.include_router(product_router(SimpleNamespace(configuration=env.configuration, store=env.store)))
    owner = app.state.tokens.exchange(app.state.tokens.bootstrap_code)
    scoped = app.state.tokens.issue('agentflow_attempt', {'model:invoke'}, 'attempt', 60)
    path = '/api/v1/settings/execution'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=env.configuration.settings.origin) as client:
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers={'Authorization': 'Bearer ' + scoped})).status_code in {401, 403}
        headers = {'Authorization': 'Bearer ' + owner, 'Origin': env.configuration.settings.origin, 'Idempotency-Key': 'api'}
        view = (await client.get(path, headers=headers)).json()
        payload = {'expected_configuration_revision': view['configuration_revision'], 'values': {'product.max_tool_calls': 600}}
        assert (await client.post(path, headers={k: v for k, v in headers.items() if k != 'Origin'}, json=payload)).status_code == 403
        assert (await client.post(path, headers={k: v for k, v in headers.items() if k != 'Idempotency-Key'}, json=payload)).status_code == 422
        result = await client.post(path, headers=headers, json=payload)
        assert result.status_code == 200, result.text
        assert result.json()['restart_required'] and result.json()['saved_values']['product.max_tool_calls'] == 600
        assert (await client.post(path, headers=headers, json=payload)).json() == result.json()
        assert 'PRIVATE_EXECUTION_SETTINGS_KEY' not in result.text
    scheduler.wake.assert_not_called()


async def test_public_research_boolean_is_editable_without_expanding_frozen_authority(execution_config):
    env = execution_config
    view = env.configuration.execution_settings()
    field = next((row for row in view['fields'] if row['key'] == 'app.research_public_web_enabled'), None)
    assert field is not None, 'Public research must be configurable through execution settings'
    assert field['boolean'] is True and field['saved_value'] is True
    await env.store.command('fixture', 'frozen-research', {}, lambda tx: tx.put('dispatch_context', 'previous',
        {'task': {'role': 'research', 'allow_public_web': False, 'allowed_web_hosts': []}}))
    before = await env.store.read('dispatch_context', 'previous')
    models_before = tomllib.loads(env.path.read_text())['models']
    result = await env.configuration.update_execution_settings(request(env.configuration,
        **{'app.research_public_web_enabled': False}), 'disable-research', env.store)
    assert result['saved_values']['app.research_public_web_enabled'] is False
    assert result['loaded_values']['app.research_public_web_enabled'] is True
    assert env.configuration.reload().settings.research_public_web_enabled is False
    assert await env.store.read('dispatch_context', 'previous') == before
    assert tomllib.loads(env.path.read_text())['models'] == models_before
    for invalid in (0, 1, 'true', 'false'):
        with pytest.raises((ValidationError, DomainError)):
            await env.configuration.update_execution_settings(request(env.configuration,
                **{'app.research_public_web_enabled': invalid}), 'invalid-research-' + str(invalid), env.store)


@pytest.mark.parametrize('limit', [-1, 0, 3, 12, 100])
async def test_review_repair_policy_accepts_continuous_disabled_or_explicit_limit(execution_config, limit):
    env = execution_config
    field = next(row for row in env.configuration.execution_settings()['fields']
                 if row['key'] == 'app.auto_review_repair_limit')
    assert field['minimum'] == -1 and field['default_value'] == 100
    assert '-1' in field['description'] and field['zero_meaning']
    await env.configuration.update_execution_settings(request(env.configuration, **{'app.auto_review_repair_limit': limit}),
                                                      'save-review-policy', env.store)
    assert env.configuration.reload().settings.auto_review_repair_limit == limit
    assert not await env.store.list('review_repair') and not await env.store.list('model_invocation')


@pytest.mark.parametrize('value', [-2, True, 1.5])
async def test_review_policy_rejects_invalid_values_in_fixed_configuration(execution_config, value):
    from agentflow.settings import Settings
    with pytest.raises(ValidationError):
        Settings(auto_review_repair_limit=value)

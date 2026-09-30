"""Private fixed-file configuration, immutable model selection, and restart recovery."""
import json
import stat
import tomllib

import pytest
import pytest_asyncio

from agentflow.common import DomainError
from agentflow.configuration import configuration_path, load_configuration
from agentflow.control.product_models import ModelSetupRequest
from agentflow.models.profiles import ModelRegistry
from agentflow.models.secrets import LocalSecretStore
from agentflow.storage import Store


@pytest.fixture
def private_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    return home


def write_config(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(content)
    path.chmod(0o600)


def setup_request(**changes):
    return ModelSetupRequest.model_validate({'role': 'both', 'provider': 'openai_compatible',
        'base_url': 'https://models.example.invalid/v1', 'model': 'owner-chosen-model',
        'api_key': 'private-fixture-credential', **changes})


@pytest_asyncio.fixture
async def configured_store(private_home):
    configuration = load_configuration(create=True)
    store = Store(configuration.settings.data_dir)
    await store.start()
    try:
        yield configuration, ModelRegistry(store), LocalSecretStore(configuration.settings.data_dir)
    finally:
        await store.close()


def test_fixed_lookup_has_no_creation_or_alternate_search_without_explicit_request(private_home, tmp_path, monkeypatch):
    alternate = tmp_path / 'other'
    alternate.mkdir()
    write_config(alternate / 'config.toml', '[app]\nport = 9888\n')
    monkeypatch.chdir(alternate)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(alternate))
    monkeypatch.setenv('AGENTFLOW_CONFIG', str(alternate / 'config.toml'))
    expected = private_home / '.config/agentflow/config.toml'
    assert configuration_path() == expected
    with pytest.raises(DomainError, match='agentflow start') as error:
        load_configuration()
    assert error.value.code == 'configuration_missing'
    assert not expected.parent.exists()
    configuration = load_configuration(create=True)
    assert configuration.config_path == expected and configuration.settings.port == 8787
    assert stat.S_IMODE(expected.stat().st_mode) == 0o600
    assert stat.S_IMODE(expected.parent.stat().st_mode) == 0o700
    assert configuration.product.model_dump() == {
        'target': 'web', 'review_mode': 'auto', 'language': 'zh-CN', 'max_model_requests': 200,
        'max_active_seconds': 1800, 'max_tool_calls': 100, 'output_root': private_home / 'AgentFlowProducts'}
    assert not configuration.models.roles.enabled and not configuration.models.coding.enabled
    assert not configuration.settings.data_dir.exists()
    assert set(tomllib.loads(expected.read_text())) == {'app', 'product', 'models'}
    previous = expected.read_bytes()
    assert load_configuration(create=True).fingerprint == configuration.fingerprint
    assert expected.read_bytes() == previous
    monkeypatch.setenv('HOME', str(alternate))
    assert configuration.reload().config_path == expected


def test_settings_and_product_defaults_load_with_offline_gateway_defaults(private_home):
    path = configuration_path()
    data = private_home / 'custom-state'
    write_config(path, f'''[app]
data_dir = {json.dumps(str(data))}
host = "::1"
port = 8789
agent_concurrency = 4
auto_review_repair_limit = 3
max_body_bytes = 1048576
owner_token_seconds = 1200
bootstrap_seconds = 60
executor_host = "192.168.1.50"
executor_port = 9555
trusted_project_execution = true
research_web_hosts = ["Docs.Example.com"]
node_output_limit_bytes = 1048576
node_active_seconds = 900
dashboard_dir = "~/dashboard"
[product]
target = "api"
review_mode = "milestones"
max_model_requests = 50
max_active_seconds = 120
max_tool_calls = 6
output_root = "~/Products"
''')
    configuration = load_configuration()
    settings = configuration.settings
    assert settings.data_dir == data and settings.host == '::1' and settings.port == 8789
    assert settings.agent_concurrency == 4 and settings.auto_review_repair_limit == 3
    assert settings.max_body_bytes == 1048576 and settings.owner_token_seconds == 1200
    assert settings.bootstrap_seconds == 60 and settings.executor_host == '192.168.1.50'
    assert settings.executor_origin == 'https://192.168.1.50:9555'
    assert settings.tls_certificate == data / 'nodes/pki/server.pem'
    assert settings.tls_private_key == data / 'nodes/pki/server.key'
    assert settings.tls_client_ca == data / 'nodes/pki/ca.pem'
    assert settings.dashboard_dir == private_home / 'dashboard'
    assert settings.trusted_project_execution and settings.research_web_hosts == ['docs.example.com']
    assert settings.node_output_limit_bytes == 1048576 and settings.node_active_seconds == 900
    assert configuration.product.output_root == private_home / 'Products'
    assert configuration.product.max_active_seconds == 120 and configuration.product.max_tool_calls == 6
    assert not data.exists(), 'Loading must not generate certificates or data directories'


@pytest.mark.parametrize('maximum', [65537, 393216, 1048576])
async def test_large_output_allowance_loads_and_registers_both_roles(configured_store, maximum):
    configuration, registry, secrets = configured_store
    write_config(configuration.config_path, '\n'.join(
        f'[models.{role}]\nprovider="openai_compatible"\n'
        'base_url="https://models.example.invalid/v1"\nmodel="owner-chosen-model"\n'
        f'api_key="private-fixture-credential"\nmax_output_tokens={maximum}\n'
        for role in ('roles', 'coding')))
    loaded = configuration.reload()
    applied = await loaded.apply_models(registry, secrets)
    for role, binding in [('roles', 'role_model_profile_id'), ('coding', 'coding_model_profile_id')]:
        assert getattr(loaded.models, role).max_output_tokens == maximum
        assert (await registry.get(applied['bindings'][binding])).max_output_tokens == maximum
    assert await registry.store.list('model_invocation') == []


@pytest.mark.parametrize('maximum', [0, -1])
def test_output_allowance_remains_positive(private_home, maximum):
    write_config(configuration_path(), f'[models.coding]\nmax_output_tokens={maximum}\n')
    with pytest.raises(DomainError) as error:
        load_configuration()
    assert error.value.code == 'configuration_invalid'


async def test_large_output_allowance_from_model_settings_survives_save_reload_and_replay(configured_store):
    configuration, registry, secrets = configured_store
    request = setup_request(max_output_tokens=393216)
    result = await configuration.update_model(request, 'large-output-setting', registry, secrets)
    disk = configuration.reload()
    assert disk.models.roles.max_output_tokens == disk.models.coding.max_output_tokens == 393216
    assert (await registry.get(result['profile_id'])).max_output_tokens == 393216
    assert await configuration.update_model(request, 'large-output-setting', registry, secrets) == result
    assert stat.S_IMODE(configuration.config_path.stat().st_mode) == 0o600
    assert await registry.store.list('model_invocation') == []


@pytest.mark.parametrize('content', [
    '[models.roles]\nprovider="owner-private-secret"\n',
    '[models.roles]\nbase_url="https://user:owner-private-secret@models.example/v1"\nmodel="m"\napi_key="other"\n',
    '[models.roles]\nbase_url="https://models.example/v1"\nmodel="m"\napi_key_env="owner-private-secret"\n',
    '[app]\nport="owner-private-secret"\n',
    '[app]\n"owner-private-secret"=true\n',
    '"owner-private-secret" = [broken',
])
def test_configuration_errors_never_repeat_values_or_unknown_field_names(private_home, content):
    write_config(configuration_path(), content)
    with pytest.raises(DomainError) as error:
        load_configuration()
    assert error.value.code == 'configuration_invalid'
    assert 'owner-private-secret' not in str(error.value)
    assert error.value.__suppress_context__


def test_public_or_linked_configuration_is_rejected(private_home, tmp_path):
    configuration = load_configuration(create=True)
    path = configuration.config_path
    path.chmod(0o644)
    with pytest.raises(DomainError) as error:
        configuration.reload()
    assert error.value.code == 'configuration_private_required'
    path.chmod(0o600)
    original = tmp_path / 'actual.toml'
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises(DomainError):
        configuration.reload()
    path.unlink()
    path.parent.rmdir()
    directory = tmp_path / 'elsewhere'
    directory.mkdir()
    path.parent.symlink_to(directory)
    with pytest.raises(DomainError):
        load_configuration(create=True)


async def test_file_apply_is_offline_private_and_idempotent_and_empty_template_keeps_legacy(configured_store):
    configuration, registry, secrets = configured_store
    initial = await configuration.apply_models(registry, secrets)
    assert initial['bindings'] == {} and not initial['configured']
    write_config(configuration.config_path, '''[models.roles]
provider = "deepseek"
base_url = "https://api.deepseek.com"
model = "explicit-model-a"
api_key = "private-fixture-credential"
[models.coding]
base_url = "https://models.example.invalid/v1"
model = "explicit-model-b"
api_key_env = "AGENTFLOW_UNSET_TEST_KEY"
''')
    complete = configuration.reload()
    applied = await complete.apply_models(registry, secrets)
    repeated = await complete.apply_models(registry, secrets)
    assert applied == repeated
    records = await registry.store.list('model_profile')
    assert len(records) == 2 and all(record['revision'] == 1 for record in records)
    roles = await registry.get(applied['bindings']['role_model_profile_id'])
    coding = await registry.get(applied['bindings']['coding_model_profile_id'])
    assert roles.protocols == ['chat_completions'] and coding.protocols == ['responses']
    assert secrets.read(roles.credential_reference) == 'private-fixture-credential'
    assert coding.credential_reference == 'env:AGENTFLOW_UNSET_TEST_KEY'
    assert 'private-fixture-credential' not in repr(complete)
    assert 'private-fixture-credential' not in complete.model_dump_json()
    assert 'private-fixture-credential' not in json.dumps(records + await registry.list() + [applied])
    assert await registry.store.list('model_invocation') == []
    for file in (configuration.settings.data_dir / 'state').iterdir():
        if file.is_file():
            assert b'private-fixture-credential' not in file.read_bytes()
    blank_applied = await configuration.apply_models(registry, secrets)
    assert blank_applied['bindings'] == applied['bindings']
    assert not blank_applied['configured']


async def test_ui_save_updates_same_file_and_preserves_unapplied_manual_settings(configured_store):
    configuration, registry, secrets = configured_store
    path = configuration.config_path
    original_runtime_port = configuration.settings.port
    path.write_text(path.read_text().replace('port = 8787', 'port = 8899').replace('max_tool_calls = 100', 'max_tool_calls = 7'))
    response = await configuration.update_model(setup_request(), 'ui-save', registry, secrets)
    assert response['configured'] and response['restart_required']
    assert response['bindings']['role_model_profile_id'] == response['bindings']['coding_model_profile_id']
    assert 'private-fixture-credential' not in json.dumps(response)
    assert configuration.settings.port == original_runtime_port and configuration.product.max_tool_calls == 100
    disk = configuration.reload()
    assert disk.settings.port == 8899 and disk.product.max_tool_calls == 7
    assert disk.models.roles.api_key.get_secret_value() == 'private-fixture-credential'
    assert configuration.models == disk.models
    assert configuration.fingerprint != disk.fingerprint
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert secrets.read('local:' + response['profile_id']) == 'private-fixture-credential'
    profile = await registry.get(response['profile_id'])
    assert profile.protocols == ['chat_completions', 'responses']
    assert await configuration.update_model(setup_request(), 'ui-save', registry, secrets) == response
    second = await configuration.update_model(setup_request(model='new-model', api_key='new-private-key'), 'next', registry, secrets)
    assert second['profile_id'] != response['profile_id']
    assert (await registry.get(response['profile_id'])).revision == 1
    assert await configuration.update_model(setup_request(), 'ui-save', registry, secrets) == response
    assert configuration.reload().models.roles.model == 'new-model', 'Replaying an old save must not revert later setup'
    assert (await registry.store.read('product_model_binding', 'default')) == second['bindings']
    with pytest.raises(DomainError) as error:
        await configuration.update_model(setup_request(model='different'), 'ui-save', registry, secrets)
    assert error.value.code == 'idempotency_conflict'


async def test_model_only_ui_update_tracks_loaded_fingerprint_and_persists_after_restart(configured_store, monkeypatch, tmp_path):
    configuration, registry, secrets = configured_store
    monkeypatch.setenv('HOME', str(tmp_path / 'different-home'))
    response = await configuration.update_model(setup_request(), 'ui', registry, secrets)
    assert not response['restart_required']
    assert response['configuration_fingerprint'] == configuration.fingerprint == configuration.reload().fingerprint
    assert not (tmp_path / 'different-home').exists()
    new_configuration = configuration.reload()
    assert (await new_configuration.apply_models(registry, secrets))['bindings'] == response['bindings']
    assert len(await registry.store.list('model_profile')) == 1


async def test_ui_environment_reference_does_not_copy_the_environment_value_into_toml(configured_store, monkeypatch):
    configuration, registry, secrets = configured_store
    monkeypatch.setenv('AGENTFLOW_CONFIG_TEST_KEY', 'private-environment-value')
    request = setup_request(api_key=None, credential_env='AGENTFLOW_CONFIG_TEST_KEY')
    response = await configuration.update_model(request, 'env', registry, secrets)
    source = configuration.config_path.read_text()
    assert 'api_key_env = "AGENTFLOW_CONFIG_TEST_KEY"' in source
    assert 'private-environment-value' not in source
    profile = await registry.get(response['profile_id'])
    assert profile.credential_reference == 'env:AGENTFLOW_CONFIG_TEST_KEY'
    assert list(secrets.root.iterdir()) == []


async def test_failed_apply_leaves_recoverable_file_without_claiming_success(configured_store, monkeypatch):
    configuration, registry, secrets = configured_store
    original = registry.register
    async def fail(*_args, **_kwargs):
        raise RuntimeError('storage failure containing private-fixture-credential')
    monkeypatch.setattr(registry, 'register', fail)
    with pytest.raises(DomainError) as error:
        await configuration.update_model(setup_request(), 'recoverable', registry, secrets)
    assert error.value.code == 'configuration_apply_pending'
    assert 'private-fixture-credential' not in str(error.value)
    assert configuration.reload().models.roles.model == 'owner-chosen-model'
    assert not configuration.models.roles.enabled
    monkeypatch.setattr(registry, 'register', original)
    recovered = configuration.reload()
    await recovered.apply_models(registry, secrets)
    response = await configuration.update_model(setup_request(), 'recoverable', registry, secrets)
    assert response['configured'] and not response['restart_required']
    assert len(await registry.store.list('model_profile')) == 1


async def test_manual_edit_during_pending_update_is_preserved(configured_store, monkeypatch):
    configuration, registry, secrets = configured_store
    original = registry.store.command
    async def edit_between_operations(scope, *args, **kwargs):
        result = await original(scope, *args, **kwargs)
        if scope == 'configuration.model.prepare':
            path = configuration.config_path
            path.write_text(path.read_text().replace('port = 8787', 'port = 8899'))
        return result
    monkeypatch.setattr(registry.store, 'command', edit_between_operations)
    response = await configuration.update_model(setup_request(), 'external-edit', registry, secrets)
    assert response['restart_required']
    assert configuration.reload().settings.port == 8899


async def test_model_save_preserves_omitted_and_relative_settings_values(configured_store):
    configuration, registry, secrets = configured_store
    write_config(configuration.config_path, '''[app]
data_dir = "~/gateway-state"
executor_host = "192.168.1.50"
[product]
output_root = "~/MyProducts"
''')
    before = tomllib.loads(configuration.config_path.read_text())
    response = await configuration.update_model(setup_request(), 'preserve-defaults', registry, secrets)
    assert response['restart_required']
    after = tomllib.loads(configuration.config_path.read_text())
    assert after['app'] == before['app'] and after['product'] == before['product']
    assert 'tls_certificate' not in after['app'], 'Model saves must not turn derived paths into explicit overrides'


async def test_invalid_model_url_cannot_change_the_config_or_expose_the_key(configured_store):
    configuration, registry, secrets = configured_store
    before = configuration.config_path.read_bytes()
    request = setup_request(base_url='https://user:private-fixture-credential@example.com')
    with pytest.raises(DomainError) as error:
        await configuration.update_model(request, 'invalid', registry, secrets)
    assert 'private-fixture-credential' not in str(error.value)
    assert configuration.config_path.read_bytes() == before
    assert await registry.store.list('model_profile') == []

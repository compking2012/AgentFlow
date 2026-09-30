"""Startup settings remain separate from frozen work and model authorizations."""
import json
import tomllib

import pytest

from agentflow.configuration import load_configuration
from agentflow.runtime.service import RuntimeService
from agentflow.storage import Store


@pytest.fixture
def private_configuration_path(tmp_path, monkeypatch):
    path = tmp_path / 'private/config.toml'
    monkeypatch.setattr('agentflow.configuration.configuration_path', lambda: path)
    return path


def write_configuration(path, data_dir, *, startup=None):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    content = '[app]\ndata_dir = ' + json.dumps(str(data_dir)) + '\n'
    if startup is not None:
        content += f'agent_startup_timeout_seconds = {startup}\n'
    content += ('[product]\nmax_active_seconds = 37\nmax_tool_calls = 11\nmax_model_requests = 7\n'
                '[models.roles]\nrequest_timeout_seconds = 9\nmax_output_tokens = 1024\napi_key_env = ""\n')
    path.write_text(content)
    path.chmod(0o600)


def startup_field(configuration):
    return next(row for row in configuration.execution_settings()['fields']
                if row['key'] == 'app.agent_startup_timeout_seconds')


def test_new_template_and_metadata_offer_sixty_second_startup_window(private_configuration_path):
    configuration = load_configuration(create=True)
    document = tomllib.loads(private_configuration_path.read_text())
    assert document['app']['agent_startup_timeout_seconds'] == 60
    field = startup_field(configuration)
    assert field['default_value'] == field['saved_value'] == field['loaded_value'] == 60
    assert field['exclusive_minimum'] == 0 and field['maximum'] == 60
    assert field['unit'] == '秒' and field['restart_on_change']


def test_missing_startup_value_uses_new_default_without_rewriting_file(private_configuration_path, tmp_path):
    write_configuration(private_configuration_path, tmp_path / 'state')
    before = private_configuration_path.read_bytes()
    configuration = load_configuration(create=True)
    field = startup_field(configuration)
    assert field['saved_value'] == field['loaded_value'] == 60
    assert field['source'] == 'application_default'
    assert private_configuration_path.read_bytes() == before
    assert configuration.product.max_active_seconds == 37
    assert configuration.models.roles.request_timeout_seconds == 9


def test_explicit_fifteen_seconds_remains_saved_and_loaded(private_configuration_path, tmp_path):
    write_configuration(private_configuration_path, tmp_path / 'state', startup=15)
    before = private_configuration_path.read_bytes()
    configuration = load_configuration(create=True)
    field = startup_field(configuration)
    assert field['default_value'] == 60
    assert field['saved_value'] == field['loaded_value'] == 15
    assert field['source'] == 'configuration_file' and not field['restart_required']
    assert private_configuration_path.read_bytes() == before
    runtime = RuntimeService(None, tmp_path / 'runtime', None, sandbox=object(), settings=configuration.settings)
    assert runtime.supervisor.handshake_timeout == 15


def test_runtime_without_settings_uses_sixty_second_startup_window(tmp_path):
    runtime = RuntimeService(None, tmp_path / 'runtime', None, sandbox=object())
    assert runtime.supervisor.handshake_timeout == 60


async def test_editing_startup_wait_preserves_frozen_state_and_all_budgets(private_configuration_path, tmp_path):
    write_configuration(private_configuration_path, tmp_path / 'state', startup=15)
    configuration = load_configuration()
    before_document = tomllib.loads(private_configuration_path.read_text())
    store = Store(configuration.settings.data_dir)
    await store.start()
    protected = ('run', 'work_item', 'attempt', 'supervised_attempt', 'coding_work_budget',
                 'coding_step_usage', 'budget_account', 'authorization', 'failure_recovery', 'timeout_recovery')
    try:
        for kind in protected:
            await store.command('startup.fixture', kind, {},
                lambda tx, kind=kind: tx.put(kind, 'existing', {'status': 'blocked', 'used': 5, 'limit': 7}))
        before_state = {kind: await store.list(kind) for kind in protected}
        result = await configuration.update_execution_settings({
            'expected_configuration_revision': configuration.execution_settings()['configuration_revision'],
            'values': {'app.agent_startup_timeout_seconds': 60},
        }, 'startup-wait', store)
        saved = configuration.reload()
        assert saved.settings.agent_startup_timeout_seconds == 60
        assert configuration.settings.agent_startup_timeout_seconds == 15
        assert result['restart_required'] and not result['current_runs_changed']
        after_document = tomllib.loads(private_configuration_path.read_text())
        assert after_document == {
            **before_document,
            'app': {**before_document['app'], 'agent_startup_timeout_seconds': 60},
        }
        assert {kind: await store.list(kind) for kind in protected} == before_state
        assert not await store.list('model_invocation') and not await store.list('run_recovery')
    finally:
        await store.close()

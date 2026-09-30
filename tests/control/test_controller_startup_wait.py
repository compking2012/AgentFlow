"""CLI startup waits follow owner configuration, not a hidden poll count."""
import asyncio
import tomllib
from types import SimpleNamespace

import httpx
import pytest
from test_startup_settings import private_configuration_path as private_configuration_path
from test_startup_settings import write_configuration

from agentflow.common import DomainError
from agentflow.configuration import load_configuration
from agentflow.control import owner_client
from agentflow.settings import Settings
from agentflow.storage import Store


@pytest.fixture
def startup(tmp_path, monkeypatch):
    state = SimpleNamespace(now=0.0, ready_after=35.0, spawns=0, exit_code=None, calls=0, unsafe=False)
    original_sleep = asyncio.sleep
    original_client = httpx.AsyncClient

    async def sleep(seconds):
        state.now += seconds
        await original_sleep(0)

    async def launch(directory):
        state.calls += 1
        if state.unsafe and state.spawns:
            raise DomainError('unsafe_owner_ipc', 'Owner channel is not private', 403)
        if state.now >= state.ready_after:
            return 'http://127.0.0.1:8317/#bootstrap=fixture'
        raise DomainError('controller_unavailable', 'Starting', 503)

    def spawn(*args, **kwargs):
        state.spawns += 1
        return SimpleNamespace(poll=lambda: state.exit_code)

    def client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(
            lambda request: httpx.Response(201, json={'owner_token': 'fixture-owner'})))

    monkeypatch.setattr(owner_client, 'launch_url', launch)
    monkeypatch.setattr(owner_client.subprocess, 'Popen', spawn)
    monkeypatch.setattr(owner_client.asyncio, 'sleep', sleep)
    monkeypatch.setattr(owner_client, 'monotonic', lambda: state.now, raising=False)
    monkeypatch.setattr(owner_client.httpx, 'AsyncClient', client)
    state.settings = SimpleNamespace(data_dir=tmp_path / 'state', controller_startup_timeout_seconds=120)
    return state


@pytest.mark.parametrize('timeout', [120, 0])
async def test_controller_can_become_ready_after_thirty_seconds_with_one_process(startup, timeout):
    startup.settings.controller_startup_timeout_seconds = timeout
    client = await owner_client.OwnerClient.connect(startup.settings)
    try:
        assert startup.now >= 35
        assert startup.spawns == 1
        assert client.origin == 'http://127.0.0.1:8317'
    finally:
        await client.close()


async def test_configured_deadline_is_not_reset_by_polling(startup):
    startup.settings.controller_startup_timeout_seconds = 2
    with pytest.raises(DomainError) as error:
        await owner_client.OwnerClient.connect(startup.settings)
    assert error.value.code == 'controller_start_timeout'
    assert 2 <= startup.now < 2.11
    assert startup.spawns == 1


async def test_process_exit_still_fails_immediately_with_unlimited_wait(startup):
    startup.settings.controller_startup_timeout_seconds = 0
    startup.exit_code = 1
    with pytest.raises(DomainError) as error:
        await owner_client.OwnerClient.connect(startup.settings)
    assert error.value.code == 'controller_start_failed'
    assert startup.now == 0 and startup.spawns == 1


async def test_unsafe_owner_channel_is_not_disguised_as_startup_timeout(startup):
    startup.unsafe = True
    with pytest.raises(DomainError) as error:
        await owner_client.OwnerClient.connect(startup.settings)
    assert error.value.code == 'unsafe_owner_ipc'
    assert startup.spawns == 1


def test_controller_startup_default_is_separate_from_agent_and_model_budgets():
    settings = Settings()
    assert settings.controller_startup_timeout_seconds == 120
    assert settings.agent_startup_timeout_seconds == 60
    for invalid in [-1, float('inf'), float('nan'), True]:
        with pytest.raises(ValueError):
            Settings(controller_startup_timeout_seconds=invalid)


async def test_owner_can_save_controller_wait_without_changing_execution_budgets(private_configuration_path, tmp_path):
    write_configuration(private_configuration_path, tmp_path / 'state', startup=15)
    original = private_configuration_path.read_bytes()
    configuration = load_configuration()
    field = next(row for row in configuration.execution_settings()['fields']
                 if row['key'] == 'app.controller_startup_timeout_seconds')
    assert field['default_value'] == field['saved_value'] == field['loaded_value'] == 120
    assert field['source'] == 'application_default'
    assert private_configuration_path.read_bytes() == original
    store = Store(configuration.settings.data_dir)
    await store.start()
    try:
        await store.command('fixture', 'budget', {}, lambda tx: tx.put('budget_account', 'existing', {'used': 9}))
        budget = await store.read('budget_account', 'existing')
        await configuration.update_execution_settings({
            'expected_configuration_revision': configuration.execution_settings()['configuration_revision'],
            'values': {'app.controller_startup_timeout_seconds': 0},
        }, 'controller-wait', store)
        saved = configuration.reload()
        assert saved.settings.controller_startup_timeout_seconds == 0
        assert saved.settings.agent_startup_timeout_seconds == 15
        assert saved.product == configuration.product
        assert saved.models == configuration.models
        assert await store.read('budget_account', 'existing') == budget
        before = tomllib.loads(original.decode())
        assert tomllib.loads(private_configuration_path.read_text()) == {
            **before, 'app': {**before['app'], 'controller_startup_timeout_seconds': 0}}
    finally:
        await store.close()

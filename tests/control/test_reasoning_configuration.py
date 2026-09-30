import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, uuid5

import pytest
import pytest_asyncio
from pydantic import SecretStr, ValidationError

from agentflow.common import canonical_digest
from agentflow.configuration import (
    Configuration,
    ModelConfiguration,
    ModelConfigurations,
    _plain,
    load_configuration,
)
from agentflow.control.coding_steps import CodingSteps
from agentflow.control.scheduler import Scheduler
from agentflow.models.budget import BudgetLedger
from agentflow.models.profiles import ModelRegistry
from agentflow.models.secrets import LocalSecretStore
from agentflow.repository import RepositoryAdapter
from agentflow.runtime.trace import ExecutionTrace
from agentflow.settings import Settings
from agentflow.storage import Store


def model(**changes):
    return ModelConfiguration(provider='openai_compatible', base_url='https://models.example.invalid/v1',
        model='deepseek-v4-pro', api_key=SecretStr('fixture-key'), max_output_tokens=16384, **changes)


def legacy_model_shape():
    # Exact pre-reasoning configuration fields, including its existing null defaults.
    return {'provider': 'openai_compatible', 'base_url': 'https://models.example.invalid/v1',
            'model': 'deepseek-v4-pro', 'api_key': 'fixture-key', 'api_key_env': None,
            'max_output_tokens': 16384, 'provider_documented_version': None, 'pricing': None,
            'max_request_bytes': 8 * 1024 * 1024, 'max_response_bytes': 16 * 1024 * 1024,
            'request_timeout_seconds': 120.0, 'allow_loopback_upstream': False, 'allowed_tool_names': None}


@pytest_asyncio.fixture
async def configuration_store(tmp_path):
    store = Store(tmp_path / 'data')
    await store.start()
    yield store
    await store.close()


def test_unset_and_explicit_null_preserve_legacy_model_and_configuration_fingerprints():
    old_shape = legacy_model_shape()
    old_fingerprint = canonical_digest(old_shape)
    for configured in (model(), model(reasoning_effort=None)):
        assert _plain(configured) == old_shape
        assert configured.fingerprint == old_fingerprint
        assert 'reasoning_effort' not in configured.model_dump()
        configuration = Configuration(models=ModelConfigurations(coding=configured))
        shape = _plain(configuration)
        assert 'reasoning_effort' not in shape['models']['coding']
        assert 'reasoning_effort' not in shape['models']['roles']
        assert configuration.fingerprint == canonical_digest(shape)
    low = model(reasoning_effort='low')
    assert low.fingerprint == canonical_digest({**old_shape, 'reasoning_effort': 'low'})
    assert low.fingerprint != old_fingerprint


async def test_old_profile_registration_ids_revisions_bindings_and_command_replay_survive_none(
        configuration_store, tmp_path):
    store = configuration_store
    registry = ModelRegistry(store)
    secrets = LocalSecretStore(tmp_path / 'secrets-data')
    configured = model()
    old_fingerprint = canonical_digest(legacy_model_shape())
    identity = str(uuid5(NAMESPACE_URL, 'agentflow:file-model:' + canonical_digest({
        'configuration': old_fingerprint, 'protocols': ['responses']})))
    profile = configured.profile(identity, ['responses'])
    old_payload = profile.model_dump(exclude={'revision', 'reasoning_effort'})
    def old_register(tx):
        saved = tx.put('model_profile', identity, old_payload)
        return {'profile_id': identity, 'revision': saved['revision']}
    original = await store.command('model_profile', 'file-model:' + identity, old_payload, old_register)
    old_run = await store.command('fixture', 'old-run', {}, lambda tx: tx.put('run', 'old-run', {
        'runtime_bindings': {'coding_model_profile_id': identity}, 'execution_state': 'paused'}))
    configuration = Configuration(models=ModelConfigurations(coding=configured))
    applied = await configuration.apply_models(registry, secrets)
    assert applied['bindings']['coding_model_profile_id'] == identity
    assert len(await store.list('model_profile')) == 1
    current = await store.read('model_profile', identity)
    assert current['revision'] == 1 and 'reasoning_effort' not in current
    assert await registry.register(profile, 'file-model:' + identity) == original
    assert (await registry.get(identity)).reasoning_effort is None
    changed = Configuration(models=ModelConfigurations(coding=model(reasoning_effort='low')))
    low = await changed.apply_models(registry, secrets)
    low_identity = low['bindings']['coding_model_profile_id']
    assert low_identity != identity and (await registry.get(low_identity)).reasoning_effort == 'low'
    assert await store.read('run', 'old-run') == old_run
    assert (await store.read('model_profile', identity))['revision'] == 1
    assert len(await store.list('model_profile')) == 2


@pytest.mark.parametrize('effort', ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'])
def test_explicit_supported_cli_values_survive_configuration_and_profile(effort):
    configured = model(reasoning_effort=effort)
    assert configured.profile('fixture', ['responses']).reasoning_effort == effort
    assert configured.model_dump()['reasoning_effort'] == effort


@pytest.mark.parametrize('effort', ['max', 'off', 'LOW', '', False, 0, ['low']])
def test_invalid_effort_is_not_coerced_or_silently_mapped(effort):
    with pytest.raises(ValidationError):
        model(reasoning_effort=effort)


def test_fixed_toml_loads_explicit_reasoning_without_changing_model_or_output_cap(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    configuration = load_configuration(create=True)
    configuration.config_path.write_text('[models.coding]\nprovider="openai_compatible"\n'
        'base_url="https://models.example.invalid/v1"\nmodel="deepseek-v4-pro"\n'
        'api_key="fixture-key"\nmax_output_tokens=16384\nreasoning_effort="low"\n')
    loaded = configuration.reload()
    assert loaded.models.coding.reasoning_effort == 'low'
    assert loaded.models.coding.model == 'deepseek-v4-pro'
    assert loaded.models.coding.max_output_tokens == 16384


@pytest.mark.parametrize('effort', [None, 'low'])
async def test_scheduler_freezes_coding_policy_into_task_and_authorization(configuration_store, tmp_path, effort):
    store = configuration_store
    profile = model(reasoning_effort=effort).profile('fixture-model-profile', ['responses'])
    limits = {'currency': 'USD', 'limit_micros': 0, 'cost_mode': 'request_limited',
              'max_model_requests': 0, 'max_active_seconds': 30, 'max_tool_calls': 10}
    run = {'id': 'run', 'project_id': 'project', 'iteration_id': 'iteration', 'runtime_bindings': {'coding_model_profile_id': profile.model_profile_id},
           'budget_limit': limits, 'execution_state': 'running'}
    attempt = {'id': 'attempt', 'run_id': 'run', 'iteration_id': 'iteration', 'work_item_id': 'work',
               'generation': 1, 'status': 'running', 'fencing_token': 1, 'input_fingerprint': 'sha256:' + 'a' * 64}
    def seed(tx):
        tx.put('project', 'project', {'local_path': str(tmp_path / 'source')})
        tx.put('iteration', 'iteration', {'budget_limit': limits})
        tx.put('run', run['id'], {key: value for key, value in run.items() if key != 'id'})
        tx.put('attempt', attempt['id'], {key: value for key, value in attempt.items() if key != 'id'})
        return tx.put('work_item', 'work', {'run_id': 'run', 'project_id': 'project',
            'step': 'implementation', 'role': 'development', 'fencing_token': attempt['fencing_token'],
            'input_fingerprint': attempt['input_fingerprint'], 'payload': {},
            'generation': 1, 'write_paths': ['src'], 'attempt_id': 'attempt', 'status': 'running'})
    work = await store.command('fixture', 'dispatch-work', {}, seed)
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.store = store
    scheduler.settings = Settings(data_dir=tmp_path / 'controller')
    scheduler.repository = RepositoryAdapter()
    scheduler.coding_steps = CodingSteps(store, scheduler.settings, scheduler.repository)
    scheduler.traces = ExecutionTrace(store)
    scheduler.runtime = SimpleNamespace(workspaces=SimpleNamespace(create_clone=AsyncMock(return_value=tmp_path / 'source')))
    scheduler.models = SimpleNamespace(registry=SimpleNamespace(get=AsyncMock(return_value=profile)), ledger=BudgetLedger(store))
    scheduler.workflow = SimpleNamespace(block_attempt=AsyncMock())
    scheduler._source = AsyncMock(return_value=(tmp_path / 'source', 'a' * 40))
    scheduler._prompt = AsyncMock(return_value='Frozen coding task')
    scheduler._execute_existing = AsyncMock()
    scheduler._context_roots = {('work', 1): tmp_path / 'context'}
    scheduler._wake = asyncio.Event()
    await scheduler._dispatch({'run': run, 'attempt': attempt, 'work_item': work})
    scheduler.workflow.block_attempt.assert_not_awaited()
    frozen = (await store.read('dispatch_context', 'attempt'))['task']
    authorization = (await store.list('task_authorization'))[0]
    assert frozen.get('reasoning_effort') == authorization.get('reasoning_effort') == effort
    assert ('reasoning_effort' in frozen) == ('reasoning_effort' in authorization) == (effort is not None)
    assert frozen['max_output_tokens'] == authorization['max_output_tokens'] == 16384
    assert frozen['profile_id'] == authorization['model_profile_id'] == profile.model_profile_id
    assert 'task_token' not in frozen


async def test_scheduler_rejects_profile_revision_changed_after_recovery_binding_validation(configuration_store, tmp_path):
    store = configuration_store
    registry = ModelRegistry(store)
    profile = model(reasoning_effort='low').profile('recovery-profile', ['responses'])
    await registry.register(profile, 'original-recovery-profile')
    limits = {'currency': 'USD', 'limit_micros': 0, 'cost_mode': 'request_limited',
              'max_model_requests': 0, 'max_active_seconds': 30, 'max_tool_calls': 10}
    run = {'id': 'run', 'project_id': 'project', 'iteration_id': 'iteration', 'runtime_bindings': {'coding_model_profile_id': 'original-run-profile'},
           'budget_limit': limits, 'execution_state': 'running'}
    attempt = {'id': 'new-attempt', 'run_id': 'run', 'iteration_id': 'iteration', 'work_item_id': 'work',
               'generation': 2, 'status': 'running', 'fencing_token': 2, 'input_fingerprint': 'sha256:' + 'b' * 64}
    binding = {'recovery_id': 'recovery', 'run_id': 'run', 'iteration_id': 'iteration', 'work_item_id': 'work',
               'generation': 2, 'model_profile_id': profile.model_profile_id, 'profile_revision': 1}
    def seed(tx):
        tx.put('project', 'project', {'local_path': str(tmp_path / 'source')})
        tx.put('iteration', 'iteration', {'budget_limit': limits})
        tx.put('run', run['id'], {key: value for key, value in run.items() if key != 'id'})
        tx.put('attempt', attempt['id'], {key: value for key, value in attempt.items() if key != 'id'})
        tx.put('run_recovery', 'recovery', {'mode': 'retry', 'actor': 'owner', 'run_id': 'run', 'iteration_id': 'iteration',
            'affected_work_item_ids': ['work'], 'model_profile_bindings': {
                'work': {**binding, 'payload_digest': canonical_digest(binding)}}})
        return tx.put('work_item', 'work', {'run_id': 'run', 'project_id': 'project', 'step': 'implementation', 'role': 'development',
            'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint'],
            'generation': 2, 'write_paths': ['src'], 'attempt_id': 'new-attempt', 'status': 'running',
            'payload': {'recovery_model_binding': binding}})
    work = await store.command('fixture', 'recovery-dispatch-work', {}, seed)
    observed = []
    async def changed_registry_read(profile_id):
        # The real resolver has already validated the persisted revision 1. Its
        # subsequent registry read observes an owner/configuration revision race.
        current = await registry.get(profile_id)
        assert current.revision == 1
        observed.append(profile_id)
        await registry.register(current.model_copy(update={'max_output_tokens': 32768}),
                                'profile-revised-between-reads', expected_revision=current.revision)
        return await registry.get(profile_id)
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.store = store
    scheduler.settings = Settings(data_dir=tmp_path / 'controller')
    scheduler.repository = RepositoryAdapter()
    scheduler.coding_steps = CodingSteps(store, scheduler.settings, scheduler.repository)
    scheduler.traces = ExecutionTrace(store)
    scheduler.runtime = SimpleNamespace(workspaces=SimpleNamespace(create_clone=AsyncMock(return_value=tmp_path / 'source')))
    scheduler.models = SimpleNamespace(registry=SimpleNamespace(get=changed_registry_read), ledger=BudgetLedger(store))
    scheduler.workflow = SimpleNamespace(block_attempt=AsyncMock())
    scheduler._source = AsyncMock(return_value=(tmp_path / 'source', 'a' * 40))
    scheduler._prompt = AsyncMock(return_value='Must not dispatch a revised model profile')
    scheduler._execute_existing = AsyncMock()
    scheduler._context_roots = {('work', 2): tmp_path / 'context'}
    scheduler._wake = asyncio.Event()
    await scheduler._dispatch({'run': run, 'attempt': attempt, 'work_item': work})
    assert observed == [profile.model_profile_id], 'Recovery binding validation must succeed before the simulated race'
    assert (await registry.get(profile.model_profile_id)).revision == 2
    scheduler.workflow.block_attempt.assert_awaited_once()
    blocked = scheduler.workflow.block_attempt.await_args.args
    assert blocked[0] == attempt['id'] and '模型版本已变化' in blocked[1]
    scheduler._prompt.assert_not_awaited()
    scheduler._execute_existing.assert_not_awaited()
    assert await store.list('task_authorization') == []
    assert await store.list('dispatch_context') == []
    assert await store.list('budget_account') == []

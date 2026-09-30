"""Current output allowances affect new dispatches, with no live configuration/model calls."""
import asyncio
import hashlib
import json
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio

from agentflow.application import Application
from agentflow.common import DomainError, canonical_digest
from agentflow.configuration import load_configuration
from agentflow.control.scheduler import Scheduler
from agentflow.control.service import WorkflowService
from agentflow.models.profiles import ModelProfile, ModelRegistry, PricingPolicy
from agentflow.models.service import ModelService
from agentflow.runtime.contracts import LaunchSpec
from agentflow.runtime.supervisor import Supervisor
from agentflow.storage import LocalArtifactStore, Store


def write_file(env, *, roles=16384, coding=16384, provider='openai_compatible',
               base_url='https://models.example.invalid/v1', model='same-model', effort='high', port=8787):
    lines = ['[app]', 'data_dir = ' + json.dumps(str(env.data_dir)), f'port = {port}']
    for role, cap in [('roles', roles), ('coding', coding)]:
        lines.extend([f'[models.{role}]', f'provider = {json.dumps(provider)}',
            f'base_url = {json.dumps(base_url)}', f'model = {json.dumps(model)}',
            'api_key = "PRIVATE_CURRENT_CONFIG_KEY"', f'max_output_tokens = {cap}',
            f'reasoning_effort = {json.dumps(effort)}'])
        if provider == 'local_test':
            lines.append('allow_loopback_upstream = true')
    env.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    env.path.write_text('\n'.join(lines) + '\n')
    env.path.chmod(0o600)


def profile(identity='original', *, provider='openai_compatible', base_url='https://models.example.invalid/v1', effort='low'):
    return ModelProfile(model_profile_id=identity, provider=provider, base_url=base_url,
        requested_model='same-model', accepted_api_model='same-model', acceptance_status='accepted',
        protocols=['chat_completions', 'responses'], credential_reference='env:OLD_OUTPUT_TEST_KEY',
        max_output_tokens=16384, reasoning_effort=effort, allowed_tool_names=['read_file'],
        request_timeout_seconds=37, allow_loopback_upstream=provider == 'local_test',
        pricing=PricingPolicy(input_micros_per_million=1_000_000, output_micros_per_million=1_000_000,
            input_token_upper_bound=100, input_bound_verified=True, output_control_verified=True,
            source_version='fixture-price'))


@pytest_asyncio.fixture
async def live_env(tmp_path, monkeypatch):
    env = SimpleNamespace(data_dir=tmp_path / 'state', path=tmp_path / 'private/config.toml', root=tmp_path)
    monkeypatch.setattr('agentflow.configuration.configuration_path', lambda: env.path)
    write_file(env)
    env.configuration = load_configuration()
    env.store = Store(env.data_dir)
    await env.store.start()
    env.registry = ModelRegistry(env.store)
    env.original = profile()
    await env.registry.register(env.original, 'original')
    try:
        yield env
    finally:
        for supervisor in getattr(env, 'fixture_supervisors', []):
            await supervisor.close()
        await env.store.close()


@pytest.mark.parametrize('role,cap', [('roles', 65536), ('coding', 65536), ('roles', 8192), ('coding', 8192)])
async def test_same_route_changes_only_output_allowance_and_keeps_other_frozen_choices(live_env, role, cap):
    env = live_env
    before = await env.store.read('model_profile', env.original.model_profile_id)
    write_file(env, **{role: cap})
    effective, evidence = await env.configuration.resolve_output_profile(env.registry, env.original, role=role)
    assert effective.max_output_tokens == evidence['max_output_tokens'] == cap
    assert evidence['source'] == 'configuration_file' and evidence['role'] == role
    assert evidence['base_profile_id'] == env.original.model_profile_id and evidence['base_profile_revision'] == 1
    ignored = {'model_profile_id', 'revision', 'max_output_tokens'}
    assert effective.model_dump(exclude=ignored) == env.original.model_dump(exclude=ignored)
    assert await env.store.read('model_profile', env.original.model_profile_id) == before
    assert not await env.store.list('product_model_binding') and not await env.store.list('model_invocation')
    assert 'PRIVATE_CURRENT_CONFIG_KEY' not in json.dumps([evidence, await env.store.list('model_profile')])


@pytest.mark.parametrize('changes', [{'model': 'different-model'}, {'base_url': 'https://another.example.invalid/v1'}])
async def test_new_default_route_does_not_change_an_existing_run_model_or_allowance(live_env, changes):
    env = live_env
    write_file(env, roles=65536, coding=65536, **changes)
    effective, evidence = await env.configuration.resolve_output_profile(env.registry, env.original, role='coding')
    assert effective == env.original and evidence['source'] == 'runtime_binding'
    assert len(await env.store.list('model_profile')) == 1


async def test_parallel_resolutions_share_an_immutable_profile_and_future_calls_reload_the_file(live_env):
    env = live_env
    write_file(env, roles=65536, coding=65536)
    rows = await asyncio.gather(*(env.configuration.resolve_output_profile(env.registry, env.original, role='coding') for _ in range(8)))
    assert len({row[0].model_profile_id for row in rows}) == 1
    assert len(await env.store.list('model_profile')) == 2
    saved = rows[0][0]
    write_file(env, coding=8192)
    later, _ = await env.configuration.resolve_output_profile(env.registry, env.original, role='coding')
    assert later.max_output_tokens == 8192 and later.model_profile_id != saved.model_profile_id
    assert (await env.registry.get(saved.model_profile_id)).max_output_tokens == 65536


@pytest.mark.parametrize('changes,required', [({'roles': 65536, 'coding': 65536}, False),
    ({'roles': 8192, 'coding': 65536, 'effort': 'low'}, True),
    ({'roles': 65536, 'coding': 65536, 'port': 8899}, True)])
def test_only_output_allowance_edits_are_exempt_from_restart(live_env, changes, required):
    write_file(live_env, **changes)
    assert live_env.configuration.restart_required() is required


@contextmanager
def upstream():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append({'path': self.path, 'body': body, 'authorization': self.headers.get('Authorization')})
            response = ({'model': body['model'], 'status': 'completed', 'output': [],
                         'usage': {'input_tokens': 2, 'output_tokens': 3}} if self.path.endswith('/responses')
                        else {'model': body['model'], 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'},
                              'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 2, 'completion_tokens': 3}})
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


async def dispatcher(env, selected, *, cost_mode='request_limited'):
    await env.registry.register(selected, 'http-profile')
    app = Application.__new__(Application)
    app.store = env.store
    app.settings = env.configuration.settings
    models = ModelService(env.store, env.data_dir, app.authorize_attempt, lambda _: 'LEGACY_HTTP_TEST_KEY')
    app.models = models
    settings = env.configuration.settings
    workflow = WorkflowService(env.store, LocalArtifactStore(env.data_dir / 'artifacts'), settings)
    runtime = SimpleNamespace(workspaces=SimpleNamespace(metadata=env.root / 'workspace_metadata',
        create_clone=AsyncMock(return_value=env.root / 'workspace')))
    scheduler = Scheduler(workflow, env.store, runtime, models, settings, configuration=env.configuration)
    scheduler._source = AsyncMock(return_value=(env.root / 'source', 'a' * 40))
    scheduler._prompt = AsyncMock(return_value='Isolated output allowance fixture')
    supervisor = Supervisor(env.store, env.data_dir)
    env.fixture_supervisors = [*getattr(env, 'fixture_supervisors', []), supervisor]
    async def start_clocked_fixture(task):
        # The proxy now requires an actually running, identity-bound child.
        # Keep output-cap tests on local HTTP while using real launch evidence.
        workspace = Path(task['workspace'])
        workspace.mkdir(parents=True, exist_ok=True)
        await supervisor.start(LaunchSpec(attempt_id=task['attempt_id'], operation_id=task['attempt_id'],
            run_id=task['run_id'], fencing_token=task['fencing_token'], input_fingerprint=task['input_fingerprint'],
            argv=[sys.executable, '-c', 'import time; time.sleep(30)'], cwd=workspace,
            timeout_seconds=task['deadline_seconds'], stop_grace_seconds=.05,
            backend='local-output-cap-fixture', backend_version='1'))
    scheduler._execute_existing = AsyncMock(side_effect=start_clocked_fixture)
    scheduler._maintain = AsyncMock()
    scheduler._trace_status = AsyncMock()
    limits = {'currency': 'USD', 'limit_micros': 20000, 'cost_mode': cost_mode,
              'max_model_requests': 20, 'max_active_seconds': 30, 'max_tool_calls': 10}
    def seed(tx):
        tx.put('project', 'project', {'local_path': str(env.root / 'source')})
        tx.put('iteration', 'iteration', {'budget_limit': limits})
        return tx.put('run', 'run', {'project_id': 'project', 'iteration_id': 'iteration', 'execution_state': 'running',
            'runtime_bindings': {'coding_model_profile_id': selected.model_profile_id, 'role_model_profile_id': selected.model_profile_id},
            'budget_limit': limits})
    run = await env.store.command('fixture', 'http-run', {}, seed)
    async def claim(role, index, *, recovered=False):
        work_id, attempt_id = f'work-{index}', f'attempt-{index}'
        generation = 2 if recovered else 1
        fingerprint = canonical_digest([role, index])
        def write(tx):
            attempt = tx.put('attempt', attempt_id, {'run_id': 'run', 'iteration_id': 'iteration', 'work_item_id': work_id,
                'generation': generation, 'fencing_token': generation, 'input_fingerprint': fingerprint, 'status': 'running'})
            payload = {}
            if recovered:
                binding = {'recovery_id': 'recovery', 'run_id': 'run', 'iteration_id': 'iteration', 'work_item_id': work_id,
                    'generation': generation, 'model_profile_id': selected.model_profile_id, 'profile_revision': 1}
                tx.put('run_recovery', 'recovery', {'mode': 'retry', 'actor': 'owner', 'run_id': 'run', 'iteration_id': 'iteration',
                    'affected_work_item_ids': [work_id], 'model_profile_bindings': {work_id: {**binding, 'payload_digest': canonical_digest(binding)}}})
                payload['recovery_model_binding'] = binding
            work = tx.put('work_item', work_id, {'run_id': 'run', 'project_id': 'project', 'generation': generation,
                'step': 'implementation' if role == 'coding' else 'research', 'role': 'development' if role == 'coding' else 'research',
                'status': 'running', 'fencing_token': generation, 'input_fingerprint': fingerprint,
                'attempt_id': attempt_id, 'payload': payload, 'write_paths': [f'file-{index}.py'] if role == 'coding' else []})
            return {'run': run, 'attempt': attempt, 'work_item': work}
        result = await env.store.command('fixture', str(uuid4()), {}, write)
        scheduler._context_roots[(work_id, generation)] = env.root / 'context'
        return result
    return scheduler, models, claim


@pytest.mark.parametrize('role', ['coding', 'roles'])
async def test_dispatch_and_actual_http_proxy_use_current_cap_while_old_dispatch_and_replay_stay_frozen(live_env, role):
    env = live_env
    with upstream() as (url, calls):
        selected = profile('http-base', provider='local_test', base_url=url, effort='low' if role == 'coding' else None)
        write_file(env, provider='local_test', base_url=url)
        scheduler, models, claim = await dispatcher(env, selected)
        scheduler.settings = scheduler.settings.model_copy(update={'max_role_iterations': 1200,
            'agent_max_log_bytes': 2 * 1024 * 1024})
        try:
            await scheduler._dispatch(await claim(role, 'old'))
            old_task = scheduler._execute_existing.await_args.args[0]
            old_frozen = await env.store.read('dispatch_context', old_task['attempt_id'])
            old_auth = await env.store.read('task_authorization', hashlib.sha256(old_task['task_token'].encode()).hexdigest())
            run = await env.store.read('run', 'run')
            write_file(env, provider='local_test', base_url=url, roles=65536, coding=65536)
            await scheduler._dispatch(await claim(role, 'new'))
            new_task = scheduler._execute_existing.await_args.args[0]
            new_auth = await env.store.read('task_authorization', hashlib.sha256(new_task['task_token'].encode()).hexdigest())
            assert old_task['max_output_tokens'] == old_auth['max_output_tokens'] == 16384
            assert new_task['max_output_tokens'] == new_auth['max_output_tokens'] == 65536
            assert new_task['output_configuration']['base_profile_id'] == selected.model_profile_id
            assert new_task['max_log_bytes'] == 2 * 1024 * 1024
            if role == 'roles':
                from typing import Annotated

                from pydantic import TypeAdapter

                from agentflow.runtime.contracts import TaskEnvelope
                assert new_task['max_iterations'] == 1200
                field = TaskEnvelope.model_fields['max_iterations']
                assert TypeAdapter(Annotated[field.annotation, *field.metadata]).validate_python(
                    new_task['max_iterations']) == 1200
            if role == 'coding':
                assert new_task['coding_step']['max_output_tokens'] == 65536
                assert new_task['reasoning_effort'] == new_auth['reasoning_effort'] == 'low'
            protocol = 'responses' if role == 'coding' else 'chat_completions'
            body = {'model': 'same-model', **({'input': 'fixture'} if role == 'coding'
                    else {'messages': [{'role': 'user', 'content': 'fixture'}]})}
            await models.forward(protocol, body, old_task['task_token'], 'old-call')
            await models.forward(protocol, body, new_task['task_token'], 'new-call')
            budget = await env.store.list('budget_account')
            await models.forward(protocol, body, old_task['task_token'], 'old-call')
            wire_field = 'max_output_tokens' if role == 'coding' else 'max_tokens'
            assert [call['body'][wire_field] for call in calls] == [16384, 65536]
            assert all(call['authorization'] == 'Bearer LEGACY_HTTP_TEST_KEY' for call in calls)
            if role == 'coding':
                assert all(call['body']['reasoning']['effort'] == 'low' for call in calls)
            assert await env.store.list('budget_account') == budget
            assert await env.store.read('dispatch_context', old_task['attempt_id']) == old_frozen
            assert await env.store.read('task_authorization', old_auth['id']) == old_auth
            assert await env.store.read('run', 'run') == run
        finally:
            await models.close()


async def test_derived_profile_cannot_hide_a_recovery_base_revision_race(live_env, monkeypatch):
    env = live_env
    selected = profile('recovery-base')
    write_file(env, coding=65536)
    scheduler, models, claim = await dispatcher(env, selected)
    original_register = models.registry.register
    changed = False
    async def revise_base_during_derivation(value, key, **kwargs):
        nonlocal changed
        result = await original_register(value, key, **kwargs)
        if value.model_profile_id != selected.model_profile_id and not changed:
            changed = True
            await original_register(selected.model_copy(update={'reasoning_effort': 'high'}),
                'concurrent-profile-revision', expected_revision=1)
        return result
    monkeypatch.setattr(models.registry, 'register', revise_base_during_derivation)
    try:
        await scheduler._dispatch(await claim('coding', 'race', recovered=True))
        assert changed
        scheduler._execute_existing.assert_not_awaited()
        assert not await env.store.list('dispatch_context') and not await env.store.list('task_authorization')
        assert (await env.store.read('work_item', 'work-race'))['status'] == 'blocked'
        assert not await env.store.list('model_invocation')
    finally:
        await models.close()


async def test_higher_configured_cap_does_not_expand_the_monetary_budget(live_env):
    env = live_env
    with upstream() as (url, calls):
        selected = profile('strict-base', provider='local_test', base_url=url)
        write_file(env, provider='local_test', base_url=url, coding=65536)
        scheduler, models, claim = await dispatcher(env, selected, cost_mode='strict')
        try:
            await scheduler._dispatch(await claim('coding', 'strict'))
            task = scheduler._execute_existing.await_args.args[0]
            before = await env.store.list('budget_account')
            with pytest.raises(DomainError) as error:
                await models.forward('responses', {'model': 'same-model', 'input': 'fixture'}, task['task_token'])
            assert error.value.code == 'budget_exceeded'
            assert calls == [] and await env.store.list('budget_account') == before
        finally:
            await models.close()


async def test_research_dispatch_freezes_default_enable_and_respects_explicit_disable(live_env):
    scheduler, models, claim = await dispatcher(live_env, profile('research-policy'))
    try:
        await scheduler._dispatch(await claim('roles', 'public-default'))
        public_task = scheduler._execute_existing.await_args.args[0]
        assert public_task['allow_public_web'] is True and public_task['allowed_web_hosts'] == []
        scheduler.settings = scheduler.settings.model_copy(update={
            'research_public_web_enabled': False, 'research_web_hosts': ['example.com']})
        await scheduler._dispatch(await claim('roles', 'research-disabled'))
        disabled_task = scheduler._execute_existing.await_args.args[0]
        assert disabled_task['allow_public_web'] is False
        frozen = await live_env.store.read('dispatch_context', disabled_task['attempt_id'])
        scheduler.settings = scheduler.settings.model_copy(update={'research_public_web_enabled': True})
        await scheduler._dispatch(await claim('roles', 'research-enabled'))
        assert scheduler._execute_existing.await_args.args[0]['allow_public_web'] is True
        assert await live_env.store.read('dispatch_context', disabled_task['attempt_id']) == frozen
        await scheduler._dispatch(await claim('coding', 'no-research-for-coding'))
        assert scheduler._execute_existing.await_args.args[0]['allow_public_web'] is False
    finally:
        await models.close()

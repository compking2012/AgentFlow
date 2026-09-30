"""Execution expiry uses sealed child time, not time spent preparing the worker."""
import asyncio
import hashlib
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agentflow.application import Application
from agentflow.common import DomainError
from agentflow.models.profiles import ModelRegistry
from agentflow.models.service import ModelService, create_model_router
from agentflow.runtime.contracts import LaunchSpec
from agentflow.runtime.process_identity import current_boot_identity
from agentflow.runtime.supervisor import Supervisor
from agentflow.settings import Settings


def test_expiry_is_distinct_from_revoked_identity(context):
    expired = context.model_copy(update={'expires_at': (datetime.now(UTC) - timedelta(seconds=1)).isoformat()})
    with pytest.raises(DomainError) as error:
        expired.assert_current('responses')
    assert error.value.code == 'task_authorization_expired'
    assert error.value.details['expires_at'] == expired.expires_at


def test_monotonic_execution_expiry_ignores_wall_clock_adjustment(context):
    source, boot = current_boot_identity()
    active = context.model_copy(update={'expires_at': '2000-01-01T00:00:00+00:00',
        'deadline_monotonic': time.monotonic() + 10,
        'deadline_boot_identity': {'boot_identity_source': source, 'boot_fingerprint': boot}})
    active.assert_current('responses')
    expired = active.model_copy(update={'expires_at': '2999-01-01T00:00:00+00:00', 'deadline_monotonic': time.monotonic() - 1})
    with pytest.raises(DomainError) as error:
        expired.assert_current('responses')
    assert error.value.code == 'task_authorization_expired'


async def test_pre_auth_expiry_returns_closed_policy_header_without_upstream_or_usage(store, tmp_path, context):
    async def authorize(token, protocol):
        raise DomainError('task_authorization_expired', 'Execution authorization expired', 403,
                          {'expires_at': context.expires_at})
    service = ModelService(store, tmp_path, authorize, lambda _: 'not-used')
    app = FastAPI()
    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse({'error': {'code': error.code, 'message': error.message}}, status_code=error.status)
    app.include_router(create_model_router(service))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://controller') as client:
            response = await client.post('/internal/v1/llm/responses', headers={'Authorization': 'Bearer test'}, json={})
        assert response.status_code == 403
        assert response.headers['X-AgentFlow-Failure-Code'] == 'task_authorization_expired'
        assert response.json()['error']['type'] == 'agentflow_policy_error'
        assert not await store.list('model_invocation') and not await store.list('model_attempt_budget')
    finally:
        await service.close()


@pytest.mark.parametrize('late', [False, True], ids=['delayed-within-startup-window', 'startup-window-expired'])
async def test_delayed_launch_gets_only_actual_short_execution_window(store, tmp_path, context, profile_factory, http_stub, late):
    from agentflow.runtime.task_authorization import authorization_window
    settings = Settings(data_dir=tmp_path)
    app = Application.__new__(Application)
    app.store, app.settings = store, settings
    app.models = SimpleNamespace(registry=ModelRegistry(store))
    profile = await app.models.registry.register(profile_factory('http://127.0.0.1:1'), 'profile')
    duration = 0.7
    token = 'deadline-fixture-token'
    authority = {**context.model_dump(), **authorization_window(duration, startup_seconds=.05 if late else 5),
                 'expected_profile_revision': profile['revision']}
    def seed(tx):
        tx.put('run', context.run_id, {'execution_state': 'running'})
        tx.put('attempt', context.attempt_id, {'work_item_id': 'work', 'run_id': context.run_id,
            'iteration_id': context.iteration_id, 'generation': 1, 'status': 'running',
            'fencing_token': context.fencing_token, 'input_fingerprint': context.input_fingerprint})
        tx.put('work_item', 'work', {'attempt_id': context.attempt_id, 'status': 'running',
            'run_id': context.run_id, 'generation': 1, 'fencing_token': context.fencing_token,
            'input_fingerprint': context.input_fingerprint})
        tx.put('dispatch_context', context.attempt_id, {'task': {'attempt_id': context.attempt_id,
            'deadline_seconds': duration, 'run_id': context.run_id, 'work_item_id': 'work',
            'fencing_token': context.fencing_token, 'input_fingerprint': context.input_fingerprint}})
        return tx.put('task_authorization', hashlib.sha256(token.encode()).hexdigest(), authority)
    await store.command('fixture', 'deadline', {}, seed)
    with pytest.raises(DomainError):
        await app.authorize_attempt(token, 'responses')
    # Longer than the complete execution allowance, entirely before child start.
    await asyncio.sleep(.1 if late else 1.05)
    loop = asyncio.get_running_loop()
    observed = []
    def respond(call):
        try:
            authorized = asyncio.run_coroutine_threadsafe(app.authorize_attempt(token, 'responses'), loop).result(3)
            observed.append(authorized)
            return 200, {'Content-Type': 'application/json'}, b'{}'
        except DomainError as error:
            return error.status, {'Content-Type': 'application/json'}, json.dumps({'code': error.code}).encode()
    supervisor = Supervisor(store, tmp_path)
    try:
        with http_stub(respond) as (url, requests):
            script = ('import time,urllib.request;time.sleep(.1);'
                f'print(urllib.request.urlopen(urllib.request.Request({url!r}, data=b"{{}}", method="POST")).status,flush=True);'
                'time.sleep(10)')
            await supervisor.start(LaunchSpec(attempt_id=context.attempt_id, operation_id='execution-clock',
                run_id=context.run_id, input_fingerprint=context.input_fingerprint, fencing_token=context.fencing_token,
                argv=[sys.executable, '-c', script], cwd=tmp_path, timeout_seconds=duration, stop_grace_seconds=.05,
                backend='fixture', backend_version='1'))
            handle = await supervisor.wait(context.attempt_id)
            if late:
                from agentflow.runtime.task_authorization import authorization_failure
                assert len(requests) == 1 and not observed
                evidence = (await store.list('task_authorization_expiry'))[0]
                assert evidence['deadline_evidence']['deadline_kind'] == 'startup'
                expected = datetime.fromisoformat(authority['expires_at']) - timedelta(seconds=duration)
                assert datetime.fromisoformat(evidence['expires_at']) == expected
                assert await authorization_failure(store, tmp_path, context.attempt_id,
                    fencing_token=context.fencing_token, input_fingerprint=context.input_fingerprint) == 'task_authorization_expired'
                assert handle.state == 'failed' and handle.reason is None
                assert not await store.list('model_invocation')
                return
            assert len(requests) == len(observed) == 1
            assert observed[0].deadline_monotonic is not None
            assert handle.state == 'failed' and handle.reason == 'timeout'
            child = json.loads((Path(handle.stdout_path).parent / 'child.json').read_text())
            assert child['execution_deadline_monotonic'] - child['execution_started_monotonic'] == pytest.approx(duration)
            assert observed[0].deadline_monotonic == child['execution_deadline_monotonic']
            assert handle.active_seconds < duration + 1
            with pytest.raises(DomainError):
                await app.authorize_attempt(token, 'responses')
            def stale_store(tx):
                row = tx.get('supervised_attempt', context.attempt_id)
                return tx.put('supervised_attempt', row['id'], {**row, 'state': 'running'}, row['revision'])
            await store.command('fixture', 'stale-terminal-state', {}, stale_store)
            with pytest.raises(DomainError) as stopped:
                await app.authorize_attempt(token, 'responses')
            assert stopped.value.code == 'stale_task_token'
            assert not await store.list('task_authorization_expiry')
    finally:
        await supervisor.close()


async def test_expiry_denial_records_identity_without_model_spend(store, tmp_path, context, profile_factory):
    from agentflow.runtime.task_authorization import authorization_failure
    app = Application.__new__(Application)
    app.store, app.settings = store, Settings(data_dir=tmp_path)
    app.models = SimpleNamespace(registry=ModelRegistry(store))
    profile = await app.models.registry.register(profile_factory('http://127.0.0.1:1'), 'profile')
    token = 'expired-task-fixture'
    expired = context.model_copy(update={'expires_at': (datetime.now(UTC) - timedelta(seconds=1)).isoformat()})
    def seed(tx):
        tx.put('run', context.run_id, {'execution_state': 'running'})
        tx.put('attempt', context.attempt_id, {'run_id': context.run_id, 'iteration_id': context.iteration_id,
            'work_item_id': 'work', 'generation': 1, 'status': 'running', 'fencing_token': context.fencing_token,
            'input_fingerprint': context.input_fingerprint})
        tx.put('work_item', 'work', {'run_id': context.run_id, 'attempt_id': context.attempt_id, 'status': 'running',
            'fencing_token': context.fencing_token, 'input_fingerprint': context.input_fingerprint})
        return tx.put('task_authorization', hashlib.sha256(token.encode()).hexdigest(),
                      {**expired.model_dump(), 'expected_profile_revision': profile['revision']})
    authority = await store.command('fixture', 'expired', {}, seed)
    for _ in range(2):
        with pytest.raises(DomainError) as error:
            await app.authorize_attempt(token, 'responses')
        assert error.value.code == 'task_authorization_expired'
    rows = await store.list('task_authorization_expiry')
    assert len(rows) == 1 and rows[0]['authorization_digest']
    assert rows[0]['authorization_id'] == authority['id']
    assert rows[0]['attempt_id'] == context.attempt_id
    assert token not in json.dumps(rows)
    assert not await store.list('model_invocation') and not await store.list('model_attempt_budget')
    assert await authorization_failure(store, tmp_path, context.attempt_id,
        fencing_token=context.fencing_token, input_fingerprint=context.input_fingerprint) == 'task_authorization_expired'
    assert await authorization_failure(store, tmp_path, context.attempt_id,
        fencing_token=context.fencing_token + 1, input_fingerprint=context.input_fingerprint) is None


async def test_expiry_record_survives_normal_deadline_close_race(store, tmp_path, context):
    from agentflow.runtime.task_authorization import record_expiry
    expired = context.model_copy(update={'expires_at': '2000-01-01T00:00:00+00:00'})
    def seed(tx):
        tx.put('attempt', context.attempt_id, {'run_id': context.run_id, 'work_item_id': 'work',
            'iteration_id': context.iteration_id, 'generation': 1, 'status': 'running',
            'fencing_token': context.fencing_token, 'input_fingerprint': context.input_fingerprint})
        tx.put('work_item', 'work', {'attempt_id': context.attempt_id, 'status': 'running',
            'fencing_token': context.fencing_token, 'input_fingerprint': context.input_fingerprint})
        return tx.put('task_authorization', 'authority', expired.model_dump())
    raw = await store.command('fixture', 'expiry-race', {}, seed)
    attempt, work = await store.read('attempt', context.attempt_id), await store.read('work_item', 'work')
    def stopped(tx):
        for kind, identity in [('attempt', context.attempt_id), ('work_item', 'work')]:
            item = tx.get(kind, identity)
            tx.put(kind, identity, {**item, 'status': 'failed'}, item['revision'])
        return {}
    await store.command('fixture', 'deadline-worker-closed', {}, stopped)
    result = await record_expiry(store, raw, expired, attempt, work, 'responses')
    assert result['runtime_failure_code'] == 'task_authorization_expired'
    assert result['attempt_id'] == context.attempt_id and result['fencing_token'] == context.fencing_token
    assert len(await store.list('task_authorization_expiry')) == 1
    assert (await store.read('attempt', context.attempt_id))['status'] == 'failed'
    assert await record_expiry(store, raw, expired, attempt, work, 'responses') == result


async def test_unexpired_context_cannot_create_expiry_evidence(store, tmp_path, context):
    from agentflow.runtime.task_authorization import record_expiry
    def seed(tx):
        tx.put('attempt', context.attempt_id, {'run_id': context.run_id, 'iteration_id': context.iteration_id,
            'work_item_id': 'work', 'generation': 1, 'status': 'running', 'fencing_token': context.fencing_token,
            'input_fingerprint': context.input_fingerprint})
        tx.put('work_item', 'work', {'attempt_id': context.attempt_id, 'status': 'running',
            'fencing_token': context.fencing_token, 'input_fingerprint': context.input_fingerprint})
        return tx.put('task_authorization', 'authority', context.model_dump())
    raw = await store.command('fixture', 'future-authority', {}, seed)
    with pytest.raises(DomainError):
        await record_expiry(store, raw, context, await store.read('attempt', context.attempt_id),
                            await store.read('work_item', 'work'), 'responses')
    assert not await store.list('task_authorization_expiry')


async def test_legacy_expiry_audit_does_not_round_before_the_deadline(store, tmp_path, context, monkeypatch):
    import agentflow.models.profiles as profiles
    import agentflow.runtime.task_authorization as authorization
    now = datetime.now(UTC).replace(microsecond=500500)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(profiles, 'datetime', Clock)
    monkeypatch.setattr(authorization, 'datetime', Clock)
    import agentflow.common as common
    monkeypatch.setattr(common, 'datetime', Clock)
    expired = context.model_copy(update={'expires_at': now.replace(microsecond=500250).isoformat()})
    def seed(tx):
        tx.put('attempt', context.attempt_id, {'run_id': context.run_id, 'iteration_id': context.iteration_id,
            'work_item_id': 'work', 'generation': 1, 'status': 'failed', 'fencing_token': context.fencing_token,
            'input_fingerprint': context.input_fingerprint})
        tx.put('work_item', 'work', {'id': 'work'})
        return tx.put('task_authorization', 'authority', expired.model_dump())
    raw = await store.command('fixture', 'precise-expiry', {}, seed)
    await authorization.record_expiry(store, raw, expired, await store.read('attempt', context.attempt_id),
                                     await store.read('work_item', 'work'), 'responses')
    assert await authorization.authorization_failure(store, tmp_path, context.attempt_id,
        fencing_token=context.fencing_token, input_fingerprint=context.input_fingerprint) == 'task_authorization_expired'

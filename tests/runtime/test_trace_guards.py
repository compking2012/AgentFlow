"""Independent trace privacy, cancellation, corruption and API-boundary checks."""
import asyncio
import base64
import json
import subprocess
import sys
import zlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_execution_trace import contents
from test_execution_trace import trace as trace

from agentflow.common import DomainError
from agentflow.control.api import create_app
from agentflow.models.profiles import AttemptContext, ModelProfile
from agentflow.models.service import ModelService
from agentflow.runtime.trace import ModelTrace, public_value
from agentflow.settings import Settings


async def test_multiline_sensitive_value_remains_private_across_flush_boundaries(trace):
    model = ModelTrace(trace, 'attempt', 'call', 'model', 'responses', ())
    model.observe({'type': 'response.output_text.delta', 'item_id': 'message',
                   'delta': '{"password": "PRIVATE_FIRST_LINE\n'})
    await model.flush()
    model.observe({'type': 'response.output_text.delta', 'item_id': 'message',
                   'delta': 'PRIVATE_SECOND_LINE"}\n'})
    await model.flush()
    await model.finish('completed')
    public = json.dumps(await contents(trace))
    assert 'PRIVATE_FIRST_LINE' not in public
    assert 'PRIVATE_SECOND_LINE' not in public


@pytest.mark.parametrize('value', ['Authorization: Basic PRIVATE_BASIC_VALUE',
    'Authorization=PRIVATE_AUTHORIZATION_VALUE', 'refresh_token=PRIVATE_REFRESH_VALUE',
    'https://example.test/path?token=PRIVATE_TOKEN&mode=read',
    'http://localhost/#/bootstrap?bootstrap_token=PRIVATE_BOOTSTRAP&mode=read',
    'https://example.test/?bootstrap=PRIVATE_BOOTSTRAP',
    'postgresql://user:PRIVATE_DATABASE_PASSWORD@example.test/db',
    'redis://:PRIVATE_DATABASE_PASSWORD@example.test/db'])
def test_labeled_credentials_in_text_are_redacted(value):
    assert 'PRIVATE_' not in public_value(value)


async def test_unfinished_sensitive_value_is_hidden_at_end_of_stream(trace):
    model = ModelTrace(trace, 'attempt', 'call', 'model', 'responses', ())
    model.observe({'type': 'response.output_text.delta', 'item_id': 'message',
                   'delta': '公开说明。\n{"password": "PRIVATE_UNFINISHED\n'})
    await model.flush()
    partial = json.dumps(await contents(trace), ensure_ascii=False)
    assert '公开说明' in partial and 'PRIVATE_UNFINISHED' not in partial
    await model.finish('incomplete')
    public = json.dumps(await contents(trace), ensure_ascii=False)
    assert 'PRIVATE_UNFINISHED' not in public and '已隐藏' in public


def test_one_megabyte_without_word_boundaries_has_bounded_scan_time():
    # A subprocess timeout also bounds a regression in a synchronous regex;
    # an event-loop timeout cannot interrupt pathological regex backtracking.
    subprocess.run([sys.executable, '-c',
        "from agentflow.runtime.trace import public_value; s='x'*1048576; assert public_value(s)==s"],
        check=True, timeout=3, capture_output=True)


@pytest.mark.parametrize('raw', [b'\xff', zlib.compress(b'valid') + b'junk'])
async def test_invalid_utf8_or_trailing_compressed_data_returns_controlled_error(trace, raw):
    await trace.emit('attempt', 'status', 'fixture', 'ok')
    encoded = raw if raw.endswith(b'junk') else zlib.compress(raw)
    def corrupt(tx):
        row = tx.get('execution_trace', 'attempt/000000000001')
        return tx.put('execution_trace', row['id'], {**row,
            'content_zlib': base64.b64encode(encoded).decode()}, row['revision'])
    await trace.store.command('fixture.corrupt', str(uuid4()), {}, corrupt)
    with pytest.raises(DomainError) as error:
        await trace.page('attempt')
    assert error.value.code == 'trace_corrupt'


@pytest.mark.parametrize('field,value', [('attempt_id', 'another-attempt'), ('run_id', 'another-run'),
    ('work_item_id', 'another-work'), ('seq', 0)])
async def test_trace_record_identity_and_sequence_match_the_requested_attempt(trace, field, value):
    await trace.emit('attempt', 'status', 'fixture', 'ok')
    def corrupt(tx):
        row = tx.get('execution_trace', 'attempt/000000000001')
        return tx.put('execution_trace', row['id'], {**row, field: value}, row['revision'])
    await trace.store.command('fixture.corrupt', str(uuid4()), {}, corrupt)
    with pytest.raises(DomainError) as error:
        await trace.page('attempt', after=0)
    assert error.value.code == 'trace_corrupt'


async def proxy_fixture(store, tmp_path, handler):
    context = AttemptContext(attempt_id='guard-attempt', run_id='guard-run', iteration_id='guard-iteration',
        model_profile_id='guard-profile', fencing_token=1, input_fingerprint='sha256:' + 'a' * 64,
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        max_model_requests=10, max_output_tokens=64, cost_mode='request_limited')
    profile = ModelProfile(model_profile_id='guard-profile', provider='local_test', requested_model='guard-model',
        accepted_api_model='guard-model', acceptance_status='accepted', base_url='http://127.0.0.1:12345/v1',
        protocols=['responses', 'chat_completions'], credential_reference='unused-fixture', allow_loopback_upstream=True)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    service = ModelService(store, tmp_path, lambda *_: context, lambda *_: 'PRIVATE_PROVIDER', client)
    await service.registry.register(profile, 'profile')
    await service.ledger.setup_accounts(context.run_id, context.iteration_id, 10000, 10000)
    return service, client


async def test_cancellation_during_request_trace_releases_the_unsent_invocation(store, tmp_path):
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(500, json={})
    service, client = await proxy_fixture(store, tmp_path, handler)
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked_trace(*args, **kwargs):
        entered.set()
        await release.wait()
    service.traces = SimpleNamespace(emit=blocked_trace)
    task = asyncio.create_task(service.forward('responses', {'model': 'guard-model', 'input': 'fixture'}, 'task-fixture'))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        invocation = (await store.list('model_invocation'))[0]
        assert calls == []
        assert invocation['state'] == 'released'
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await service.close()
        await client.aclose()


@pytest.mark.parametrize('stream', [False, True])
async def test_trace_failure_keeps_response_budget_and_idempotent_replay(store, tmp_path, stream):
    calls = []
    payload = {'id': 'resp-fixture', 'status': 'completed', 'model': 'guard-model', 'output': [],
               'usage': {'input_tokens': 2, 'output_tokens': 3}}
    wire = ('data: ' + json.dumps({'type': 'response.completed', 'response': payload}) + '\n\n').encode()
    async def handler(request):
        calls.append(request)
        return (httpx.Response(200, content=wire, headers={'content-type': 'text/event-stream'}) if stream
                else httpx.Response(200, json=payload))
    service, client = await proxy_fixture(store, tmp_path, handler)
    async def failed_trace(*args, **kwargs):
        raise RuntimeError('isolated trace failure')
    service.traces = SimpleNamespace(emit=failed_trace)
    async def response_bytes(response):
        return b''.join([chunk async for chunk in response.body_iterator]) if stream else response.body
    try:
        request = {'model': 'guard-model', 'input': 'fixture', 'stream': stream}
        result = await response_bytes(await service.forward('responses', request, 'task-fixture', 'same'))
        before = await store.list('budget_account')
        replay = await response_bytes(await service.forward('responses', request, 'task-fixture', 'same'))
        assert (result == replay) if stream else (json.loads(result) == json.loads(replay))
        assert result == wire if stream else json.loads(result) == payload
        assert len(calls) == 1 and await store.list('budget_account') == before
        assert (await store.list('model_invocation'))[0]['state'] == 'completed_unpriced'
    finally:
        await service.close()
        await client.aclose()


async def test_trace_routes_require_owner_scope_and_reject_cross_run_work(trace, tmp_path):
    settings = Settings(data_dir=tmp_path / 'api')
    app = create_app(settings, store=trace.store)
    owner = app.state.tokens.exchange(app.state.tokens.bootstrap_code)
    scoped = app.state.tokens.issue('agentflow_attempt', {'model:invoke'}, 'attempt', 60)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=settings.origin) as client:
        for path in ('/api/v1/attempts/attempt/trace', '/api/v1/runs/run/work_items/work/attempts'):
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers={'Authorization': 'Bearer ' + scoped})).status_code in {401, 403}
            response = await client.get(path, headers={'Authorization': 'Bearer ' + owner})
            assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        mismatch = await client.get('/api/v1/runs/other/work_items/work/attempts', headers={'Authorization': 'Bearer ' + owner})
        assert mismatch.status_code == 404


async def test_page_byte_limit_cursors_do_not_skip_rows_in_either_direction(trace):
    for index in range(70):
        await trace.emit('attempt', 'status', str(index), 'x ' * 8192)
    forward, after = [], 0
    while True:
        page = await trace.page('attempt', after=after, limit=100)
        assert sum(len(row['content'].encode()) for row in page['items']) <= 512 * 1024
        forward.extend(row['seq'] for row in page['items'])
        if not page['has_more_after']:
            break
        after = page['next_after']
    backward, before = [], None
    while True:
        page = await trace.page('attempt', before=before, limit=100)
        assert sum(len(row['content'].encode()) for row in page['items']) <= 512 * 1024
        backward = [row['seq'] for row in page['items']] + backward
        if not page['has_more_before']:
            break
        before = page['next_before']
    assert forward == backward == list(range(1, 71))

import json

import pytest
from test_proxy import make_service


async def seed_attempt(store, context):
    def seed(tx):
        tx.put('attempt', context.attempt_id, {'run_id': context.run_id, 'work_item_id': 'work',
            'status': 'running', 'generation': 1, 'started_at': '2026-09-23T00:00:00+00:00'})
        return {}
    await store.command('fixture', 'attempt', {}, seed)


@pytest.mark.parametrize('stream', [False, True])
async def test_visible_model_trace_retains_exact_transport_and_accounting(stream, store, tmp_path, context, http_stub, profile_factory):
    await seed_attempt(store, context)
    response = {'id': 'response', 'model': 'fixture-model', 'status': 'completed',
        'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'visible response'}]}],
        'usage': {'input_tokens': 3, 'output_tokens': 4}}
    chunks = [b'data: {"type":"response.reasoning_text.delta","delta":"HIDDEN_CHAIN"}\n\n',
              b'data: {"type":"response.output_text.delta","delta":"visible response"}\n\n',
              ('data: ' + json.dumps({'type': 'response.completed', 'response': response}) + '\n\n').encode()]
    content = chunks if stream else json.dumps(response).encode()
    with http_stub(lambda _: (200, {'Content-Type': 'text/event-stream' if stream else 'application/json'}, content)) as (url, calls):
        service = await make_service(store, tmp_path, context, profile_factory(url))
        try:
            body = {'model': 'fixture-model', 'instructions': 'check src/api.mjs; secret="upstream-only-key"',
                'input': [{'type': 'reasoning', 'summary': [{'text': 'HIDDEN_INPUT_CHAIN'}]},
                          {'role': 'user', 'content': 'implement the feature'}], 'stream': stream}
            result = await service.forward('responses', body, 'task-only-token', 'trace-request')
            raw = b''.join([x async for x in result.body_iterator]) if stream else result.body
            assert raw == b''.join(chunks) if stream else json.loads(raw) == response
            page = await service.traces.page(context.attempt_id, after=0, limit=100)
            text = json.dumps(page, ensure_ascii=False)
            assert 'src/api.mjs' in text and 'visible response' in text
            for hidden in ('upstream-only-key', 'task-only-token', 'HIDDEN_CHAIN', 'HIDDEN_INPUT_CHAIN'):
                assert hidden not in text
            before = await service.traces.page(context.attempt_id, after=0, limit=100)
            replay = await service.forward('responses', body, 'task-only-token', 'trace-request')
            if stream:
                assert b''.join([x async for x in replay.body_iterator]) == raw
            assert await service.traces.page(context.attempt_id, after=0, limit=100) == before
            assert len(calls) == 1
            assert (await service.ledger.snapshot('run', context.run_id))['settled_micros'] == 7
        finally:
            await service.close()


async def test_trace_failure_does_not_fail_or_repeat_a_model_request(store, tmp_path, context, http_stub, profile_factory, monkeypatch):
    await seed_attempt(store, context)
    response = {'id': 'response', 'model': 'fixture-model', 'status': 'completed', 'output': [],
                'usage': {'input_tokens': 3, 'output_tokens': 4}}
    with http_stub(lambda _: (200, {'Content-Type': 'application/json'}, json.dumps(response).encode())) as (url, calls):
        service = await make_service(store, tmp_path, context, profile_factory(url))
        async def broken(*args, **kwargs):
            raise OSError('fixture logging unavailable')
        monkeypatch.setattr(service.traces, 'emit', broken)
        try:
            result = await service.forward('responses', {'model': 'fixture-model', 'input': 'fixture'}, 'task-only-token')
            assert json.loads(result.body) == response and len(calls) == 1
            assert (await service.ledger.snapshot('run', context.run_id))['settled_micros'] == 7
        finally:
            await service.close()


async def test_authority_is_rechecked_after_trace_yields_before_http_send(store, tmp_path, context, http_stub, profile_factory, monkeypatch):
    from agentflow.common import DomainError
    await seed_attempt(store, context)
    with http_stub(lambda _: (500, {'Content-Type': 'application/json'}, b'{}')) as (url, calls):
        service = await make_service(store, tmp_path, context, profile_factory(url))
        original = service.traces.emit
        async def revise_during_trace(*args, **kwargs):
            await original(*args, **kwargs)
            async def revoked(*_):
                raise DomainError('stale_task_token', 'Fixture authority revoked', 403)
            monkeypatch.setattr(service, 'authorize_attempt', revoked)
        monkeypatch.setattr(service.traces, 'emit', revise_during_trace)
        try:
            with pytest.raises(DomainError) as error:
                await service.forward('responses', {'model': 'fixture-model', 'input': 'fixture'}, 'task-only-token')
            assert error.value.code == 'stale_task_token'
            assert calls == []
            assert (await store.list('model_invocation'))[0]['state'] == 'released'
        finally:
            await service.close()

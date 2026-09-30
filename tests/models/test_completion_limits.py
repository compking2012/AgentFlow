"""Known truncation is not a usable tool response; usage and raw proof remain durable."""
import asyncio
import copy
import json
import threading
import time
from pathlib import Path

import pytest
from test_proxy import make_service

from agentflow.common import DomainError
from agentflow.models.provider import ModelProvider, ResponseTracker


def request(protocol, *, stream=False):
    body = {'model': 'fixture-model', 'stream': stream}
    return body | ({'messages': [{'role': 'user', 'content': 'fixture only'}]} if protocol == 'chat_completions'
                   else {'input': 'fixture only'})


def truncated(protocol, *, reason='max_output_tokens'):
    if protocol == 'chat_completions':
        return {'id': 'limited-chat', 'model': 'fixture-model', 'choices': [{'index': 0,
            'finish_reason': 'length' if reason == 'max_output_tokens' else 'content_filter',
            'message': {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'incomplete-finish',
                'type': 'function', 'function': {'name': 'finish', 'arguments': '{"result":{"summary":"looks-complete"}}'}}]}}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 64, 'completion_tokens_details': {'reasoning_tokens': 60}}}
    return {'id': 'limited-response', 'model': 'fixture-model', 'status': 'incomplete',
        'incomplete_details': {'reason': reason}, 'output': [{'type': 'reasoning', 'summary': []}],
        'usage': {'input_tokens': 3, 'output_tokens': 64, 'output_tokens_details': {'reasoning_tokens': 64}}}


def sse(payload):
    return ('data: ' + json.dumps(payload) + '\n\n').encode()


@pytest.mark.parametrize('protocol', ['chat_completions', 'responses'])
def test_complete_transport_retains_a_specific_truncation_outcome(protocol):
    tracker = ResponseTracker(protocol, output_limit=64)
    tracker.observe(truncated(protocol))
    assert tracker.terminal and tracker.terminal_kind == 'incomplete'
    assert tracker.failure_code == 'model_output_limit'
    metadata = tracker.completion_metadata()
    assert metadata['output_limit'] == metadata['output_tokens'] == 64
    assert metadata['reasoning_tokens'] == (60 if protocol == 'chat_completions' else 64)
    assert metadata['non_reasoning_output_seen'] is (protocol == 'chat_completions')
    assert 'visible_tokens_reserved' not in metadata


def test_chat_done_cannot_turn_length_into_completed():
    tracker = ResponseTracker('chat_completions')
    tracker.feed(sse({'choices': [{'index': 0, 'delta': {'content': 'prefix'}, 'finish_reason': 'length'}]}))
    assert not tracker.terminal
    tracker.feed(b'data: [DONE]\n\n')
    tracker.finish()
    assert tracker.terminal_kind == 'incomplete' and tracker.failure_code == 'model_output_limit'


@pytest.mark.parametrize('protocol', ['chat_completions', 'responses'])
async def test_nonstream_truncation_rejects_valid_json_but_preserves_raw_usage_and_exact_replay(
        protocol, store, tmp_path, context, http_stub, profile_factory):
    raw = truncated(protocol)
    with http_stub(lambda _: (200, {'Content-Type': 'application/json'}, json.dumps(raw).encode())) as (url, calls):
        service = await make_service(store, tmp_path, context, profile_factory(url))
        try:
            result = await service.forward(protocol, request(protocol), 'task-only-token', 'limited')
            assert result.status_code == 422 and result.headers['x-agentflow-failure-code'] == 'model_output_limit'
            assert json.loads(result.body)['error']['code'] == 'model_output_limit'
            assert b'looks-complete' not in result.body
            invocation = (await store.list('model_invocation'))[0]
            assert invocation['state'] == 'settled'
            receipt = invocation['response_receipt']
            assert receipt['body'] == raw and json.loads(Path(receipt['path']).read_bytes()) == raw
            assert receipt['completion']['output_status'] == 'incomplete'
            assert receipt['completion']['failure_code'] == 'model_output_limit'
            assert receipt['completion']['output_limit'] == 64
            replay = await service.forward(protocol, request(protocol), 'task-only-token', 'limited')
            assert replay.status_code == result.status_code and json.loads(replay.body) == json.loads(result.body)
            assert replay.headers['x-agentflow-failure-code'] == 'model_output_limit'
            account = await service.ledger.snapshot('run', context.run_id)
            assert account['request_count'] == 1 and account['settled_micros'] == 67 and account['reserved_micros'] == 0
            assert len(calls) == 1
        finally:
            await service.close()


async def test_chat_stream_length_never_delivers_a_valid_looking_finish_tool_to_the_sdk(
        store, tmp_path, context, http_stub, profile_factory):
    raw = truncated('chat_completions')
    chunks = [sse({'choices': [{'index': 0, 'delta': raw['choices'][0]['message'], 'finish_reason': None}]}),
        sse({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'length'}]}),
        sse({'choices': [], 'usage': raw['usage']}), b'data: [DONE]\n\n']
    with http_stub(lambda _: (200, {'Content-Type': 'text/event-stream'}, chunks)) as (url, calls):
        service = await make_service(store, tmp_path, context, profile_factory(url))
        try:
            body = request('chat_completions', stream=True)
            result = await service.forward('chat_completions', body, 'task-only-token', 'stream-limited')
            received = b''.join([part async for part in result.body_iterator])
            assert b'model_output_limit' in received
            assert b'looks-complete' not in received and b'tool_calls' not in received and b'[DONE]' not in received
            invocation = (await store.list('model_invocation'))[0]
            receipt = invocation['response_receipt']
            assert invocation['state'] == 'settled'
            assert Path(receipt['path']).read_bytes() == b''.join(chunks)
            assert receipt['completion']['finish_reasons'] == ['length']
            assert receipt['client_response']['body'].encode() == received
            replay = await service.forward('chat_completions', body, 'task-only-token', 'stream-limited')
            assert b''.join([part async for part in replay.body_iterator]) == received
            assert len(calls) == 1
            account = await service.ledger.snapshot('run', context.run_id)
            assert account['settled_micros'] == 67 and account['request_count'] == 1
        finally:
            await service.close()


async def test_chat_stream_is_held_until_verified_while_owner_trace_can_observe_progress(
        store, tmp_path, context, http_stub, profile_factory):
    first_sent, release = threading.Event(), threading.Event()
    prefix = sse({'choices': [{'index': 0, 'delta': {'content': 'visible-prefix\n' * 400}, 'finish_reason': None}]})
    terminal = sse({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 3, 'completion_tokens': 4}}) + b'data: [DONE]\n\n'
    class GatedChunks(list):
        def __iter__(self):
            # Respect the existing 0.5-second trace flush interval before the
            # first complete line, then hold the protocol terminal separately.
            time.sleep(0.55)
            yield prefix
            first_sent.set()
            release.wait(10)
            yield terminal
    with http_stub(lambda _: (200, {'Content-Type': 'text/event-stream'}, GatedChunks())) as (url, calls):
        service = await make_service(store, tmp_path, context, profile_factory(url))
        await store.command('fixture', 'attempt', {}, lambda tx: tx.put('attempt', context.attempt_id,
            {'run_id': context.run_id, 'work_item_id': 'work', 'status': 'running'}))
        pending = None
        try:
            result = await service.forward('chat_completions', request('chat_completions', stream=True), 'task-only-token', 'gated')
            pending = asyncio.create_task(anext(result.body_iterator))
            assert await asyncio.to_thread(first_sent.wait, 5)
            for _ in range(100):
                page = await service.traces.page(context.attempt_id, after=0, limit=100)
                if 'visible-prefix' in json.dumps(page):
                    break
                await asyncio.sleep(0.01)
            assert 'visible-prefix' in json.dumps(page)
            assert not pending.done(), 'No Chat bytes can reach SDK tools before terminal validation'
            release.set()
            first = await asyncio.wait_for(pending, 5)
            received = first + b''.join([part async for part in result.body_iterator])
            assert received == prefix + terminal and len(calls) == 1
            assert (await store.list('model_invocation'))[0]['response_receipt']['completion']['output_status'] == 'completed'
        finally:
            release.set()
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await service.close()


async def test_responses_stream_keeps_native_incomplete_signal_and_records_total_reasoning_usage(
        store, tmp_path, context, http_stub, profile_factory):
    response = truncated('responses')
    chunks = [sse({'type': 'response.incomplete', 'response': response})]
    with http_stub(lambda _: (200, {'Content-Type': 'text/event-stream'}, chunks)) as (url, calls):
        service = await make_service(store, tmp_path, context, profile_factory(url))
        try:
            body = request('responses', stream=True)
            result = await service.forward('responses', body, 'task-only-token', 'native-incomplete')
            assert b''.join([part async for part in result.body_iterator]) == b''.join(chunks)
            receipt = (await store.list('model_invocation'))[0]['response_receipt']
            assert receipt['completion']['failure_code'] == 'model_output_limit'
            assert receipt['completion']['output_tokens'] == receipt['completion']['reasoning_tokens'] == 64
            assert not receipt['completion']['non_reasoning_output_seen']
            assert 'client_response' not in receipt
            replay = await service.forward('responses', body, 'task-only-token', 'native-incomplete')
            assert b''.join([part async for part in replay.body_iterator]) == b''.join(chunks)
            assert len(calls) == 1
        finally:
            await service.close()


@pytest.mark.parametrize('protocol', ['chat_completions', 'responses'])
async def test_non_budget_incomplete_response_is_not_misdiagnosed_as_output_limit(
        protocol, store, tmp_path, context, http_stub, profile_factory):
    response = truncated(protocol, reason='content_filter')
    with http_stub(lambda _: (200, {'Content-Type': 'application/json'}, json.dumps(response).encode())) as (url, calls):
        service = await make_service(store, tmp_path, context, profile_factory(url))
        try:
            result = await service.forward(protocol, request(protocol), 'task-only-token')
            assert result.status_code == 422 and json.loads(result.body)['error']['code'] == 'invalid_model_output'
            assert len(calls) == 1
        finally:
            await service.close()


def test_output_bounds_do_not_mutate_thinking_or_owner_reasoning_policy(context, profile_factory):
    profile = profile_factory('http://127.0.0.1:1')
    body = request('chat_completions') | {'max_completion_tokens': 1000,
        'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high'}
    before = copy.deepcopy(body)
    normalized, limit = ModelProvider().normalize_request(profile, context, 'chat_completions', body)
    assert normalized['max_completion_tokens'] == limit == 64
    assert normalized['thinking'] == body['thinking'] and normalized['reasoning_effort'] == 'high'
    assert body == before
    frozen = context.model_copy(update={'reasoning_effort': 'high'})
    explicit = profile.model_copy(update={'reasoning_effort': 'high'})
    normalized, limit = ModelProvider().normalize_request(explicit, frozen, 'responses',
        request('responses') | {'max_output_tokens': 1000, 'reasoning': {'effort': 'high'}})
    assert normalized['max_output_tokens'] == limit == 64 and normalized['reasoning']['effort'] == 'high'


@pytest.mark.parametrize('event', [
    {'type': 'response.completed', 'response': {'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'}}},
    {'type': 'response.incomplete', 'response': {'status': 'completed'}},
    {'type': 'response.completed', 'response': {'status': 'completed', 'incomplete_details': {'reason': 'max_output_tokens'}}},
])
def test_conflicting_response_terminal_signals_are_not_complete(event):
    with pytest.raises(DomainError) as error:
        ResponseTracker('responses').feed(sse(event))
    assert error.value.code == 'invalid_response'


async def test_chat_error_cannot_hide_behind_usable_choices_and_completed_transport(
        store, tmp_path, context, http_stub, profile_factory):
    value = truncated('chat_completions')
    value['choices'][0]['finish_reason'] = 'tool_calls'
    value['error'] = {'code': 'provider_failure', 'message': 'Fixture provider error'}
    with http_stub(lambda _: (200, {'Content-Type': 'application/json'}, json.dumps(value).encode())) as (url, calls):
        service = await make_service(store, tmp_path, context, profile_factory(url))
        try:
            result = await service.forward('chat_completions', request('chat_completions'), 'task-only-token')
            assert result.status_code == 422 and json.loads(result.body)['error']['code'] == 'model_request_failed'
            assert b'looks-complete' not in result.body and len(calls) == 1
        finally:
            await service.close()


async def test_invalid_json_retains_raw_proof_but_does_not_settle_unverified_usage(
        store, tmp_path, context, http_stub, profile_factory):
    raw = b'{"status":"incomplete",not-valid-json'
    with http_stub(lambda _: (200, {'Content-Type': 'application/json'}, raw)) as (url, calls):
        service = await make_service(store, tmp_path, context, profile_factory(url))
        try:
            with pytest.raises(DomainError):
                await service.forward('responses', request('responses'), 'task-only-token')
            invocation = (await store.list('model_invocation'))[0]
            assert invocation['state'] == 'uncertain' and not invocation.get('response_receipt')
            assert (service.data_dir / (invocation['id'] + '.json')).read_bytes() == raw
            assert len(calls) == 1
        finally:
            await service.close()

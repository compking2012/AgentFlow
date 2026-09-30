"""Actual loopback HTTP forwarding, without generating tokens or calling a model."""
import copy
import json

import pytest

from agentflow.models.profiles import ModelProfile
from agentflow.models.service import ModelService

LARGE_OUTPUT_LIMIT = 393216


async def make_proxy(store, tmp_path, context, url, profile_limit, attempt_limit):
    frozen = context.model_copy(update={'cost_mode': 'request_limited', 'max_output_tokens': attempt_limit})
    profile = ModelProfile(model_profile_id=context.model_profile_id, provider='local_test',
        requested_model='fixture-model', accepted_api_model='fixture-model', acceptance_status='accepted',
        base_url=url, allow_loopback_upstream=True, protocols=['responses', 'chat_completions'],
        credential_reference='fixture-only', max_output_tokens=profile_limit)
    service = ModelService(store, tmp_path, lambda *_: frozen, lambda _: 'fixture-provider-key')
    await service.registry.register(profile, 'fixture-profile')
    await service.ledger.setup_accounts(frozen.run_id, frozen.iteration_id, 0, 0)
    return service, frozen


def request_body(protocol):
    if protocol == 'responses':
        return {'model': 'fixture-model', 'input': 'Local HTTP contract fixture only.'}
    return {'model': 'fixture-model', 'messages': [{'role': 'user', 'content': 'Local HTTP contract fixture only.'}]}


def complete_response(protocol):
    if protocol == 'responses':
        return {'id': 'fixture-response', 'object': 'response', 'status': 'completed', 'model': 'fixture-model',
                'output': [], 'usage': {'input_tokens': 1, 'output_tokens': 1}}
    return {'id': 'fixture-chat', 'object': 'chat.completion', 'model': 'fixture-model',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'fixture'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}


@pytest.mark.parametrize(('protocol', 'field'), [
    ('responses', 'max_output_tokens'),
    ('chat_completions', 'max_tokens'),
    ('chat_completions', 'max_completion_tokens'),
])
async def test_393216_is_forwarded_over_real_http_without_an_agentflow_65536_cap(
        store, tmp_path, context, http_stub, protocol, field):
    payload = {**request_body(protocol), field: LARGE_OUTPUT_LIMIT}
    original = copy.deepcopy(payload)
    response = complete_response(protocol)
    with http_stub(lambda _: (200, {'Content-Type': 'application/json'}, json.dumps(response).encode())) as (url, calls):
        service, frozen = await make_proxy(store, tmp_path, context, url, LARGE_OUTPUT_LIMIT, LARGE_OUTPUT_LIMIT)
        try:
            result = await service.forward(protocol, payload, 'fixture-task-token', 'same-request')
            replay = await service.forward(protocol, payload, 'fixture-task-token', 'same-request')
            assert result.status_code == replay.status_code == 200
            assert len(calls) == 1
            assert calls[0]['path'] == ('/responses' if protocol == 'responses' else '/chat/completions')
            assert calls[0]['body'][field] == LARGE_OUTPUT_LIMIT
            assert calls[0]['body']['model'] == 'fixture-model'
            assert payload == original
            assert (await service.registry.get(frozen.model_profile_id)).max_output_tokens == LARGE_OUTPUT_LIMIT
            assert (await service.ledger.snapshot('run', frozen.run_id))['request_count'] == 1
        finally:
            await service.close()


@pytest.mark.parametrize(('protocol', 'field'), [('responses', 'max_output_tokens'), ('chat_completions', 'max_tokens')])
async def test_missing_request_limit_inherits_393216_profile_and_attempt_over_http(
        store, tmp_path, context, http_stub, protocol, field):
    response = complete_response(protocol)
    with http_stub(lambda _: (200, {'Content-Type': 'application/json'}, json.dumps(response).encode())) as (url, calls):
        service, _ = await make_proxy(store, tmp_path, context, url, LARGE_OUTPUT_LIMIT, LARGE_OUTPUT_LIMIT)
        try:
            assert (await service.forward(protocol, request_body(protocol), 'fixture-task-token')).status_code == 200
            assert len(calls) == 1 and calls[0]['body'][field] == LARGE_OUTPUT_LIMIT
        finally:
            await service.close()


@pytest.mark.parametrize('client_limit', [16384, LARGE_OUTPUT_LIMIT])
@pytest.mark.parametrize(('protocol', 'field'), [
    ('responses', 'max_output_tokens'),
    ('chat_completions', 'max_tokens'),
    ('chat_completions', 'max_completion_tokens'),
])
async def test_streaming_requests_also_forward_393216_without_clamping(
        store, tmp_path, context, http_stub, protocol, field, client_limit):
    if protocol == 'responses':
        chunks = [b'data: {"type":"response.completed","response":{"status":"completed",'
                  b'"model":"fixture-model","usage":{"input_tokens":1,"output_tokens":1}}}\n\n']
    else:
        chunks = [b'data: {"choices":[{"delta":{"content":"fixture"}}]}\n\n',
                  b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n',
                  b'data: [DONE]\n\n']
    with http_stub(lambda _: (200, {'Content-Type': 'text/event-stream'}, chunks)) as (url, calls):
        service, _ = await make_proxy(store, tmp_path, context, url, LARGE_OUTPUT_LIMIT, LARGE_OUTPUT_LIMIT)
        try:
            result = await service.forward(protocol, {**request_body(protocol), field: client_limit,
                                                      'stream': True}, 'fixture-task-token')
            assert b''.join([chunk async for chunk in result.body_iterator]) == b''.join(chunks)
            assert len(calls) == 1 and calls[0]['body'][field] == LARGE_OUTPUT_LIMIT
            assert calls[0]['body']['stream'] is True
        finally:
            await service.close()


@pytest.mark.parametrize(('profile_limit', 'attempt_limit', 'requested', 'expected'), [
    (131072, LARGE_OUTPUT_LIMIT, LARGE_OUTPUT_LIMIT, 131072),
    (LARGE_OUTPUT_LIMIT, 98304, LARGE_OUTPUT_LIMIT, 98304),
    (LARGE_OUTPUT_LIMIT, LARGE_OUTPUT_LIMIT, 32768, LARGE_OUTPUT_LIMIT),
    (32768, 98304, LARGE_OUTPUT_LIMIT, 32768),
    (65536, 65536, 16384, 65536),
    (65536, 16384, 1024, 16384),
])
@pytest.mark.parametrize(('protocol', 'field'), [
    ('responses', 'max_output_tokens'),
    ('chat_completions', 'max_tokens'),
    ('chat_completions', 'max_completion_tokens'),
])
async def test_server_profile_and_attempt_override_client_output_limit_over_http(
        store, tmp_path, context, http_stub, protocol, field, profile_limit, attempt_limit, requested, expected):
    response = complete_response(protocol)
    with http_stub(lambda _: (200, {'Content-Type': 'application/json'}, json.dumps(response).encode())) as (url, calls):
        service, _ = await make_proxy(store, tmp_path, context, url, profile_limit, attempt_limit)
        try:
            result = await service.forward(protocol, {**request_body(protocol), field: requested}, 'fixture-task-token')
            assert result.status_code == 200
            assert len(calls) == 1 and calls[0]['body'][field] == expected
        finally:
            await service.close()


@pytest.mark.parametrize(('protocol', 'field'), [('responses', 'max_output_tokens'), ('chat_completions', 'max_tokens')])
async def test_provider_400_does_not_reduce_limit_retry_or_replace_model(
        store, tmp_path, context, http_stub, protocol, field):
    rejection = {'error': {'code': 'unsupported_value', 'param': field,
                           'message': 'Fixture provider rejects this requested output limit.'}}
    with http_stub(lambda _: (400, {'Content-Type': 'application/json'}, json.dumps(rejection).encode())) as (url, calls):
        service, frozen = await make_proxy(store, tmp_path, context, url, LARGE_OUTPUT_LIMIT, LARGE_OUTPUT_LIMIT)
        try:
            payload = {**request_body(protocol), field: LARGE_OUTPUT_LIMIT}
            result = await service.forward(protocol, payload, 'fixture-task-token', 'rejected-request')
            assert result.status_code == 400 and json.loads(result.body) == rejection
            assert len(calls) == 1
            assert calls[0]['body'][field] == LARGE_OUTPUT_LIMIT
            assert calls[0]['body']['model'] == 'fixture-model'
            # Explicit reconciliation of the same request replays the provider's
            # rejection; it does not negotiate a smaller limit behind the owner.
            replay = await service.forward(protocol, payload, 'fixture-task-token', 'rejected-request')
            assert replay.status_code == 400 and json.loads(replay.body) == rejection
            assert len(calls) == 1
            assert (await service.registry.get(frozen.model_profile_id)).max_output_tokens == LARGE_OUTPUT_LIMIT
            assert (await service.ledger.snapshot('run', frozen.run_id))['request_count'] == 1
        finally:
            await service.close()

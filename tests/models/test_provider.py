import json

import pytest

from agentflow.common import DomainError
from agentflow.models.profiles import ModelProfile
from agentflow.models.provider import ModelProvider, ResponseTracker


def deepseek(price):
    return ModelProfile(model_profile_id="profile-one", provider="deepseek", requested_model="deepseek-v4-flash",
                        provider_documented_version="DeepSeek-V4.1-Flash", accepted_api_model="deepseek-flash",
                        acceptance_status="accepted", base_url="https://api.deepseek.com",
                        protocols=["responses", "chat_completions"], credential_reference="fixture", pricing=price)


@pytest.mark.parametrize("field,value", [("previous_response_id", "resp_old"), ("conversation", "old"), ("store", True), ("background", True)])
def test_deepseek_stateless_constraints(field, value, price, context):
    with pytest.raises(DomainError, match="support"):
        ModelProvider().normalize_request(deepseek(price), context, "responses",
                                         {"model": "deepseek-flash", "input": "x", field: value})


def test_requested_legacy_model_not_silently_routed(price, context):
    profile = deepseek(price).model_copy(update={"acceptance_status": "pending_user_confirmation", "accepted_api_model": None})
    with pytest.raises(DomainError, match="accepted"):
        ModelProvider().normalize_request(profile, context, "responses", {"model": "deepseek-v4-flash", "input": "x"})


def test_fixed_upstream_tools_and_output_bound(price, context):
    provider = ModelProvider()
    body, maximum = provider.normalize_request(deepseek(price), context, "responses", {
        "model": "deepseek-flash", "input": "x", "max_output_tokens": 9000,
        "tools": [{"type": "custom", "name": "apply_patch"}],
    })
    assert maximum == body["max_output_tokens"] == 64
    with pytest.raises(DomainError):
        provider.normalize_request(deepseek(price), context, "responses", {**body, "base_url": "http://127.0.0.1"})
    with pytest.raises(DomainError):
        provider.normalize_request(deepseek(price), context, "responses", {**body, "tools": [{"type": "custom", "name": "arbitrary"}]})


@pytest.mark.parametrize(('protocol', 'field'), [
    ('responses', 'max_output_tokens'),
    ('chat_completions', 'max_tokens'),
    ('chat_completions', 'max_completion_tokens'),
])
@pytest.mark.parametrize('value', [False, 0, -1, 1.5, '65536', {}, []])
def test_server_owned_output_limit_still_rejects_malformed_client_fields(price, context, protocol, field, value):
    payload = {'model': 'deepseek-flash', field: value}
    payload.update({'input': 'fixture'} if protocol == 'responses' else {
        'messages': [{'role': 'user', 'content': 'fixture'}]})
    with pytest.raises(DomainError) as error:
        ModelProvider().normalize_request(deepseek(price), context, protocol, payload)
    assert error.value.code == 'invalid_request'


def test_server_owned_output_limit_still_rejects_conflicting_chat_fields(price, context):
    with pytest.raises(DomainError) as error:
        ModelProvider().normalize_request(deepseek(price), context, 'chat_completions', {
            'model': 'deepseek-flash', 'messages': [{'role': 'user', 'content': 'fixture'}],
            'max_tokens': 16384, 'max_completion_tokens': 65536})
    assert error.value.code == 'invalid_request'


def test_split_unicode_sse_and_terminal_marker():
    tracker = ResponseTracker("chat_completions")
    data = ('data: ' + json.dumps({"choices": [{"delta": {"content": "中文"}}]}, ensure_ascii=False) + '\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":4}}\n\n'
            'data: [DONE]\n\n').encode()
    for value in data:
        tracker.feed(bytes([value]))
    tracker.finish()
    assert tracker.input_tokens == 3 and tracker.output_tokens == 4


def test_eof_is_not_success():
    tracker = ResponseTracker("responses")
    tracker.feed(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n')
    with pytest.raises(DomainError, match="terminal"):
        tracker.finish()


def test_pricing_requires_proven_bound(price):
    with pytest.raises(DomainError):
        price.model_copy(update={"input_bound_verified": False}).reservation_cost(64)

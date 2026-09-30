import copy
import json

import httpx
import pytest

from agentflow.common import DomainError
from agentflow.models.profiles import ModelProfile
from agentflow.models.provider import ModelProvider
from agentflow.models.service import ModelService


def profile(effort=None):
    return ModelProfile(model_profile_id='profile-one', provider='openai_compatible',
        requested_model='deepseek-v4-pro', accepted_api_model='deepseek-v4-pro', acceptance_status='accepted',
        base_url='https://models.example.invalid/v1', protocols=['responses', 'chat_completions'],
        credential_reference='fixture', max_output_tokens=16384, reasoning_effort=effort)


@pytest.mark.parametrize('reasoning', [None, {}, {'summary': 'auto'}, {'effort': None, 'summary': 'auto'}, {'effort': 'low'}])
def test_explicit_reasoning_is_frozen_and_fills_missing_or_null_without_changing_output_cap(context, reasoning):
    context = context.model_copy(update={'reasoning_effort': 'low', 'max_output_tokens': 8192})
    payload = {'model': 'deepseek-v4-pro', 'input': 'fixture', 'max_output_tokens': 32768,
               **({'reasoning': reasoning} if reasoning is not None else {})}
    original = copy.deepcopy(payload)
    body, maximum = ModelProvider().normalize_request(profile('low'), context, 'responses', payload)
    assert body['reasoning']['effort'] == 'low'
    assert body['max_output_tokens'] == maximum == 8192
    assert payload == original
    if reasoning and reasoning.get('summary'):
        assert body['reasoning']['summary'] == reasoning['summary']


@pytest.mark.parametrize('effort', [None, 'low'])
def test_sdk_output_default_does_not_reduce_owner_ceiling_or_rewrite_reasoning(context, effort):
    frozen = context.model_copy(update={'reasoning_effort': effort, 'max_output_tokens': 65536})
    accepted = profile(effort).model_copy(update={'max_output_tokens': 65536})
    payload = {'model': 'deepseek-v4-pro', 'input': 'fixture', 'max_output_tokens': 16384,
               'reasoning': {'effort': effort or 'high', 'summary': 'auto'}}
    original = copy.deepcopy(payload)
    body, maximum = ModelProvider().normalize_request(accepted, frozen, 'responses', payload)
    assert body['max_output_tokens'] == maximum == 65536
    assert body['reasoning'] == payload['reasoning'] and payload == original


@pytest.mark.parametrize('reasoning', [None, {'effort': None, 'summary': 'auto'}, {'effort': 'high'}])
def test_unconfigured_reasoning_preserves_legacy_request(context, reasoning):
    payload = {'model': 'deepseek-v4-pro', 'input': 'fixture',
               **({'reasoning': reasoning} if reasoning is not None else {})}
    body, _ = ModelProvider().normalize_request(profile(), context, 'responses', payload)
    assert ('reasoning' in body) == ('reasoning' in payload)
    assert body.get('reasoning') == reasoning
    assert 'reasoning_effort' not in profile().model_dump()
    assert 'reasoning_effort' not in context.model_dump()


@pytest.mark.parametrize(('profile_effort', 'task_effort'), [('low', None), (None, 'low'), ('low', 'high')])
def test_profile_and_frozen_task_reasoning_must_match(context, profile_effort, task_effort):
    context = context.model_copy(update={'reasoning_effort': task_effort})
    with pytest.raises(DomainError) as error:
        ModelProvider().normalize_request(profile(profile_effort), context, 'responses',
            {'model': 'deepseek-v4-pro', 'input': 'fixture'})
    assert error.value.code == 'reasoning_policy_mismatch'


@pytest.mark.parametrize('requested', ['high', 'minimal', 'none', 'xhigh'])
def test_request_cannot_override_or_alias_the_frozen_reasoning_policy(context, requested):
    with pytest.raises(DomainError) as error:
        ModelProvider().normalize_request(profile('low'), context.model_copy(update={'reasoning_effort': 'low'}),
            'responses', {'model': 'deepseek-v4-pro', 'input': 'fixture', 'reasoning': {'effort': requested}})
    assert error.value.code == 'reasoning_policy_conflict'


@pytest.mark.parametrize('reasoning', ['low', False, {'effort': ['low']}, {'effort': 0}])
def test_malformed_explicit_reasoning_is_rejected_without_type_errors(context, reasoning):
    with pytest.raises(DomainError) as error:
        ModelProvider().normalize_request(profile('low'), context.model_copy(update={'reasoning_effort': 'low'}),
            'responses', {'model': 'deepseek-v4-pro', 'input': 'fixture', 'reasoning': reasoning})
    assert error.value.code == 'invalid_request'


def test_responses_policy_does_not_rewrite_native_chat_compatibility_behavior(context):
    payload = {'model': 'deepseek-v4-pro', 'messages': [{'role': 'user', 'content': 'fixture'}],
               'reasoning_effort': 'none'}
    body, _ = ModelProvider().normalize_request(profile('low'), context, 'chat_completions', payload)
    assert body['reasoning_effort'] == 'none' and 'reasoning' not in body


async def test_provider_rejection_is_returned_without_retry_mapping_or_changing_models(store, context, tmp_path):
    calls = []
    def reject(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(400, json={'error': {'code': 'unsupported_value', 'message': 'reasoning effort unsupported'}})
    context = context.model_copy(update={'reasoning_effort': 'low', 'cost_mode': 'request_limited'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject), trust_env=False) as client:
        service = ModelService(store, tmp_path, lambda *_: context, lambda *_: 'fixture-only', http_client=client)
        await service.registry.register(profile('low'), 'reasoning-profile')
        await service.ledger.setup_accounts(context.run_id, context.iteration_id, 0, 0)
        response = await service.forward('responses', {'model': 'deepseek-v4-pro', 'input': 'fixture',
            'reasoning': {'effort': None, 'summary': 'auto'}}, 'fixture-token')
        assert response.status_code == 400
    assert len(calls) == 1 and calls[0]['model'] == 'deepseek-v4-pro'
    assert calls[0]['reasoning'] == {'effort': 'low', 'summary': 'auto'}
    assert calls[0]['max_output_tokens'] == context.max_output_tokens

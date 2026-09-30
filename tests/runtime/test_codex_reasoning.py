import json
import platform
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agentflow.adapters.codex import CodexExecAdapter
from agentflow.common import DomainError
from agentflow.models.profiles import AttemptContext, ModelProfile
from agentflow.models.provider import ModelProvider
from agentflow.repository import RepositoryAdapter
from agentflow.runtime.contracts import TaskEnvelope
from agentflow.runtime.sandbox import MacSeatbeltSandbox
from agentflow.runtime.service import RuntimeService
from agentflow.runtime.supervisor import Supervisor


def response_stream(model, text):
    item = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}
    response = {'id': 'response_fixture', 'object': 'response', 'created_at': 1, 'status': 'completed',
                'model': model, 'output': [item], 'usage': {'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 20}}
    events = [
        {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
        {'type': 'response.output_item.added', 'output_index': 0,
         'item': {**item, 'status': 'in_progress', 'content': []}},
        {'type': 'response.content_part.added', 'output_index': 0, 'item_id': item['id'], 'content_index': 0,
         'part': {'type': 'output_text', 'text': '', 'annotations': []}},
        {'type': 'response.output_text.delta', 'output_index': 0, 'item_id': item['id'], 'content_index': 0, 'delta': text},
        {'type': 'response.output_text.done', 'output_index': 0, 'item_id': item['id'], 'content_index': 0, 'text': text},
        {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
        {'type': 'response.completed', 'response': response},
    ]
    return [f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events]


@pytest.mark.parametrize('model', ['gpt-5.4', 'deepseek-v4-pro'])
@pytest.mark.parametrize('effort', [None, 'low'])
@pytest.mark.parametrize('output_limit', [16384, 65536])
async def test_actual_codex_request_carries_configured_effort_including_provider_alias(
        task, store, tmp_path, http_fixture, model, effort, output_limit):
    executable = Path('/Applications/ChatGPT.app/Contents/Resources/codex')
    if platform.system() != 'Darwin' or not executable.is_file():
        pytest.skip('Requires installed Codex and the verified macOS sandbox')
    frozen = AttemptContext(attempt_id=task.attempt_id, run_id=task.run_id, iteration_id=task.iteration_id,
        model_profile_id='fixture', fencing_token=task.fencing_token, input_fingerprint=task.input_fingerprint,
        expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(), max_model_requests=0,
        max_output_tokens=output_limit, reasoning_effort=effort)
    profile = ModelProfile(model_profile_id='fixture', provider='openai_compatible', requested_model=model,
        accepted_api_model=model, acceptance_status='accepted', protocols=['responses'],
        base_url='https://fixture.example.invalid/v1', credential_reference='fixture',
        max_output_tokens=output_limit, reasoning_effort=effort)
    normalized = []
    def answer(call, _number):
        assert call['path'] == '/v1/responses' and call['body']['model'] == model
        if effort is not None:
            assert call['body'].get('reasoning', {}).get('effort') == effort
        body, maximum = ModelProvider().normalize_request(profile, frozen, 'responses', call['body'])
        assert maximum == body['max_output_tokens'] == output_limit
        if effort is None:
            assert body.get('reasoning') == call['body'].get('reasoning')
        else:
            assert body['reasoning']['effort'] == effort
        normalized.append(body)
        return 200, {'Content-Type': 'text/event-stream'}, response_stream(model, json.dumps({'review': 'fixture'}))
    supervisor = Supervisor(store, tmp_path / 'runtime')
    supervisor.start = AsyncMock(wraps=supervisor.start)
    adapter = CodexExecAdapter(supervisor, MacSeatbeltSandbox(tmp_path / 'sandbox_profiles'), executable)
    RepositoryAdapter()._run(task.workspace, ['init'])
    with http_fixture(answer) as (url, calls):
        task = task.model_copy(update={'role': 'development', 'allow_code_write': True,
            'allowed_write_roots': [task.workspace], 'proxy_base_url': url, 'model': model,
            'max_output_tokens': output_limit, 'reasoning_effort': effort, 'max_active_seconds': 30,
            'max_log_bytes': 2 * 1024 * 1024})
        try:
            await adapter.start(task)
            assert supervisor.start.await_args.args[0].max_log_bytes == task.max_log_bytes
            await supervisor.wait(task.attempt_id)
            result = await adapter.collect_artifacts(task.attempt_id)
            handle = await adapter.inspect(task.attempt_id)
            assert result['execution_status'] == 'completed', Path(handle.stderr_path).read_text()
            assert len(calls) == len(normalized) == 1
            assert result['result'] == {'review': 'fixture'}
        finally:
            await supervisor.close()


@pytest.mark.parametrize('effort', [None, 'low'])
async def test_runtime_envelope_preserves_frozen_policy_and_legacy_serialization(task, tmp_path, effort):
    profile = ModelProfile(model_profile_id='fixture', provider='openai_compatible', requested_model='fixture-model',
        accepted_api_model='fixture-model', acceptance_status='accepted', protocols=['responses'],
        base_url='https://fixture.example.invalid/v1', credential_reference='fixture', reasoning_effort=effort)
    runtime = RuntimeService.__new__(RuntimeService)
    runtime.data_dir = tmp_path / 'runtime'
    runtime.models = SimpleNamespace(registry=SimpleNamespace(get=AsyncMock(return_value=profile)),
                                    secret_resolver=lambda _: 'fixture-only')
    runtime.workspaces = SimpleNamespace(assert_owned=lambda _, **_options: None)
    runtime.codex, runtime.openhands = object(), object()
    payload = {'attempt_id': task.attempt_id, 'run_id': task.run_id, 'iteration_id': task.iteration_id,
               'role': 'development', 'step': 'implementation', 'goal': task.goal,
               'input_fingerprint': task.input_fingerprint, 'workspace': str(task.workspace),
               'profile_id': 'fixture', 'task_token': 'fixture-token', 'cost_mode': 'request_limited',
               'allowed_write_paths': ['.'], **({'reasoning_effort': effort} if effort is not None else {})}
    envelope, backend = await runtime._envelope(payload)
    assert backend is runtime.codex and envelope.reasoning_effort == effort
    encoded = envelope.model_dump(mode='json')
    assert ('reasoning_effort' in encoded) == (effort is not None)
    assert 'proxy_token' not in encoded
    assert TaskEnvelope.model_validate({**encoded, 'proxy_token': 'fixture-restored'}).reasoning_effort == effort
    if effort is not None:
        with pytest.raises(DomainError) as error:
            await runtime._envelope({key: value for key, value in payload.items() if key != 'reasoning_effort'})
        assert error.value.code == 'reasoning_policy_mismatch'

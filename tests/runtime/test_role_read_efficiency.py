"""Measured native SDK round trips using local HTTP replies, never paid models."""
import hashlib
import json
import platform
import time

import pytest

from agentflow.adapters.openhands import OpenHandsRoleAdapter
from agentflow.adapters.openhands.tools import ToolBroker
from agentflow.runtime.sandbox import MacSeatbeltSandbox
from agentflow.runtime.supervisor import Supervisor


def observation(call, identity):
    messages = [message for message in call['body']['messages']
                if message.get('role') == 'tool' and message.get('tool_call_id') == identity]
    assert len(messages) == 1
    content = messages[0]['content']
    if isinstance(content, list):
        content = ''.join(part['text'] for part in content if part.get('type') == 'text')
    return json.loads(content)


def response(number, actions):
    return 200, {'Content-Type': 'application/json'}, json.dumps({
        'id': f'fixture-{number}', 'object': 'chat.completion', 'created': 1, 'model': 'fixture-model',
        'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': identity, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}
                           for identity, name, arguments in actions]}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}).encode()


def record_measurement(tmp_path, scenario, variant, calls, tool_calls, elapsed):
    result = {'scenario': scenario, 'variant': variant, 'model_http_requests': len(calls), 'tool_calls': tool_calls,
        'serialized_request_bytes': sum(len(json.dumps(call['body'], ensure_ascii=False,
            separators=(',', ':')).encode()) for call in calls), 'elapsed_seconds': round(elapsed, 4),
        'scope': 'real SDK and local scripted HTTP; no model inference or provider calls'}
    (tmp_path / 'read-efficiency.json').write_text(json.dumps(result, indent=2) + '\n')


@pytest.mark.parametrize('page_size', [12000, None], ids=['legacy-12k', 'default-24k'])
async def test_default_page_reads_the_same_complete_required_evidence_with_fewer_round_trips(
        task, store, tmp_path, http_fixture, page_size):
    if platform.system() != 'Darwin':
        pytest.skip('Native SDK execution requires the supported macOS sandbox')
    document = json.dumps({'content': 'MANDATORY_START_' + 'x' * 22900 + '_MANDATORY_END'}, separators=(',', ':'))
    digest = hashlib.sha256(document.encode()).hexdigest()
    context = tmp_path / 'frozen-context'
    context.mkdir()
    filename = digest + '.json'
    source = context / filename
    source.write_text(document)
    source.chmod(0o400)
    schema = {'type': 'object', 'properties': {'source_digest': {'type': 'string'}, 'read_characters': {'type': 'integer'}},
              'required': ['source_digest', 'read_characters'], 'additionalProperties': False}
    pieces, expected = [], {'source_digest': 'sha256:' + digest, 'read_characters': len(document)}

    def answer(call, number):
        assert call['path'] == '/v1/chat/completions'
        tools = {tool['function']['name']: tool['function'] for tool in call['body']['tools']}
        assert tools['finish']['parameters']['properties']['result'] == schema
        assert call['body']['model'] == task.model
        if number == 1:
            offset = 0
        else:
            prior = observation(call, f'page-{number - 1}')
            assert prior['digest'] == 'sha256:' + digest
            pieces.append(prior['text'])
            if not prior['has_more']:
                assert ''.join(pieces) == document
                assert json.loads(''.join(pieces))['content'].endswith('_MANDATORY_END')
                return response(number, [('finished', 'finish', {'message': 'Complete evidence verified', 'result': expected})])
            offset = prior['next_offset']
        arguments = {'path': filename, 'offset': offset}
        if page_size is not None:
            arguments['limit'] = page_size
        return response(number, [(f'page-{number}', 'agentflow_io', {'operation': 'read_context', 'arguments': arguments})])

    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / 'sandbox_profiles'))
    with http_fixture(answer) as (url, calls):
        envelope = task.model_copy(update={'proxy_base_url': url, 'context_directory': context, 'allowed_read_roots': [context],
            'goal': f'Read the complete required document {filename} and report its SHA256 and exact character count.',
            'max_active_seconds': 60, 'max_tool_calls': 3, 'max_iterations': 4, 'output_schema': schema})
        started = time.perf_counter()
        try:
            await adapter.start(envelope)
            await supervisor.wait(envelope.attempt_id)
            result = await adapter.collect_artifacts(envelope.attempt_id)
            assert result['execution_status'] == 'completed', result
            assert result['result'] == expected and result['quality_result'] == 'unknown'
            assert len(calls) == result['tool_calls'] == (3 if page_size == 12000 else 2)
            assert source.read_text() == document and source.stat().st_mode & 0o222 == 0
            assert not await store.list('model_invocation')
            record_measurement(tmp_path, 'complete_required_context', 'legacy_12k' if page_size else 'default_24k',
                               calls, result['tool_calls'], time.perf_counter() - started)
        finally:
            await supervisor.close()


@pytest.mark.parametrize('batch', [False, True], ids=['separate-read-turns', 'same-turn-reads'])
async def test_same_turn_reads_preserve_each_tool_budget_and_reduce_model_round_trips(task, store, tmp_path, http_fixture, batch):
    if platform.system() != 'Darwin':
        pytest.skip('Native SDK execution requires the supported macOS sandbox')
    contents = {'first.py': 'FIRST = 1\n', 'second.py': 'SECOND = 2\n'}
    for name, content in contents.items():
        (task.workspace / name).write_text(content)
    schema = {'type': 'object', 'properties': {'read_files': {'type': 'array', 'items': {'type': 'string'}}},
              'required': ['read_files'], 'additionalProperties': False}

    def read(name):
        return name, 'agentflow_io', {'operation': 'read_code', 'arguments': {'path': name}}

    def answer(call, number):
        tools = {tool['function']['name']: tool['function'] for tool in call['body']['tools']}
        assert tools['finish']['parameters']['properties']['result'] == schema
        if number == 1:
            return response(number, [read(name) for name in contents] if batch else [read('first.py')])
        if number == 2 and not batch:
            assert observation(call, 'first.py')['text'] == contents['first.py']
            return response(number, [read('second.py')])
        for name, content in contents.items():
            assert observation(call, name)['text'] == content
        return response(number, [('finished', 'finish', {'message': 'Both files verified', 'result': {'read_files': list(contents)}})])

    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / 'sandbox_profiles'))
    with http_fixture(answer) as (url, calls):
        envelope = task.model_copy(update={'proxy_base_url': url, 'goal': 'Read both independent source files and report their names.',
            'max_active_seconds': 60, 'max_tool_calls': 3, 'max_iterations': 4, 'output_schema': schema})
        started = time.perf_counter()
        try:
            await adapter.start(envelope)
            await supervisor.wait(envelope.attempt_id)
            result = await adapter.collect_artifacts(envelope.attempt_id)
            assert result['execution_status'] == 'completed', result
            assert result['result'] == {'read_files': list(contents)}
            assert len(calls) == (2 if batch else 3)
            assert result['tool_calls'] == 3
            audit = json.loads((envelope.artifact_dir / 'tool_audit.json').read_text())
            assert [event['operation'] for event in audit['events']] == ['read_code', 'read_code', 'finish']
            assert not await store.list('model_invocation')
            record_measurement(tmp_path, 'two_independent_reads', 'batched' if batch else 'sequential',
                               calls, result['tool_calls'], time.perf_counter() - started)
        finally:
            await supervisor.close()


def test_explicit_small_context_pages_remain_available(task, tmp_path):
    document = json.dumps({'content': 'x' * 30000})
    digest = hashlib.sha256(document.encode()).hexdigest()
    context = tmp_path / 'context'
    context.mkdir()
    (context / (digest + '.json')).write_text(document)
    broker = ToolBroker(task.model_copy(update={'context_directory': context, 'allowed_read_roots': [context]}))
    result = broker.read_context(digest + '.json', limit=512)
    assert result['text'] == document[:512] and result['next_offset'] == 512 and result['has_more']

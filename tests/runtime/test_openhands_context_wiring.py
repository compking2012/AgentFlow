"""Context wiring; the native SDK case talks only to a local scripted HTTP fixture."""
import asyncio
import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jsonschema import validate

from agentflow.adapters.openhands import OpenHandsRoleAdapter
from agentflow.adapters.openhands.tools import ToolBroker
from agentflow.common import DomainError
from agentflow.runtime.contracts import TaskEnvelope
from agentflow.runtime.sandbox import MacSeatbeltSandbox
from agentflow.runtime.supervisor import Supervisor


async def test_worker_configuration_keeps_explicit_context_readable_after_adapter_adds_source_root(task, tmp_path):
    data = tmp_path / 'controller'
    context = data / 'stage_context' / ('a' * 64)
    context.mkdir(parents=True)
    text = json.dumps({'content': 'Readable stage evidence. ' * 1200}, ensure_ascii=False)
    raw = text.encode()
    filename = hashlib.sha256(raw).hexdigest() + '.json'
    (context / filename).write_bytes(raw)
    (context / filename).chmod(0o400)
    other = tmp_path / 'other-readable-root'
    other.mkdir()
    other_raw = b'{"content":"Different allowed root, not stage evidence"}'
    other_name = hashlib.sha256(other_raw).hexdigest() + '.json'
    (other / other_name).write_bytes(other_raw)
    envelope = task.model_copy(update={'context_directory': context, 'allowed_read_roots': [context, other]})
    # These doubles authorize no execution: they only allow start() to prepare
    # the real private worker config and capture its launch descriptor.
    supervisor = SimpleNamespace(root=data / 'supervisor', start=AsyncMock(return_value={'state': 'not_started'}))
    sandbox = SimpleNamespace(prepare=AsyncMock(return_value=([], {'verified': True, 'fixture': 'configuration_only'})))
    adapter = OpenHandsRoleAdapter(supervisor, sandbox)
    adapter.probe = AsyncMock(return_value={'available': True, 'version': 'configuration-fixture'})
    await adapter.start(envelope)
    launch = supervisor.start.await_args.args[0]
    config = json.loads(Path(launch.argv[-1]).read_text())
    assert config['context_directory'] == str(context)
    assert len(config['allowed_read_roots']) >= 3, 'The adapter also permits its implementation source imports'
    assert 'proxy_token' not in config
    # This is the same reconstruction performed by the actual worker main().
    config['proxy_token'] = launch.environment['AGENTFLOW_PROXY_TOKEN']
    worker_task = TaskEnvelope.model_validate(config)
    broker = ToolBroker(worker_task)
    first = broker.execute('read_context', path=filename, limit=12000)
    second = broker.execute('read_context', path=filename, offset=first['next_offset'], limit=12000)
    assert first['has_more'] and first['text'] + second['text'] == text[:24000]
    assert first['digest'] == 'sha256:' + filename[:-5]
    assert (context / filename).stat().st_mode & 0o222 == 0
    with pytest.raises((DomainError, FileNotFoundError)):
        broker.execute('read_context', path=other_name)
    assert len(supervisor.start.await_args_list) == 1


def test_context_directory_cannot_use_a_root_outside_the_worker_allowlist(task, tmp_path):
    context = tmp_path / 'context'
    context.mkdir()
    raw = b'{"content":"fixture"}'
    filename = hashlib.sha256(raw).hexdigest() + '.json'
    (context / filename).write_bytes(raw)
    broker = ToolBroker(task.model_copy(update={'context_directory': context, 'allowed_read_roots': []}))
    with pytest.raises(DomainError) as error:
        broker.execute('read_context', path=filename)
    assert error.value.code == 'forbidden_path'


async def test_actual_openhands_reads_two_context_pages_then_finishes_through_local_http(task, store, tmp_path, http_fixture):
    if platform.system() != 'Darwin':
        pytest.skip('Actual OpenHands worker requires the verified macOS sandbox')
    try:
        assert importlib.metadata.version('openhands-sdk') == '1.49.2'
    except importlib.metadata.PackageNotFoundError:
        pytest.skip('Install the project OpenHands SDK dependency for this protocol test')

    document = json.dumps({'title': '固定的本地上下文', 'content': '这是分页读取的产品资料。' * 1300},
                          ensure_ascii=False, separators=(',', ':'))
    assert 12000 < len(document) < 24000
    digest = hashlib.sha256(document.encode()).hexdigest()
    filename = digest + '.json'
    context = (tmp_path / 'stage_context' / hashlib.sha256(b'local-scripted-context').hexdigest()).resolve()
    context.mkdir(parents=True)
    source = context / filename
    source.write_text(document)
    source.chmod(0o400)
    schema = {'type': 'object', 'properties': {
        'review': {'type': 'string'}, 'source_digest': {'type': 'string'}, 'read_characters': {'type': 'integer'}},
        'required': ['review', 'source_digest', 'read_characters'], 'additionalProperties': False}
    observations = {}
    final = {}

    def observation(call, identity):
        matches = [message for message in call['body']['messages']
                   if message.get('role') == 'tool' and message.get('tool_call_id') == identity]
        assert len(matches) == 1
        content = matches[0]['content']
        if isinstance(content, list):
            content = ''.join(part['text'] for part in content if part.get('type') == 'text')
        return json.loads(content)

    def answer(call, number):
        assert call['path'] == '/v1/chat/completions'
        assert call['authorization'] == 'Bearer scoped-fixture-token'
        tools = {tool['function']['name']: tool['function'] for tool in call['body']['tools']}
        assert set(tools) == {'agentflow_io', 'finish'}
        assert 'read_context' in tools['agentflow_io']['parameters']['properties']['operation']['enum']
        if number == 1:
            name = 'agentflow_io'
            arguments = {'operation': 'read_context', 'arguments': {'path': filename, 'offset': 0, 'limit': 12000}}
        elif number == 2:
            first = observation(call, 'context-page-1')
            observations['first'] = first
            assert first['text'] == document[:12000] and first['has_more'] is True
            assert first['offset'] == 0 and first['next_offset'] == 12000
            assert first['path'] == filename and first['digest'] == 'sha256:' + digest
            name = 'agentflow_io'
            arguments = {'operation': 'read_context', 'arguments': {
                'path': filename, 'offset': first['next_offset'], 'limit': 12000}}
        elif number == 3:
            second = observation(call, 'context-page-2')
            observations['second'] = second
            assert second['text'] == document[12000:] and second['has_more'] is False
            assert second['offset'] == 12000 and second['next_offset'] == len(document)
            assert second['path'] == filename and second['digest'] == 'sha256:' + digest
            reconstructed = observations['first']['text'] + second['text']
            assert reconstructed == document
            final.update(review='Two context pages read through the SDK tool channel',
                         source_digest='sha256:' + hashlib.sha256(reconstructed.encode()).hexdigest(),
                         read_characters=len(reconstructed))
            assert tools['finish']['parameters']['properties']['result'] == schema
            name, arguments = 'finish', {'message': 'Context verified', 'result': dict(final)}
        else:
            raise AssertionError('The three scripted requests must not retry or invoke another model')
        message = {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': f'context-page-{number}', 'type': 'function',
             'function': {'name': name, 'arguments': json.dumps(arguments)}}]}
        response = {'id': f'local-context-{number}', 'object': 'chat.completion', 'created': 1,
            'model': 'fixture-model', 'choices': [{'index': 0, 'message': message, 'finish_reason': 'tool_calls'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}
        return 200, {'Content-Type': 'application/json'}, json.dumps(response).encode()

    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / 'sandbox_profiles'))
    with http_fixture(answer) as (url, requests):
        envelope = task.model_copy(update={'proxy_base_url': url, 'context_directory': context,
            'allowed_read_roots': [context], 'max_active_seconds': 40, 'max_tool_calls': 3, 'max_iterations': 4,
            'output_schema': schema,
            'goal': f'Read both pages of the indexed context {filename}, then report its digest and character count.'})
        try:
            await adapter.start(envelope)
            await asyncio.wait_for(supervisor.wait(envelope.attempt_id), 50)
            result = await adapter.collect_artifacts(envelope.attempt_id)
            error_file = envelope.artifact_dir / 'role_error.json'
            error = error_file.read_text() if error_file.exists() else ''
            assert result['execution_status'] == 'completed', error
            assert result['quality_result'] == 'unknown'
            assert len(requests) == result['tool_calls'] == 3
            assert result['result'] == final
            validate(result['result'], schema)
            audit = json.loads((envelope.artifact_dir / 'tool_audit.json').read_text())
            assert audit['calls'] == 3
            assert [event['operation'] for event in audit['events']] == ['read_context', 'read_context', 'finish']
            assert source.read_text() == document and source.stat().st_mode & 0o222 == 0
            assert (envelope.workspace / 'original.py').read_text() == 'ORIGINAL = True\n'
            assert 'scoped-fixture-token' not in (envelope.artifact_dir / 'openhands_events.jsonl').read_text()
            assert not await store.list('model_invocation'), 'No provider service or paid account is part of this fixture'
        finally:
            await supervisor.close()

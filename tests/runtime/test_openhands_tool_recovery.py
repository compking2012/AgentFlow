"""Real SDK recovery from a normal model request for a nonexistent source file."""
import json
import platform
from pathlib import Path

import pytest

from agentflow.adapters.openhands import OpenHandsRoleAdapter
from agentflow.common import canonical_digest
from agentflow.runtime.sandbox import MacSeatbeltSandbox
from agentflow.runtime.supervisor import Supervisor


async def test_missing_optional_source_file_is_reported_to_model_and_conversation_continues(
    task, store, tmp_path, http_fixture,
):
    if platform.system() != 'Darwin':
        pytest.skip('Real SDK execution requires the supported macOS sandbox')

    def answer(call, number):
        assert call['path'] == '/v1/chat/completions'
        if number == 1:
            tool_calls = [{'id': 'read_optional_file', 'type': 'function', 'function': {
                'name': 'agentflow_io', 'arguments': json.dumps({
                    'operation': 'read_code', 'arguments': {'path': 'AGENTS.md'}})}}]
        else:
            assert number == 2, 'An optional missing file must not start an unbounded retry loop'
            results = [message for message in call['body']['messages'] if message.get('role') == 'tool']
            assert any(message.get('tool_call_id') == 'read_optional_file' for message in results)
            assert 'AGENTS.md' in json.dumps(results)
            tool_calls = [{'id': 'finish_without_optional_file', 'type': 'function', 'function': {
                'name': 'finish', 'arguments': json.dumps({
                    'message': 'Optional file does not exist; finish using provided input.',
                    'result': {'review': 'Optional file absent; no source modification.'}})}}]
        return 200, {'Content-Type': 'application/json'}, json.dumps({
            'id': f'chatcmpl-optional-{number}', 'object': 'chat.completion', 'created': 1,
            'model': 'fixture-model', 'choices': [{'index': 0, 'finish_reason': 'tool_calls',
                'message': {'role': 'assistant', 'content': None, 'tool_calls': tool_calls}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20},
        }).encode()

    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / 'sandbox_profiles'))
    with http_fixture(answer) as (url, calls):
        task = task.model_copy(update={'proxy_base_url': url, 'max_active_seconds': 40})
        try:
            await adapter.start(task)
            await supervisor.wait(task.attempt_id)
            result = await adapter.collect_artifacts(task.attempt_id)
            error = task.artifact_dir / 'role_error.json'
            details = error.read_text() if error.exists() else ''
            details += Path((await adapter.inspect(task.attempt_id)).stderr_path).read_text()
            assert result['execution_status'] == 'completed', details
            assert result['result'] == {'review': 'Optional file absent; no source modification.'}
            assert result['tool_calls'] == 2 and len(calls) == 2
            assert not (task.workspace / 'AGENTS.md').exists()
            assert (task.workspace / 'original.py').read_text() == 'ORIGINAL = True\n'
        finally:
            await supervisor.close()


def completion(number, tool_calls):
    return 200, {'Content-Type': 'application/json'}, json.dumps({
        'id': f'chatcmpl-tool-recovery-{number}', 'object': 'chat.completion', 'created': 1,
        'model': 'fixture-model', 'choices': [{'index': 0, 'finish_reason': 'tool_calls',
            'message': {'role': 'assistant', 'content': None, 'tool_calls': tool_calls}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20},
    }).encode()


def io_call(identity, operation, arguments):
    return {'id': identity, 'type': 'function', 'function': {'name': 'agentflow_io',
            'arguments': json.dumps({'operation': operation, 'arguments': arguments})}}


async def test_invalid_none_reference_is_corrected_in_the_same_sdk_conversation(task, store, tmp_path, http_fixture):
    if platform.system() != 'Darwin':
        pytest.skip('Real SDK execution requires the supported macOS sandbox')

    def answer(call, number):
        if number == 1:
            return completion(number, [io_call('bad_reference', 'result_status', {'result_ref': 'none'})])
        outputs = {m.get('tool_call_id'): m for m in call['body']['messages'] if m.get('role') == 'tool'}
        assert 'bad_reference' in outputs
        assert 'invalid_result_reference' in json.dumps(outputs['bad_reference'])
        if number == 2:
            return completion(number, [io_call('list_drafts', 'result_status', {})])
        assert number == 3 and 'list_drafts' in outputs
        assert 'drafts' in json.dumps(outputs['list_drafts'])
        return completion(number, [{'id': 'finish_corrected', 'type': 'function', 'function': {'name': 'finish',
            'arguments': json.dumps({'message': 'Corrected the reference argument.',
                                    'result': {'review': 'Completed after correcting result_status'}})}}])

    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / 'sandbox_profiles'))
    with http_fixture(answer) as (url, calls):
        task = task.model_copy(update={'proxy_base_url': url, 'max_active_seconds': 40, 'max_iterations': 5})
        try:
            await adapter.start(task)
            await supervisor.wait(task.attempt_id)
            result = await adapter.collect_artifacts(task.attempt_id)
            assert result['execution_status'] == 'completed', result
            assert result['result'] == {'review': 'Completed after correcting result_status'}
            assert result['tool_calls'] == 3 and len(calls) == 3
            assert not (task.artifact_dir / 'role_error.json').exists()
            assert (task.workspace / 'original.py').read_text() == 'ORIGINAL = True\n'
        finally:
            await supervisor.close()


@pytest.mark.parametrize('quota', [False, True])
async def test_bad_context_in_multi_tool_batch_keeps_pairing_and_obeys_quota(task, store, tmp_path, http_fixture, quota):
    import hashlib
    if platform.system() != 'Darwin':
        pytest.skip('Real SDK execution requires the supported macOS sandbox')
    context = tmp_path / 'frozen-context'
    context.mkdir()
    content = b'{"evidence":"FROZEN_VALID_INPUT"}'
    name = hashlib.sha256(content).hexdigest() + '.json'
    (context / name).write_bytes(content)
    def answer(call, number):
        if number == 1:
            return completion(number, [io_call('bad_context', 'read_context', {'path': '.'}),
                                       io_call('good_code', 'read_code', {'path': 'original.py'})])
        messages = call['body']['messages']
        outputs = {message.get('tool_call_id'): message for message in messages if message.get('role') == 'tool'}
        # Simulate the real provider's strict transcript check, rather than
        # allowing a fake endpoint to accept missing error observations.
        if not {'bad_context', 'good_code'} <= set(outputs):
            return 400, {'Content-Type': 'application/json'}, json.dumps({'error': {'type': 'invalid_request_error',
                'message': "An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id' (insufficient tool messages following tool_calls)."}}).encode()
        if number == 2:
            return completion(number, [io_call('correct_context', 'read_context', {'path': name})])
        return completion(number, [{'id': 'finish_after_error', 'type': 'function', 'function': {'name': 'finish',
            'arguments': json.dumps({'message': 'Verified inputs', 'result': {'review': 'Recovered using frozen inputs'}})}}])
    artifacts = tmp_path / 'attempt_artifacts' / canonical_digest(task.attempt_id).split(':')[1]
    artifacts.mkdir(parents=True)
    task = task.model_copy(update={'artifact_dir': artifacts})
    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / 'sandbox_profiles'))
    with http_fixture(answer) as (url, calls):
        task = task.model_copy(update={'proxy_base_url': url, 'context_directory': context,
            'allowed_read_roots': [context], 'max_active_seconds': 40, 'max_iterations': 5,
            'max_tool_calls': 1 if quota else 10})
        try:
            await adapter.start(task)
            await supervisor.wait(task.attempt_id)
            result = await adapter.collect_artifacts(task.attempt_id)
            details = (task.artifact_dir / 'role_error.json').read_text() if (task.artifact_dir / 'role_error.json').exists() else ''
            audit = json.loads((task.artifact_dir / 'tool_audit.json').read_text())
            if quota:
                assert result['execution_status'] == 'failed'
                assert result['runtime_failure_code'] == 'role_tool_limit_exceeded', result
                assert len(calls) == 1, details
                assert audit['calls'] == 1
                assert not (task.artifact_dir / 'openhands_final.json').exists()
            else:
                assert result['execution_status'] == 'completed', details
                assert result['result'] == {'review': 'Recovered using frozen inputs'}
                assert len(calls) == 3 and audit['calls'] == 4
                tools = [message for message in calls[1]['body']['messages'] if message.get('role') == 'tool']
                assert [message['tool_call_id'] for message in tools] == ['bad_context', 'good_code']
                assert 'ORIGINAL = True' in json.dumps(tools)
                events = [json.loads(line) for line in (task.artifact_dir / 'openhands_events.jsonl').read_text().splitlines()]
                bad = [event for event in events if event.get('tool_call_id') == 'bad_context' and 'observation' in event]
                assert len(bad) == 1 and bad[0]['observation']['is_error'] is True
                good = [event for event in events if event.get('tool_call_id') == 'good_code' and 'observation' in event]
                assert len(good) == 1 and good[0]['observation']['is_error'] is False
                assert 'FROZEN_VALID_INPUT' in json.dumps(calls[2]['body']['messages'])
        finally:
            await supervisor.close()


def paired_messages(messages):
    pending = set()
    for message in messages:
        if message.get('role') == 'tool':
            if message.get('tool_call_id') not in pending:
                return False
            pending.remove(message['tool_call_id'])
        else:
            if pending:
                return False
            pending = {call['id'] for call in message.get('tool_calls', [])}
    return not pending


async def test_sdk_parse_error_in_multi_tool_batch_preserves_wire_pairing(task, store, tmp_path, http_fixture):
    if platform.system() != 'Darwin':
        pytest.skip('Real SDK execution requires macOS sandbox')
    def answer(call, number):
        if number == 1:
            malformed = io_call('invalid_action', 'read_code', {})
            malformed['function']['arguments'] = json.dumps({'arguments': {'path': '.'}})
            return completion(number, [io_call('first_read', 'read_code', {'path': 'original.py'}), malformed,
                                       io_call('last_read', 'read_code', {'path': 'original.py'})])
        if not paired_messages(call['body']['messages']):
            return 400, {'Content-Type': 'application/json'}, json.dumps({'error': {'type': 'invalid_request_error',
                'message': "An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id' (insufficient tool messages following tool_calls)."}}).encode()
        return completion(number, [{'id': 'finished', 'type': 'function', 'function': {'name': 'finish',
            'arguments': json.dumps({'message': 'Done', 'result': {'review': 'All real tool outcomes retained'}})}}])
    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / 'sandbox_profiles'))
    with http_fixture(answer) as (url, calls):
        task = task.model_copy(update={'proxy_base_url': url, 'max_active_seconds': 40, 'max_iterations': 3})
        try:
            await adapter.start(task)
            await supervisor.wait(task.attempt_id)
            result = await adapter.collect_artifacts(task.attempt_id)
            details = (task.artifact_dir / 'role_error.json').read_text() if (task.artifact_dir / 'role_error.json').exists() else ''
            assert result['execution_status'] == 'completed', details
            assert len(calls) == 2 and paired_messages(calls[1]['body']['messages'])
            assert {message['tool_call_id'] for message in calls[1]['body']['messages'] if message.get('role') == 'tool'} == {
                'first_read', 'invalid_action', 'last_read'}
        finally:
            await supervisor.close()


@pytest.mark.parametrize('scenario', ['exact_quota', 'mixed_finish', 'finish_io_error', 'final_slot_finish'])
async def test_terminal_and_mixed_finish_boundaries_use_real_sdk(task, store, tmp_path, http_fixture, scenario):
    if platform.system() != 'Darwin':
        pytest.skip('Real SDK execution requires macOS sandbox')
    artifacts = tmp_path / 'attempt_artifacts' / canonical_digest(task.attempt_id).split(':')[1]
    artifacts.mkdir(parents=True)
    if scenario == 'finish_io_error':
        (artifacts / 'openhands_final.json').mkdir()
    def finish(result):
        return {'id': 'finish_main', 'type': 'function', 'function': {'name': 'finish',
            'arguments': json.dumps({'message': 'Finish', 'result': result})}}
    def answer(call, number):
        if number > 1 and not paired_messages(call['body']['messages']):
            return 400, {'Content-Type': 'application/json'}, json.dumps({'error': {'type': 'invalid_request_error',
                'message': "An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'"}}).encode()
        if number == 1 and scenario == 'exact_quota':
            return completion(number, [io_call('single_read', 'read_code', {'path': 'original.py'})])
        if number == 1 and scenario == 'mixed_finish':
            trailing = io_call('must_not_write', 'write_document', {'path': 'trailing.md', 'content': 'not executed'})
            return completion(number, [finish({'extra': 'invalid'}), trailing])
        return completion(number, [finish({'review': 'Completed separately'})])
    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / 'sandbox_profiles'))
    with http_fixture(answer) as (url, calls):
        task = task.model_copy(update={'proxy_base_url': url, 'artifact_dir': artifacts,
            'max_tool_calls': 1 if scenario in {'exact_quota', 'final_slot_finish'} else 8, 'max_iterations': 4, 'max_active_seconds': 40})
        try:
            await adapter.start(task)
            await supervisor.wait(task.attempt_id)
            result = await adapter.collect_artifacts(task.attempt_id)
            events = [json.loads(line) for line in (artifacts / 'openhands_events.jsonl').read_text().splitlines()]
            if scenario == 'mixed_finish':
                assert result['execution_status'] == 'completed', result
                assert len(calls) == 2 and paired_messages(calls[1]['body']['messages'])
                assert not (artifacts / 'trailing.md').exists()
                outputs = [row for row in calls[1]['body']['messages'] if row.get('role') == 'tool']
                assert {row['tool_call_id'] for row in outputs} == {'finish_main', 'must_not_write'}
            elif scenario == 'final_slot_finish':
                assert result['execution_status'] == 'completed' and len(calls) == 1, result
                assert result['tool_calls'] == 1 and result['result'] == {'review': 'Completed separately'}
            else:
                assert result['execution_status'] == 'failed'
                assert len(calls) == 1
                assert result['runtime_failure_code'] == ('role_tool_limit_exceeded' if scenario == 'exact_quota' else 'worker_internal_error')
                observations = [row for row in events if row.get('tool_call_id') == 'finish_main' and ('observation' in row or 'error' in row)]
                assert str(artifacts) not in json.dumps(observations)
                assert not (artifacts / 'role_result.json').exists()
        finally:
            await supervisor.close()

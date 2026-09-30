import hashlib
import json
import os
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agentflow.adapters.codex import CodexExecAdapter
from agentflow.common import canonical_digest
from agentflow.runtime.failures import (
    classify_codex_failure,
    read_codex_failure,
    read_frozen_codex_failure,
    refine_codex_failure,
    runtime_failure_code,
    runtime_failure_message,
)

SCHEMA = {'type': 'object', 'properties': {'summary': {'type': 'string'}},
          'required': ['summary'], 'additionalProperties': False}
LIMIT_MESSAGE = 'stream disconnected before completion: Incomplete response returned, reason: max_output_tokens'
_ABSENT = object()


def write_evidence(tmp_path, attempt_id, *, state='completed', exit_code=0, events=None, final=_ABSENT,
                   reason=None, receipt=True):
    data = tmp_path / 'data'
    private = data / 'supervisor' / canonical_digest({'attempt_id': attempt_id}).split(':')[1]
    artifacts = data / 'attempt_artifacts' / canonical_digest(attempt_id).split(':')[1]
    home = data / 'codex_homes' / canonical_digest(attempt_id).split(':')[1]
    for directory in (private, artifacts, home):
        directory.mkdir(parents=True, mode=0o700)
    if receipt:
        (private / 'result.json').write_text(json.dumps({'attempt_id': attempt_id, 'execution_status': state,
            'exit_code': exit_code, 'reason': reason, 'fencing_token': 1, 'nonce': 'fixture-only'}))
    if events is not None:
        (private / 'stdout.jsonl').write_text('\n'.join(json.dumps(event) for event in events) + '\n')
    if final is not _ABSENT:
        (artifacts / 'codex_final.json').write_text(json.dumps(final))
    # Real adapter schemas may be 0644 inside a private 0700 attempt directory.
    (home / 'output_schema.json').write_text(json.dumps(SCHEMA))
    return data, private, artifacts


def adapter_fixture(task, data, private, artifacts, *, state='completed', exit_code=0, reason=None):
    task = task.model_copy(update={'artifact_dir': artifacts, 'output_schema': SCHEMA})
    handle = SimpleNamespace(state=state, reason=reason, exit_code=exit_code,
        input_fingerprint=task.input_fingerprint, fencing_token=task.fencing_token,
        stdout_path=str(private / 'stdout.jsonl'), stderr_path=str(private / 'stderr.log'))
    supervisor = SimpleNamespace(root=data / 'supervisor', inspect=AsyncMock(return_value=handle))
    return task, handle, CodexExecAdapter(supervisor, None)


@pytest.mark.parametrize('error', [
    {'type': 'error', 'message': LIMIT_MESSAGE + ' private-provider-key'},
    {'type': 'turn.failed', 'error': {'message': LIMIT_MESSAGE + ' private-provider-key'}},
    {'type': 'turn.failed', 'error': {'reason': 'max_output_tokens'}},
])
async def test_observed_output_limit_is_specific_and_never_exposes_provider_text(task, tmp_path, error):
    data, private, artifacts = write_evidence(tmp_path, task.attempt_id, state='failed', exit_code=1, events=[error])
    task, _, adapter = adapter_fixture(task, data, private, artifacts, state='failed', exit_code=1)
    result = await adapter.collect_artifacts(task.attempt_id, task)
    assert result['execution_status'] == 'failed'
    assert result['runtime_failure_code'] == read_codex_failure(data, task.attempt_id) == 'model_output_limit'
    assert result['summary'] == runtime_failure_message('model_output_limit')
    assert result['result'] is None and result['artifacts'] == []
    assert 'private-provider-key' not in json.dumps(result)
    assert runtime_failure_code(result['runtime_failure_code']) == 'model_output_limit'


async def test_observed_exit_zero_wrong_final_schema_stays_failed_without_repair(task, tmp_path):
    final = {'status': 'done', 'message': 'private-response-content', 'files_changed': ['src/app.mjs'],
             'validations_note': 'not run'}
    data, private, artifacts = write_evidence(tmp_path, task.attempt_id,
        events=[{'type': 'turn.completed'}], final=final)
    before = (artifacts / 'codex_final.json').read_bytes()
    task, _, adapter = adapter_fixture(task, data, private, artifacts)
    result = await adapter.collect_artifacts(task.attempt_id, task)
    assert result['execution_status'] == 'failed' and result['quality_result'] == 'unknown'
    assert result['runtime_failure_code'] == 'final_schema_invalid'
    assert result['errors'] == ['final_schema_invalid']
    assert result['result'] is None and result['artifacts'] == []
    assert 'private-response-content' not in json.dumps(result)
    assert read_codex_failure(data, task.attempt_id) == 'final_schema_invalid'
    assert (artifacts / 'codex_final.json').read_bytes() == before


@pytest.mark.parametrize(('state', 'exit_code', 'reason', 'events', 'final', 'expected'), [
    ('failed', 1, None, [], _ABSENT, 'worker_exited'),
    ('failed', 1, 'timeout', [{'type': 'error', 'message': LIMIT_MESSAGE}], _ABSENT, 'worker_timeout'),
    ('failed', 1, None, [{'type': 'turn.failed', 'error': {'message':
        'exceeded retry limit, last status: 429 Too Many Requests'}}], _ABSENT, 'model_rate_limited'),
    ('completed', 0, None, [{'type': 'turn.completed'}], _ABSENT, 'final_output_missing'),
    ('completed', 0, None, [{'type': 'turn.started'}], {'summary': 'done'}, 'terminal_event_missing'),
    ('completed', 0, None, None, {'summary': 'done'}, 'terminal_event_missing'),
    ('completed', 0, None, [{'type': 'private-event-secret'}, {'type': 'turn.completed'}],
     {'summary': 'done'}, 'invalid_model_output'),
    ('execution_unknown', None, 'process_disappeared_without_receipt', [], _ABSENT, 'execution_receipt_missing'),
])
async def test_collection_distinguishes_process_receipt_final_and_event_failures(
        task, tmp_path, state, exit_code, reason, events, final, expected):
    data, private, artifacts = write_evidence(tmp_path, task.attempt_id, state=state, exit_code=exit_code,
                                             reason=reason, events=events, final=final)
    task, _, adapter = adapter_fixture(task, data, private, artifacts, state=state, exit_code=exit_code, reason=reason)
    result = await adapter.collect_artifacts(task.attempt_id, task)
    assert result['runtime_failure_code'] == expected
    assert result['summary'] == runtime_failure_message(expected)
    assert result['result'] is None and result['artifacts'] == []
    assert read_codex_failure(data, task.attempt_id) == expected
    assert 'private-event-secret' not in json.dumps(result)


async def test_valid_code_result_remains_completed_and_does_not_claim_quality_passed(task, tmp_path):
    final = {'summary': 'Implemented reader updates.'}
    data, private, artifacts = write_evidence(tmp_path, task.attempt_id,
        events=[{'type': 'turn.completed'}], final=final)
    task, _, adapter = adapter_fixture(task, data, private, artifacts)
    result = await adapter.collect_artifacts(task.attempt_id, task)
    assert result['execution_status'] == 'completed' and result['quality_result'] == 'unknown'
    assert result['runtime_failure_code'] is None and result['result'] == final
    assert result['artifacts'] == [str(artifacts / 'codex_final.json')]
    assert read_codex_failure(data, task.attempt_id) is None


async def test_fence_mismatch_never_attributes_another_attempt_output(task, tmp_path):
    data, private, artifacts = write_evidence(tmp_path, task.attempt_id, state='failed', exit_code=1,
        events=[{'type': 'error', 'message': LIMIT_MESSAGE}])
    task, handle, adapter = adapter_fixture(task, data, private, artifacts, state='failed', exit_code=1)
    handle.fencing_token += 1
    result = await adapter.collect_artifacts(task.attempt_id, task)
    assert result['execution_status'] == 'execution_unknown'
    assert result['runtime_failure_code'] == 'execution_unconfirmed'


@pytest.mark.parametrize('event', [
    {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': LIMIT_MESSAGE}},
    {'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': LIMIT_MESSAGE}},
    {'type': 'error', 'message': 'A document mentions max_output_tokens but no confirmed truncation.'},
    {'type': 'error', 'message': 'An example mentions HTTP status 429 but not a failed request.'},
])
def test_output_limit_is_not_inferred_from_model_prose_or_a_bare_setting_name(event):
    assert classify_codex_failure(state='failed', exit_code=1, events=[event]) == 'worker_exited'


def test_rate_limit_message_does_not_attribute_every_429_to_the_provider():
    message = runtime_failure_message('model_rate_limited')
    assert '本轮运行额度' in message and '供应商状态' in message
    assert runtime_failure_code('model_request_limit_reached') == 'model_request_limit_reached'


def test_historical_lookup_reads_no_stderr_or_launch_credentials(tmp_path, monkeypatch):
    data, private, _ = write_evidence(tmp_path, 'attempt', state='failed', exit_code=1,
        events=[{'type': 'error', 'message': LIMIT_MESSAGE}])
    (private / 'stderr.log').write_text('Authorization: Bearer private-secret')
    (private / 'launch.json').write_text('private-environment-secret')
    original = os.open

    def guarded(path, *args, **kwargs):
        assert str(path).split('/')[-1] not in {'stderr.log', 'launch.json'}
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, 'open', guarded)
    assert read_codex_failure(data, 'attempt') == 'model_output_limit'


def test_missing_history_has_no_side_effects_and_existing_attempt_without_receipt_is_distinct(tmp_path):
    data = tmp_path / 'missing'
    assert read_codex_failure(data, '../attempt') is None and not data.exists()
    data, _, _ = write_evidence(tmp_path, 'attempt', receipt=False)
    assert read_codex_failure(data, 'attempt') == 'execution_receipt_missing'


@pytest.mark.parametrize('unsafe', ['symlink_final', 'symlink_events', 'hardlink_final', 'public_directory', 'large_events'])
def test_historical_lookup_rejects_unsafe_or_unbounded_evidence(tmp_path, unsafe):
    data, private, artifacts = write_evidence(tmp_path, 'attempt',
        events=[{'type': 'turn.completed'}], final={'summary': 'done'})
    target = artifacts / 'codex_final.json'
    if unsafe in {'symlink_final', 'hardlink_final'}:
        outside = tmp_path / 'private.json'
        outside.write_text('{"secret":"private-secret"}')
        target.unlink()
        target.symlink_to(outside) if unsafe == 'symlink_final' else os.link(outside, target)
    elif unsafe == 'symlink_events':
        target = private / 'stdout.jsonl'
        target.unlink()
        target.symlink_to(tmp_path / 'private.json')
    elif unsafe == 'public_directory':
        private.chmod(0o755)
    else:
        with (private / 'stdout.jsonl').open('wb') as file:
            file.truncate(16 * 1024 * 1024 + 1)
    assert read_codex_failure(data, 'attempt') is None


async def test_codex_launch_enforces_compact_final_schema_contract_without_changing_budget(task, tmp_path):
    data = tmp_path / 'runtime'
    supervisor = SimpleNamespace(root=data / 'supervisor', start=AsyncMock(return_value='started'))
    sandbox = SimpleNamespace(prepare=AsyncMock(return_value=([],
        {'verified': True, 'filesystem': 'hard', 'network': 'hard'})))
    adapter = CodexExecAdapter(supervisor, sandbox, '/usr/bin/true')
    adapter.probe = AsyncMock(return_value={'available': True, 'version': 'fixture'})
    adapter._help_text = AsyncMock(return_value='')
    task = task.model_copy(update={'allow_code_write': True, 'allowed_write_roots': [task.workspace],
                                  'output_schema': SCHEMA})
    assert await adapter.start(task) == 'started'
    launch = supervisor.start.await_args.args[0]
    assert launch.stdin_text.startswith(task.goal)
    assert 'one final JSON object' in launch.stdin_text and 'small, focused patches' in launch.stdin_text
    assert 'Do not echo long documents' in launch.stdin_text
    assert json.loads(launch.stdin_text.split('Final output schema: ', 1)[1]) == SCHEMA
    assert launch.output_schema == SCHEMA and launch.timeout_seconds == task.max_active_seconds
    assert task.max_output_tokens == 4096


def reasoning_events(tokens=16384):
    return [
        {'type': 'response.created', 'response': {'id': 'response-one', 'status': 'in_progress', 'output': []}},
        {'type': 'response.output_item.added', 'item': {'id': 'reasoning-one', 'type': 'reasoning', 'summary': []}},
        {'type': 'response.content_part.added', 'part': {'type': 'reasoning_text', 'text': ''}},
        {'type': 'response.reasoning_text.delta', 'delta': 'PRIVATE_REASONING_DO_NOT_EXPOSE'},
        {'type': 'response.content_part.done', 'part': {'type': 'reasoning_text', 'text': 'PRIVATE_REASONING_DO_NOT_EXPOSE'}},
        {'type': 'response.incomplete', 'response': {'id': 'response-one', 'status': 'incomplete', 'error': None,
            'incomplete_details': {'reason': 'max_output_tokens'}, 'max_output_tokens': 16384,
            'output': [{'id': 'reasoning-one', 'type': 'reasoning', 'summary': []}],
            'usage': {'input_tokens': 12, 'output_tokens': tokens,
                      'output_tokens_details': {'reasoning_tokens': tokens}}}},
    ]


def encode_events(events):
    return ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n' for event in events).encode()


async def write_invocation(store, task, *, events=None, tokens=16384, state='completed_unpriced'):
    identity = str(uuid4())
    directory = store.data_dir / 'model_invocations'
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / (identity + '.sse')
    raw = encode_events(events if events is not None else reasoning_events(tokens))
    path.write_bytes(raw)
    path.chmod(0o600)
    receipt = {'path': str(path), 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest(),
               'status_code': 200, 'media_type': 'text/event-stream'}
    def save(tx):
        if not tx.get('attempt', task.attempt_id):
            tx.put('attempt', task.attempt_id, {'run_id': task.run_id, 'iteration_id': task.iteration_id,
                'work_item_id': task.work_item_id, 'fencing_token': task.fencing_token,
                'input_fingerprint': task.input_fingerprint, 'status': 'running'})
            tx.put('dispatch_context', task.attempt_id, {'task': {'attempt_id': task.attempt_id,
                'run_id': task.run_id, 'iteration_id': task.iteration_id, 'work_item_id': task.work_item_id,
                'fencing_token': task.fencing_token, 'input_fingerprint': task.input_fingerprint,
                'profile_id': task.model_profile_id}})
        return tx.put('model_invocation', identity, {'operation_id': identity, 'attempt_id': task.attempt_id,
            'run_id': task.run_id, 'iteration_id': task.iteration_id, 'fencing_token': task.fencing_token,
            'input_fingerprint': task.input_fingerprint, 'profile_id': task.model_profile_id,
            'profile_revision': 1, 'protocol': 'responses', 'state': state,
            'created_at': '2026-09-22T00:00:00.000Z', 'usage': {'input_tokens': 12, 'output_tokens': tokens},
            'response_receipt': receipt})
    invocation = await store.command('fixture.invocation', identity, {}, save)
    return path, invocation


async def change_record(store, kind, identity, fields):
    def change(tx):
        record = tx.get(kind, identity)
        return tx.put(kind, identity, {**record, **fields}, record['revision'])
    return await store.command('fixture.change', str(uuid4()), {}, change)


async def diagnose(store, task, fallback='model_output_limit', **fields):
    return await refine_codex_failure(store, store.data_dir, task.attempt_id,
        **{'fencing_token': task.fencing_token, 'input_fingerprint': task.input_fingerprint, 'fallback': fallback, **fields})


@pytest.mark.parametrize('tokens', [16384, 16385])
@pytest.mark.parametrize('state', ['settled', 'completed_unpriced'])
async def test_verified_reasoning_exhaustion_uses_only_complete_usage_and_never_exposes_content(store, task, tokens, state, caplog):
    path, invocation = await write_invocation(store, task, tokens=tokens, state=state)
    before = path.read_bytes()
    records = await store.list('model_invocation')
    assert await diagnose(store, task) == 'reasoning_output_limit'
    message = runtime_failure_message('reasoning_output_limit')
    assert '本次响应的输出额度被推理耗尽' in message and '未返回可执行输出' in message
    assert '核验检查点' in message and '缩小步骤继续' in message and '连续无进展时暂停' in message
    assert 'PRIVATE_REASONING_DO_NOT_EXPOSE' not in message + caplog.text
    assert runtime_failure_code('reasoning_output_limit') == 'reasoning_output_limit'
    assert await store.list('model_invocation') == records and path.read_bytes() == before
    assert (await store.read('model_invocation', invocation['id']))['usage']['output_tokens'] == tokens


@pytest.mark.parametrize('event_type', ['response.content_part.added', 'response.content_part.done'])
@pytest.mark.parametrize('part', [{'type': 'output_text', 'text': 'reasoning_output_limit'},
    {'type': 'summary_text'}, {'type': 'unknown'}, {'text': 'reasoning_text'}, None, 'reasoning_text'])
async def test_only_reasoning_text_content_parts_can_support_reasoning_diagnosis(store, task, event_type, part):
    events = reasoning_events()
    next(event for event in events if event['type'] == event_type)['part'] = part
    await write_invocation(store, task, events=events)
    assert await diagnose(store, task) == 'model_output_limit'


@pytest.mark.parametrize('case', ['text_output', 'tool_output', 'custom_tool_output', 'tool_delta', 'text_delta',
    'completed', 'wrong_reason', 'missing_usage', 'partial_reasoning', 'no_reasoning_details', 'boolean_usage',
    'zero_tokens', 'wrong_response_id', 'event_name_mismatch', 'truncated_frame', 'missing_terminal',
    'extra_terminal', 'plain_prose', 'json_string', 'duplicate_usage', 'ledger_usage_mismatch'])
async def test_reasoning_diagnosis_rejects_unproved_terminal_or_output_shapes(store, task, case):
    events = reasoning_events()
    terminal = events[-1]['response']
    if case in {'text_output', 'tool_output', 'custom_tool_output'}:
        terminal['output'].append({'type': {'text_output': 'message', 'tool_output': 'function_call',
            'custom_tool_output': 'custom_tool_call'}[case], 'text': 'reasoning_output_limit'})
    elif case in {'tool_delta', 'text_delta'}:
        events.insert(1, {'type': 'response.function_call_arguments.delta' if case == 'tool_delta' else 'response.output_text.delta',
                          'delta': 'reasoning_output_limit'})
    elif case == 'completed':
        events[-1]['type'], terminal['status'] = 'response.completed', 'completed'
    elif case == 'wrong_reason':
        terminal['incomplete_details']['reason'] = 'content_filter'
    elif case == 'missing_usage':
        terminal.pop('usage')
    elif case == 'partial_reasoning':
        terminal['usage']['output_tokens_details']['reasoning_tokens'] -= 1
    elif case == 'no_reasoning_details':
        terminal['usage'].pop('output_tokens_details')
    elif case == 'boolean_usage':
        terminal['usage']['output_tokens_details']['reasoning_tokens'] = True
    elif case == 'zero_tokens':
        terminal['usage']['output_tokens'] = terminal['usage']['output_tokens_details']['reasoning_tokens'] = 0
    elif case == 'wrong_response_id':
        terminal['id'] = 'another-response'
    elif case == 'missing_terminal':
        events.pop()
    elif case == 'extra_terminal':
        events.append(events[-1])
    elif case == 'plain_prose':
        events = [{'type': 'response.output_text.delta', 'delta': json.dumps(terminal)}]
    path, invocation = await write_invocation(store, task, events=events)
    raw = path.read_bytes()
    if case == 'event_name_mismatch':
        raw = raw.replace(b'event: response.incomplete', b'event: response.completed')
    elif case == 'truncated_frame':
        raw = raw[:-2]
    elif case == 'json_string':
        raw = b'data: ' + json.dumps(json.dumps(events[-1])).encode() + b'\n\n'
    elif case == 'duplicate_usage':
        raw = raw.replace(b'"output_tokens": 16384', b'"output_tokens": 1, "output_tokens": 16384')
    if raw != path.read_bytes():
        path.write_bytes(raw)
        await change_record(store, 'model_invocation', invocation['id'], {'response_receipt': {
            **invocation['response_receipt'], 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}})
    if case == 'ledger_usage_mismatch':
        await change_record(store, 'model_invocation', invocation['id'], {'usage': {'input_tokens': 12, 'output_tokens': 1}})
    assert await diagnose(store, task) == 'model_output_limit'


@pytest.mark.parametrize('case', ['missing_file', 'bad_digest', 'missing_receipt', 'partial_path', 'external_path',
    'symlink_file', 'hardlink_file', 'symlink_directory', 'public_directory', 'public_file', 'oversized_file',
    'wrong_status', 'wrong_media', 'bad_operation_id', 'non_uuid_id'])
async def test_reasoning_diagnosis_only_reads_fixed_private_hash_verified_complete_sse(store, task, case):
    path, invocation = await write_invocation(store, task)
    receipt = invocation['response_receipt']
    if case == 'missing_file':
        path.unlink()
    elif case == 'bad_digest':
        await change_record(store, 'model_invocation', invocation['id'], {'response_receipt': {**receipt, 'digest': 'sha256:' + '0' * 64}})
    elif case == 'missing_receipt':
        await change_record(store, 'model_invocation', invocation['id'], {'response_receipt': None})
    elif case in {'partial_path', 'external_path'}:
        target = path.with_suffix('.partial') if case == 'partial_path' else store.data_dir / 'unrelated.sse'
        path.rename(target)
        await change_record(store, 'model_invocation', invocation['id'], {'response_receipt': {**receipt, 'path': str(target)}})
    elif case in {'symlink_file', 'hardlink_file'}:
        target = store.data_dir / 'outside.sse'
        path.rename(target)
        path.symlink_to(target) if case == 'symlink_file' else os.link(target, path)
    elif case == 'symlink_directory':
        target = store.data_dir / 'outside'
        path.parent.rename(target)
        path.parent.symlink_to(target, target_is_directory=True)
    elif case == 'public_directory':
        path.parent.chmod(0o755)
    elif case == 'public_file':
        path.chmod(0o644)
    elif case == 'oversized_file':
        with path.open('wb') as file:
            file.truncate(16 * 1024 * 1024 + 1)
    elif case in {'wrong_status', 'wrong_media'}:
        await change_record(store, 'model_invocation', invocation['id'], {'response_receipt': {
            **receipt, **({'status_code': 500} if case == 'wrong_status' else {'media_type': 'application/json'})}})
    elif case == 'bad_operation_id':
        await change_record(store, 'model_invocation', invocation['id'], {'operation_id': str(uuid4())})
    else:
        async def fake_list(kind):
            return [{**invocation, 'id': '../outside', 'operation_id': '../outside'}] if kind == 'model_invocation' else []
        store.list = fake_list
    assert await diagnose(store, task) == 'model_output_limit'


@pytest.mark.parametrize('field,value', [('attempt_id', 'other-attempt'), ('run_id', 'other-run'),
    ('iteration_id', 'other-iteration'), ('fencing_token', 2), ('fencing_token', True),
    ('input_fingerprint', 'different-input'), ('profile_id', 'other-profile'), ('protocol', 'chat_completions'),
    ('state', 'uncertain'), ('state', 'dispatching'), ('created_at', 'not-a-date'), ('created_at', '2026-09-22T00:00:00')])
async def test_invocation_must_match_frozen_attempt_and_be_fully_accounted(store, task, field, value):
    _, invocation = await write_invocation(store, task)
    await change_record(store, 'model_invocation', invocation['id'], {field: value})
    assert await diagnose(store, task) == 'model_output_limit'


@pytest.mark.parametrize('case', ['ambiguous_time', 'newer_complete', 'newer_unsettled', 'newer_reasoning'])
async def test_only_unique_latest_matching_invocation_can_explain_the_failure(store, task, case):
    await write_invocation(store, task)
    events = reasoning_events()
    if case == 'newer_complete':
        events[-1]['type'], events[-1]['response']['status'] = 'response.completed', 'completed'
    _, latest = await write_invocation(store, task, events=events)
    if case != 'ambiguous_time':
        await change_record(store, 'model_invocation', latest['id'], {'created_at': '2026-09-22T00:00:01Z',
            **({'state': 'dispatching'} if case == 'newer_unsettled' else {})})
    assert await diagnose(store, task) == ('reasoning_output_limit' if case == 'newer_reasoning' else 'model_output_limit')


async def test_reasoning_evidence_cannot_override_unknown_or_other_failures_or_stale_task_identity(store, task):
    await write_invocation(store, task)
    for code in ('execution_unconfirmed', 'worker_timeout', 'final_schema_invalid', None):
        assert await diagnose(store, task, fallback=code) == code
    assert await diagnose(store, task, fallback='PRIVATE_UNTRUSTED_TEXT') is None
    assert await diagnose(store, task, fencing_token=2) == 'model_output_limit'
    assert await diagnose(store, task, input_fingerprint='other-input') == 'model_output_limit'
    context = await store.read('dispatch_context', task.attempt_id)
    await change_record(store, 'dispatch_context', task.attempt_id, {'task': {**context['task'], 'profile_id': 'other-profile'}})
    assert await diagnose(store, task) == 'model_output_limit'


async def test_historical_reasoning_diagnosis_uses_attempt_identity_after_work_generation_advances(store, task):
    await write_invocation(store, task)
    await change_record(store, 'attempt', task.attempt_id, {'status': 'failed', 'generation': 3})
    replacement = {'run_id': task.run_id, 'generation': 4, 'attempt_id': 'new-attempt',
                   'fencing_token': task.fencing_token + 2, 'input_fingerprint': canonical_digest('new-generation')}
    await store.command('fixture.new-generation', 'advance', {},
                        lambda tx: tx.put('work_item', task.work_item_id, replacement))
    assert await diagnose(store, task) == 'reasoning_output_limit'
    assert await diagnose(store, task, fencing_token=replacement['fencing_token'],
                          input_fingerprint=replacement['input_fingerprint']) == 'model_output_limit'
    assert (await store.read('work_item', task.work_item_id))['generation'] == 4


async def test_reasoning_diagnosis_rechecks_ledger_after_file_read(store, task, monkeypatch):
    await write_invocation(store, task)
    original = store.list
    reads = 0
    async def changing(kind):
        nonlocal reads
        result = await original(kind)
        if kind == 'model_invocation':
            reads += 1
            if reads == 2:
                result[0]['revision'] += 1
        return result
    monkeypatch.setattr(store, 'list', changing)
    assert await diagnose(store, task) == 'model_output_limit'


async def test_reasoning_diagnosis_never_reads_requests_credentials_or_launcher_files(store, task, monkeypatch):
    path, _ = await write_invocation(store, task)
    original = os.open
    files = []
    def guarded(name, *args, **kwargs):
        assert str(name).split('/')[-1] not in {'launch.json', 'stderr.log', 'request.json', 'codex_final.json'}
        if str(name).endswith('.sse'):
            files.append(str(name))
        return original(name, *args, **kwargs)
    monkeypatch.setattr(os, 'open', guarded)
    assert await diagnose(store, task) == 'reasoning_output_limit'
    assert files == [path.name]


async def frozen_schema_records(store, task):
    def save(tx):
        tx.put('run', task.run_id, {'input_fingerprint': task.input_fingerprint, 'execution_state': 'running'})
        work = tx.put('work_item', task.work_item_id, {'run_id': task.run_id, 'step': 'implementation',
            'key': 'implementation', 'role': 'development', 'status': 'failed', 'quality_result': 'unknown',
            'generation': 1, 'fencing_token': task.fencing_token, 'input_fingerprint': task.input_fingerprint,
            'attempt_id': task.attempt_id, 'artifact_ids': [], 'dependencies': []})
        identity = {'run_id': task.run_id, 'iteration_id': task.iteration_id, 'work_item_id': task.work_item_id,
            'fencing_token': task.fencing_token, 'input_fingerprint': task.input_fingerprint}
        tx.put('attempt', task.attempt_id, {**identity, 'status': 'failed', 'generation': 1})
        tx.put('dispatch_context', task.attempt_id, {'task': {**identity, 'attempt_id': task.attempt_id, 'output_schema': SCHEMA}})
        tx.put('supervised_attempt', task.attempt_id, {**identity, 'attempt_id': task.attempt_id,
            'backend': 'codex_exec', 'state': 'completed', 'exit_code': 0})
        return work
    return await store.command('fixture.schema', 'frozen-schema', {}, save)


async def diagnose_frozen_schema(store, task):
    return await read_frozen_codex_failure(store, store.data_dir, task.attempt_id,
        run_id=task.run_id, work_item_id=task.work_item_id, fencing_token=task.fencing_token,
        input_fingerprint=task.input_fingerprint)


async def test_schema_diagnosis_survives_home_cleanup_in_product_and_workflow_views(store, task, tmp_path, monkeypatch):
    from agentflow.control.presentation import RunPresentationService
    from agentflow.control.products import ProductService
    data, private, artifacts = write_evidence(tmp_path, task.attempt_id,
        events=[{'type': 'turn.completed'}], final={'unexpected': 'PRIVATE_FINAL_CONTENT'})
    work = await frozen_schema_records(store, task)
    before = (artifacts / 'codex_final.json').read_bytes()
    home = data / 'codex_homes' / canonical_digest(task.attempt_id).split(':')[1]
    shutil.rmtree(home)
    assert read_codex_failure(data, task.attempt_id) is None
    (private / 'stderr.log').write_text('PRIVATE_RAW_ERROR')
    original = os.open
    def only_existing_safe_evidence(name, *args, **kwargs):
        assert str(name).split('/')[-1] not in {'stderr.log', 'launch.json', 'request.json', 'output_schema.json'}
        return original(name, *args, **kwargs)
    monkeypatch.setattr(os, 'open', only_existing_safe_evidence)
    assert await diagnose_frozen_schema(store, task) == 'final_schema_invalid'
    settings = SimpleNamespace(data_dir=data)
    products = object.__new__(ProductService)
    products.store, products.settings = store, settings
    reasons = await products._failure_reasons([work])
    view = await RunPresentationService(store, None, settings).workflow(task.run_id)
    assert reasons == ['代码开发：' + runtime_failure_message('final_schema_invalid')]
    assert view['stages'][0]['tasks'][0]['blocking_reason'] == runtime_failure_message('final_schema_invalid')
    assert 'PRIVATE_' not in json.dumps({'reasons': reasons, 'view': view})
    assert (artifacts / 'codex_final.json').read_bytes() == before
    assert await store.read('work_item', task.work_item_id) == work


@pytest.mark.parametrize('field,value', [('attempt_id', 'other-attempt'), ('run_id', 'other-run'),
    ('work_item_id', 'other-work'), ('iteration_id', 'other-iteration'), ('fencing_token', 2),
    ('fencing_token', True), ('input_fingerprint', 'other-input')])
async def test_wrong_dispatch_identity_never_supplies_schema_after_home_cleanup(store, task, tmp_path, field, value):
    data, _, _ = write_evidence(tmp_path, task.attempt_id, events=[{'type': 'turn.completed'}], final={'unexpected': 'value'})
    await frozen_schema_records(store, task)
    context = await store.read('dispatch_context', task.attempt_id)
    await change_record(store, 'dispatch_context', task.attempt_id, {'task': {**context['task'], field: value}})
    shutil.rmtree(data / 'codex_homes' / canonical_digest(task.attempt_id).split(':')[1])
    assert await diagnose_frozen_schema(store, task) is None


@pytest.mark.parametrize('schema', [None, False, {'type': 'invalid-schema-type'}, {'description': 'x' * (256 * 1024)}])
async def test_missing_or_invalid_frozen_schema_preserves_original_home_fallback(store, task, tmp_path, schema):
    write_evidence(tmp_path, task.attempt_id, events=[{'type': 'turn.completed'}], final={'unexpected': 'value'})
    await frozen_schema_records(store, task)
    context = await store.read('dispatch_context', task.attempt_id)
    await change_record(store, 'dispatch_context', task.attempt_id, {'task': {**context['task'], 'output_schema': schema}})
    assert await diagnose_frozen_schema(store, task) == 'final_schema_invalid'


async def test_missing_dispatch_records_keep_the_existing_bounded_fallback(store, task, tmp_path):
    data, _, _ = write_evidence(tmp_path, task.attempt_id,
        events=[{'type': 'turn.completed'}], final={'unexpected': 'value'})
    assert await diagnose_frozen_schema(store, task) == 'final_schema_invalid'
    shutil.rmtree(data / 'codex_homes' / canonical_digest(task.attempt_id).split(':')[1])
    assert await diagnose_frozen_schema(store, task) is None


async def test_frozen_schema_identity_is_rechecked_after_evidence_read(store, task, tmp_path, monkeypatch):
    data, _, _ = write_evidence(tmp_path, task.attempt_id, events=[{'type': 'turn.completed'}], final={'unexpected': 'value'})
    await frozen_schema_records(store, task)
    shutil.rmtree(data / 'codex_homes' / canonical_digest(task.attempt_id).split(':')[1])
    original = store.read
    reads = 0
    async def changed_context(kind, identity):
        nonlocal reads
        value = await original(kind, identity)
        if kind == 'dispatch_context':
            reads += 1
            if reads == 2:
                return {**value, 'task': {**value['task'], 'output_schema': {}}}
        return value
    monkeypatch.setattr(store, 'read', changed_context)
    assert await diagnose_frozen_schema(store, task) is None


@pytest.mark.parametrize('tool_id,complete,count', [('real-tool', True, 2), ('', False, 1), (' \t ', False, 1)])
async def test_tool_accounting_deduplicates_real_ids_and_rejects_blank_identity(task, tmp_path, tool_id, complete, count):
    events = [{'type': stage, 'item': {'type': 'command_execution', 'id': tool_id}}
              for stage in ('item.started', 'item.updated', 'item.completed')]
    events += [{'type': 'item.completed', 'item': {'type': 'file_change', 'id': 'second-tool'}},
               {'type': 'turn.failed', 'error': {'message': 'known stopped failure'}}]
    data, private, artifacts = write_evidence(tmp_path, task.attempt_id, state='failed', exit_code=1, events=events)
    task, _, adapter = adapter_fixture(task, data, private, artifacts, state='failed', exit_code=1)
    result = await adapter.collect_artifacts(task.attempt_id, task)
    assert result['execution_status'] == 'failed'
    assert result['tool_observation_complete'] is complete
    assert result['observed_tool_calls'] == count

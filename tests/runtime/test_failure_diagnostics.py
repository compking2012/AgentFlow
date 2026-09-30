import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agentflow.adapters.openhands import OpenHandsRoleAdapter
from agentflow.common import canonical_digest
from agentflow.runtime.failures import (
    classify_codex_failure,
    classify_role_error,
    read_role_failure,
    runtime_failure_code,
    runtime_failure_message,
)


@pytest.mark.parametrize('kind,message,code', [
    ('ConversationRunError', "'PromptTokensDetailsWrapper' object has no attribute 'cache_creation_tokens'",
     'response_usage_incompatible'),
    ('AuthenticationError', 'private-provider-key', 'model_authentication_failed'),
    ('RateLimitError', 'private-scoped-token', 'model_rate_limited'),
    ('APIConnectionError', 'https://user:private-password@example.com', 'model_connection_failed'),
    ('ValidationError', 'private-response-content', 'invalid_model_output'),
    ('RuntimeError', 'OpenHands did not produce a schema-valid finish result', 'invalid_model_output'),
    ('UnrecognizedError', 'private-provider-key', 'worker_internal_error'),
])
def test_error_classifier_emits_only_closed_codes_and_fixed_messages(kind, message, code):
    assert classify_role_error({'type': kind, 'message': message}) == code
    assert 'private-' not in runtime_failure_message(code)
    assert classify_role_error({'type': ['bad'], 'message': message}) is None


@pytest.mark.parametrize('label,code', [
    ('litellm.AuthenticationError', 'model_authentication_failed'),
    ('openai._exceptions.PermissionDeniedError', 'model_authentication_failed'),
    ('litellm.exceptions.RateLimitError', 'model_rate_limited'),
    ('BadRequestError', 'model_request_failed'),
    ('litellm.InternalServerError', 'model_request_failed'),
    ('openai.APIConnectionError', 'model_connection_failed'),
    ('APITimeoutError', 'worker_timeout'),
])
def test_conversation_wrapper_preserves_explicit_provider_exception_category(label, code):
    # Shape observed in real SDK HTTP500 fixture role_error.json. Only the label
    # changes; the surrounding private message must never be returned to users.
    message = f'Conversation failed id=fixture: {label}: private-provider-response api_key=private-secret'
    assert classify_role_error({'type': 'ConversationRunError', 'message': message}) == code
    assert 'private-' not in runtime_failure_message(code)


@pytest.mark.parametrize('message', [
    'Provider response contains 401 and 429, api_key=private-secret',
    'The model mentioned AuthenticationError without an exception label',
    'NotAuthenticationError: private-provider-response',
    "{'AuthenticationError': 'private-provider-response'}",
])
def test_wrapper_does_not_infer_provider_error_from_status_numbers_or_arbitrary_text(message):
    assert classify_role_error({'type': 'ConversationRunError', 'message': message}) == 'worker_internal_error'


async def test_failed_adapter_collects_private_diagnostic_without_starting_any_process(task, tmp_path):
    data = tmp_path / 'data'
    artifacts = data / 'attempt_artifacts' / canonical_digest(task.attempt_id).split(':')[1]
    artifacts.mkdir(parents=True, mode=0o700)
    task = task.model_copy(update={'artifact_dir': artifacts})
    evidence = artifacts / 'role_error.json'
    evidence.write_text(json.dumps({'type': 'ConversationRunError', 'message':
        "'PromptTokensDetailsWrapper' object has no attribute 'cache_creation_tokens'; private-scoped-token"}))
    evidence.chmod(0o600)
    handle = SimpleNamespace(state='failed', reason=None, input_fingerprint=task.input_fingerprint,
        fencing_token=task.fencing_token, stdout_path='/protected/stdout', stderr_path='/protected/stderr')
    supervisor = SimpleNamespace(root=data / 'supervisor', inspect=AsyncMock(return_value=handle))
    adapter = OpenHandsRoleAdapter(supervisor, None)
    result = await adapter.collect_artifacts(task.attempt_id, task)
    assert result['execution_status'] == 'failed' and result['quality_result'] == 'unknown'
    assert result['artifacts'] == [] and result['runtime_failure_code'] == 'response_usage_incompatible'
    assert result['summary'] == runtime_failure_message('response_usage_incompatible')
    assert 'private-scoped-token' not in json.dumps(result)
    handle.fencing_token += 1
    assert (await adapter.collect_artifacts(task.attempt_id, task))['runtime_failure_code'] == 'worker_exited'


def test_missing_diagnostic_evidence_has_no_side_effects(tmp_path):
    assert read_role_failure(tmp_path / 'missing', 'attempt') is None
    assert not (tmp_path / 'missing').exists()


@pytest.mark.parametrize('code', ['model_output_limit', 'reasoning_output_limit'])
def test_private_worker_protocol_code_is_preserved_without_parsing_message_text(tmp_path, code):
    artifact = tmp_path / 'attempt_artifacts' / canonical_digest('attempt').split(':')[1]
    artifact.mkdir(parents=True, mode=0o700)
    path = artifact / 'role_error.json'
    path.write_text(json.dumps({'type': 'ConversationRunError', 'message': 'PRIVATE_ERROR_TEXT', 'runtime_failure_code': code}))
    path.chmod(0o600)
    assert read_role_failure(tmp_path, 'attempt') == code
    assert 'PRIVATE_ERROR_TEXT' not in runtime_failure_message(code)
    assert classify_role_error({'type': 'RuntimeError', 'message': code}) == 'worker_internal_error'


def test_unknown_or_malformed_worker_codes_do_not_become_output_limit():
    for code in ('PRIVATE_RESPONSE_VALUE', {}, ['model_output_limit'], True):
        assert classify_role_error({'type': 'RuntimeError', 'message': 'private',
                                    'runtime_failure_code': code}) == 'worker_internal_error'
    assert classify_role_error({'type': 'LengthFinishReasonError', 'message': 'private'}) == 'model_output_limit'
    assert classify_role_error({'type': 'ConversationRunError',
        'message': 'Conversation failed: openai.LengthFinishReasonError: private'}) == 'model_output_limit'


def test_only_control_error_events_can_report_proxy_output_limit():
    assert classify_codex_failure(state='failed', exit_code=1, events=[
        {'type': 'error', 'code': 'model_output_limit', 'message': 'private'}]) == 'model_output_limit'
    assert classify_codex_failure(state='completed', exit_code=0, events=[
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'model_output_limit'}}]) is None


@pytest.mark.parametrize('code', ['coding_budget_exhausted', 'coding_no_progress',
                                  'invalid_coding_checkpoint', 'stale_coding_step'])
def test_coding_progress_failures_keep_distinct_closed_diagnostics(code):
    assert runtime_failure_code(code) == code != 'model_output_limit'
    assert runtime_failure_message(code)
    assert '模型单次输出达到上限' not in runtime_failure_message(code)


@pytest.mark.parametrize('kind,message,expected', [
    ('BadRequestError', "An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'", 'role_tool_transcript_invalid'),
    ('ConversationRunError', "Conversation failed: litellm.BadRequestError: An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id' (insufficient tool messages following tool_calls).", 'role_tool_transcript_invalid'),
    ('BadRequestError', 'Invalid max_tokens parameter', 'model_request_failed'),
    ('BadRequestError', 'tool_calls value unsupported', 'model_request_failed'),
    ('RuntimeError', "An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'", 'worker_internal_error'),
])
def test_tool_transcript_diagnostic_is_a_closed_provider_bad_request(kind, message, expected):
    assert classify_role_error({'type': kind, 'message': message}) == expected


@pytest.mark.parametrize('backend', ['codex', 'role'])
@pytest.mark.parametrize('expired', [False, True])
async def test_collector_expiry_uses_controller_receipt_not_provider_403(task, store, tmp_path, backend, expired):
    from datetime import UTC, datetime, timedelta

    from agentflow.adapters.codex import CodexExecAdapter
    from agentflow.models.profiles import AttemptContext
    from agentflow.runtime.task_authorization import record_expiry
    artifacts = tmp_path / 'attempt_artifacts' / canonical_digest(task.attempt_id).split(':')[1]
    artifacts.mkdir(parents=True, mode=0o700)
    task = task.model_copy(update={'artifact_dir': artifacts})
    error = artifacts / 'role_error.json'
    error.write_text(json.dumps({'type': 'PermissionDeniedError', 'message': 'PRIVATE_VENDOR_403'}))
    error.chmod(0o600)
    context = AttemptContext(attempt_id=task.attempt_id, run_id=task.run_id, iteration_id=task.iteration_id,
        model_profile_id=task.model_profile_id, fencing_token=task.fencing_token, input_fingerprint=task.input_fingerprint,
        expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(), max_model_requests=10, max_output_tokens=64)
    def seed(tx):
        tx.put('attempt', task.attempt_id, {'run_id': task.run_id, 'iteration_id': task.iteration_id,
            'work_item_id': task.work_item_id, 'generation': 1, 'status': 'failed',
            'fencing_token': task.fencing_token, 'input_fingerprint': task.input_fingerprint})
        tx.put('work_item', task.work_item_id, {'attempt_id': task.attempt_id, 'status': 'failed',
            'fencing_token': task.fencing_token, 'input_fingerprint': task.input_fingerprint})
        return tx.put('task_authorization', 'fixture-authority', context.model_dump())
    raw = await store.command('fixture', 'collector-expiry', {}, seed)
    if expired:
        await record_expiry(store, raw, context, await store.read('attempt', task.attempt_id),
                            await store.read('work_item', task.work_item_id), 'responses' if backend == 'codex' else 'chat_completions')
        await seed_transport_diagnostic(store, task, backend)
    handle = SimpleNamespace(state='failed', reason=None, exit_code=1, active_seconds=.1,
        input_fingerprint=task.input_fingerprint, fencing_token=task.fencing_token,
        stdout_path=str(tmp_path / 'missing-stdout'), stderr_path=str(tmp_path / 'missing-stderr'))
    supervisor = SimpleNamespace(root=tmp_path / 'supervisor', store=store, inspect=AsyncMock(return_value=handle))
    adapter = CodexExecAdapter(supervisor, None) if backend == 'codex' else OpenHandsRoleAdapter(supervisor, None)
    result = await adapter.collect_artifacts(task.attempt_id, task)
    assert result['runtime_failure_code'] == ('task_authorization_expired' if expired else
                                            'worker_exited' if backend == 'codex' else 'model_authentication_failed')
    assert 'PRIVATE_VENDOR_403' not in result['summary']


async def seed_transport_diagnostic(store, task, backend):
    identity = {'attempt_id': task.attempt_id, 'run_id': task.run_id, 'iteration_id': task.iteration_id,
        'fencing_token': task.fencing_token, 'input_fingerprint': task.input_fingerprint}
    protocol = 'responses' if backend == 'codex' else 'chat_completions'
    def seed(tx):
        if not tx.get('attempt', task.attempt_id):
            tx.put('attempt', task.attempt_id, {**identity, 'work_item_id': task.work_item_id, 'generation': 1, 'status': 'failed'})
        tx.put('dispatch_context', task.attempt_id, {'task': {**identity, 'work_item_id': task.work_item_id,
            'profile_id': task.model_profile_id, 'step': 'implementation' if backend == 'codex' else 'code_review'}})
        tx.put('task_authorization', 'fixture-transport-authority', {**identity,
            'model_profile_id': task.model_profile_id, 'expected_profile_revision': 1, 'protocols': [protocol]})
        tx.put('model_attempt_budget', task.attempt_id, {'attempt_id': task.attempt_id, 'request_count': 1, 'uncertain_invocations': 1})
        return tx.put('model_invocation', 'fixture-invocation', {**identity, 'operation_id': 'fixture-invocation',
            'profile_id': task.model_profile_id, 'profile_revision': 1, 'protocol': protocol,
            'request_ordinal': 1, 'state': 'uncertain', 'reason': 'dispatch_result_unknown',
            'created_at': '2026-01-01T00:00:00+00:00', 'transport_failure': {
                'version': 1, 'origin': 'model_proxy', 'invocation_id': 'fixture-invocation',
                'phase': 'send', 'kind': 'read_timeout', 'delivery': 'unknown',
                'observed_at': '2026-01-01T00:00:01+00:00'}})
    return await store.command('fixture', 'transport-diagnostic', {}, seed)


@pytest.mark.parametrize('backend', ['codex', 'role'])
@pytest.mark.parametrize('state,reason,expected', [
    ('failed', None, 'model_transport_read_timeout'),
    ('failed', 'timeout', 'worker_timeout'),
    ('execution_unknown', None, 'execution_unconfirmed'),
])
async def test_collectors_refine_only_generic_failed_outcome_from_proxy_metadata(task, store, tmp_path, backend, state, reason, expected):
    from agentflow.adapters.codex import CodexExecAdapter
    artifacts = tmp_path / 'attempt_artifacts' / canonical_digest(task.attempt_id).split(':')[1]
    artifacts.mkdir(parents=True, mode=0o700)
    task = task.model_copy(update={'artifact_dir': artifacts})
    await seed_transport_diagnostic(store, task, backend)
    handle = SimpleNamespace(state=state, reason=reason, exit_code=1, active_seconds=.1,
        input_fingerprint=task.input_fingerprint, fencing_token=task.fencing_token,
        stdout_path=str(tmp_path / 'missing-stdout'), stderr_path=str(tmp_path / 'missing-stderr'))
    supervisor = SimpleNamespace(root=tmp_path / 'supervisor', store=store, inspect=AsyncMock(return_value=handle))
    adapter = CodexExecAdapter(supervisor, None) if backend == 'codex' else OpenHandsRoleAdapter(supervisor, None)
    before = {kind: await store.list(kind) for kind in ('model_invocation', 'model_attempt_budget', 'budget_account')}
    result = await adapter.collect_artifacts(task.attempt_id, task)
    assert result['runtime_failure_code'] == expected
    assert result['summary'] == runtime_failure_message(expected)
    assert {kind: await store.list(kind) for kind in before} == before
    handle.fencing_token += 1
    result = await adapter.collect_artifacts(task.attempt_id, task)
    assert result['runtime_failure_code'] != 'model_transport_read_timeout'

"""Regression: completed coding checkpoints must not trigger endless status checks."""
import copy
import json

import pytest

from agentflow.models.provider import ModelProvider


def request():
    return {'model': 'fixture-model', 'input': [{'role': 'user', 'content': 'Implement the assigned tests.'}],
            'text': {'format': {'type': 'json_schema', 'name': 'codex_output_schema', 'schema': {
                'type': 'object', 'properties': {'summary': {'type': 'string'},
                    'status': {'type': 'string', 'enum': ['continue', 'complete']},
                    'next_action': {'type': 'string'}},
                'required': ['summary', 'status', 'next_action'], 'additionalProperties': False}}},
            'tools': [{'type': 'function', 'name': 'exec_command'}], 'tool_choice': 'auto'}


def check(payload, command, output=' M tests/web.spec.mjs\n', *, code=0, name='exec_command'):
    identity = f'call-{len(payload["input"])}'
    payload['input'].extend([
        {'type': 'function_call', 'name': name, 'call_id': identity,
         'arguments': json.dumps({'cmd': command})},
        {'type': 'function_call_output', 'call_id': identity,
         'output': f'Chunk ID: {identity}\nWall time: 0.001 seconds\nProcess exited with code {code}\n'
                   f'Original token count: 10\nOutput:\n{output}'},
    ])


def repeated(payload):
    for _ in range(3):
        check(payload, 'git status --short')
        check(payload, 'git diff --stat', 'tests/web.spec.mjs | 300 lines\n')


def normalize(payload, context, profile_factory):
    return ModelProvider().normalize_request(profile_factory('http://127.0.0.1'), context, 'responses', payload)[0]


def test_unchanged_successful_check_cycle_requests_receipt_without_deciding_completion(context, profile_factory):
    payload = request()
    repeated(payload)
    original = copy.deepcopy(payload)
    body = normalize(payload, context, profile_factory)
    assert len(body['input']) == len(original['input']) + 1
    assert body['input'][:-1] == original['input']
    assert body['input'][-1]['role'] == 'developer'
    assert body['tool_choice'] == 'none' and body['tools'] == original['tools']
    assert body['text'] == original['text'] and body['max_output_tokens'] == context.max_output_tokens
    assert payload == original
    # Replayed/resubmitted requests do not accumulate duplicate reminders.
    assert normalize(body, context, profile_factory)['input'] == body['input']


@pytest.mark.parametrize('change', ['write', 'failed_check', 'changed_output', 'pending', 'foreign_tool', 'unknown_shell'])
def test_new_work_or_unverified_checks_do_not_trigger_completion_advice(context, profile_factory, change):
    payload = request()
    repeated(payload)
    if change == 'write':
        check(payload, 'printf new > tests/web.spec.mjs', '')
    elif change == 'failed_check':
        check(payload, 'node --check tests/web.spec.mjs', 'SyntaxError', code=1)
    elif change == 'changed_output':
        check(payload, 'git status --short', ' M tests/web.spec.mjs\n M tests/api.spec.mjs\n')
    elif change == 'pending':
        payload['input'].append({'type': 'function_call', 'name': 'exec_command', 'call_id': 'pending',
                                 'arguments': '{"cmd":"git status --short"}'})
    elif change == 'foreign_tool':
        check(payload, 'git status --short', name='apply_patch')
    else:
        check(payload, 'git status --short; custom-command')
    assert normalize(payload, context, profile_factory)['input'] == payload['input']


def test_non_coding_schema_and_regular_inspection_are_unchanged(context, profile_factory):
    payload = request()
    check(payload, 'git status --short')
    assert normalize(payload, context, profile_factory)['input'] == payload['input']


def test_real_status_diff_syntax_cycle_with_varying_done_markers(context, profile_factory):
    payload = request()
    for marker in ['VERIFIED_OK', 'DONE', 'FINAL_READY']:
        check(payload, 'git -C . status --short')
        check(payload, 'git -C . diff --stat', 'tests/web.spec.mjs | 300 lines\n')
        check(payload, f'git -C . diff --check && node --check tests/web.spec.mjs && echo {marker}', marker + '\n')
    assert len(normalize(payload, context, profile_factory)['input']) == len(payload['input']) + 1
    repeated(payload)
    payload['text']['format']['schema']['properties']['status']['enum'] = ['passed', 'failed']
    assert normalize(payload, context, profile_factory)['input'] == payload['input']


def test_checks_with_explicit_coding_directory_get_the_same_loop_feedback(context, profile_factory):
    payload = request()
    for _ in range(3):
        check(payload, 'cd "/project path/.agentflow/workspaces/current" && git status --short')
        check(payload, 'cd "/project path/.agentflow/workspaces/current" && git diff --stat', 'tests/unit.test.mjs | 327 lines\n')
    assert len(normalize(payload, context, profile_factory)['input']) == len(payload['input']) + 1


def test_repeated_stickywall_syntax_pass_loop_requests_an_honest_progress_receipt(context, profile_factory):
    payload = request()
    for _ in range(3):
        check(payload, 'cd /project/.agentflow/workspaces/current && node --check tests/web.spec.mjs && echo PASS', 'PASS\n')
    body = normalize(payload, context, profile_factory)
    assert body['tool_choice'] == 'none'
    assert body['tools'] == payload['tools']
    assert body['text'] == payload['text']
    assert 'status=continue' in body['input'][-1]['content']
    assert body['max_output_tokens'] == context.max_output_tokens


def test_checks_of_different_directories_are_not_one_repeated_observation(context, profile_factory):
    payload = request()
    for directory in ['/one', '/two', '/three']:
        check(payload, f'cd {directory} && git status --short')
        check(payload, f'cd {directory} && git diff --stat', 'tests/unit.test.mjs | 327 lines\n')
    assert normalize(payload, context, profile_factory)['input'] == payload['input']


def test_hint_never_pushes_a_valid_request_over_its_byte_limit(context, profile_factory):
    payload = request()
    repeated(payload)
    profile = profile_factory('http://127.0.0.1', max_request_bytes=1024)
    # Leave room for normal protocol fields, but not an advisory paragraph.
    limit = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) + 100
    profile = profile.model_copy(update={'max_request_bytes': limit})
    body, _ = ModelProvider().normalize_request(profile, context, 'responses', payload)
    assert body['input'] == payload['input']


@pytest.mark.parametrize('call_id', [[], {}, None])
def test_malformed_tool_evidence_is_not_a_completion_signal(context, profile_factory, call_id):
    payload = request()
    repeated(payload)
    payload['input'].append({'type': 'function_call_output', 'call_id': call_id, 'output': ''})
    assert normalize(payload, context, profile_factory)['input'] == payload['input']


def test_repeated_completion_markers_request_a_receipt_without_marking_work_complete(context, profile_factory):
    payload = request()
    for command, output in [('echo done', 'done\n'), ('echo ok', 'ok\n'), ('true', '')]:
        check(payload, command, output)
    before = copy.deepcopy(payload)
    body = normalize(payload, context, profile_factory)
    assert body['tool_choice'] == 'none'
    assert body['tools'] == before['tools'] and body['text'] == before['text']
    assert body['input'][:-1] == before['input']
    assert len(body['input']) == len(before['input']) + 1
    assert body['max_output_tokens'] == context.max_output_tokens
    assert payload == before
    assert normalize(body, context, profile_factory) == body


@pytest.mark.parametrize('command', ['echo done > result.txt', 'echo "$(touch changed)"', 'node -e "write()"'])
def test_real_or_unrecognized_work_breaks_a_completion_marker_streak(context, profile_factory, command):
    payload = request()
    check(payload, 'echo done', 'done\n')
    check(payload, 'true', '')
    check(payload, command, '')
    check(payload, 'echo ok', 'ok\n')
    assert normalize(payload, context, profile_factory)['tool_choice'] == 'auto'


@pytest.mark.parametrize('has_choice', [True, False])
def test_receipt_hint_respects_request_size_and_restores_original_tool_choice(context, profile_factory, has_choice):
    payload = request()
    if not has_choice:
        payload.pop('tool_choice')
    for command in ['echo done', 'echo ok', 'true']:
        check(payload, command, '')
    limit = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) + 100
    profile = profile_factory('http://127.0.0.1', max_request_bytes=limit)
    body, _ = ModelProvider().normalize_request(profile, context, 'responses', payload)
    assert body['input'] == payload['input']
    assert ('tool_choice' in body) == has_choice
    assert body.get('tool_choice') == payload.get('tool_choice')

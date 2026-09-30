"""Native SDK staged results and truncation rejection, backed by local HTTP fixtures."""
import json
import platform
from pathlib import Path

import httpx
import pytest
from test_role_read_efficiency import observation, response

from agentflow.adapters.openhands import OpenHandsRoleAdapter
from agentflow.adapters.openhands.output_builder import (
    ResultBuilderStore,
    export_partial,
    import_partial,
    result_identity,
)
from agentflow.adapters.openhands.worker import _trusted_output_failure
from agentflow.common import canonical_digest
from agentflow.control.scheduler import DOCUMENT_SCHEMA, TEST_PLAN_SCHEMA
from agentflow.control.stage_context import StageContext
from agentflow.runtime.sandbox import MacSeatbeltSandbox
from agentflow.runtime.supervisor import Supervisor
from agentflow.storage import LocalArtifactStore


def io(identity, operation, **arguments):
    return identity, 'agentflow_io', {'operation': operation, 'arguments': arguments}


def base_fields():
    return {'title': 'Bounded result fixture', 'summary': 'Complete required content', 'sources': [], 'unknowns': []}


def require_native():
    if platform.system() != 'Darwin':
        pytest.skip('Native worker requires the supported macOS sandbox')


@pytest.mark.parametrize('mode', ['inline', 'staged'])
async def test_large_model_uses_configured_allowance_for_complete_role_output(task, store, tmp_path, http_fixture, mode):
    require_native()
    content = '完整测试内容。' * (1300 if mode == 'inline' else 300)
    expected = {**base_fields(), 'content': content}
    if mode == 'inline':
        assert len(json.dumps(expected, ensure_ascii=False).encode()) > 16384
    else:
        assert len(json.dumps(content, ensure_ascii=True).encode()) > 4096

    def answer(call, number):
        if mode == 'inline':
            assert number == 1
            action = ('finished', 'finish', {'message': 'Complete result', 'result': expected})
        elif number == 1:
            action = io('begin', 'result_begin', fields=base_fields(), streamed_fields={'content': 'string'}, request_id='large')
        elif number == 2:
            prior = observation(call, 'begin')
            action = io('section', 'result_append', result_ref=prior['result_ref'], field='content',
                chunk_id='section', expected_offset=0, value=content, final=True)
        else:
            assert number == 3
            prior = observation(call, 'section')
            action = ('finished', 'finish', {'message': 'Complete sealed result', 'result_ref': prior['result_ref']})
        return response(number, [action])

    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / 'sandbox_profiles'))
    with http_fixture(answer) as (url, calls):
        envelope = task.model_copy(update={'role': 'product', 'proxy_base_url': url, 'max_output_tokens': 65536,
            'artifact_dir': tmp_path / 'attempt_artifacts' / canonical_digest(task.attempt_id).split(':')[1],
            'max_active_seconds': 60, 'max_tool_calls': 10, 'max_iterations': 10,
            'output_schema': DOCUMENT_SCHEMA, 'goal': 'Return complete required Chinese content within the configured allowance.'})
        try:
            await adapter.start(envelope)
            await supervisor.wait(envelope.attempt_id)
            result = await adapter.collect_artifacts(envelope.attempt_id)
            assert result['execution_status'] == 'completed', result
            assert result['result'] == expected
            assert len(calls) == (1 if mode == 'inline' else 3)
            assert all(call['body']['max_completion_tokens'] == 65536 for call in calls)
        finally:
            await supervisor.close()


async def test_long_document_and_test_cases_finish_by_reference_without_repeating_full_output(task, store, tmp_path, http_fixture):
    require_native()
    parts = [f'Section {index:02}: ' + ('complete criteria. ' * 24) + '\n' for index in range(24)]
    cases = [{'case_id': f'case-{index}', 'requirement_id': f'R-{index}', 'target_config_id': 'target',
              'phase': 'unit', 'framework_case_ids': [f'fixture::{index}']} for index in range(20)]
    batches = [cases[index:index + 2] for index in range(0, len(cases), 2)]
    expected = {**base_fields(), 'content': ''.join(parts), 'test_cases': cases}
    steps = [('content', part, index == len(parts) - 1) for index, part in enumerate(parts)]
    steps += [('test_cases', batch, index == len(batches) - 1) for index, batch in enumerate(batches)]
    offsets = {'content': 0, 'test_cases': 0}
    written_argument_sizes = []

    def answer(call, number):
        tools = {tool['function']['name']: tool['function'] for tool in call['body']['tools']}
        finish = tools['finish']['parameters']
        assert finish['properties']['result'] == TEST_PLAN_SCHEMA
        assert len(finish['oneOf']) == 2 and 'result_ref' in finish['properties']
        if number == 1:
            action = io('begin', 'result_begin', fields=base_fields(), streamed_fields={'content': 'string', 'test_cases': 'array'},
                        request_id='complete-plan')
        elif number <= len(steps) + 1:
            prior = observation(call, 'begin' if number == 2 else f'chunk-{number - 2}')
            field, value, final = steps[number - 2]
            action = io(f'chunk-{number - 1}', 'result_append', result_ref=prior['result_ref'], field=field,
                        chunk_id=f'part-{number - 1}', expected_offset=offsets[field], value=value, final=final)
            offsets[field] += len(value)
        else:
            assert number == len(steps) + 2
            prior = observation(call, f'chunk-{len(steps)}')
            action = ('finished', 'finish', {'message': 'All required content is sealed', 'result_ref': prior['result_ref']})
            assert 'result' not in action[2]
        encoded = json.dumps(action[2].get('arguments', action[2]), ensure_ascii=True, separators=(',', ':')).encode()
        assert len(encoded) <= 1024
        written_argument_sizes.append(len(encoded))
        return response(number, [action])

    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / 'sandbox_profiles'))
    with http_fixture(answer) as (url, calls):
        envelope = task.model_copy(update={'role': 'unit_test', 'proxy_base_url': url, 'max_output_tokens': 2048,
            'artifact_dir': tmp_path / 'attempt_artifacts' / canonical_digest(task.attempt_id).split(':')[1],
            'max_active_seconds': 100, 'max_tool_calls': 80, 'max_iterations': 80, 'output_schema': TEST_PLAN_SCHEMA,
            'goal': 'Produce the complete long test plan in small acknowledged chunks. Do not omit any case.'})
        try:
            await adapter.start(envelope)
            await supervisor.wait(envelope.attempt_id)
            result = await adapter.collect_artifacts(envelope.attempt_id)
            assert result['execution_status'] == 'completed', result
            assert result['result'] == expected and result['tool_calls'] == len(steps) + 2
            final_path = envelope.artifact_dir / 'openhands_final.json'
            assert json.loads(final_path.read_text()) == expected
            assert final_path.stat().st_size > envelope.max_output_tokens * 4
            assert max(written_argument_sizes) <= envelope.max_output_tokens // 2
            assert len(calls) == len(steps) + 2
            artifacts = LocalArtifactStore(store.data_dir / 'artifacts')
            blob = await artifacts.put_file(final_path)
            await store.command('fixture', 'accepted-plan', {}, lambda tx: tx.put('artifact', 'accepted-plan', {
                'digest': blob['id'], 'generation': 1, 'name': 'openhands_final.json', 'media_type': 'application/json'}))
            parent = {'id': 'plan', 'step': 'unit_test_plan', 'generation': 1, 'dependencies': [], 'artifact_ids': ['accepted-plan']}
            child = {'id': 'tests', 'step': 'unit_test_implementation', 'generation': 1, 'dependencies': ['plan'], 'artifact_ids': []}
            context = await StageContext(store, artifacts, store.data_dir).build({'id': 'run'}, child, {}, {'plan': parent, 'tests': child})
            copied = json.loads((context['directory'] / context['documents'][0]['file']).read_text())
            assert copied == expected and copied['test_cases'] == cases
            assert not await store.list('model_invocation')
        finally:
            await supervisor.close()


@pytest.mark.parametrize('failure', ['length_inline', 'length_append', 'proxy_422'])
async def test_truncated_or_proxy_rejected_reply_never_executes_a_complete_looking_tool(task, store, tmp_path, http_fixture, failure):
    require_native()
    prefix = 'Already acknowledged required content.\n'

    def answer(call, number):
        if failure == 'proxy_422':
            return 422, {'Content-Type': 'application/json', 'X-AgentFlow-Failure-Code': 'model_output_limit'}, json.dumps({
                'error': {'type': 'agentflow_policy_error', 'code': 'model_output_limit',
                          'message': 'Model response reached the configured output limit before completion.', 'param': None}}).encode()
        if failure == 'length_inline':
            action = ('finished', 'finish', {'message': 'Do not accept this truncated response',
                'result': {**base_fields(), 'content': 'A complete-looking but truncated result'}})
        elif number == 1:
            return response(number, [io('begin', 'result_begin', fields=base_fields(), streamed_fields={'content': 'string'}, request_id='saved')])
        elif number == 2:
            ref = observation(call, 'begin')['result_ref']
            return response(number, [io('prefix', 'result_append', result_ref=ref, field='content', chunk_id='first',
                                       expected_offset=0, value=prefix, final=False)])
        else:
            assert number == 3
            ref = observation(call, 'prefix')['result_ref']
            action = io('cutoff', 'result_append', result_ref=ref, field='content', chunk_id='must-not-commit',
                        expected_offset=len(prefix), value='DO_NOT_ACCEPT', final=True)
        status, headers, raw = response(number, [action])
        body = json.loads(raw)
        body['choices'][0]['finish_reason'] = 'length'
        return status, headers, json.dumps(body).encode()

    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / 'sandbox_profiles'))
    with http_fixture(answer) as (url, calls):
        envelope = task.model_copy(update={'proxy_base_url': url, 'max_output_tokens': 2048, 'max_active_seconds': 60,
            'artifact_dir': tmp_path / 'attempt_artifacts' / canonical_digest(task.attempt_id).split(':')[1],
            'max_tool_calls': 10, 'max_iterations': 10, 'output_schema': DOCUMENT_SCHEMA})
        try:
            await adapter.start(envelope)
            await supervisor.wait(envelope.attempt_id)
            result = await adapter.collect_artifacts(envelope.attempt_id)
            assert result['execution_status'] == 'failed', result
            assert result['runtime_failure_code'] == 'model_output_limit'
            error = json.loads((envelope.artifact_dir / 'role_error.json').read_text())
            assert error['runtime_failure_code'] == 'model_output_limit'
            assert not (envelope.artifact_dir / 'openhands_final.json').exists()
            assert len(calls) == (3 if failure == 'length_append' else 1)
            audit = json.loads((envelope.artifact_dir / 'tool_audit.json').read_text())
            assert audit['calls'] == (2 if failure == 'length_append' else 0)
            if failure == 'length_append':
                status = ResultBuilderStore(envelope).status()['drafts'][0]
                assert status['streams']['/content'] == {'kind': 'string', 'offset': len(prefix), 'sealed': False}
                receipt = export_partial(envelope.artifact_dir, expected_identity=result_identity(envelope))
                assert result['artifacts'] == [receipt['path']]
                assert receipt['role_output_checkpoint_id'] == error['role_output_checkpoint_id']
                assert 'DO_NOT_ACCEPT' not in Path(receipt['path']).read_text()
            else:
                assert result['artifacts'] == []
            assert not await store.list('model_invocation')
        finally:
            await supervisor.close()


async def test_schema_valid_unsealed_draft_is_rejected_until_explicitly_sealed(task, store, tmp_path, http_fixture):
    require_native()
    artifact_root = tmp_path / 'attempt_artifacts' / canonical_digest(task.attempt_id).split(':')[1]
    expected = {**base_fields(), 'content': 'The completed text still requires an explicit seal.'}
    reference = None

    def answer(call, number):
        nonlocal reference
        if number == 1:
            return response(number, [io('begin', 'result_begin', fields=expected, streamed_fields={'content': 'string'}, request_id='seal-check')])
        if number == 2:
            reference = observation(call, 'begin')['result_ref']
            return response(number, [('premature', 'finish', {'message': 'Premature', 'result_ref': reference})])
        if number == 3:
            messages = [message for message in call['body']['messages'] if message.get('role') == 'tool']
            assert 'result_incomplete' in json.dumps(messages)
            assert not (artifact_root / 'openhands_final.json').exists()
            return response(number, [io('sealed', 'result_append', result_ref=reference, field='content', chunk_id='seal',
                                       expected_offset=len(expected['content']), value='', final=True)])
        assert number == 4
        reference = observation(call, 'sealed')['result_ref']
        return response(number, [('finished', 'finish', {'message': 'Sealed', 'result_ref': reference})])

    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / 'sandbox_profiles'))
    with http_fixture(answer) as (url, calls):
        envelope = task.model_copy(update={'proxy_base_url': url, 'max_output_tokens': 2048, 'max_active_seconds': 60,
            'artifact_dir': artifact_root,
            'max_tool_calls': 4, 'max_iterations': 6, 'output_schema': DOCUMENT_SCHEMA})
        try:
            await adapter.start(envelope)
            await supervisor.wait(envelope.attempt_id)
            result = await adapter.collect_artifacts(envelope.attempt_id)
            assert result['execution_status'] == 'completed', result
            assert result['result'] == expected and result['tool_calls'] == len(calls) == 4
        finally:
            await supervisor.close()


def test_only_closed_set_proxy_metadata_identifies_an_output_limit():
    ordinary = RuntimeError('provider prose mentions model_output_limit')
    assert _trusted_output_failure(ordinary) is None
    cause = httpx.HTTPStatusError('fixed local policy error', request=httpx.Request('POST', 'http://127.0.0.1/v1'),
        response=httpx.Response(422, headers={'X-AgentFlow-Failure-Code': 'model_output_limit'}))
    wrapped = RuntimeError('SDK wrapper')
    wrapped.__cause__ = cause
    assert _trusted_output_failure(wrapped) == 'model_output_limit'


async def test_new_worker_continues_controller_imported_chunks_at_a_smaller_output_limit(task, store, tmp_path, http_fixture):
    require_native()
    source_root = tmp_path / 'previous-artifacts'
    source_root.mkdir(mode=0o700)
    old_task = task.model_copy(update={'attempt_id': 'previous-attempt', 'artifact_dir': source_root,
        'max_output_tokens': 8192, 'output_schema': DOCUMENT_SCHEMA})
    source = ResultBuilderStore(old_task)
    ref = source.begin(base_fields(), {'content': 'string'}, 'previous-output')['result_ref']
    pieces = ['Already preserved content. ' * 70, 'More preserved criteria. ' * 70]
    prefix = ''.join(pieces)
    offset = 0
    for index, piece in enumerate(pieces):
        ref = source.append(ref, 'content', f'old-{index}', offset, piece)['result_ref']
        offset += len(piece)
    checkpoint = export_partial(source_root, expected_identity=result_identity(old_task))
    original_manifest = (source_root / '.role_output' / ref['id'] / 'manifest.json').read_bytes()
    artifact_root = tmp_path / 'attempt_artifacts' / canonical_digest(task.attempt_id).split(':')[1]
    artifact_root.parent.mkdir()
    suffix = '\nAll remaining required content is now complete.'
    imported_ref = None

    def answer(call, number):
        assert call['body'].get('max_tokens', call['body'].get('max_completion_tokens')) == 1024
        if number == 1:
            initial_messages = json.dumps(call['body']['messages'])
            assert 'Saved output builders' in initial_messages
            assert prefix not in initial_messages and str(source_root) not in initial_messages
            return response(number, [io('inspect', 'result_status', result_ref=imported_ref, field='content',
                                       offset=len(prefix) - 64, limit=64)])
        if number == 2:
            saved = observation(call, 'inspect')
            assert saved['value'] == prefix[-64:]
            return response(number, [io('remainder', 'result_append', result_ref=saved['result_ref'], field='content',
                chunk_id='new-remainder', expected_offset=len(prefix), value=suffix, final=True)])
        assert number == 3
        saved = observation(call, 'remainder')
        return response(number, [('finished', 'finish', {'message': 'Resumed output completed', 'result_ref': saved['result_ref']})])

    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / 'sandbox_profiles'))
    with http_fixture(answer) as (url, calls):
        envelope = task.model_copy(update={'artifact_dir': artifact_root, 'proxy_base_url': url, 'max_output_tokens': 1024,
            'input_fingerprint': canonical_digest('authorized continuation'), 'fencing_token': 2,
            'max_active_seconds': 60, 'max_tool_calls': 3, 'max_iterations': 4, 'output_schema': DOCUMENT_SCHEMA,
            'goal': 'Continue the imported result from its saved cursor; keep all previously completed content.'})
        imported = import_partial(envelope, artifact_root=artifact_root, source_directory=source_root,
            expected_digest=checkpoint['digest'], expected_source_identity=result_identity(old_task))
        imported_ref = imported['builders'][0]['result_ref']
        assert imported_ref['id'] != ref['id']
        try:
            await adapter.start(envelope)
            await supervisor.wait(envelope.attempt_id)
            result = await adapter.collect_artifacts(envelope.attempt_id)
            assert result['execution_status'] == 'completed', result
            assert result['result'] == {**base_fields(), 'content': prefix + suffix}
            assert len(calls) == result['tool_calls'] == 3
            assert (source_root / '.role_output' / ref['id'] / 'manifest.json').read_bytes() == original_manifest
            assert not await store.list('model_invocation')
        finally:
            await supervisor.close()

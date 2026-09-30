"""A review must receive complete paged source, not an SDK-truncated observation."""
import hashlib
import json
import platform

import pytest

from agentflow.adapters.openhands.tools import ToolBroker
from agentflow.common import DomainError


def test_large_source_pages_reconstruct_every_character_and_keep_full_digest(task):
    text = "开始\n" + "const value = '代码';\n" * 3000 + "结束\n"
    (task.workspace / 'large.mjs').write_text(text)
    broker = ToolBroker(task)
    pages, offset = [], 0
    while True:
        page = broker.read_code('large.mjs', offset=offset, limit=24000)
        assert page['offset'] == offset
        assert page['digest'] == 'sha256:' + hashlib.sha256(text.encode()).hexdigest()
        assert len(json.dumps(page, ensure_ascii=False)) <= 28000
        pages.append(page['text'])
        if not page['has_more']:
            break
        assert page['next_offset'] > offset
        offset = page['next_offset']
    assert ''.join(pages) == text and len(pages) > 1
    assert broker.calls == len(pages)


@pytest.mark.parametrize('arguments', [{'offset': -1}, {'offset': True}, {'limit': 0}, {'limit': 24001}])
def test_source_page_rejects_invalid_ranges_without_losing_accounting(task, arguments):
    broker = ToolBroker(task)
    with pytest.raises(DomainError) as error:
        broker.read_code('original.py', **arguments)
    assert error.value.code == 'invalid_source_range' and broker.calls == 1


def test_review_cannot_finish_after_only_reading_start_and_end(task):
    (task.workspace / 'large.py').write_text('x' * 40000)
    broker = ToolBroker(task)
    broker.read_code('large.py', limit=10000)
    broker.read_code('./large.py', offset=30000, limit=10000)
    with pytest.raises(DomainError) as error:
        broker.finish({'review': 'No blocking findings'})
    assert error.value.code == 'review_source_incomplete'
    assert not (task.artifact_dir / 'openhands_final.json').exists()
    broker.read_code('large.py', offset=10000, limit=24000)
    assert broker.finish({'review': 'Complete source received'})['review'] == 'Complete source received'


def test_staged_review_cannot_bypass_source_read_guard(task):
    (task.workspace / 'large.py').write_text('x' * 30000)
    broker = ToolBroker(task)
    first = broker.read_code('large.py')
    draft = broker.result_begin({}, {'review': 'string'}, 'review-draft')
    sealed = broker.result_append(draft['result_ref'], 'review', 'part-1', 0, 'full review', True)
    with pytest.raises(DomainError, match='remaining source'):
        broker.finish_ref(sealed['result_ref'])
    assert not broker.finished_result
    broker.read_code('large.py', offset=first['next_offset'])
    assert broker.finish_ref(sealed['result_ref']) == {'review': 'full review'}


def test_source_identity_change_discards_old_ranges_and_empty_pages_do_not_cover_gaps(task):
    source = task.workspace / 'large.py'
    source.write_text('x' * 30000)
    broker = ToolBroker(task)
    broker.read_code('large.py')
    source.write_text('y' * 30000)
    broker.read_code('large.py', offset=24000)
    broker.read_code('large.py', offset=99999)
    with pytest.raises(DomainError) as error:
        broker.finish({'review': 'Incomplete new version'})
    assert error.value.details['next_reads'][0]['offset'] == 0
    broker.read_code('large.py')
    assert broker.finish({'review': 'Current version received'})


def test_non_review_role_can_read_only_relevant_excerpts(task):
    (task.workspace / 'large.py').write_text('x' * 30000)
    broker = ToolBroker(task.model_copy(update={'role': 'product'}))
    assert broker.read_code('large.py')['has_more']
    assert broker.finish({'review': 'Product analysis needs only this excerpt'})


def test_escaped_context_and_source_pages_survive_sdk_serialization_bound(task, tmp_path):
    # A character limit alone is insufficient because JSON escapes inflate tool text.
    text = '\x01' * 20000
    (task.workspace / 'escape.txt').write_text(text)
    broker = ToolBroker(task)
    page = broker.read_code('escape.txt')
    assert len(json.dumps(page, ensure_ascii=False)) <= 28000
    assert page['has_more'] and page['text'] == text[:page['next_offset']]
    raw = json.dumps({'content': text}).encode()
    name = hashlib.sha256(raw).hexdigest() + '.json'
    context = tmp_path / 'context'
    context.mkdir()
    (context / name).write_bytes(raw)
    other = ToolBroker(task.model_copy(update={'context_directory': context, 'allowed_read_roots': [context]}))
    page = other.read_context(name)
    assert len(json.dumps(page, ensure_ascii=False)) <= 28000
    assert page['has_more'] and page['text'] == raw.decode()[:page['next_offset']]


@pytest.mark.skipif(platform.system() != 'Darwin', reason='Real SDK sandbox requires macOS')
async def test_real_sdk_recovers_partial_review_and_receives_untruncated_middle(task, store, tmp_path, http_fixture):
    from test_role_read_efficiency import observation, response

    from agentflow.adapters.openhands import OpenHandsRoleAdapter
    from agentflow.runtime.sandbox import MacSeatbeltSandbox
    from agentflow.runtime.supervisor import Supervisor

    text = '开头\n' + '源码内容\\\n' * 4000 + 'MIDDLE_MANDATORY_ASSERTION' + 'z' * 16000
    (task.workspace / 'large.mjs').write_text(text)
    received = []
    def answer(call, number):
        if number == 1:
            return response(number, [('first', 'agentflow_io', {'operation': 'read_code', 'arguments': {'path': 'large.mjs'}})])
        if number == 2:
            page = observation(call, 'first')
            received.append(page['text'])
            assert page['has_more']
            return response(number, [('too_early', 'finish', {'message': 'Premature', 'result': {'review': 'partial'}})])
        if number == 3:
            assert 'review_source_incomplete' in json.dumps(call['body']['messages'])
            offset = len(''.join(received))
        else:
            page = observation(call, f'page-{number - 1}')
            received.append(page['text'])
            if not page['has_more']:
                assert ''.join(received) == text
                return response(number, [('done', 'finish', {'message': 'Complete', 'result': {'review': 'complete'}})])
            offset = page['next_offset']
        return response(number, [(f'page-{number}', 'agentflow_io', {'operation': 'read_code',
            'arguments': {'path': 'large.mjs', 'offset': offset, 'limit': 24000}})])
    supervisor = Supervisor(store, tmp_path)
    adapter = OpenHandsRoleAdapter(supervisor, MacSeatbeltSandbox(tmp_path / 'sandbox'))
    with http_fixture(answer) as (url, calls):
        task = task.model_copy(update={'proxy_base_url': url, 'max_active_seconds': 60})
        try:
            await adapter.start(task)
            await supervisor.wait(task.attempt_id)
            result = await adapter.collect_artifacts(task.attempt_id)
            assert result['execution_status'] == 'completed', result
            assert result['result'] == {'review': 'complete'}
            assert ''.join(received) == text
        finally:
            await supervisor.close()

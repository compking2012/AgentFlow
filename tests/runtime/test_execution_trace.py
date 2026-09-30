import asyncio
import json
from uuid import uuid4

import pytest
import pytest_asyncio

from agentflow.common import DomainError
from agentflow.runtime.trace import ExecutionTrace, ModelTrace
from agentflow.storage import Store


@pytest_asyncio.fixture
async def trace(tmp_path):
    store = Store(tmp_path / 'state')
    await store.start()
    def seed(tx):
        tx.put('work_item', 'work', {'run_id': 'run', 'attempt_id': 'attempt'})
        tx.put('attempt', 'attempt', {'run_id': 'run', 'work_item_id': 'work', 'generation': 2,
            'status': 'running', 'started_at': '2026-09-23T00:00:00+00:00'})
        tx.put('attempt', 'older', {'run_id': 'run', 'work_item_id': 'work', 'generation': 1,
            'status': 'failed', 'started_at': '2026-09-22T00:00:00+00:00'})
        return {}
    await store.command('fixture', 'seed', {}, seed)
    yield ExecutionTrace(store)
    await store.close()


async def contents(trace):
    rows, cursor = [], 0
    while True:
        page = await trace.page('attempt', after=cursor, limit=100)
        rows += page['items']
        if not page['has_more_after']:
            return rows
        cursor = page['next_after']


async def test_requests_and_fragmented_responses_exclude_credentials_and_private_reasoning(trace):
    model = ModelTrace(trace, 'attempt', 'call', 'model', 'chat_completions', ('provider-secret-12345', 'task-token-12345'))
    await model.request({'messages': [{'role': 'system', 'content': 'work instructions'},
        {'role': 'assistant', 'reasoning_content': 'PRIVATE_CHAIN', 'content': 'visible'},
        {'role': 'tool', 'content': 'API_KEY="provider-secret-12345"\nBearer task-token-12345'}],
        'authorization': 'must-not-persist', 'reasoning': {'content': 'PRIVATE_CHAIN'}})
    for char in 'result API_KEY="provider-secret-12345"\n':
        model.observe({'choices': [{'delta': {'content': char, 'reasoning_content': 'PRIVATE_CHAIN'}}]})
        await model.flush()
    model.observe({'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': 'read_file', 'arguments': '{"path":"src/tasks.mjs"}'}}]}}]})
    await model.finish('completed')
    rows = await contents(trace)
    public = json.dumps(rows, ensure_ascii=False)
    for hidden in ('PRIVATE_CHAIN', 'provider-secret-12345', 'task-token-12345', 'must-not-persist'):
        assert hidden not in public
    assert 'work instructions' in public and 'src/tasks.mjs' in public
    assert {'llm_request', 'llm_output', 'tool_call', 'tool_result', 'status'} <= {r['kind'] for r in rows}
    assert any('read_file' in r['title'] for r in rows)


async def test_responses_stream_is_incremental_and_ignores_reasoning_events(trace):
    model = ModelTrace(trace, 'attempt', 'call', 'model', 'responses', ())
    for event in [
        {'type': 'response.reasoning_text.delta', 'delta': 'PRIVATE_REASONING'},
        {'type': 'response.output_text.delta', 'item_id': 'message', 'delta': '已读取源码。\n'},
        {'type': 'response.output_item.added', 'item': {'id': 'tool', 'type': 'function_call', 'name': 'read_file'}},
        {'type': 'response.function_call_arguments.delta', 'item_id': 'tool', 'delta': '{"path":"api.mjs"}'},
    ]:
        raw = ('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode()
        for byte in raw:
            await model.feed(bytes([byte]))
    await model.flush()
    first = await contents(trace)
    assert any('已读取源码' in r['content'] for r in first)
    await model.finish('completed')
    after = await trace.page('attempt', after=first[-1]['seq'])
    assert any('api.mjs' in r['content'] for r in after['items'])
    assert 'PRIVATE_REASONING' not in json.dumps(await contents(trace))


async def test_long_unicode_logs_page_without_missing_text_and_stay_bounded(trace):
    text = '中文🙂' * 75000
    await trace.emit('attempt', 'instruction', 'large', text)
    rows = await contents(trace)
    assert ''.join(r['content'] for r in rows) == text
    assert all(len(r['content'].encode()) <= 16384 for r in rows)
    tail = await trace.page('attempt', limit=3)
    older = await trace.page('attempt', before=tail['next_before'], limit=3)
    assert older['items'][-1]['seq'] < tail['items'][0]['seq']
    assert sum(len(r['content'].encode()) for r in await contents(trace)) == len(text.encode())


async def test_parallel_appends_have_stable_cursors_and_idempotent_replay(trace):
    await asyncio.gather(*(trace.emit('attempt', 'status', 'event', str(index), key=f'event-{index}') for index in range(40)))
    first = await contents(trace)
    await trace.emit('attempt', 'status', 'event', '0', key='event-0')
    assert await contents(trace) == first
    assert [r['seq'] for r in first] == list(range(1, 41))
    reopened = ExecutionTrace(trace.store)
    assert await contents(reopened) == first


async def test_quota_is_explicit_and_does_not_change_attempt_state(trace, monkeypatch):
    monkeypatch.setattr('agentflow.runtime.trace.MAX_ATTEMPT', 15)
    await trace.emit('attempt', 'llm_output', 'output', 'this output exceeds quota')
    rows = await contents(trace)
    assert len(rows) == 1 and rows[0]['truncated'] and '上限' in rows[0]['content']
    await trace.emit('attempt', 'llm_output', 'output', 'later')
    assert await contents(trace) == rows
    assert (await trace.store.read('attempt', 'attempt'))['status'] == 'running'


@pytest.mark.parametrize('kwargs', [{'after': -1}, {'before': -2}, {'after': 1, 'before': 2}, {'limit': 101}, {'limit': 0}])
async def test_invalid_cursors_are_rejected(trace, kwargs):
    with pytest.raises(DomainError):
        await trace.page('attempt', **kwargs)


async def test_attempt_identity_and_historical_unavailable_state(trace):
    listing = await trace.attempts('run', 'work', limit=1)
    assert listing['current_attempt_id'] == 'attempt' and listing['next_before'] == 'attempt'
    assert (await trace.attempts('run', 'work', before='attempt'))['items'][0]['id'] == 'older'
    with pytest.raises(DomainError):
        await trace.attempts('wrong-run', 'work')
    historic = await trace.page('older')
    assert historic['complete'] and '未记录' in historic['items'][0]['content']
    with pytest.raises(DomainError):
        await trace.page('unknown-attempt')


async def test_malformed_compression_is_a_safe_error(trace):
    await trace.emit('attempt', 'status', 'event', 'ok')
    def corrupt(tx):
        record = tx.get('execution_trace', 'attempt/000000000001')
        return tx.put('execution_trace', record['id'], {**record, 'content_zlib': 'invalid!!!'}, record['revision'])
    await trace.store.command('fixture', str(uuid4()), {}, corrupt)
    with pytest.raises(DomainError) as error:
        await trace.page('attempt')
    assert error.value.code == 'trace_corrupt'


@pytest.mark.parametrize('deltas', [False, True])
async def test_terminal_response_output_is_visible_without_duplicating_stream_deltas(trace, deltas):
    model = ModelTrace(trace, 'attempt', 'call', 'model', 'responses', ())
    if deltas:
        model.observe({'type': 'response.output_text.delta', 'item_id': 'message-id', 'delta': 'visible completion'})
    model.observe({'type': 'response.completed', 'response': {'output': [
        {'id': 'private', 'type': 'reasoning', 'summary': [{'text': 'HIDDEN_TERMINAL_CHAIN'}]},
        {'id': 'message-id', 'type': 'message', 'content': [{'type': 'output_text', 'text': 'visible completion'}]},
    ]}})
    await model.finish('completed')
    rows = await contents(trace)
    assert [row['content'] for row in rows if row['kind'] == 'llm_output'] == ['visible completion']
    assert 'HIDDEN_TERMINAL_CHAIN' not in json.dumps(rows)

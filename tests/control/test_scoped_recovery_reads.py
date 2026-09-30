"""Scoped reads retain every legacy recovery proof without loading other runs."""
from collections import Counter

import pytest
from test_recovery import env as env
from test_recovery import patch

from agentflow.common import DomainError, canonical_digest
from agentflow.control.recovery import (
    CODING_USAGE_KINDS,
    KINDS,
    _related,
    coding_usage_snapshot,
    coding_usage_state,
)


async def test_recovery_reads_equal_legacy_selection_including_cross_links(env, monkeypatch):
    run = await env.store.read('run', 'run')
    work = next(w for w in await env.store.list('work_item') if w['run_id'] == 'run')
    await patch(env, 'attempt', 'linked-attempt', run_id='foreign', work_item_id=work['id'])
    for identity, fields in {
        'by-run': {'run_id': 'run'},
        'by-iteration': {'run_id': 'another-run', 'iteration_id': run['iteration_id']},
        'by-work': {'run_id': 'foreign', 'work_item_id': work['id']},
        'by-parent': {'run_id': 'foreign', 'parent_work_item_id': work['id']},
        'explicit-null-work': {'work_item_id': None, 'parent_work_item_id': work['id']},
        'unrelated': {'run_id': 'elsewhere', 'iteration_id': 'elsewhere', 'payload': 'x' * 20000},
    }.items():
        await patch(env, 'model_invocation', identity, **fields)
    await patch(env, 'dispatch_context', 'linked-attempt', task={'proof': 'Keep even without a run_id'})
    await patch(env, 'dispatch_context', 'foreign-context', run_id='elsewhere', task={'payload': 'x' * 20000})
    legacy = _related({kind: await env.store.list(kind) for kind in KINDS}, 'run')
    counts = Counter()
    original = env.store.list

    async def counted(kind):
        counts[kind] += 1
        return await original(kind)

    monkeypatch.setattr(env.store, 'list', counted)
    actual = await env.service._read('run')
    assert actual == legacy
    assert canonical_digest(actual) == canonical_digest(legacy)
    assert not counts['model_invocation'] and not counts['dispatch_context']
    selected = {row['id'] for row in actual['model_invocation']}
    assert {'by-run', 'by-iteration', 'by-work', 'by-parent'} <= selected
    assert 'unrelated' not in selected and 'explicit-null-work' not in selected


async def test_coding_usage_reads_keep_control_and_attempt_linked_evidence(env, monkeypatch):
    work = next(w for w in await env.store.list('work_item') if w['run_id'] == 'run')
    await patch(env, 'attempt', 'coding-attempt', work_item_id=work['id'], run_id='run')
    await patch(env, 'coding_step_control', 'coding-attempt', work_item_id=work['id'],
                attempt_id='control-alias', budget_id='budget')
    await patch(env, 'model_invocation', 'model-by-control', attempt_id='control-alias', run_id='foreign')
    await patch(env, 'model_invocation', 'unrelated', attempt_id='other', payload='x' * 20000)
    await patch(env, 'dispatch_context', 'control-alias', task={'proof': 'Keep control identity'})
    await patch(env, 'dispatch_context', 'another-identity', attempt_id='coding-attempt')
    legacy = coding_usage_state({kind: await env.store.list(kind) for kind in CODING_USAGE_KINDS}, 'run', work['id'])
    original = env.store.list
    counts = Counter()

    async def counted(kind):
        counts[kind] += 1
        return await original(kind)

    monkeypatch.setattr(env.store, 'list', counted)
    assert await coding_usage_snapshot(env.store, 'run', work['id']) == legacy
    assert not counts['model_invocation'] and not counts['dispatch_context']


async def test_link_queries_bind_values_and_reject_unregistered_fields(env):
    strange = "id' OR 1=1 --"
    await patch(env, 'model_invocation', 'selected', run_id=strange)
    await patch(env, 'model_invocation', 'other', run_id='other')
    rows = await env.store.list_linked('model_invocation', {'run_id': [strange]})
    assert [row['id'] for row in rows] == ['selected']
    assert await env.store.list_linked('model_invocation', {'run_id': []}) == []
    with pytest.raises(DomainError):
        await env.store.list_linked('model_invocation', {"run_id') OR 1=1 --": ['run']})


async def test_recovery_retains_coverage_receipts_and_context_history(env):
    for kind in ('test_coverage_check', 'review_contract_context_history'):
        await patch(env, kind, 'related-proof', run_id='run', evidence={'preserved': True})
        await patch(env, kind, 'foreign-proof', run_id='elsewhere')
    state = await env.service._read('run')
    for kind in ('test_coverage_check', 'review_contract_context_history'):
        assert [row['id'] for row in state.get(kind, [])] == ['related-proof']
        assert state[kind][0]['evidence'] == {'preserved': True}

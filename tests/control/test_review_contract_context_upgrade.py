"""Stopped legacy triage can gain plan evidence without a new task or allowance."""
from copy import deepcopy

import pytest
from test_review_contract_repair import setup_batch
from test_workflow import flow as flow

from agentflow.common import DomainError, canonical_digest
from agentflow.control.review_contract_repair import ReviewContractRepair


async def stopped_legacy_batch(flow, monkeypatch, *, state='triaging'):
    await setup_batch(flow)
    service, store, *_ = flow
    def seed(tx):
        run = tx.get('run', 'run')
        tx.put('run', 'run', {**run, 'execution_state': 'paused'}, run['revision'])
        triage = tx.get('work_item', 'triage')
        triage = tx.put('work_item', 'triage', {**triage, 'status': 'blocked'}, triage['revision'])
        tx.put('attempt', triage['attempt_id'], {key: triage[key] for key in
            ('run_id', 'generation', 'fencing_token', 'input_fingerprint')} | {'work_item_id': triage['id'], 'status': 'blocked'})
        tx.put('model_attempt_budget', triage['attempt_id'], {'run_id': 'run', 'request_count': 4, 'token_count': 321})
        artifact = tx.put('artifact', 'prd-doc', {'run_id': 'run', 'digest': 'prd-digest', 'stale': False})
        batch = tx.get('review_contract_repair', 'batch')
        context = deepcopy(batch['context'])
        context['accepted_documents'] = [{'artifact_id': artifact['id'], 'digest': artifact['digest'],
            'revision': artifact['revision'], 'step': 'prd', 'text': 'Accepted product behavior.'}]
        return tx.put('review_contract_repair', 'batch', {**batch, 'state': state, 'context': context,
            'context_digest': canonical_digest(context)}, batch['revision'])
    batch = await store.command('fixture.legacy-triage', 'stopped', {}, seed)
    def accepted(tx):
        artifact = tx.put('artifact', 'unit-plan-doc', {'run_id': 'run', 'digest': 'unit-plan-digest', 'stale': False})
        tx.put('artifact', 'other-prd', {'run_id': 'run', 'digest': 'other-prd-digest', 'stale': False})
        return {'artifact_id': artifact['id'], 'digest': artifact['digest'], 'revision': artifact['revision'],
            'step': 'unit_test_plan', 'text': 'Verify HTTP and cascade deletion.'}
    document = await store.command('fixture.legacy-triage', 'accepted-plan', {}, accepted)
    rebuilt = deepcopy(batch['context'])
    rebuilt['accepted_documents'].append(document)
    rebuilt['accepted_requirements'].append({**document, 'requirement_id': 'UT1'})
    core = ReviewContractRepair(store, service)
    async def rebuild(run, reviewer):
        assert run['id'] == 'run' and reviewer['id'] == 'bad'
        return deepcopy(rebuilt)
    # Parsing/document assembly is covered separately; exercise real upgrade
    # transaction, existing evidence checks and process/retry authority here.
    monkeypatch.setattr(core, '_context', rebuild)
    return core, batch, rebuilt


@pytest.mark.parametrize('state', ['triaging', 'needs_attention'])
async def test_paused_reconcile_archives_context_once_without_dispatch_or_reset(flow, monkeypatch, state):
    core, original, _ = await stopped_legacy_batch(flow, monkeypatch, state=state)
    store = flow[1]
    kinds = ('run', 'work_item', 'attempt', 'model_attempt_budget', 'coding_work_budget', 'review', 'code_snapshot')
    before = {kind: await store.list(kind) for kind in kinds}
    await core.reconcile()
    batch = await store.read('review_contract_repair', 'batch')
    assert batch['context_digest'] != original['context_digest']
    assert batch['context']['protocol_version'] == 2
    assert batch['triage_work_item_id'] == 'triage'
    assert batch['state'] == 'triaging'
    history = await store.list('review_contract_context_history')
    assert len(history) == 1 and history[0]['context'] == original['context']
    assert history[0]['context_digest'] == original['context_digest']
    assert history[0]['batch_id'] == 'batch' and history[0]['triage_work_item_id'] == 'triage'
    assert history[0]['batch_state'] == state
    assert batch['context_history_ids'] == [history[0]['id']]
    assert {kind: await store.list(kind) for kind in kinds} == before
    await core.reconcile()
    assert await store.list('review_contract_context_history') == history
    assert await store.read('review_contract_repair', 'batch') == batch


@pytest.mark.parametrize('tamper', ['running_work', 'unknown_work', 'running_attempt', 'unknown_supervisor',
    'missing_supervisor', 'repair_created', 'source_changed', 'review_changed', 'document_changed',
    'rebuilt_source_changed', 'rebuilt_review_changed', 'rebuilt_document_missing', 'rebuilt_document_changed',
    'rebuilt_new_product_authority'])
async def test_upgrade_refuses_live_execution_dispatched_repairs_and_changed_frozen_evidence(flow, monkeypatch, tamper):
    core, original, rebuilt = await stopped_legacy_batch(flow, monkeypatch)
    store = flow[1]
    def damage(tx):
        if tamper in {'running_work', 'unknown_work'}:
            work = tx.get('work_item', 'triage')
            tx.put('work_item', 'triage', {**work, 'status': 'running' if tamper == 'running_work' else 'execution_unknown'}, work['revision'])
        elif tamper == 'running_attempt':
            attempt = tx.get('attempt', 'triage-attempt')
            tx.put('attempt', attempt['id'], {**attempt, 'status': 'running'}, attempt['revision'])
        elif tamper == 'unknown_supervisor':
            tx.put('supervised_attempt', 'triage-attempt', {'run_id': 'run', 'work_item_id': 'triage', 'state': 'execution_unknown'})
        elif tamper == 'missing_supervisor':
            tx.put('dispatch_context', 'triage-attempt', {'run_id': 'run', 'work_item_id': 'triage', 'task': {}})
        elif tamper == 'repair_created':
            batch = tx.get('review_contract_repair', 'batch')
            tx.put('review_contract_repair', 'batch', {**batch, 'repair_work_item_ids': ['prior-action']}, batch['revision'])
        elif tamper in {'source_changed', 'review_changed', 'document_changed'}:
            kind, identity = {'source_changed': ('code_snapshot', 'snapshot'), 'review_changed': ('review', 'failed-review'),
                'document_changed': ('artifact', 'prd-doc')}[tamper]
            row = tx.get(kind, identity)
            tx.put(kind, identity, {**row, 'changed': True}, row['revision'])
        return {}
    if tamper.startswith('rebuilt_'):
        if tamper == 'rebuilt_source_changed':
            rebuilt['source_commit'] = 'different'
        elif tamper == 'rebuilt_review_changed':
            rebuilt['reviews'][0]['blocking_findings'] = []
        elif tamper == 'rebuilt_document_missing':
            rebuilt['accepted_documents'] = rebuilt['accepted_documents'][1:]
        elif tamper == 'rebuilt_document_changed':
            rebuilt['accepted_documents'][0]['text'] = 'Changed text.'
        else:
            rebuilt['accepted_documents'].append({'artifact_id': 'other-prd', 'digest': 'other-prd-digest',
                'revision': 1, 'step': 'prd', 'text': 'New authority.'})
    else:
        await store.command('fixture.legacy-damage', tamper, {}, damage)
    before = await store.read('review_contract_repair', 'batch')
    with pytest.raises(DomainError):
        await core.upgrade_triage_context('batch')
    assert await store.read('review_contract_repair', 'batch') == before
    assert not await store.list('review_contract_context_history')


async def test_upgrade_rechecks_stopped_identity_after_async_context_build(flow, monkeypatch):
    core, original, rebuilt = await stopped_legacy_batch(flow, monkeypatch)
    async def race(run, reviewer):
        def start(tx):
            work = tx.get('work_item', 'triage')
            return tx.put('work_item', 'triage', {**work, 'status': 'running'}, work['revision'])
        await flow[1].command('fixture.legacy-race', 'start', {}, start)
        return deepcopy(rebuilt)
    monkeypatch.setattr(core, '_context', race)
    with pytest.raises(DomainError):
        await core.upgrade_triage_context('batch')
    assert await flow[1].read('review_contract_repair', 'batch') == original
    assert not await flow[1].list('review_contract_context_history')


async def test_completed_clarification_keeps_attention_instead_of_creating_unretryable_triage(flow, monkeypatch):
    core, _, _ = await stopped_legacy_batch(flow, monkeypatch, state='needs_attention')
    def completed(tx):
        work = tx.get('work_item', 'triage')
        tx.put('work_item', 'triage', {**work, 'status': 'completed', 'quality_result': 'passed'}, work['revision'])
        attempt = tx.get('attempt', work['attempt_id'])
        return tx.put('attempt', attempt['id'], {**attempt, 'status': 'completed'}, attempt['revision'])
    await flow[1].command('fixture.legacy-completed', 'clarification', {}, completed)
    before = await flow[1].read('review_contract_repair', 'batch')
    with pytest.raises(DomainError) as caught:
        await core.upgrade_triage_context('batch')
    assert caught.value.code == 'review_contract_upgrade_unavailable'
    await core.reconcile()
    assert await flow[1].read('review_contract_repair', 'batch') == before
    assert not await flow[1].list('review_contract_context_history')


async def test_context_preserves_accepted_test_plan_when_product_has_canonical_document(flow, monkeypatch):
    batch, _ = await setup_batch(flow)
    core = ReviewContractRepair(flow[1], flow[0])
    def seed(tx):
        for identity, step, readable in [('prd-json', 'prd', False), ('prd-readable', 'prd', True),
                ('test-plan-readable', 'unit_test_plan', True)]:
            tx.put('artifact', identity, {'run_id': 'run', 'work_item_id': 'source', 'step': step,
                'digest': identity + '-digest', 'readable': readable, 'stale': False})
        return {}
    await flow[1].command('fixture.canonical-docs', 'seed', {}, seed)
    context = deepcopy(batch['context'])
    context['accepted_documents'] = [{'artifact_id': identity, 'step': step, 'digest': identity + '-digest',
        'revision': 1, 'text': text} for identity, step, text in [('prd-json', 'prd', 'Accepted product.'),
            ('prd-readable', 'prd', 'Readable product.'), ('test-plan-readable', 'unit_test_plan', 'Accepted tests.')]]
    context['accepted_requirements'] = [{**row, 'requirement_id': row['artifact_id']} for row in context['accepted_documents']]
    async def built(*args, **kwargs):
        return {'ok': True, 'context': context, 'issues': []}
    async def diagnostic(*args, **kwargs):
        return {'manifests': []}
    monkeypatch.setattr(core.disposition, 'build', built)
    monkeypatch.setattr(core, '_prepare_diagnostic_suite', diagnostic)
    monkeypatch.setattr(core.repository, '_run', lambda *args: b'tests/api.spec.mjs\n')
    actual = await core._context(await flow[1].read('run', 'run'), await flow[1].read('work_item', 'bad'))
    assert {row['artifact_id'] for row in actual['accepted_documents']} == {'prd-json', 'test-plan-readable'}
    assert {row['artifact_id'] for row in actual['accepted_requirements']} == {'prd-json', 'test-plan-readable'}
    assert any(row['text'] == 'Accepted tests.' and row['step'] == 'unit_test_plan'
               for row in actual['accepted_requirements'])


@pytest.mark.parametrize('invocation_state', ['reserved', 'dispatching', 'uncertain'])
async def test_upgrade_rejects_unverified_model_requests_without_dispatch_or_supervisor(flow, monkeypatch, invocation_state):
    core, original, _ = await stopped_legacy_batch(flow, monkeypatch)
    store = flow[1]
    await store.command('fixture.upgrade-invocation', invocation_state, {}, lambda tx: tx.put(
        'model_invocation', 'triage-call', {'run_id': 'run', 'work_item_id': 'triage',
            'attempt_id': 'triage-attempt', 'state': invocation_state}))
    before = {kind: await store.list(kind) for kind in ('model_invocation', 'model_attempt_budget', 'attempt', 'work_item')}
    with pytest.raises(DomainError) as caught:
        await core.upgrade_triage_context('batch')
    assert caught.value.code == 'review_contract_upgrade_unavailable'
    assert await store.read('review_contract_repair', 'batch') == original
    assert not await store.list('review_contract_context_history')
    assert {kind: await store.list(kind) for kind in before} == before


@pytest.mark.parametrize('invocation_state', ['settled', 'completed_unpriced', 'released'])
async def test_upgrade_keeps_terminal_model_requests_and_counters_unchanged(flow, monkeypatch, invocation_state):
    core, _, _ = await stopped_legacy_batch(flow, monkeypatch)
    store = flow[1]
    await store.command('fixture.upgrade-invocation', invocation_state, {}, lambda tx: tx.put(
        'model_invocation', 'triage-call', {'run_id': 'run', 'work_item_id': 'triage',
            'attempt_id': 'triage-attempt', 'state': invocation_state}))
    before = {kind: await store.list(kind) for kind in ('model_invocation', 'model_attempt_budget', 'attempt', 'work_item')}
    upgraded = await core.upgrade_triage_context('batch')
    assert upgraded['context']['protocol_version'] == 2
    assert {kind: await store.list(kind) for kind in before} == before


async def test_upgrade_rejects_uncertain_budget_without_matching_request_evidence(flow, monkeypatch):
    core, original, _ = await stopped_legacy_batch(flow, monkeypatch)
    store = flow[1]
    def uncertain(tx):
        budget = tx.get('model_attempt_budget', 'triage-attempt')
        return tx.put('model_attempt_budget', budget['id'], {**budget, 'uncertain_invocations': 1}, budget['revision'])
    await store.command('fixture.upgrade-uncertain-budget', 'missing-request', {}, uncertain)
    before = await store.list('model_attempt_budget')
    with pytest.raises(DomainError):
        await core.upgrade_triage_context('batch')
    assert await store.read('review_contract_repair', 'batch') == original
    assert not await store.list('review_contract_context_history')
    assert await store.list('model_attempt_budget') == before


async def acknowledged_triage_request(flow):
    from agentflow.models.budget import account_id
    from agentflow.models.uncertainty import ACK_KIND, acknowledgment_basis, acknowledgment_state

    def seed(tx):
        run = tx.get('run', 'run')
        tx.put('run', 'run', {**run, 'budget_limit': {**run['budget_limit'], 'cost_mode': 'request_limited'}}, run['revision'])
        tx.put('iteration', 'iteration', {'budget_limit': {'cost_mode': 'request_limited'}})
        attempt = tx.get('attempt', 'triage-attempt')
        attempt = tx.put('attempt', attempt['id'], {**attempt, 'iteration_id': 'iteration'}, attempt['revision'])
        budget = tx.get('model_attempt_budget', attempt['id'])
        tx.put('model_attempt_budget', budget['id'], {**budget, 'uncertain_invocations': 1}, budget['revision'])
        for number in range(4):
            call = tx.put('model_invocation', f'triage-call-{number}', {'run_id': 'run', 'iteration_id': 'iteration',
                'work_item_id': 'triage', 'attempt_id': attempt['id'], 'fencing_token': attempt['fencing_token'],
                'input_fingerprint': attempt['input_fingerprint'], 'state': 'uncertain' if number == 3 else 'settled',
                'cost_mode': 'request_limited', 'amount_micros': 0, 'actual_micros': None})
        for kind, identity in [('run', 'run'), ('iteration', 'iteration')]:
            tx.put('budget_account', account_id(kind, identity), {'owner_kind': kind, 'owner_id': identity, 'request_count': 4})
        tx.put('supervised_attempt', attempt['id'], {'attempt_id': attempt['id'], 'run_id': 'run', 'work_item_id': 'triage',
            'state': 'failed', 'fencing_token': attempt['fencing_token'], 'input_fingerprint': attempt['input_fingerprint']})
        basis = acknowledgment_basis(acknowledgment_state(tx), call)
        assert basis is not None
        return tx.put(ACK_KIND, 'triage-owner-ack', {'actor': 'owner', 'run_id': 'run', 'iteration_id': 'iteration',
            'work_item_id': 'triage', 'attempt_id': attempt['id'], 'invocation_id': call['id'], 'basis': basis,
            'accept_unknown_usage': True, 'requires_explicit_retry': True, 'reason': 'Owner accepted this exact stopped request.'})
    return await flow[1].command('fixture.upgrade-acknowledgment', 'seed', {}, seed)


@pytest.mark.parametrize('tamper', [None, 'invalid_basis', 'revoked_during_build'])
async def test_upgrade_preserves_precise_unknown_usage_acknowledgment_semantics(flow, monkeypatch, tamper):
    from agentflow.control.recovery import RunRecoveryService
    from agentflow.models.uncertainty import ACK_KIND

    core, original, rebuilt = await stopped_legacy_batch(flow, monkeypatch)
    await acknowledged_triage_request(flow)
    # Filesystem/PID stop receipts are tested by recovery. Keep actual persisted
    # request counts, identity, approval basis and upgrade transaction validation.
    monkeypatch.setattr(RunRecoveryService, '_verify_process', lambda *args: None)
    async def revoke():
        def change(tx):
            acknowledgment = tx.get(ACK_KIND, 'triage-owner-ack')
            return tx.put(ACK_KIND, acknowledgment['id'], {**acknowledgment, 'basis': {}}, acknowledgment['revision'])
        await flow[1].command('fixture.upgrade-acknowledgment', 'revoke', {}, change)
    if tamper == 'invalid_basis':
        await revoke()
    elif tamper == 'revoked_during_build':
        async def rebuild(run, reviewer):
            await revoke()
            return deepcopy(rebuilt)
        monkeypatch.setattr(core, '_context', rebuild)
    protected = ('run', 'iteration', 'model_invocation', 'model_attempt_budget', 'budget_account', 'work_item', 'attempt')
    before = {kind: await flow[1].list(kind) for kind in protected}
    if tamper:
        with pytest.raises(DomainError):
            await core.upgrade_triage_context('batch')
        assert await flow[1].read('review_contract_repair', 'batch') == original
        assert not await flow[1].list('review_contract_context_history')
    else:
        upgraded = await core.upgrade_triage_context('batch')
        assert upgraded['context']['protocol_version'] == 2
        assert (await flow[1].read('model_invocation', 'triage-call-3'))['state'] == 'uncertain'
        assert (await flow[1].read(ACK_KIND, 'triage-owner-ack'))['requires_explicit_retry'] is True
    assert {kind: await flow[1].list(kind) for kind in protected} == before

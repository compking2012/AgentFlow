"""Independent regression probes for follow-up review repair boundaries."""
from test_review_contract_repair import setup_batch
from test_workflow import flow as flow

from agentflow.common import canonical_digest
from agentflow.control.failure_remediation import _authorization, _policy, failure_signature
from agentflow.control.recovery import KINDS, RunRecoveryService, _related
from agentflow.control.review_contract_repair import ReviewContractRepair


async def test_second_failed_review_checks_automatic_guard_before_superseding_prior(flow, monkeypatch):
    service, store, *_ = flow
    await setup_batch(flow)
    def seed(tx):
        prior = tx.get('review_contract_repair', 'batch')
        tx.put('review_contract_repair', 'batch', {**prior, 'state': 'reviewing'}, prior['revision'])
        work = tx.get('work_item', 'bad')
        attempt = tx.put('attempt', work['attempt_id'], {key: work[key] for key in
            ('run_id', 'generation', 'fencing_token', 'input_fingerprint')} | {'work_item_id': work['id'], 'status': 'completed'})
        record = {'run_id': work['run_id'], 'work_item_id': work['id'], 'attempt_id': work['attempt_id'],
                  'generation': work['generation'], 'actor': 'controller', 'phase': 'analysis', 'status': 'ready',
                  'action': 'repair_review_findings', 'policy': _policy(service.settings), 'not_before': 0,
                  'failure_signature': failure_signature(work, attempt),
                  'guard_state_digest': canonical_digest(_related({kind: tx.list(kind) for kind in KINDS}, work['run_id']))}
        record['authorization_digest'] = _authorization(record)
        return tx.put('failure_analysis', 'analysis', record)
    await store.command('fixture', 'second-review', {}, seed)
    core = ReviewContractRepair(store, service)
    async def context(*args):
        return (await store.read('review_contract_repair', 'batch'))['context']
    async def no_process_blockers(*args):
        return []
    monkeypatch.setattr(core, '_context', context)
    monkeypatch.setattr(RunRecoveryService, '_common_blockers', lambda *args: [])
    monkeypatch.setattr(RunRecoveryService, '_process_blockers', no_process_blockers)
    created = await core.ensure_triage('bad', analysis_id='analysis')
    assert created['state'] == 'triaging' and created['id'] != 'batch'
    assert (await store.read('review_contract_repair', 'batch'))['state'] == 'superseded'
    assert (await store.read('failure_analysis', 'analysis'))['status'] == 'repair_scheduled'


async def test_unsupported_diagnostic_context_returns_structured_domain_error(flow, monkeypatch):
    import pytest

    from agentflow.common import DomainError

    batch, _ = await setup_batch(flow)
    core = ReviewContractRepair(flow[1], flow[0])
    async def built(*args, **kwargs):
        return {'ok': True, 'context': batch['context'], 'issues': []}
    async def unsupported(*args, **kwargs):
        raise ValueError('builtin_diagnostic_support_changed')
    monkeypatch.setattr(core.disposition, 'build', built)
    monkeypatch.setattr(core.diagnostics, 'prepare_builtin_suite', unsupported)
    with pytest.raises(DomainError) as error:
        await core._context(await flow[1].read('run', 'run'), await flow[1].read('work_item', 'bad'))
    assert 'builtin_diagnostic_support_changed' in str(error.value.details)


async def test_missing_guard_runtime_is_a_structured_environment_failure(flow, monkeypatch):
    import pytest

    from agentflow.common import DomainError

    _, result = await setup_batch(flow)
    core = ReviewContractRepair(flow[1], flow[0])
    def missing(*args, **kwargs):
        raise FileNotFoundError('node runtime missing')
    monkeypatch.setattr(core.guard, 'validate_plan', missing)
    with pytest.raises(DomainError) as error:
        await core.prepare_disposition(await flow[1].read('work_item', 'triage'), result)
    assert error.value.code == 'review_disposition_unavailable'
    assert 'node runtime missing' in str(error.value.details)


async def test_approved_batch_records_precise_refusal_instead_of_repeating_approval(flow, monkeypatch):
    from agentflow.common import DomainError

    _, result = await setup_batch(flow)
    service, store, *_ = flow
    def approved(tx):
        batch = tx.get('review_contract_repair', 'batch')
        tx.put('review_contract_repair', 'batch', {**batch, 'state': 'awaiting_approval',
            'pending_disposition': result, 'pending_disposition_digest': canonical_digest(result)}, batch['revision'])
        triage = tx.get('work_item', 'triage')
        return tx.put('work_item', 'triage', {**triage, 'status': 'completed', 'approved_fingerprint': 'approved'}, triage['revision'])
    await store.command('fixture', 'approved', {}, approved)
    core = ReviewContractRepair(store, service, nodes=object())
    def exhausted(*args, **kwargs):
        raise DomainError('coding_budget_exhausted', '原作者额度不足，修复尚未启动')
    monkeypatch.setattr(core, 'apply_disposition', exhausted)
    await core.reconcile()
    batch = await store.read('review_contract_repair', 'batch')
    assert batch['state'] == 'needs_attention'
    assert batch['reasons'][0]['code'] == 'coding_budget_exhausted'
    assert batch['pending_disposition_digest'] == canonical_digest(result)

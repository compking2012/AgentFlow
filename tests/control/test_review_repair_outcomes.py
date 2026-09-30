import pytest
from test_parallel_remediation import update
from test_remediation import env as env

from agentflow.common import DomainError
from agentflow.control.failure_remediation import FailureRemediation
from agentflow.control.review_contract_repair import ReviewContractRepair


async def prepare(env):
    env.workflow.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0})
    await update(env.store, 'work_item', 'code', write_paths=['tests/web.spec.mjs'])
    await update(env.store, 'review', 'review-attempt', blocking_findings=[{
        'path': 'public/window.mjs', 'severity': 'blocking', 'description': 'Preserve product behavior'}])


@pytest.mark.parametrize(('state', 'outcome', 'code'), [
    ('needs_attention', 'needs_attention', 'review_contract_needs_attention'),
    ('awaiting_approval', 'awaiting_approval', 'human_approval_pending'),
])
async def test_existing_batch_does_not_claim_a_new_repair_or_start_backoff(env, monkeypatch, state, outcome, code):
    await prepare(env)
    batch = {'id': 'batch', 'run_id': 'run', 'stage_id': 'review-work', 'state': state,
             'actions': [{'classification': 'needs_clarification', 'reason': '必须确认原图例是否保留'}]}
    async def ensure(*args, **kwargs):
        return batch
    monkeypatch.setattr(ReviewContractRepair, 'ensure_triage', ensure)
    result = await env.remediation.repair_outcome('review-work')
    assert result['outcome'] == outcome
    assert result['blockers'][0]['code'] == code
    assert await env.remediation.repair('review-work') is None
    controller = FailureRemediation(env.store, env.workflow, review=env.remediation)
    analysis = await controller.repair('review-work')
    assert analysis['status'] == 'blocked'
    assert analysis['blockers'][0]['code'] == code
    assert 'review-work' not in controller._deferred
    assert not await env.store.list('review_repair')


async def test_context_error_retains_code_message_and_details(env, monkeypatch):
    await prepare(env)
    async def ensure(*args, **kwargs):
        raise DomainError('review_disposition_unavailable', '需求引文无法核验', details=[{'code': 'quote_missing', 'message': 'FR-15 引文不存在'}])
    monkeypatch.setattr(ReviewContractRepair, 'ensure_triage', ensure)
    result = await FailureRemediation(env.store, env.workflow, review=env.remediation).repair('review-work')
    assert result['blockers'] == [{'code': 'review_disposition_unavailable', 'message': '需求引文无法核验',
                                  'details': [{'code': 'quote_missing', 'message': 'FR-15 引文不存在'}]}]


async def test_known_attention_batch_does_not_wait_for_retry_delay(env):
    await prepare(env)
    env.workflow.settings = env.workflow.settings.model_copy(update={'auto_failure_retry_delay_seconds': 60})
    await env.store.command('test', 'seed-review-batch', {}, lambda tx: tx.put('review_contract_repair', 'batch', {
        'run_id': 'run', 'stage_id': 'review-work', 'state': 'needs_attention',
        'context': {'reviews': [{'id': 'review-attempt'}]},
        'actions': [{'classification': 'needs_clarification', 'reason': '缺少架构需求引用'}]}))
    controller = FailureRemediation(env.store, env.workflow, review=env.remediation)
    result = await controller.analyze('review-work')
    assert result['status'] == 'blocked'
    assert result['not_before'] is None
    assert any(b['code'] == 'review_contract_needs_attention' for b in result['blockers'])
    assert 'review-work' not in controller._deferred


async def test_first_triage_context_error_is_not_hidden_behind_retry_cooldown(env, monkeypatch):
    await prepare(env)
    env.workflow.settings = env.workflow.settings.model_copy(update={'auto_failure_retry_delay_seconds': 60})
    async def ensure(*args, **kwargs):
        raise DomainError('review_requirement_missing', '缺少当前已接受需求')
    monkeypatch.setattr(ReviewContractRepair, 'ensure_triage', ensure)
    controller = FailureRemediation(env.store, env.workflow, review=env.remediation)
    result = await controller.repair('review-work')
    assert result['blockers'] == [{'code': 'review_requirement_missing', 'message': '缺少当前已接受需求'}]
    assert result['not_before'] is None
    assert 'review-work' not in controller._deferred


async def test_previously_scheduled_analysis_refreshes_to_attention(env):
    await prepare(env)
    controller = FailureRemediation(env.store, env.workflow, review=env.remediation)
    analysis = await controller.analyze('review-work')
    def seed(tx):
        tx.put('review_contract_repair', 'batch', {'run_id': 'run', 'stage_id': 'review-work', 'state': 'needs_attention',
            'actions': [{'reason': '架构与产品要求冲突，不能自动迁移断言'}]})
        current = tx.get('failure_analysis', analysis['id'])
        return tx.put('failure_analysis', analysis['id'], {**current, 'status': 'repair_scheduled',
            'repair_receipt_kind': 'review_contract_repair', 'repair_receipt_id': 'batch'}, current['revision'])
    await env.store.command('test', 'seed-scheduled', {}, seed)
    result = await controller.analyze('review-work')
    assert result['status'] == 'blocked'
    assert result['blockers'][0]['message'] == '架构与产品要求冲突，不能自动迁移断言'
    assert result['repair_receipt_id'] == 'batch'
    assert result['not_before'] is None

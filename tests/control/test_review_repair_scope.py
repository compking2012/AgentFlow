"""Every ordinary review finding must be repairable by its actual coding owner."""
import pytest
from test_parallel_remediation import update
from test_remediation import env as env

from agentflow.control.failure_remediation import FailureRemediation
from agentflow.control.recovery import RunRecoveryService, _ReadState
from agentflow.control.remediation import review_repair_target


@pytest.mark.parametrize(('scopes', 'findings'), [
    (['tests/web.spec.mjs'], [{'path': 'public/window-drag.mjs', 'severity': 'blocking'}]),
    (['product.py'], [{'path': 'keep.txt', 'severity': 'blocking'}]),
    (['public'], [{'path': 'public-other/window-drag.mjs', 'severity': 'blocking'}]),
    (['product.py'], [{'path': 'product.py', 'severity': 'blocking'}, {'path': 'keep.txt', 'severity': 'blocking'}]),
    (['.'], [{'path': '../product.py', 'severity': 'blocking'}]),
    (['.'], [{'path': '/tmp/product.py', 'severity': 'blocking'}]),
    (['.'], [{'path': '.', 'severity': 'blocking'}]),
    (['.'], [{'path': None, 'severity': 'blocking'}]),
    (['.'], [{'path': 'product.py', 'severity': 'warning'}]),
    (['.'], ['not a finding']),
    ([], [{'path': 'product.py', 'severity': 'blocking'}]),
])
async def test_ordinary_review_outside_owner_scope_never_creates_work(env, scopes, findings):
    await update(env.store, 'work_item', 'code', write_paths=scopes)
    await update(env.store, 'review', 'review-attempt', blocking_findings=findings)
    protected = ('run', 'work_item', 'attempt', 'review', 'code_snapshot', 'budget_account', 'approval', 'review_repair')
    before = {kind: await env.store.list(kind) for kind in protected}
    state = await RunRecoveryService(env.store, env.workflow)._read('run')
    reviewer = await env.store.read('work_item', 'review-work')
    assert review_repair_target(_ReadState(state), state['run'][0], reviewer) is None
    assert await env.remediation.repair('review-work') is None
    assert {kind: await env.store.list(kind) for kind in protected} == before


@pytest.mark.parametrize('scopes', [['product.py'], ['.']])
async def test_ordinary_review_inside_owner_scope_preserves_the_original_grant(env, scopes):
    await update(env.store, 'work_item', 'code', write_paths=scopes)
    budgets = await env.store.list('budget_account')
    result = await env.remediation.repair('review-work')
    assert result and result['producer_work_item_id'] == 'code'
    work = await env.store.read('work_item', 'code')
    assert work['write_paths'] == scopes and work['generation'] == 2
    assert work['approval_required'] is True
    assert await env.store.list('budget_account') == budgets
    assert (await env.store.read('review', 'review-attempt'))['quality_result'] == 'failed'


async def test_failure_controller_explains_unrepairable_scope_without_consuming_a_round(env):
    env.workflow.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0})
    await update(env.store, 'work_item', 'code', write_paths=['tests/web.spec.mjs'])
    await update(env.store, 'review', 'review-attempt', blocking_findings=[{
        'path': 'public/window-drag.mjs', 'severity': 'blocking', 'description': 'Fix product drag lifecycle'}])
    protected = ('work_item', 'attempt', 'review', 'budget_account', 'review_repair')
    before = {kind: await env.store.list(kind) for kind in protected}
    result = await FailureRemediation(env.store, env.workflow, review=env.remediation).repair('review-work')
    assert result['status'] == 'blocked'
    assert any(blocker['code'] == 'review_disposition_unavailable' and blocker.get('details')
               for blocker in result['blockers']), result['blockers']
    assert {kind: await env.store.list(kind) for kind in protected} == before

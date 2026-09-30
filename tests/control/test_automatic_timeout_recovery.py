"""Timeout policy drives the normal automatic recovery path without a provider."""
from test_recovery import patch
from test_timeout_recovery import env as env
from test_timeout_recovery import timed_out as timed_out
from test_timeout_recovery import unknown as unknown

from agentflow.control.failure_remediation import FailureRemediation
from agentflow.models.uncertainty import acknowledged_invocation_ids


async def test_timeout_policy_retries_from_preserved_code_without_manual_budget_or_usage_action(timed_out):
    env = timed_out
    env.workflow.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0})
    controller = FailureRemediation(env.store, env.workflow, recovery=env.service, models=env.models)
    original_calls = await env.store.list('model_invocation')
    original_accounts = await env.store.list('budget_account')
    upstream = await env.store.read('work_item', 'upstream')
    analyzed = await controller.analyze('bad')
    assert analyzed['failure_code'] == 'worker_timeout' and analyzed['status'] == 'blocked'
    assert not await env.store.list('timeout_recovery'), 'Reading failure analysis cannot grant execution'
    recovered = await controller.repair('bad')
    assert recovered['status'] == 'repair_scheduled', recovered
    work = await env.store.read('work_item', 'bad')
    assert work['generation'] == 2 and work['status'] == 'pending' and work['approval_required']
    assert work['payload']['recovery_checkpoint_id']
    assert await env.store.read('work_item', 'upstream') == upstream
    assert await env.store.list('model_invocation') == original_calls
    assert await env.store.list('budget_account') == original_accounts
    assert env.invocation_id in acknowledged_invocation_ids(await env.service._read('run'))
    assert len(await env.store.list('timeout_recovery')) == 1
    claim = await env.workflow.claim_next('run', 'test', 'claim-resumed-timeout')
    assert claim['work_item']['id'] == 'bad' and claim['attempt']['id'] != 'bad-attempt'
    assert env.workspace.joinpath('src/keep.js').read_text() == 'export const valuable = 42;\n'


async def test_disabled_timeout_policy_never_grants_or_restarts_work(timed_out):
    env = timed_out
    env.workflow.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0,
                                                         'auto_timeout_retry_limit': 0})
    controller = FailureRemediation(env.store, env.workflow, recovery=env.service, models=env.models)
    before = await env.store.read('work_item', 'bad')
    result = await controller.repair('bad')
    assert result['status'] == 'blocked'
    assert any(b['code'] == 'automatic_timeout_retry_limit' for b in result['blockers'])
    assert not await env.store.list('timeout_recovery')
    assert not await env.store.list('run_recovery')
    assert await env.store.read('work_item', 'bad') == before


async def test_timeout_waits_for_configured_cooldown_without_early_grants(timed_out, monkeypatch):
    from types import SimpleNamespace

    import agentflow.control.failure_remediation as module

    env = timed_out
    current = [module.time.time()]
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: current[0]))
    env.workflow.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 30})
    controller = FailureRemediation(env.store, env.workflow, recovery=env.service, models=env.models)
    result = await controller.repair('bad')
    assert result['status'] == 'blocked'
    assert any(b['code'] == 'retry_backoff' for b in result['blockers'])
    assert not await env.store.list('timeout_recovery')
    current[0] += 30
    result = await controller.repair('bad')
    assert result['status'] == 'repair_scheduled', result
    assert len(await env.store.list('timeout_recovery')) == 1
    assert len(await env.store.list('run_recovery')) == 1


async def test_timeout_cap_also_counts_prior_retries_that_needed_no_new_allowance(timed_out):
    env = timed_out
    env.workflow.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0,
        'auto_failure_retry_limit': 3, 'auto_timeout_retry_limit': 1})
    await patch(env, 'failure_analysis', 'earlier-timeout', run_id='run', work_item_id='bad',
                attempt_id='earlier-attempt', failure_code='worker_timeout', status='repair_scheduled')
    controller = FailureRemediation(env.store, env.workflow, recovery=env.service, models=env.models)
    result = await controller.repair('bad')
    assert result['status'] == 'blocked'
    assert any(b['code'] == 'automatic_timeout_retry_limit' for b in result['blockers'])
    assert not await env.store.list('timeout_recovery')
    assert not await env.store.list('run_recovery')


async def test_stopped_model_consumer_is_rechecked_without_an_unrelated_store_event(timed_out):
    env = timed_out
    env.workflow.settings = env.settings.model_copy(update={'auto_failure_retry_delay_seconds': 0})
    controller = FailureRemediation(env.store, env.workflow, recovery=env.service, models=env.models)
    env.models._active[env.invocation_id] = object()
    await controller.reconcile()
    assert not await env.store.list('timeout_recovery')
    env.models._active.clear()
    # Transport cleanup is an in-memory change. No user click or database
    # mutation should be needed to wake the pending automatic retry.
    for _ in range(3):
        await controller.reconcile()
    assert len(await env.store.list('run_recovery')) == 1
    assert (await env.store.read('work_item', 'bad'))['status'] == 'pending'

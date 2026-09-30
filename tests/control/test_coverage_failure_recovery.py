"""A rejected additive repair keeps actionable evidence and the normal retry path."""
from test_failure_remediation import automatic as automatic
from test_recovery import env as env
from test_recovery import patch, stopped_workspace

from agentflow.common import DomainError
from agentflow.control.failure_messages import exception_diagnostic
from agentflow.runtime.failures import runtime_failure_message


async def test_coverage_guard_failure_retries_with_original_structural_errors(automatic):
    env = automatic
    await stopped_workspace(env)
    error = DomainError('test_coverage_invalid', '测试补齐改变了原语句。',
                        details={'errors': ['tests/unit.test.mjs: original assertion changed']})
    diagnostic = exception_diagnostic(error, error.code)
    assert diagnostic['details'] == error.details
    assert '测试补齐' in runtime_failure_message(error.code)
    for kind, identity in (('work_item', 'bad'), ('attempt', 'bad-attempt')):
        await patch(env, kind, identity, runtime_failure_code=error.code, failure_diagnostic=diagnostic)
    accounts = await env.store.list('budget_account')
    upstream = await env.store.read('work_item', 'upstream')
    analysis = await env.automatic.analyze('bad')
    assert analysis['action'] == 'retry_current'
    result = await env.automatic.repair('bad')
    assert result['status'] == 'repair_scheduled'
    work = await env.store.read('work_item', 'bad')
    assert work['generation'] == 2
    assert 'original assertion changed' in work['payload']['recovery_instruction']
    assert '追加' in work['payload']['recovery_instruction']
    assert await env.store.list('budget_account') == accounts
    assert await env.store.read('work_item', 'upstream') == upstream

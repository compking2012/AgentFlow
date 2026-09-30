"""Actionable owner summaries without discarding or exposing protected diagnostics."""
from agentflow.control.planning_recovery import planning_failure_diagnostic, planning_state_diagnostic
from agentflow.runtime.failures import runtime_failure_message, valid_planning_failure_details
from agentflow.runtime.trace import public_value


def exception_diagnostic(error, code):
    # Existing provider/preflight categories keep their safe public wording.
    # Detailed model output validation is controller-owned, never a raw API error.
    detailed = code in {'planning_validation_failed', 'planning_state_changed', 'controller_validation_failed',
                        'review_disposition_invalid', 'test_migration_invalid', 'test_coverage_invalid', 'review_diagnostic_failed'}
    return {'code': error.code, 'message': public_value(error.message) if detailed else runtime_failure_message(code),
            'details': public_value(error.details) if detailed else None}


def failure_display_message(code, diagnostic=None):
    message = runtime_failure_message(code)
    if not isinstance(diagnostic, dict):
        return message
    details = diagnostic.get('details')
    if code == 'planning_validation_failed' and valid_planning_failure_details(details):
        issues = details['issues']
        writes = sum(issue['code'] == 'role_write_scope' for issue in issues)
        text = f'计划校验发现 {len(issues)} 项问题。'
        if writes:
            text += f'{writes} 个只读任务填写了写权限；审查对象应填写 inspection_paths，write_paths 必须为空。'
        locations = [f"{issue.get('stage_key') or '计划'} / {issue.get('child_key') or issue['path']}"
                     for issue in issues[:5]]
        return public_value(text + ' 错误位置：' + '；'.join(locations))[:1400]
    if code == 'controller_validation_failed':
        raw = public_value(f"{diagnostic.get('code', 'unknown')}：{diagnostic.get('message', '')}")
        return (message or '控制器校验未通过。') + ' ' + raw[:800]
    return message


async def failure_display(store, settings, work, attempt, code):
    diagnostic = work.get('failure_diagnostic') or (attempt or {}).get('failure_diagnostic')
    if attempt and code in {None, 'work_blocked', 'controller_validation_failed', 'planning_validation_failed', 'planning_state_changed'}:
        changed = planning_state_diagnostic(work, attempt)
        if changed:
            return 'planning_state_changed', failure_display_message('planning_state_changed', changed)
        planned = await planning_failure_diagnostic(store, settings, work, attempt)
        if planned:
            diagnostic, code = planned, 'planning_validation_failed'
    return code, failure_display_message(code, diagnostic)

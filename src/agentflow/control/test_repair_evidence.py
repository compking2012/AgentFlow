"""Evidence-derived authority shared by test repair and its independent review."""

from agentflow.testing.reports import NormalizedReport

MISSING_CASE_REPAIR_SCOPE = (
    'Complete only the exact missing required framework case IDs with executable tests and substantive assertions. '
    'Keep all original case IDs, assertions, expected values and acceptance criteria unchanged. '
    'Do not modify product source, test plans, support files, tooling, recipes or sandbox policy. '
    'Do not skip tests, fabricate results or weaken coverage. New tests are permitted only for missing required IDs.'
)


def test_repair_report_kind(report):
    try:
        parsed = NormalizedReport.model_validate(report)
    except (ValueError, TypeError):
        return None
    if not parsed.cases or any(case.status not in {'passed', 'failed', 'error'} for case in parsed.cases):
        return None
    failed = any(case.status in {'failed', 'error'} for case in parsed.cases)
    if (parsed.execution_status == 'error' and parsed.errors == ['missing_required_cases']
            and parsed.missing_case_ids and parsed.quality_result == ('failed' if failed else 'unknown')):
        return 'missing_required_cases'
    if (parsed.execution_status == 'completed' and not parsed.errors and not parsed.missing_case_ids
            and parsed.quality_result == ('failed' if failed else 'passed')):
        return 'runtime_setup'
    return None


def test_repair_kind(reports):
    kinds = [test_repair_report_kind(report) for report in reports]
    if not kinds or None in kinds:
        return None
    if 'missing_required_cases' in kinds:
        return 'missing_required_cases'
    if any(report['quality_result'] == 'failed' for report in reports):
        return 'runtime_setup'
    return None

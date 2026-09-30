import base64
import json

import pytest

from agentflow.testing.reports import parse_playwright


def report(path, metric):
    value = {'suites': [{'title': 'API', 'specs': [{'title': '读取事项', 'tests': [{'projectName': 'api', 'results': [
        {'status': 'passed', 'duration': 100, 'attachments': [{
            'name': 'agentflow-performance', 'contentType': 'application/vnd.agentflow.performance+json',
            'body': base64.b64encode(json.dumps({'metrics': [metric]}).encode()).decode()}]}]}]}]}]}
    path.write_text(json.dumps(value))
    return parse_playwright(path)


def test_actual_report_attachment_carries_measurement_units_sample_count_and_case_identity(tmp_path):
    result = report(tmp_path / 'report.json', {'name': 'API latency p95', 'value': 12.5, 'unit': 'ms', 'sample_count': 100})
    assert result.quality_result == 'passed'
    metric = result.performance_metrics[0]
    assert metric.value == 12.5 and metric.sample_count == 100 and metric.case_id == 'API::读取事项::api'
    assert result.cases[0].duration_seconds == .1, 'Suite duration is distinct from measured API latency'


@pytest.mark.parametrize('change', [{'value': float('nan')}, {'value': float('inf')}, {'value': -1},
                                  {'value': True}, {'sample_count': 0}, {'sample_count': True}, {'unit': 'invented'}])
def test_invalid_performance_never_becomes_success_or_a_zero_measurement(tmp_path, change):
    result = report(tmp_path / 'report.json', {'name': 'API latency p95', 'value': 12.5, 'unit': 'ms',
                                             'sample_count': 100, **change})
    assert result.execution_status == 'error' and not result.performance_metrics
    assert 'invalid_performance_measurement' in result.errors

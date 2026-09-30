import json

import pytest

from agentflow.common import DomainError
from agentflow.testing.reports import parse_junit, parse_playwright, parse_xcresult_export


def test_junit_uses_actual_cases_not_summary_counters(tmp_path):
    p = tmp_path / "report.xml"
    p.write_text('<testsuite tests="900" failures="0"><testcase classname="Auth" name="denied"><failure message="wrongly allowed"/></testcase></testsuite>')
    report = parse_junit(p, {"Auth::denied"})
    assert report.quality_result == "failed" and len(report.cases) == 1
    p.write_text('<testsuite tests="2"><testcase name="ok"/></testsuite>')
    report = parse_junit(p, {"ok", "missing"})
    assert report.quality_result == "unknown" and report.missing_case_ids == ["missing"]


def test_zero_skipped_and_xml_entities_cannot_pass(tmp_path):
    p = tmp_path / "report.xml"
    for content in ['<testsuites/>', '<testsuite><testcase name="skip"><skipped/></testcase></testsuite>']:
        p.write_text(content)
        assert parse_junit(p).quality_result != "passed"
    p.write_text('<!DOCTYPE foo [<!ENTITY x SYSTEM "file:///etc/passwd">]><testsuite>&x;</testsuite>')
    with pytest.raises(DomainError):
        parse_junit(p)


def test_playwright_preserves_failure_before_retry_and_missing_tests(tmp_path):
    p = tmp_path / "pw.json"
    p.write_text(json.dumps({"suites": [{"title": "tickets", "specs": [{"title": "save", "tests": [
        {"projectName": "chromium", "results": [{"status": "failed", "duration": 20}, {"status": "passed", "duration": 10}]}]}]}]}))
    report = parse_playwright(p)
    assert report.quality_result == "failed" and len(report.cases) == 2
    p.write_text(json.dumps({"suites": [], "stats": {"expected": 400}}))
    assert parse_playwright(p).quality_result == "unknown"


def test_xcresult_needs_actual_leaf_test_records(tmp_path):
    p = tmp_path / "xcresult.json"
    p.write_text(json.dumps({"testNodes": [{"nodeType": "Test Plan", "children": [
        {"nodeType": "Test Case", "nodeIdentifier": "TicketTests/testSave()", "result": "Passed"},
        {"nodeType": "Test Case", "nodeIdentifier": "TicketTests/testUnauthorized()", "result": "Failed"}]}]}))
    report = parse_xcresult_export(p)
    assert report.quality_result == "failed" and len(report.cases) == 2
    p.write_text('{"status":"success","totalTests":42}')
    assert parse_xcresult_export(p).quality_result == "unknown"

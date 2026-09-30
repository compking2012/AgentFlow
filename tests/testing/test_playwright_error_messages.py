import hashlib
import json

import pytest

from agentflow.testing.reports import parse_playwright


def _parse_results(tmp_path, results):
    path = tmp_path / "playwright.json"
    path.write_text(json.dumps({"suites": [{"title": "notifications", "specs": [{
        "title": "scroll and bottom actions", "tests": [{
            "projectName": "chromium", "results": results,
        }],
    }]}]}), encoding="utf-8")
    return parse_playwright(path), path


def test_secondary_operation_error_reaches_normalized_message(tmp_path):
    timeout = "Test timeout of 20000ms exceeded."
    detail = (
        "locator.click: Test timeout of 20000ms exceeded.\n"
        "Call log:\n"
        "  - waiting for locator('.menu-bar__notifications')\n"
        "  - notification-banner intercepts pointer events"
    )
    report, path = _parse_results(tmp_path, [{
        "status": "timedOut", "duration": 20000,
        "error": {"message": timeout},
        "errors": [{"message": timeout}, {"message": detail, "stack": detail + "\n    at test.ts:10:3"}],
    }, {"status": "passed", "duration": 50}])

    assert detail in report.cases[0].message
    assert "\n    at test.ts:10:3" in report.cases[0].message
    assert report.cases[0].message.count("notification-banner intercepts pointer events") == 1
    assert report.cases[0].message.startswith(timeout + "\n")
    assert report.raw_digest == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert [(case.case_id, case.status, case.attempt, case.duration_seconds) for case in report.cases] == [
        ("notifications::scroll and bottom actions::chromium", "error", 0, 20),
        ("notifications::scroll and bottom actions::chromium", "passed", 1, 0.05),
    ]
    assert report.execution_status == "completed"
    assert report.quality_result == "failed"
    assert report.errors == []


def test_duplicate_long_stacks_do_not_displace_unique_operation_error(tmp_path):
    timeout = "Test timeout of 20000ms exceeded."
    stack = timeout + "\n" + "    at repeated-frame.ts:10:3\n" * 1000
    repeated = {"message": timeout, "stack": stack}
    report, _ = _parse_results(tmp_path, [{
        "status": "failed", "error": repeated,
        "errors": [repeated] * 20 + [{"message": "locator.click: notification-banner intercepts pointer events"}],
    }])

    message = report.cases[0].message
    assert "locator.click: notification-banner intercepts pointer events" in message
    assert message.count(timeout) == 1
    assert len(message) <= 8000


def test_long_error_keeps_space_for_other_unique_errors(tmp_path):
    report, _ = _parse_results(tmp_path, [{
        "status": "failed", "errors": [
            {"message": "locator.click: " + "x" * 20000},
            {"message": "expect.toBeVisible: notification panel remained hidden"},
        ],
    }])

    message = report.cases[0].message
    assert "locator.click:" in message
    assert "expect.toBeVisible: notification panel remained hidden" in message
    assert 7900 <= len(message) <= 8000


@pytest.mark.parametrize(("error_fields", "expected"), [
    ({"error": "plain error"}, "plain error"),
    ({"error": {"message": "message only"}}, "message only"),
    ({"error": {"value": "thrown value"}}, "thrown value"),
    ({"errors": [{"message": "errors only"}]}, "errors only"),
    ({"errors": ["string error"]}, "string error"),
    ({"error": {"stack": "stack only"}}, "stack only"),
    ({}, ""),
    ({"error": None, "errors": None}, ""),
    ({"error": {}, "errors": []}, ""),
    ({"error": {"unexpected": "shape"}}, '"unexpected": "shape"'),
    ({"errors": {"message": "single object"}}, "single object"),
    ({"errors": "single string"}, "single string"),
])
def test_error_shapes_remain_readable_without_changing_quality(tmp_path, error_fields, expected):
    report, _ = _parse_results(tmp_path, [{"status": "passed", **error_fields}])

    message = report.cases[0].message
    if expected:
        assert expected in message
    else:
        assert message == ""
    assert report.cases[0].status == "passed"
    assert report.quality_result == "passed"
    assert report.execution_status == "completed"

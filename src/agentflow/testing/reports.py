"""Parse native framework evidence without trusting its summary counters."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Literal

from defusedxml import ElementTree
from pydantic import ConfigDict, Field

from agentflow.common import DomainError
from agentflow.execution.manifests import file_digest
from agentflow.execution.models import WireModel


class CaseResult(WireModel):
    case_id: str
    status: Literal["passed", "failed", "error", "skipped", "unknown"]
    duration_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    message: str = ""
    attempt: int = Field(default=0, ge=0)


class PerformanceMetric(WireModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str = Field(min_length=1, max_length=120)
    value: float = Field(ge=0, allow_inf_nan=False)
    unit: Literal['ms', 's', 'bytes', 'requests/s', 'percent']
    sample_count: int = Field(ge=1, le=10000000)
    case_id: str


class NormalizedReport(WireModel):
    framework: str
    raw_digest: str
    cases: list[CaseResult]
    execution_status: Literal["completed", "error"]
    quality_result: Literal["passed", "failed", "unknown"]
    missing_case_ids: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    performance_metrics: list[PerformanceMetric] = Field(default_factory=list)


def _read(path: Path, maximum_bytes: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise DomainError("missing_report", "A regular raw framework report is required", 422)
    if path.stat().st_size > maximum_bytes:
        raise DomainError("report_too_large", "Report exceeds parser limit", 413)
    return path.read_bytes()


def _finish(framework: str, path: Path, cases: list[CaseResult], expected: set[str] | None,
            errors: list[str] | None = None) -> NormalizedReport:
    issues = list(errors or [])
    ids = {case.case_id for case in cases}
    missing = sorted((expected or set()) - ids)
    if not cases:
        issues.append("zero_discovered_tests")
    if missing:
        issues.append("missing_required_cases")
    pairs = [(c.case_id, c.attempt) for c in cases]
    if len(set(pairs)) != len(pairs):
        issues.append("duplicate_case_attempt")
    # Retried passes never erase a failed original attempt.
    failed = any(c.status in {"failed", "error"} for c in cases)
    incomplete = any(c.status in {"skipped", "unknown"} for c in cases)
    return NormalizedReport(framework=framework, raw_digest=file_digest(path), cases=cases,
                            execution_status="error" if issues else "completed",
                            quality_result="failed" if failed else "unknown" if issues or incomplete else "passed",
                            missing_case_ids=missing, errors=issues)


def parse_junit(path: Path, expected_case_ids: set[str] | None = None,
                maximum_bytes: int = 16 * 1024 * 1024) -> NormalizedReport:
    try:
        root = ElementTree.fromstring(_read(path, maximum_bytes))
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError("invalid_report", "JUnit XML is malformed or contains forbidden entities", 422) from exc
    if root.tag.split("}")[-1] not in {"testsuites", "testsuite", "test-run"}:
        raise DomainError("invalid_report", "Unrecognized JUnit/NUnit root", 422)
    cases = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in {"testcase", "test-case"}:
            continue
        name = el.get("name")
        if not name:
            raise DomainError("invalid_report", "Test case has no name", 422)
        case_id = el.get("fullname") or "::".join(filter(None, [el.get("classname"), name]))
        children = {c.tag.split("}")[-1]: c for c in el}
        result = el.get("result", "").lower()
        state = "passed"
        if "error" in children or result == "error":
            state = "error"
        elif "failure" in children or result == "failed":
            state = "failed"
        elif "skipped" in children or result in {"skipped", "inconclusive", "ignored"}:
            state = "skipped"
        elif result and result not in {"passed", "success"}:
            state = "unknown"
        try:
            duration = float(el.get("time", el.get("duration", "0")))
        except ValueError as exc:
            raise DomainError("invalid_report", "Invalid test duration", 422) from exc
        messages = [c.get("message", "") + " ".join(c.itertext()) for c in el if c.tag in {"failure", "error", "skipped"}]
        cases.append(CaseResult(case_id=case_id, status=state, duration_seconds=duration,
                                message="\n".join(messages)[:8000]))
    return _finish("junit", path, cases, expected_case_ids)


def _bounded_error_messages(messages: list[str], maximum: int) -> str:
    # Reserve a share for every distinct error before allowing long call logs to
    # consume the remainder. Short messages give unused space back to long ones.
    messages = messages[:max(0, (maximum + 1) // 2)]
    remaining = maximum - max(0, len(messages) - 1)
    fragments = [""] * len(messages)
    for position, index in enumerate(sorted(range(len(messages)), key=lambda i: len(messages[i]))):
        allowance = remaining // (len(messages) - position)
        message = messages[index]
        fragment = message if len(message) <= allowance else message[:max(0, allowance - 1)] + "…"
        fragments[index] = fragment
        remaining -= len(fragment)
    return "\n".join(fragments)


def _playwright_error_message(result: dict[str, Any]) -> str:
    messages: dict[str, None] = {}
    stacks: dict[str, None] = {}
    errors = result.get("errors", [])
    entries = errors if isinstance(errors, list) else [errors]
    for error in [result.get("error"), *entries]:
        if isinstance(error, str):
            if error.strip():
                messages[error.strip()] = None
        elif isinstance(error, dict):
            bodies = [error[key].strip() for key in ("message", "value")
                      if isinstance(error.get(key), str) and error[key].strip()]
            for body in bodies:
                messages[body] = None
            stack = error.get("stack")
            if isinstance(stack, str) and stack.strip():
                for body in bodies:
                    if stack.startswith(body):
                        stack = stack[len(body):].lstrip("\r\n")
                if stack.strip():
                    (stacks if bodies else messages)[stack.rstrip()] = None
            elif not bodies and error:
                messages[json.dumps(error, ensure_ascii=False)] = None
        elif error is not None and error != []:
            messages[json.dumps(error, ensure_ascii=False)] = None
    # Put all error bodies ahead of stack frames, which often repeat the same
    # timeout and must not displace a later operation's actionable call log.
    message = _bounded_error_messages(list(messages), 8000)
    remaining = 8000 - len(message) - bool(message)
    extra = _bounded_error_messages([stack for stack in stacks if stack not in messages], remaining)
    return message + ("\n" if message and extra else "") + extra


def parse_playwright(path: Path, expected_case_ids: set[str] | None = None,
                     maximum_bytes: int = 32 * 1024 * 1024) -> NormalizedReport:
    try:
        raw = json.loads(_read(path, maximum_bytes))
    except (ValueError, UnicodeError) as exc:
        raise DomainError("invalid_report", "Malformed Playwright JSON", 422) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("suites"), list):
        raise DomainError("invalid_report", "Missing Playwright suites", 422)
    cases: list[CaseResult] = []
    errors = ["runner_error"] if raw.get("errors") else []
    metrics = []

    def visit(suite: dict[str, Any], prefix: list[str]) -> None:
        trail = prefix + ([suite["title"]] if suite.get("title") else [])
        for spec in suite.get("specs", []):
            title = spec.get("title")
            if not isinstance(title, str):
                errors.append("unnamed_spec")
                continue
            for test in spec.get("tests", []):
                identity = "::".join([*trail, title, test.get("projectName", "default")])
                results = test.get("results", [])
                if not results:
                    cases.append(CaseResult(case_id=identity, status="unknown"))
                for index, result in enumerate(results):
                    state = {"passed": "passed", "failed": "failed", "timedOut": "error",
                             "interrupted": "error", "skipped": "skipped"}.get(result.get("status"), "unknown")
                    cases.append(CaseResult(case_id=identity, status=state, attempt=index,
                                            duration_seconds=max(0, result.get("duration", 0)) / 1000,
                                            message=_playwright_error_message(result)))
                    for attachment in result.get('attachments', []):
                        if attachment.get('contentType') != 'application/vnd.agentflow.performance+json':
                            continue
                        try:
                            body = attachment.get('body', '')
                            if not isinstance(body, str) or len(body) > 45000:
                                raise ValueError('Measurement attachment exceeds limit')
                            payload = json.loads(base64.b64decode(body, validate=True))
                            values = payload['metrics']
                            if not isinstance(values, list) or len(values) > 32:
                                raise ValueError('Invalid measurement count')
                            for metric in values:
                                metrics.append(PerformanceMetric.model_validate({**metric, 'case_id': identity}))
                        except (ValueError, KeyError, TypeError):
                            errors.append('invalid_performance_measurement')
        for child in suite.get("suites", []):
            visit(child, trail)
    for suite in raw["suites"]:
        visit(suite, [])
    return _finish("playwright", path, cases, expected_case_ids, errors).model_copy(update={'performance_metrics': metrics})


def parse_xcresult_export(path: Path, expected_case_ids: set[str] | None = None,
                          maximum_bytes: int = 32 * 1024 * 1024) -> NormalizedReport:
    """Parse xcrun xcresulttool get test-results tests --format json output.

    Exporting an xcresult bundle requires the actual Xcode adapter. This parser
    does not treat a top-level 'success' summary as evidence of test execution.
    """
    try:
        raw = json.loads(_read(path, maximum_bytes))
    except (ValueError, UnicodeError) as exc:
        raise DomainError("invalid_report", "Malformed xcresult export", 422) from exc
    cases: list[CaseResult] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            identity = node.get("nodeIdentifier") or node.get("testIdentifier")
            result = node.get("result") or node.get("testStatus")
            node_type = str(node.get("nodeType", ""))
            is_case = node_type in {"Test Case", "Test Case Run", "TestCase"} or "testIdentifier" in node
            if identity and result and is_case:
                state = {"Passed": "passed", "Success": "passed", "Failed": "failed",
                         "Failure": "failed", "Skipped": "skipped", "Expected Failure": "failed"}.get(result, "unknown")
                cases.append(CaseResult(case_id=str(identity), status=state,
                                        message=str(node.get("details", ""))[:8000]))
            for key in ("testNodes", "children", "tests", "subtests", "summaries"):
                if key in node:
                    visit(node[key])
    visit(raw)
    return _finish("xcresult", path, cases, expected_case_ids)


def parse_instrumentation(path: Path, expected_case_ids: set[str] | None = None,
                          maximum_bytes: int = 16 * 1024 * 1024) -> NormalizedReport:
    """Parse real `adb shell am instrument -w -r` output, including runner failures."""
    raw = _read(path, maximum_bytes).decode("utf-8", errors="replace")
    current: dict[str, str] = {}
    cases: list[CaseResult] = []
    errors = []
    for line in raw.splitlines():
        if line.startswith("INSTRUMENTATION_STATUS: "):
            key, separator, value = line[len("INSTRUMENTATION_STATUS: "):].partition("=")
            if separator:
                current[key] = value
        elif line.startswith("INSTRUMENTATION_STATUS_CODE: "):
            try:
                code = int(line.split(":", 1)[1].strip())
            except ValueError:
                errors.append("invalid_instrumentation_status")
                continue
            if code != 1 and current.get("test"):
                identity = "::".join(filter(None, [current.get("class"), current["test"]]))
                state = {0: "passed", -1: "error", -2: "failed", -3: "skipped", -4: "skipped"}.get(code, "unknown")
                cases.append(CaseResult(case_id=identity, status=state, message=current.get("stack", "")[:8000]))
            current = {}
        elif "INSTRUMENTATION_FAILED" in line or "Process crashed" in line or "shortMsg=" in line:
            errors.append("instrumentation_runner_failed")
    if "INSTRUMENTATION_CODE:" not in raw:
        errors.append("instrumentation_did_not_finish")
    return _finish("instrumentation", path, cases, expected_case_ids, errors)

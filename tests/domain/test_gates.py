from copy import deepcopy

import pytest

from agentflow.domain.gates import evaluate_gate


def valid_gate_inputs():
    candidate = {"fingerprint": "candidate-a", "state": "testing", "required_review_ids": ["review"],
                 "required_approval_ids": ["approval"]}
    matrix = [{"execution_key": "ios-case", "required": True}, {"execution_key": "api-case", "required": True}]
    checks = [{"execution_key": key, "candidate_fingerprint": "candidate-a", "evidence_verified": True,
               "execution_status": "completed", "quality_result": "passed", "executed_case_count": 1,
               "assertion_count": 2, "raw_report_artifact_id": "report"} for key in ["ios-case", "api-case"]]
    approvals = [{"id": "approval", "decision": "approve", "fingerprint": "candidate-a"}]
    reviews = [{"id": "review", "candidate_fingerprint": "candidate-a", "quality_result": "passed",
                "reviewer_id": "reviewer", "author_id": "author", "blocking_findings": []}]
    return candidate, matrix, checks, approvals, reviews


def test_complete_evidence_is_required_for_exact_candidate():
    values = valid_gate_inputs()
    gate = evaluate_gate(*values)
    assert gate["passed"] and gate["required_count"] == gate["valid_count"] == 2


@pytest.mark.parametrize("change", [
    {"evidence_verified": False}, {"candidate_fingerprint": "older"}, {"stale": True},
    {"execution_status": "execution_unknown"}, {"quality_result": "failed"},
    {"executed_case_count": 0}, {"assertion_count": 0}, {"raw_report_artifact_id": None},
])
def test_human_approval_cannot_override_invalid_or_missing_native_result(change):
    values = deepcopy(valid_gate_inputs())
    values[2][0].update(change)
    assert not evaluate_gate(*values)["passed"]


def test_self_review_empty_matrix_and_stale_approval_fail():
    values = valid_gate_inputs()
    values[4][0]["reviewer_id"] = "author"
    assert not evaluate_gate(*values)["passed"]
    values = valid_gate_inputs()
    values[1].clear()
    assert not evaluate_gate(*values)["passed"]
    values = valid_gate_inputs()
    values[3][0]["stale"] = True
    assert not evaluate_gate(*values)["passed"]


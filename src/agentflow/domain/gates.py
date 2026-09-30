"""Quality gates never accept an agent's prose or a human approval as test evidence."""

from __future__ import annotations

from agentflow.common import canonical_digest


def evaluate_gate(candidate: dict, matrix: list[dict], checks: list[dict],
                  approvals: list[dict], reviews: list[dict]) -> dict:
    reasons: list[str] = []
    fingerprint = candidate["fingerprint"]
    if candidate.get("state") not in {"platform_artifacts_frozen", "testing", "verified"}:
        reasons.append("Platform artifacts have not been frozen")
    required = {entry["execution_key"] for entry in matrix if entry.get("required", True)}
    if not required:
        reasons.append("Required execution matrix is empty")
    valid = set()
    for check in checks:
        if (check.get("candidate_fingerprint") != fingerprint or not check.get("evidence_verified")
                or check.get("stale")):
            continue
        assertion_count = check.get("assertion_count")
        assertion_evidence = (isinstance(assertion_count, int) and assertion_count > 0) or (
            assertion_count is None and check.get("framework_case_evidence") is True)
        if (check.get("execution_status") == "completed" and check.get("quality_result") == "passed"
                and check.get("executed_case_count", 0) > 0 and assertion_evidence
                and check.get("raw_report_artifact_id")):
            valid.add(check["execution_key"])
        elif check.get("execution_key") in required:
            reasons.append(f"Required check is not passing: {check['execution_key']}")
    missing = sorted(required - valid)
    reasons.extend(f"Missing valid evidence: {key}" for key in missing)
    required_reviews = set(candidate.get("required_review_ids", []))
    completed_reviews = {r["id"] for r in reviews if r.get("candidate_fingerprint") == fingerprint
                         and r.get("quality_result") == "passed" and not r.get("blocking_findings")
                         and r.get("reviewer_id") != r.get("author_id") and not r.get("stale")}
    if not required_reviews:
        reasons.append("Independent review requirement is missing")
    if required_reviews - completed_reviews:
        reasons.append("Independent review is incomplete or blocking")
    needed_approvals = set(candidate.get("required_approval_ids", []))
    valid_approvals = {a["id"] for a in approvals if a.get("decision") == "approve"
                       and a.get("fingerprint") == fingerprint and not a.get("stale")}
    if needed_approvals - valid_approvals:
        reasons.append("Human approval is missing or stale")
    return {"passed": not reasons, "reasons": sorted(set(reasons)), "required_count": len(required),
            "valid_count": len(required & valid), "fingerprint": canonical_digest({
                "candidate": fingerprint, "required": sorted(required), "valid": sorted(valid),
                "reasons": sorted(set(reasons)),
            })}

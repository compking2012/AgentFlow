"""Frozen planning affordances and pure, aggregate model-output validation."""

from __future__ import annotations

import copy
from pathlib import PurePosixPath, PureWindowsPath

from agentflow.common import DomainError

from .planning import CODING_STEPS, ROLES
from .review_phase import allowed_review_focuses, review_phase_contract, validate_review_focus


def safe_planning_path(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or any(ord(c) < 32 for c in value)
        or "\\" in value
        or any(c in value for c in "*?[]:")
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or any(
            p == ".."
            or p.rstrip(". ").casefold() == ".git"
            or p.casefold() == ".git"
            or (p not in {".", ""} and p != p.rstrip(". "))
            for p in value.split("/")
        )
    ):
        raise DomainError("invalid_write_scope", "Paths must be explicit safe workspace-relative paths", 422)
    return str(PurePosixPath(value))


def build_planning_contract(
    run, producer, plan, work_items, *, max_children, max_work_items, max_edges, reused_steps=()
):
    items = [i for i in work_items if not i.get("archived") and i.get("run_id", run["id"]) == run["id"]]
    by_id = {i["id"]: i for i in items}
    selected = set(plan.get("actual_steps", []))
    stages = []
    for item in items:
        authorized = any(
            all(s.get(k) == item.get(k) for k in ("key", "step", "role")) for s in plan.get("work_specs", [])
        )
        mandatory = item["step"] == "code_review" and any(
            item["key"] == s + ":review" and s in selected
            for s in ("unit_test_implementation", "integration_test_implementation")
        )
        if (
            not authorized
            or (item["step"] not in selected and not mandatory)
            or ROLES.get(item["step"]) != item["role"]
            or item["role"] == "system"
            or item.get("status") != "pending"
            or item.get("attempt_id")
            or item.get("kind") in {"aggregation", "stage_child"}
            or item.get("parent_stage_id")
            or item["step"] in {"unit_test_execution", "integration_test_execution"}
        ):
            continue
        phase = review_phase_contract(item, by_id, reused_steps=reused_steps)
        stages.append(
            dict(
                stage_key=item["key"],
                work_item_id=item["id"],
                revision=item.get("revision"),
                input_fingerprint=item.get("input_fingerprint"),
                generation=item.get("generation"),
                step=item["step"],
                role=item["role"],
                write_paths=[safe_planning_path(p) for p in item.get("write_paths", [])],
                dependency_count=len(item.get("dependencies", [])),
                review_phase_contract=phase,
                allowed_review_focuses=allowed_review_focuses(phase) if phase else [],
            )
        )
    return copy.deepcopy(
        dict(
            version=1,
            run_id=run["id"],
            run_revision=run.get("revision"),
            input_fingerprint=run.get("input_fingerprint"),
            plan_id=plan.get("id", run.get("plan_id")),
            plan_revision=plan.get("revision"),
            producer={
                k: producer.get(k)
                for k in ("id", "revision", "attempt_id", "fencing_token", "input_fingerprint")
            },
            limits=dict(max_children=max_children, max_work_items=max_work_items, max_edges=max_edges),
            graph=dict(
                work_item_count=len(items),
                edge_count=sum(len(i.get("dependencies", [])) for i in items),
                existing_keys=[i["key"] for i in items],
            ),
            stages=sorted(stages, key=lambda s: s["stage_key"]),
        )
    )


def validate_parallel_work(proposals, contract):
    if not isinstance(contract, dict) or contract.get("version") != 1:
        raise DomainError("invalid_planning_contract", "Planning contract is missing or unsupported")
    issues = []

    def issue(code, stage, child, path, message):
        issues.append(dict(code=code, stage_key=stage, child_key=child, path=path, message=message))

    if not isinstance(proposals, list):
        issue("invalid_expansion", None, None, "parallel_work", "Expected an array")
        proposals = []
    stages = {s["stage_key"]: s for s in contract["stages"]}
    normalized = []
    seen = set()
    additional = 0
    edges = contract["graph"]["edge_count"]
    for n, group in enumerate(proposals):
        base = f"parallel_work/{n}"
        if (
            not isinstance(group, dict)
            or set(group) != {"stage_key", "children"}
            or not isinstance(group.get("stage_key"), str)
        ):
            issue("invalid_expansion", None, None, base, "Expected stage_key and children only")
            continue
        key = group["stage_key"]
        stage = stages.get(key)
        if key in seen:
            issue("duplicate_stage", key, None, base, "Each stage may appear only once")
        seen.add(key)
        if stage is None:
            issue(
                "stage_outside_scope",
                key,
                None,
                base,
                "Choose an available stage from the frozen planning contract",
            )
        children = group["children"]
        if not isinstance(children, list):
            issue("invalid_expansion", key, None, base + "/children", "Expected an array")
            continue
        if not 2 <= len(children) <= contract["limits"]["max_children"]:
            issue(
                "expansion_limit_exceeded",
                key,
                None,
                base + "/children",
                "Child count exceeds the frozen bounds",
            )
        additional += len(children)
        if stage:
            edges += len(children) * (stage["dependency_count"] + 1) - stage["dependency_count"]
        child_keys = set()
        clean = []
        for j, child in enumerate(children):
            path = f"{base}/children/{j}"
            if (
                not isinstance(child, dict)
                or not {"key", "goal", "write_paths"} <= set(child)
                or set(child) - {"key", "goal", "write_paths", "inspection_paths", "review_focus"}
            ):
                issue(
                    "invalid_expansion",
                    key,
                    None,
                    path,
                    "Expected key, goal, write_paths and optional inspection_paths/review_focus",
                )
                continue
            ck = child["key"]
            valid_key = (
                isinstance(ck, str)
                and bool(ck.strip())
                and len(ck) <= 128
                and not any(ord(c) < 32 or c in "/\\" for c in ck)
            )
            if not valid_key:
                issue("invalid_expansion", key, None, path + "/key", "Child key must be a bounded identifier")
            elif ck in child_keys or key + ":" + ck in contract["graph"]["existing_keys"]:
                issue("duplicate_child", key, ck, path + "/key", "Child key must be unique")
            if valid_key:
                child_keys.add(ck)
            ck = ck if isinstance(ck, str) else None
            if (
                not isinstance(child["goal"], str)
                or not child["goal"].strip()
                or len(child["goal"]) > 16000
                or "\0" in child["goal"]
            ):
                issue("invalid_expansion", key, ck, path + "/goal", "Goal must be bounded and concrete")
            out = copy.deepcopy(child)
            for field in ("write_paths", "inspection_paths"):
                if field not in child:
                    continue
                values = child[field]
                normal = []
                if not isinstance(values, list) or len(values) > 128:
                    issue("invalid_write_scope", key, ck, path + "/" + field, "Expected at most 128 paths")
                    continue
                for p in values:
                    try:
                        normal.append(safe_planning_path(p))
                    except DomainError as error:
                        issue(error.code, key, ck, path + "/" + field, error.message)
                out[field] = sorted(set(normal))
            if stage:
                paths = out["write_paths"]
                if stage["step"] not in CODING_STEPS and paths:
                    issue(
                        "role_write_scope",
                        key,
                        ck,
                        path + "/write_paths",
                        "Non-coding children must have empty write_paths; use inspection_paths for inspection targets",
                    )
                if (
                    stage["step"] in CODING_STEPS
                    and isinstance(paths, list)
                    and all(isinstance(p, str) for p in paths)
                    and (
                        not paths
                        or any(
                            not any(s == "." or p == s or p.startswith(s + "/") for s in stage["write_paths"])
                            for p in paths
                        )
                    )
                ):
                    issue(
                        "write_scope_expansion",
                        key,
                        ck,
                        path + "/write_paths",
                        "Coding write paths must remain within the original stage scope",
                    )
                try:
                    validate_review_focus(child.get("review_focus"), stage["review_phase_contract"])
                except DomainError as error:
                    issue(error.code, key, ck, path + "/review_focus", error.message)
            clean.append(out)
        normalized.append(dict(stage_key=key, children=sorted(clean, key=lambda c: str(c["key"]))))
    if (
        contract["graph"]["work_item_count"] + additional > contract["limits"]["max_work_items"]
        or edges > contract["limits"]["max_edges"]
    ):
        issue(
            "graph_limit_exceeded",
            None,
            None,
            "parallel_work",
            "The full proposed graph exceeds frozen work or edge limits",
        )
    if issues:
        raise DomainError(
            "planning_validation_failed",
            "Correct all planning issues before finishing",
            422,
            details=dict(
                origin="planning_validation",
                phase="plan_validation",
                category="correctable_output",
                issues=issues,
            ),
        )
    return sorted(normalized, key=lambda g: g["stage_key"])


# Shared with the transactional expander; one path authority for both phases.
normalize_scope_path = safe_planning_path

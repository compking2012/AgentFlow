"""Versioned work graphs. Dependencies describe accepted outputs, not running processes."""

from __future__ import annotations

from dataclasses import dataclass, field

from agentflow.common import DomainError, canonical_digest

STEPS = (
    "goal", "research", "prd", "requirements", "architecture", "development_plan",
    "implementation", "code_review", "unit_test_plan", "integration_test_strategy", "unit_test_implementation",
    "integration_test_implementation", "unit_test_execution",
    "integration_test_execution", "delivery", "retrospective",
)
ROLES = {
    'review_disposition': 'review', 'review_validation': 'integration_test',
    'review_unit_migration': 'unit_test', 'review_integration_migration': 'integration_test',
    "goal": "product", "research": "research", "prd": "product", "requirements": "product",
    "architecture": "architecture_planning", "development_plan": "architecture_planning",
    "implementation": "development", "code_review": "review", "unit_test_plan": "unit_test",
    "unit_test_implementation": "unit_test", "unit_test_execution": "unit_test",
    "integration_test_strategy": "integration_test", "integration_test_implementation": "integration_test",
    "integration_test_execution": "integration_test", "delivery": "system", "retrospective": "product",
}
CODING_STEPS = frozenset({"implementation", "unit_test_implementation", "integration_test_implementation",
                          'review_unit_migration', 'review_integration_migration'})
EXECUTION_STEPS = frozenset({"unit_test_execution", "integration_test_execution"})
PREREQUISITES = {step: (STEPS[i - 1],) if i else () for i, step in enumerate(STEPS)}


def select_steps(selection: dict) -> list[str]:
    mode = selection.get("mode", "full")
    if mode == "full":
        return list(STEPS)
    if mode == "from_to":
        try:
            first, last = STEPS.index(selection["from_step"]), STEPS.index(selection["to_step"])
        except (ValueError, KeyError) as exc:
            raise DomainError("invalid_selection", "Unknown start or end stage", 422) from exc
        if first > last:
            raise DomainError("invalid_selection", "Start stage must precede end stage", 422)
        return list(STEPS[first:last + 1])
    if mode == "selected":
        requested = selection.get("selected_steps", [])
        if not requested or len(requested) != len(set(requested)) or set(requested) - set(STEPS):
            raise DomainError("invalid_selection", "Select unique known stages", 422)
        return [step for step in STEPS if step in requested]
    raise DomainError("invalid_selection", "Unknown selection mode", 422)


@dataclass(frozen=True)
class WorkSpec:
    key: str
    step: str
    role: str
    dependencies: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    required: bool = True
    payload: dict = field(default_factory=dict)


def validate_graph(specs: list[WorkSpec]) -> None:
    by_key = {s.key: s for s in specs}
    if len(by_key) != len(specs) or not specs:
        raise DomainError("invalid_graph", "Work graph must contain unique nonempty keys", 422)
    visiting, done = set(), set()

    def visit(key: str) -> None:
        if key in visiting:
            raise DomainError("dependency_cycle", "Work dependencies contain a cycle", 422)
        if key in done:
            return
        if key not in by_key:
            raise DomainError("missing_dependency", f"Missing dependency: {key}", 422)
        visiting.add(key)
        spec = by_key[key]
        if spec.role not in set(ROLES.values()):
            raise DomainError("invalid_role", f"Unknown role: {spec.role}", 422)
        for parent in spec.dependencies:
            visit(parent)
        visiting.remove(key)
        done.add(key)

    for key in by_key:
        visit(key)


def descendants(items: list[dict], roots: set[str]) -> set[str]:
    affected = set(roots)
    while True:
        added = {item["id"] for item in items if set(item.get("dependencies", [])) & affected} - affected
        if not added:
            return affected
        affected |= added


def output_fingerprint(item: dict, artifacts: list[dict]) -> str:
    return canonical_digest({
        "work_item_id": item["id"], "generation": item["generation"],
        "input_fingerprint": item["input_fingerprint"],
        "artifacts": sorted((a["id"], a["digest"]) for a in artifacts),
        "policy_fingerprint": item["policy_fingerprint"],
    })


def paths_conflict(left: list[str], right: list[str]) -> bool:
    for a in left:
        for b in right:
            a, b = a.strip("/"), b.strip("/")
            if not a or not b or a == "." or b == "." or a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                return True
    return False

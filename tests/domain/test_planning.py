import pytest

from agentflow.common import DomainError
from agentflow.domain.planning import WorkSpec, descendants, paths_conflict, select_steps, validate_graph


def test_stage_selection_keeps_only_requested_range():
    assert select_steps({"mode": "from_to", "from_step": "prd", "to_step": "architecture"}) == [
        "prd", "requirements", "architecture"]
    assert select_steps({"mode": "selected", "selected_steps": ["code_review", "prd"]}) == ["prd", "code_review"]


@pytest.mark.parametrize("selection", [
    {"mode": "from_to", "from_step": "delivery", "to_step": "prd"},
    {"mode": "from_to", "from_step": "missing", "to_step": "prd"},
    {"mode": "selected", "selected_steps": ["prd", "prd"]},
    {"mode": "selected", "selected_steps": []}, {"mode": "unknown"},
])
def test_invalid_selection_is_not_silently_expanded(selection):
    with pytest.raises(DomainError):
        select_steps(selection)


def test_graph_validation_and_selective_invalidation():
    validate_graph([WorkSpec("a", "implementation", "development"),
                    WorkSpec("b", "code_review", "review", ("a",))])
    with pytest.raises(DomainError, match="cycle"):
        validate_graph([WorkSpec("a", "implementation", "development", ("b",)),
                        WorkSpec("b", "code_review", "review", ("a",))])
    with pytest.raises(DomainError, match="Missing dependency"):
        validate_graph([WorkSpec("a", "implementation", "development", ("missing",))])
    items = [{"id": "a", "dependencies": []}, {"id": "b", "dependencies": ["a"]},
             {"id": "c", "dependencies": ["b"]}, {"id": "unrelated", "dependencies": []}]
    assert descendants(items, {"a"}) == {"a", "b", "c"}


@pytest.mark.parametrize("left,right,expected", [
    (["src"], ["src/a.py"], True), (["src/a"], ["src/ab"], False),
    (["."], ["tests"], True), ([], ["."], False), (["one"], ["two"], False),
])
def test_write_scope_conflicts(left, right, expected):
    assert paths_conflict(left, right) is expected


from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from agentflow.common import DomainError, canonical_digest
from agentflow.control.recovery import resolve_recovery_model_profile
from agentflow.control.service import WorkflowService
from agentflow.domain.expansion import StageExpander
from agentflow.domain.planning import CODING_STEPS
from agentflow.models.profiles import ModelProfile
from agentflow.settings import Settings
from agentflow.storage import LocalArtifactStore, Store


@pytest_asyncio.fixture
async def store(tmp_path):
    value = Store(tmp_path / "data")
    await value.start()
    yield value
    await value.close()


async def seed(store, step="implementation", role="development", **stage_changes):
    stage = {"key": step, "step": step, "role": role, "run_id": "run", "project_id": "project",
        "dependencies": ["previous"], "generation": 3, "status": "pending", "quality_result": "unknown",
        "fencing_token": 2, "input_fingerprint": "sha256:" + "a" * 64,
        "policy_fingerprint": "sha256:" + "b" * 64, "approval_required": True, "required": True,
        "artifact_ids": [], "attempt_id": None, "write_paths": ["src"] if step in CODING_STEPS else [],
        "payload": {"fixed_policy": "retained"}, **stage_changes}

    def apply(tx):
        tx.put("plan", "plan", {"actual_steps": [step], "work_specs": [
            {"key": step, "step": step, "role": role, "required": True}]})
        tx.put("run", "run", {"plan_id": "plan", "project_id": "project", "execution_state": "running",
            "input_fingerprint": stage["input_fingerprint"], "budget_limit": {"limit_micros": 1234}})
        tx.put("work_item", "previous", {**stage, "key": "previous", "dependencies": [],
            "status": "completed", "quality_result": "passed", "artifact_ids": ["upstream-artifact"]})
        result = tx.put("work_item", "stage", stage)
        tx.put("work_item", "next", {**stage, "key": "next-review", "step": "code_review", "role": "review",
            "dependencies": ["stage"], "write_paths": [], "approval_required": True, "required": True})
        return result

    return await store.command("fixture", "seed", {"step": step}, apply)


def children(coding=True):
    return [{"key": "one", "goal": "First independently testable part", "write_paths": ["src/one.py"] if coding else []},
            {"key": "two", "goal": "Second independently testable part", "write_paths": ["src/two.py"] if coding else []}]


@pytest.mark.parametrize("step,role", [
    ("research", "research"), ("prd", "product"), ("architecture", "architecture_planning"),
    ("implementation", "development"), ("code_review", "review"),
    ("unit_test_plan", "unit_test"), ("integration_test_strategy", "integration_test"),
])
async def test_all_seven_roles_can_expand_without_dropping_quality_stages(store, step, role):
    stage = await seed(store, step, role)
    downstream = await store.read("work_item", "next")
    budget = (await store.read("run", "run"))["budget_limit"]
    proposed = children(step in CODING_STEPS)
    if step == 'code_review':
        proposed = [{**child, 'review_focus': 'current_code'} for child in proposed]
    result = await StageExpander(store).expand("run", "stage", proposed, "expand", stage["revision"])
    assert result["stage"]["kind"] == "aggregation"
    assert result["stage"]["dependencies"] == result["work_item_ids"]
    assert result["stage"]["approval_required"] and result["stage"]["required"]
    assert result["stage"]["role"] == role and result["stage"]["write_paths"] == []
    assert len(result["items"]) == 2
    for child in result["items"]:
        for field in ("step", "role", "required", "approval_required", "policy_fingerprint", "generation", "input_fingerprint"):
            assert child[field] == stage[field]
        assert child["dependencies"] == ["previous"] and child["parent_stage_id"] == "stage"
        assert child["status"] == "pending" and child["quality_result"] == "unknown"
        assert child["attempt_id"] is None and child["fencing_token"] == 0
        assert child["payload"]["fixed_policy"] == "retained" and child["payload"]["goal"] == child["goal"]
    assert await store.read("work_item", "next") == downstream
    assert (await store.read("run", "run"))["budget_limit"] == budget


async def test_expansion_is_atomic_concurrent_idempotent_and_cannot_recurse(store):
    await seed(store)
    expander = StageExpander(store)
    results = await asyncio.gather(*[expander.expand("run", "stage", children(), "same", 1) for _ in range(5)])
    assert all(result == results[0] for result in results)
    assert len(await store.list("work_item")) == 5
    with pytest.raises(DomainError, match="different"):
        await expander.expand("run", "stage", [{**child, "goal": "changed"} for child in children()], "same", 1)
    with pytest.raises(DomainError, match="changed"):
        await expander.expand("run", "stage", children(), "new", 1)
    current = await store.read("work_item", "stage")
    with pytest.raises(DomainError, match="once"):
        await expander.expand("run", "stage", children(), "again", current["revision"])
    with pytest.raises(DomainError, match="recursively"):
        await expander.expand("run", results[0]["items"][0]["id"], children(), "recursive", 1)


@pytest.mark.parametrize("field,value", [("role", "review"), ("step", "delivery"), ("required", False),
    ("approval_required", False), ("budget_limit", {}), ("dependencies", [])])
async def test_proposal_cannot_change_role_gate_budget_or_dependencies(store, field, value):
    await seed(store)
    proposed = children()
    proposed[0][field] = value
    with pytest.raises(DomainError, match="only"):
        await StageExpander(store).expand("run", "stage", proposed, "bad", 1)
    assert len(await store.list("work_item")) == 3


@pytest.mark.parametrize("path", ["../escape", "/absolute", ".git/config", "src/.GIT/config", "C:\\escape", "src/*.py", "other/file.py"])
async def test_child_writes_are_safe_and_cannot_expand_parent_scope(store, path):
    await seed(store)
    proposed = children()
    proposed[0]["write_paths"] = [path]
    with pytest.raises(DomainError):
        await StageExpander(store).expand("run", "stage", proposed, "bad", 1)
    assert len(await store.list("work_item")) == 3


async def test_non_coding_role_cannot_gain_source_writes(store):
    await seed(store, "prd", "product")
    with pytest.raises(DomainError, match="Non-coding"):
        await StageExpander(store).expand("run", "stage", children(), "bad", 1)


@pytest.mark.parametrize("kind", ["candidate", "delivery_intent"])
async def test_frozen_candidate_or_delivery_blocks_new_required_work(store, kind):
    await seed(store)
    await store.command("fixture", kind, {}, lambda tx: tx.put(kind, "frozen", {"run_id": "run"}))
    with pytest.raises(DomainError, match="Frozen"):
        await StageExpander(store).expand("run", "stage", children(), "bad", 1)
    assert len(await store.list("work_item")) == 3


async def test_graph_size_duplicates_scope_and_cycles_are_rejected(store):
    await seed(store)
    with pytest.raises(DomainError, match="limit"):
        await StageExpander(store, max_work_items=4).expand("run", "stage", children(), "size", 1)
    with pytest.raises(DomainError, match="unique"):
        await StageExpander(store).expand("run", "stage", [children()[0], children()[0]], "duplicate", 1)

    def cycle(tx):
        value = tx.get("work_item", "previous")
        tx.put("work_item", "previous", {**value, "dependencies": ["stage"]}, value["revision"])
        return {}

    await store.command("fixture", "cycle", {}, cycle)
    with pytest.raises(DomainError, match="cycle"):
        await StageExpander(store).expand("run", "stage", children(), "cycle", 1)
    assert len(await store.list("work_item")) == 3


async def test_claimed_stage_and_missing_plan_scope_cannot_expand(store):
    await seed(store, status="running", attempt_id="attempt")
    with pytest.raises(DomainError, match="pending"):
        await StageExpander(store).expand("run", "stage", children(), "running", 1)

    def alter(tx):
        stage = tx.get("work_item", "stage")
        tx.put("work_item", "stage", {**stage, "status": "pending", "attempt_id": None}, stage["revision"])
        plan = tx.get("plan", "plan")
        tx.put("plan", "plan", {**plan, "work_specs": []}, plan["revision"])
        return {}

    await store.command("fixture", "alter", {}, alter)
    with pytest.raises(DomainError, match="frozen Run plan"):
        await StageExpander(store).expand("run", "stage", children(), "scope", 2)


async def test_run_input_change_between_read_and_transaction_is_rejected(store, monkeypatch):
    await seed(store)
    original = store.command

    async def race(scope, key, payload, handler):
        if scope == "stage.expand":
            def revise(tx):
                run = tx.get("run", "run")
                return tx.put("run", "run", {**run, "input_fingerprint": "sha256:" + "c" * 64}, run["revision"])
            await original("fixture", "revise-input", {}, revise)
        return await original(scope, key, payload, handler)

    monkeypatch.setattr(store, "command", race)
    with pytest.raises(DomainError, match="inputs changed"):
        await StageExpander(store).expand("run", "stage", children(), "stale", 1)
    assert len(await store.list("work_item")) == 3


async def test_a_plan_spec_cannot_expand_beyond_selected_steps(store):
    await seed(store)
    def shrink_selection(tx):
        plan = tx.get("plan", "plan")
        return tx.put("plan", "plan", {**plan, "actual_steps": ["research"]}, plan["revision"])
    await store.command("fixture", "selection", {}, shrink_selection)
    with pytest.raises(DomainError, match="frozen Run plan"):
        await StageExpander(store).expand("run", "stage", children(), "scope", 1)


@pytest.mark.parametrize('field', ['attempt_id', 'fencing_token', 'input_fingerprint'])
async def test_expansion_rejects_output_from_replaced_planning_attempt(store, field):
    await seed(store)
    context = {'work_item_id': 'previous', 'attempt_id': 'current', 'fencing_token': 3, 'input_fingerprint': 'fresh'}
    def prepare(tx):
        row = tx.get('work_item', 'previous')
        return tx.put('work_item', 'previous', {**row, 'status': 'running', **{k: context[k] for k in
            ['attempt_id', 'fencing_token', 'input_fingerprint']}}, row['revision'])
    await store.command('fixture', 'producer', {}, prepare)
    stale = {**context, field: 2 if field == 'fencing_token' else 'old'}
    with pytest.raises(DomainError, match='current planning attempt'):
        await StageExpander(store).expand('run', 'stage', children(), 'stale-proposal', 1, producer=stale)
    assert len(await store.list('work_item')) == 3


async def test_replanning_then_expanding_does_not_copy_parent_model_identity_to_new_children(store, tmp_path):
    await seed(store)
    expander = StageExpander(store)
    original_expansion = await expander.expand('run', 'stage', children(), 'original-expansion', 1)
    parent = original_expansion['stage']
    binding = {'recovery_id': 'owner-model-choice', 'run_id': 'run', 'iteration_id': 'iteration',
        'work_item_id': 'stage', 'generation': parent['generation'],
        'model_profile_id': 'selected-coding', 'profile_revision': 1}
    profile = ModelProfile(model_profile_id='selected-coding', provider='openai_compatible',
        requested_model='fixture-model', accepted_api_model='fixture-model', acceptance_status='accepted',
        base_url='https://fixture.invalid/v1', protocols=['responses'], credential_reference='env:UNUSED_FIXTURE_KEY')

    def saved_choice(tx):
        # A previously authorized model choice for the existing aggregation only.
        tx.put('model_profile', profile.model_profile_id, profile.model_dump(exclude={'revision'}))
        tx.put('run_recovery', 'owner-model-choice', {'run_id': 'run', 'iteration_id': 'iteration',
            'mode': 'retry', 'actor': 'owner', 'affected_work_item_ids': ['stage'],
            'model_profile_bindings': {'stage': {**binding, 'payload_digest': canonical_digest(binding)}}})
        stage = tx.get('work_item', 'stage')
        tx.put('work_item', 'stage', {**stage, 'payload': {**stage['payload'], 'recovery_model_binding': binding}}, stage['revision'])
        previous = tx.get('work_item', 'previous')
        tx.put('work_item', 'previous', {**previous, 'step': 'development_plan', 'role': 'architecture_planning',
            'write_paths': []}, previous['revision'])
        plan = tx.get('plan', 'plan')
        tx.put('plan', 'plan', {**plan, 'actual_steps': ['development_plan', 'implementation', 'code_review']}, plan['revision'])
        run = tx.get('run', 'run')
        return tx.put('run', 'run', {**run, 'iteration_id': 'iteration',
            'runtime_bindings': {'coding_model_profile_id': 'original-coding'}}, run['revision'])

    run = await store.command('fixture', 'saved-model-choice', {}, saved_choice)
    original_receipt = await store.read('run_recovery', 'owner-model-choice')
    settings = Settings(data_dir=tmp_path / 'data')
    workflow = WorkflowService(store, LocalArtifactStore(settings.data_dir / 'artifacts'), settings)
    await workflow.revise('run', {'expected_revision': run['revision'], 'work_item_ids': ['previous'],
        'reason': 'Plan a new module split'}, 'replan-modules')
    stage = await store.read('work_item', 'stage')
    current_run = await store.read('run', 'run')
    assert stage['kind'] == 'stage' and stage['generation'] == parent['generation'] + 1
    assert await resolve_recovery_model_profile(store, current_run, stage) == 'selected-coding'
    inherited_receipts = await store.list('run_recovery')
    result = await expander.expand('run', 'stage', children(), 'new-planned-expansion', stage['revision'])
    assert set(result['work_item_ids']).isdisjoint(original_expansion['work_item_ids'])
    for child in result['items']:
        assert child['parent_stage_id'] == 'stage'
        assert child['payload']['fixed_policy'] == 'retained'
        assert child['payload']['goal'] == child['goal']
        assert 'recovery_model_binding' not in child['payload']
        override = await resolve_recovery_model_profile(store, current_run, child)
        assert (override or current_run['runtime_bindings']['coding_model_profile_id']) == 'original-coding'
    assert await resolve_recovery_model_profile(store, current_run, result['stage']) == 'selected-coding'
    assert await store.list('run_recovery') == inherited_receipts
    assert await store.read('run_recovery', 'owner-model-choice') == original_receipt
    for identity in original_expansion['work_item_ids']:
        assert (await store.read('work_item', identity))['archived']


async def seed_review(store, producer_step='implementation'):
    stage = await seed(store, 'code_review', 'review')
    def producer(tx):
        row = tx.get('work_item', 'previous')
        tx.put('work_item', 'previous', {**row, 'step': producer_step,
            'role': 'development' if producer_step == 'implementation' else 'unit_test'}, row['revision'])
        return {}
    await store.command('fixture', 'review-producer', {}, producer)
    return stage


async def test_review_expansion_rejects_a_future_test_coverage_facet_atomically(store):
    stage = await seed_review(store)
    proposed = [{**child, 'review_focus': focus} for child, focus in zip(children(False),
        ['current_code', 'unit_test_coverage'], strict=True)]
    with pytest.raises(DomainError) as error:
        await StageExpander(store).expand('run', 'stage', proposed, 'future-tests', stage['revision'])
    assert error.value.code == 'review_phase_mismatch'
    assert len(await store.list('work_item')) == 3
    assert await store.read('work_item', 'stage') == stage


async def test_review_expansion_requires_a_structured_focus_instead_of_guessing_from_goal(store):
    stage = await seed_review(store)
    with pytest.raises(DomainError) as error:
        await StageExpander(store).expand('run', 'stage', children(False), 'ambiguous-review', stage['revision'])
    assert error.value.code == 'review_focus_required'
    assert len(await store.list('work_item')) == 3


@pytest.mark.parametrize('producer_step,focus,required', [
    ('implementation', 'existing_test_regressions', []),
    ('unit_test_implementation', 'unit_test_coverage', ['unit']),
])
async def test_review_children_inherit_controller_phase_and_validated_focus(store, producer_step, focus, required):
    stage = await seed_review(store, producer_step)
    proposed = [{**child, 'review_focus': focus} for child in children(False)]
    result = await StageExpander(store).expand('run', 'stage', proposed, 'valid-review', stage['revision'])
    for child in result['items']:
        assert child['payload']['review_focus'] == focus
        assert child['payload']['review_phase_contract']['required_test_phases'] == required
        assert child['payload']['review_phase_contract']['existing_test_regressions'] == 'required'
    assert result['stage']['payload']['review_phase_contract']['required_test_phases'] == required

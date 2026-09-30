import asyncio
from uuid import uuid4

import pytest
import pytest_asyncio

from agentflow.common import DomainError
from agentflow.control.service import WorkflowService
from agentflow.domain.planning import WorkSpec
from agentflow.settings import Settings
from agentflow.storage import LocalArtifactStore, Store


@pytest_asyncio.fixture
async def flow(tmp_path):
    settings = Settings(data_dir=tmp_path / "control", agent_concurrency=3)
    store = Store(settings.data_dir)
    await store.start()
    artifacts = LocalArtifactStore(settings.data_dir / "artifacts")
    profiles = [{"model_profile_id": "model", "revision": 1, "acceptance_status": "accepted", "credential_status": "configured"}]
    async def reader():
        return profiles
    service = WorkflowService(store, artifacts, settings, reader)
    project = await service.create_project({"name": "fixture", "local_path": str(tmp_path / "project"),
        "import_mode": "initialize_managed", "dirty_worktree_policy": "require_clean"}, "create-project")
    yield service, store, artifacts, project, profiles
    await store.close()


def plan_payload(project, approvals=None):
    return {"project_id": project["id"], "goal": "A versioned product brief", "purpose": "artifact_only",
        "selection": {"mode": "selected", "selected_steps": ["goal"]}, "input_versions": [],
        "approval_steps": approvals or [], "authorized_rework_steps": ["goal"],
        "runtime_bindings": {"role_model_profile_id": "model", "coding_backend_id": None, "coding_model_profile_id": None},
        "budget_limit": {"currency": "USD", "limit_micros": 1000, "max_model_requests": 10,
                         "max_tool_calls": 10, "max_active_seconds": 30}, "app_targets": []}


async def start(flow, approvals=None):
    service, _, _, project, _ = flow
    plan = await service.create_plan(plan_payload(project, approvals), str(uuid4()))
    run = await service.start_run({"plan_id": plan["id"], "expected_revision": plan["revision"]}, str(uuid4()))
    return run


async def finish(flow, claim, quality="passed"):
    service, _, artifacts, _, _ = flow
    saved = await artifacts.put_bytes(b'{"goal":"testable outcome"}')
    digest = saved["id"]
    attempt = claim["attempt"]
    return await service.finish_attempt(attempt["id"], {"fencing_token": attempt["fencing_token"],
        "input_fingerprint": attempt["input_fingerprint"], "execution_status": "completed", "quality_result": quality},
        str(uuid4()), verified_artifacts=[{"digest": digest, "name": "brief.json", "media_type": "application/json"}])


async def test_run_start_idempotency_and_late_profile_recheck(flow):
    service, _, _, project, profiles = flow
    plan = await service.create_plan(plan_payload(project), "plan")
    profiles[0]["acceptance_status"] = "rejected"
    with pytest.raises(DomainError, match="configuration changed"):
        await service.start_run({"plan_id": plan["id"], "expected_revision": plan["revision"]}, "start")
    profiles[0]["acceptance_status"] = "accepted"
    request = {"plan_id": plan["id"], "expected_revision": plan["revision"]}
    first = await service.start_run(request, "start")
    second = await service.start_run(request, "start")
    assert first == second


async def test_approval_binds_output_and_rejection_creates_new_generation(flow):
    service, store, _, _, _ = flow
    run = await start(flow, ["goal"])
    claim = await service.claim_next(run["id"], "worker", "claim")
    item = await finish(flow, claim)
    assert item["status"] == "waiting_approval"
    approval = (await store.list("approval"))[0]
    with pytest.raises(DomainError, match="no longer matches"):
        await service.decide(approval["id"], {"decision": "approve", "expected_revision": 1,
            "expected_fingerprint": "wrong"}, "wrong")
    await service.decide(approval["id"], {"decision": "reject", "expected_revision": 1,
        "expected_fingerprint": approval["fingerprint"], "reason": "Missing scope", "change_expectation": "Add scope"}, "reject")
    current = await store.read("work_item", item["id"])
    assert current["generation"] == 2 and current["status"] == "pending"
    assert len(await store.list("work_revision")) == 1
    with pytest.raises(DomainError, match="current work version"):
        await finish(flow, claim)
    new_claim = await service.claim_next(run["id"], "worker-2", "claim-2")
    await finish(flow, new_claim)
    new_approval = [a for a in await store.list("approval") if not a["stale"]][0]
    await service.decide(new_approval["id"], {"decision": "approve", "expected_revision": 1,
        "expected_fingerprint": new_approval["fingerprint"]}, "approve")
    assert (await service.run_detail(run["id"]))["execution_state"] == "completed"


async def test_accepting_failed_report_does_not_make_quality_pass(flow):
    service, store, _, _, _ = flow
    run = await start(flow, ["goal"])
    claim = await service.claim_next(run["id"], "worker", "claim")
    await finish(flow, claim, "failed")
    approval = (await store.list("approval"))[0]
    await service.decide(approval["id"], {"decision": "approve", "expected_revision": 1,
        "expected_fingerprint": approval["fingerprint"]}, "approve")
    report = await service.run_detail(run["id"])
    assert report["execution_state"] == "completed" and report["quality_result"] == "failed"
    assert report["delivery_ids"] == []


async def test_all_seven_roles_allow_parallel_instances_with_exclusive_writes(flow):
    service, store, _, _, _ = flow
    run = await start(flow)
    await finish(flow, await service.claim_next(run["id"], "worker", "initial"))
    # Reopen through a deliberate revision before extending the graph.
    detail = await service.run_detail(run["id"])
    item = detail["work_items"][0]
    await service.revise(run["id"], {"expected_revision": detail["revision"], "work_item_ids": [item["id"]], "reason": "Expand modules"}, "reopen")
    roles = ["research", "product", "architecture_planning", "development", "review", "unit_test", "integration_test"]
    for index, role in enumerate(roles):
        current = await store.read("run", run["id"])
        specs = [WorkSpec(f"{role}-{i}", "implementation" if role == "development" else "research", role,
                          write_paths=(f"modules/{role}/{i}",)) for i in range(2)]
        await service.add_work_items(run["id"], specs, current["revision"], f"expand-{index}")
    claims = await asyncio.gather(*(service.claim_next(run["id"], f"worker-{i}", f"claim-{i}") for i in range(10)))
    active = [c for c in claims if c["attempt"]]
    assert len(active) == 3
    assert len({c["attempt"]["id"] for c in active}) == 3
    assert len(await store.list("work_item")) == 15


async def test_cancel_requires_active_attempt_to_acknowledge(flow):
    service, _, _, _, _ = flow
    run = await start(flow)
    claim = await service.claim_next(run["id"], "worker", "claim")
    await service.control_run(run["id"], {"action": "cancel", "expected_revision": run["revision"], "reason": "Stop"}, "cancel")
    assert (await service.run_detail(run["id"]))["execution_state"] == "cancelling"
    await finish(flow, claim)
    assert (await service.run_detail(run["id"]))["execution_state"] == "cancelled"


async def test_missing_native_target_config_stays_a_plan_gap(flow):
    service, _, _, project, _ = flow
    payload = plan_payload(project)
    payload["selection"] = {"mode": "selected", "selected_steps": ["integration_test_execution"]}
    payload["app_targets"] = ["ios_native"]
    plan = await service.create_plan(payload, "native-gap")
    assert plan["state"] == "missing_inputs"
    assert any(g["code"] == "missing_target_config" for g in plan["missing_inputs"])
    with pytest.raises(DomainError, match="missing inputs"):
        await service.start_run({"plan_id": plan["id"], "expected_revision": 1}, "start-gap")



async def test_parent_continuation_cannot_reset_iteration_budget(flow):
    service, store, _, project, _ = flow
    parent = await start(flow)
    original = await store.read('iteration', parent['iteration_id'])
    payload = {**plan_payload(project), 'parent_run_id': parent['id']}
    payload['budget_limit'] = {**payload['budget_limit'], 'limit_micros': 100000}
    plan = await service.create_plan(payload, 'continuation')
    assert plan['iteration_id'] == parent['iteration_id']
    child = await service.start_run({'plan_id': plan['id'], 'expected_revision': plan['revision']}, 'child')
    assert child['iteration_id'] == parent['iteration_id']
    assert await store.read('iteration', parent['iteration_id']) == original
    with pytest.raises(DomainError, match='retain its parent'):
        await service.create_plan({**payload, 'iteration_id': str(uuid4())}, 'different-iteration')


async def test_replanning_archives_prior_expansion_and_restores_stage_dependencies(flow):
    from agentflow.domain.expansion import StageExpander
    service, store, _, _, _ = flow
    run = await start(flow)
    specs = [WorkSpec('dev-plan', 'development_plan', 'architecture_planning'),
             WorkSpec('code', 'implementation', 'development', dependencies=('dev-plan',), write_paths=('src',)),
             WorkSpec('review', 'code_review', 'review', dependencies=('code',))]
    await service.add_work_items(run['id'], specs, run['revision'], 'graph')
    def authorize(tx):
        plan = tx.get('plan', run['plan_id'])
        return tx.put('plan', plan['id'], {**plan, 'actual_steps': ['goal', 'development_plan', 'implementation', 'code_review'],
            'work_specs': plan['work_specs'] + [s.__dict__ for s in specs]}, plan['revision'])
    await store.command('fixture', 'authorize-expansion', {}, authorize)
    items = {i['key']: i for i in await store.list('work_item')}
    await StageExpander(store).expand(run['id'], items['code']['id'], [
        {'key': 'one', 'goal': 'First module', 'write_paths': ['src/one.py']},
        {'key': 'two', 'goal': 'Second module', 'write_paths': ['src/two.py']}], 'split', items['code']['revision'])
    expanded = await store.read('work_item', items['code']['id'])
    current = await store.read('run', run['id'])
    await service.revise(run['id'], {'expected_revision': current['revision'],
        'work_item_ids': [items['dev-plan']['id']], 'reason': 'Plan new module boundaries'}, 'replan')
    restored = await store.read('work_item', items['code']['id'])
    assert restored['kind'] == 'stage' and restored['dependencies'] == [items['dev-plan']['id']]
    assert restored['write_paths'] == ['src'] and 'expanded_child_ids' not in restored
    for identity in expanded['expanded_child_ids']:
        child = await store.read('work_item', identity)
        assert child['status'] == 'superseded' and child['archived'] and not child['required']
    assert len((await service.run_detail(run['id']))['work_items']) == 4

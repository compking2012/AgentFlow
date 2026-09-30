"""Publication integration tests: real Git/files/SQLite, seeded quality-service facts."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agentflow.common import DomainError
from agentflow.control.delivery import DeliveryCoordinator
from agentflow.control.service import WorkflowService
from agentflow.execution.manifests import execution_key
from agentflow.execution.service import NodeService
from agentflow.repository import RepositoryAdapter
from agentflow.settings import Settings
from agentflow.storage import LocalArtifactStore, Store


@asynccontextmanager
async def fixture(tmp: Path, approval_required=False):
    settings = Settings(data_dir=tmp / "data")
    store = Store(settings.data_dir)
    await store.start()
    artifacts = LocalArtifactStore(settings.data_dir / "artifacts")
    workflow = WorkflowService(store, artifacts, settings)
    project = await workflow.create_project({"name": "Delivery fixture", "local_path": str(tmp / "repo"),
        "import_mode": "initialize_managed", "dirty_worktree_policy": "require_clean"}, "project")
    repository = RepositoryAdapter()
    clone = tmp / "clone"
    await repository.clone_snapshot(Path(project["local_path"]), clone, project["base_commit"])
    (clone / "feature.txt").write_text("implemented feature\n")
    snapshot = await repository.freeze_workspace(clone, project["base_commit"], "Fixture feature")
    nodes = NodeService(store, settings.data_dir, "https://127.0.0.1:9443")
    report = await nodes.import_input(b'<testsuite tests="1"><testcase classname="Feature" name="persists"/></testsuite>',
                                      "junit.xml", "run")
    fingerprint = "sha256:" + "a" * 64
    candidate_fingerprint = "sha256:" + "b" * 64
    mapping = {stage: {"test_case_id": stage, "target_config_id": "target", "phase": stage}
               for stage in ("unit", "integration")}
    def seed(tx):
        run = tx.put("run", "run", {"run_id": "run", "project_id": project["id"], "iteration_id": "iteration",
            "execution_state": "running", "quality_result": "unknown", "input_fingerprint": fingerprint,
            "delivery_ids": [], "purpose": "code_delivery", "blocking_reasons": []})
        for identity, step in [("review-work", "code_review"), ("unit-work", "unit_test_execution"),
                               ("integration-work", "integration_test_execution"), ("delivery-work", "delivery")]:
            row = {"run_id": "run", "project_id": project["id"], "step": step, "role": "system" if step == "delivery" else "review",
                "status": "running" if step == "delivery" else "completed", "generation": 1,
                "quality_result": "unknown" if step == "delivery" else "passed", "required": True,
                "input_fingerprint": fingerprint, "policy_fingerprint": fingerprint, "artifact_ids": [],
                "fencing_token": 1, "dependencies": [], "write_paths": [],
                "approval_required": approval_required if step == "delivery" else False,
                "attempt_id": "delivery-attempt" if step == "delivery" else None}
            tx.put("work_item", identity, row)
        tx.put("candidate", "candidate", {"run_id": "run", "run_input_fingerprint": fingerprint,
            "source_commit": snapshot["commit_oid"], "tree_oid": snapshot["tree_oid"], "source_repository": str(clone),
            "fingerprint": candidate_fingerprint, "state": "testing", "matrix_mappings": mapping})
        tx.put("review", "review", {"run_id": "run", "work_item_id": "review-work", "generation": 1,
            "reviewed_commit": snapshot["commit_oid"], "reviewer_id": "reviewer", "author_id": "author",
            "quality_result": "passed", "blocking_findings": []})
        for stage in mapping:
            tx.put("check", stage, {"run_id": "run", "candidate_fingerprint": candidate_fingerprint,
                "execution_key": execution_key(stage, "target", candidate_fingerprint), "evidence_verified": True,
                "execution_status": "completed", "quality_result": "passed", "executed_case_count": 1,
                "assertion_count": None, "framework_case_evidence": True, "raw_report_artifact_id": report["id"]})
        attempt = tx.put("attempt", "delivery-attempt", {"run_id": "run", "work_item_id": "delivery-work",
            "generation": 1, "fencing_token": 1, "input_fingerprint": fingerprint, "status": "running"})
        return {"run": run, "work_item": tx.get("work_item", "delivery-work"), "attempt": attempt}
    claim = await store.command("fixture", "seed", {}, seed)
    try:
        yield DeliveryCoordinator(store, workflow, nodes), claim, workflow, store, repository, project, snapshot
    finally:
        await store.close()


async def assert_delivered(store, repository, project, snapshot):
    run = await store.read("run", "run")
    assert run["quality_result"] == "passed" and run["execution_state"] == "completed"
    assert len(run["delivery_ids"]) == 1
    target = Path(project["local_path"])
    actual = await asyncio.to_thread(repository._run, target, ["rev-parse", "refs/heads/codex/agentflow/run"])
    assert actual.decode().strip() == snapshot["commit_oid"]
    head = await asyncio.to_thread(repository._run, target, ["rev-parse", "HEAD"])
    assert head.decode().strip() == project["base_commit"]
    assert not (target / "feature.txt").exists()


async def test_real_git_delivery_preserves_user_checkout(tmp_path):
    async with fixture(tmp_path) as (delivery, claim, _workflow, store, repository, project, snapshot):
        await delivery.execute(claim)
        await assert_delivered(store, repository, project, snapshot)


async def test_delivery_human_approval_is_before_git_effect(tmp_path):
    async with fixture(tmp_path, approval_required=True) as (delivery, claim, workflow, store, repository, project, snapshot):
        await delivery.execute(claim)
        assert await store.list("delivery") == []
        approval = (await store.list("approval"))[0]
        assert (await store.read("work_item", "delivery-work"))["status"] == "waiting_approval"
        await workflow.decide(approval["id"], {"decision": "approve", "expected_revision": approval["revision"],
            "expected_fingerprint": approval["fingerprint"]}, "approve")
        assert (await store.read("work_item", "delivery-work"))["status"] == "pending_delivery"
        new_claim = await workflow.claim_next("run", "controller", "delivery-after-approval")
        await delivery.execute(new_claim)
        await assert_delivered(store, repository, project, snapshot)
        assert len(await store.list("approval")) == 1


@pytest.mark.parametrize("after_commit", [False, True])
async def test_git_success_database_response_loss_reconciles_once(tmp_path, monkeypatch, after_commit):
    async with fixture(tmp_path) as (delivery, claim, _workflow, store, repository, project, snapshot):
        original = store.command
        failed = False
        async def fault(scope, key, payload, handler):
            nonlocal failed
            if scope == "delivery.confirm" and not failed:
                failed = True
                if after_commit:
                    await original(scope, key, payload, handler)
                raise OSError("simulated controller crash boundary")
            return await original(scope, key, payload, handler)
        monkeypatch.setattr(store, "command", fault)
        with pytest.raises(OSError):
            await delivery.execute(claim)
        await delivery.reconcile()
        await delivery.reconcile()
        await assert_delivered(store, repository, project, snapshot)
        assert len(await store.list("delivery_intent")) == 1


async def test_missing_native_check_or_corrupt_report_never_publishes(tmp_path):
    async with fixture(tmp_path) as (delivery, claim, _workflow, store, _repository, _project, _snapshot):
        def invalidate(tx):
            check = tx.get("check", "unit")
            return tx.put("check", check["id"], {**check, "quality_result": "unknown"}, check["revision"])
        await store.command("fixture", "invalidate", {}, invalidate)
        with pytest.raises(DomainError, match="not deliverable"):
            await delivery.execute(claim)
        assert await store.list("delivery") == []
        assert await store.list("delivery_intent") == []


async def historical_repair(workflow, store):
    blob = await workflow.artifacts.put_bytes(b'old repair result retained for audit')
    def seed(tx):
        artifact = tx.put('artifact', 'archived-artifact', {'run_id': 'run', 'work_item_id': 'archived-work',
                          'digest': blob['id'], 'stale': True})
        live = tx.get('work_item', 'review-work')
        fields = {key: value for key, value in live.items() if key not in {'id', 'revision'}}
        work = tx.put('work_item', 'archived-work', {**fields, 'step': 'implementation', 'required': False,
                      'archived': True, 'status': 'superseded', 'artifact_ids': [artifact['id']]})
        return {'artifact': artifact, 'work': work}
    result = await store.command('fixture', 'historical-repair', {}, seed)
    return result['artifact'], result['work']


async def test_archived_repair_artifacts_do_not_block_delivery_or_get_reactivated(tmp_path):
    async with fixture(tmp_path) as (delivery, claim, workflow, store, repository, project, snapshot):
        artifact, work = await historical_repair(workflow, store)
        await delivery.execute(claim)
        await assert_delivered(store, repository, project, snapshot)
        assert await store.read('artifact', artifact['id']) == artifact
        assert await store.read('work_item', work['id']) == work


async def test_current_stale_artifact_still_prevents_delivery(tmp_path):
    async with fixture(tmp_path) as (delivery, claim, workflow, store, _repository, _project, _snapshot):
        artifact, _ = await historical_repair(workflow, store)
        def attach(tx):
            work = tx.get('work_item', 'review-work')
            return tx.put('work_item', work['id'], {**work, 'artifact_ids': [artifact['id']]}, work['revision'])
        await store.command('fixture', 'live-stale-artifact', {}, attach)
        with pytest.raises(DomainError) as error:
            await delivery.execute(claim)
        assert error.value.code == 'stale_artifact'
        assert not await store.list('delivery_intent')


async def test_archived_work_versions_remain_fenced_during_publication(tmp_path, monkeypatch):
    async with fixture(tmp_path) as (delivery, claim, workflow, store, _repository, _project, _snapshot):
        await historical_repair(workflow, store)
        original = store.command
        async def reactivate(scope, key, payload, handler):
            if scope == 'delivery.prepare':
                def change(tx):
                    old = tx.get('work_item', 'archived-work')
                    return tx.put('work_item', old['id'], {**old, 'archived': False, 'required': True,
                                  'status': 'pending'}, old['revision'])
                await original('fixture', 'concurrent-reactivation', {}, change)
            return await original(scope, key, payload, handler)
        monkeypatch.setattr(store, 'command', reactivate)
        with pytest.raises(DomainError) as error:
            await delivery.execute(claim)
        assert error.value.code == 'quality_changed'
        assert not await store.list('delivery_intent')

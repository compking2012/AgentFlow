"""Reconciled local Git publication after deterministic quality and human gates."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from agentflow.common import DomainError, utc_now
from agentflow.domain.gates import evaluate_gate
from agentflow.execution.manifests import execution_key, file_digest
from agentflow.repository import RepositoryAdapter


class DeliveryCoordinator:
    def __init__(self, store, workflow, nodes):
        self.store, self.workflow, self.nodes = store, workflow, nodes
        self.repository = RepositoryAdapter()

    async def _assess(self, run, work):
        candidates = [c for c in await self.store.list("candidate") if c["run_id"] == run["id"]
                      and c["run_input_fingerprint"] == run["input_fingerprint"]]
        if len(candidates) != 1:
            raise DomainError("candidate_missing", "One current frozen candidate is required")
        candidate = candidates[0]
        items = {i["id"]: i for i in await self.store.list("work_item") if i["run_id"] == run["id"]}
        for item in items.values():
            if (item.get('archived') or item["id"] == work["id"] or item["step"] == "retrospective"
                    or not item.get("required", True)):
                continue
            if item["status"] != "completed" or item["quality_result"] in {"failed", "inconclusive"}:
                raise DomainError("unfinished_work", "Required development, review, testing or approval work is incomplete")
        reviews = [r for r in await self.store.list("review") if r["run_id"] == run["id"]
                   and r["reviewed_commit"] == candidate["source_commit"]
                   and not items[r["work_item_id"]].get('archived')
                   and items[r["work_item_id"]]["generation"] == r["generation"]]
        checks = [c for c in await self.store.list("check") if c["run_id"] == run["id"]]
        approvals = [a for a in await self.store.list("approval") if a["run_id"] == run["id"]]
        matrix = [{"execution_key": execution_key(m["test_case_id"], m["target_config_id"], candidate["fingerprint"]),
                   "required": True} for m in candidate["matrix_mappings"].values()]
        assessment = evaluate_gate({**candidate, "required_review_ids": [r["id"] for r in reviews],
            "required_approval_ids": []}, matrix, checks, [],
            [{**r, "candidate_fingerprint": candidate["fingerprint"]} for r in reviews])
        if not assessment["passed"]:
            raise DomainError("quality_gate_failed", "Candidate is not deliverable", details=assessment)
        if not self.nodes:
            raise DomainError("evidence_unavailable", "Node evidence store is required")
        for check in checks:
            if check["candidate_fingerprint"] != candidate["fingerprint"]:
                continue
            artifact = await self.store.read("node_artifact", check["raw_report_artifact_id"])
            if not artifact or file_digest(self.nodes.artifacts.object_path(artifact["digest"])) != artifact["digest"]:
                raise DomainError("corrupt_evidence", "A required original test report is missing or corrupt")
        for item in items.values():
            # Superseded repairs retain deliberately stale artifacts for audit.
            # Keep their record versions in publication's CAS evidence below,
            # but they are not inputs to the current delivery candidate.
            if item.get('archived'):
                continue
            for identity in item.get("artifact_ids", []):
                artifact = await self.store.read("artifact", identity)
                if not artifact or artifact.get("stale"):
                    raise DomainError("stale_artifact", "A source artifact or approval input is stale")
                await self.workflow.artifacts.verify(artifact["digest"])
        return candidate, assessment, items, reviews, checks, approvals

    async def execute(self, claim):
        run, work, attempt = claim["run"], claim["work_item"], claim["attempt"]
        candidate, gate, items, reviews, checks, approvals = await self._assess(run, work)
        approved = next((a for a in approvals if a.get("kind") == "delivery" and not a.get("stale")
                         and a["work_item_id"] == work["id"] and a["decision"] == "approve"
                         and a["fingerprint"] == candidate["fingerprint"]), None)
        if work["approval_required"] and not approved:
            approval_id = str(uuid5(NAMESPACE_URL, f"delivery:{work['id']}:{work['generation']}:{candidate['fingerprint']}"))
            def wait(tx):
                current = tx.get("work_item", work["id"])
                if current["attempt_id"] != attempt["id"]:
                    raise DomainError("stale_delivery", "Delivery work changed")
                tx.put("approval", approval_id, {"run_id": run["id"], "work_item_id": work["id"],
                    "fingerprint": candidate["fingerprint"], "generation": work["generation"],
                    "decision": None, "stale": False, "kind": "delivery", "candidate_id": candidate["id"]})
                tx.put("work_item", work["id"], {**current, "status": "waiting_approval",
                    "output_fingerprint": candidate["fingerprint"], "quality_result": "passed"}, current["revision"])
                old = tx.get("attempt", attempt["id"])
                tx.put("attempt", old["id"], {**old, "status": "waiting_approval"}, old["revision"])
                tx.event("delivery.awaiting_approval", {"candidate_id": candidate["id"], "approval_id": approval_id}, run_id=run["id"])
                return {"approval_id": approval_id}
            await self.store.command("delivery.approval", approval_id, {"candidate_id": candidate["id"]}, wait)
            return
        project = await self.store.read("project", run["project_id"])
        intent_id = str(uuid5(NAMESPACE_URL, f"publish:{run['id']}:{candidate['id']}:{candidate['fingerprint']}"))
        evidence = [("candidate", candidate), ("run", run)]
        evidence += [("work_item", value) for value in items.values()]
        evidence += [("review", r) for r in reviews] + [("check", c) for c in checks] + [("approval", a) for a in approvals]
        payload = {"run_id": run["id"], "work_item_id": work["id"], "attempt_id": attempt["id"],
            "candidate_id": candidate["id"], "candidate_fingerprint": candidate["fingerprint"],
            "source_repository": candidate["source_repository"], "target_repository": project["local_path"],
            "candidate_commit": candidate["source_commit"], "tree_oid": candidate["tree_oid"],
            "base_ref": run.get("base_ref", project["base_ref"]), "base_commit": run.get("base_commit", project["base_commit"]),
            "delivery_ref": f"refs/heads/codex/agentflow/{run['id']}", "gate": gate,
            "status": "prepared", "created_at": utc_now()}
        def prepare(tx):
            for kind, observed in evidence:
                current = tx.get(kind, observed["id"])
                if current is None or current["revision"] != observed["revision"]:
                    raise DomainError("quality_changed", "Recheck evidence after concurrent changes")
            current = tx.get("run", run["id"])
            if current.get("delivery_intent_id"):
                raise DomainError("delivery_in_progress", "Another publication is being reconciled")
            tx.put("run", run["id"], {**current, "execution_state": "publishing", "delivery_intent_id": intent_id}, current["revision"])
            return tx.put("delivery_intent", intent_id, payload)
        intent = await self.store.command("delivery.prepare", intent_id, payload, prepare)
        await self._publish(intent)

    async def _publish(self, intent):
        directory = self.workflow.settings.data_dir / "deliveries" / intent["id"]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        bundle = directory / "candidate.bundle"
        try:
            existing = await asyncio.to_thread(self.repository._run, Path(intent["target_repository"]),
                ["show-ref", "--verify", "--hash", intent["delivery_ref"]], check=False)
            if existing.decode().strip() != intent["candidate_commit"]:
                if not bundle.exists():
                    await self.repository.prepare_bundle(Path(intent["source_repository"]), intent["candidate_commit"], bundle)
                await self.repository.import_bundle(Path(intent["target_repository"]), bundle, intent["candidate_commit"])
                await self._assess(await self.store.read("run", intent["run_id"]),
                                   await self.store.read("work_item", intent["work_item_id"]))
            published = await self.repository.publish(Path(intent["target_repository"]), intent["base_ref"],
                intent["base_commit"], intent["delivery_ref"], intent["candidate_commit"], intent["id"],
                expected_tree_oid=intent["tree_oid"])
            published = {k: v for k, v in published.items() if k != "already_published"}
            def confirm(tx):
                current = tx.get("delivery_intent", intent["id"])
                run = tx.get("run", intent["run_id"])
                delivery = tx.put("delivery", intent["id"], {**published, "run_id": run["id"],
                    "candidate_id": intent["candidate_id"], "candidate_fingerprint": intent["candidate_fingerprint"],
                    "confirmed_at": utc_now()})
                tx.put("delivery_intent", intent["id"], {**current, "status": "confirmed"}, current["revision"])
                tx.put("run", run["id"], {**run, "execution_state": "running", "delivery_intent_id": None,
                    "quality_result": "passed", "delivery_ids": sorted(set(run["delivery_ids"] + [delivery["id"]]))}, run["revision"])
                tx.event("delivery.confirmed", {"delivery_id": delivery["id"], "ref": intent["delivery_ref"]}, run_id=run["id"])
                return delivery
            delivery = await self.store.command("delivery.confirm", intent["id"], {"published": published}, confirm)
        except Exception:
            # If Git took effect but DB confirmation failed, retain the prepared intent for reconciliation.
            reference = await asyncio.to_thread(self.repository._run, Path(intent["target_repository"]),
                ["show-ref", "--verify", "--hash", intent["delivery_ref"]], check=False)
            if reference.decode().strip() == intent["candidate_commit"]:
                raise
            def failed(tx):
                current = tx.get("delivery_intent", intent["id"])
                run = tx.get("run", intent["run_id"])
                tx.put("delivery_intent", intent["id"], {**current, "status": "failed", "error": "publication_failed"}, current["revision"])
                return tx.put("run", run["id"], {**run, "execution_state": "running", "delivery_intent_id": None}, run["revision"])
            await self.store.command("delivery.failed", str(uuid4()), {"intent_id": intent["id"]}, failed)
            raise
        await self._complete_work(intent, delivery)

    async def _complete_work(self, intent, delivery):
        work = await self.store.read("work_item", intent["work_item_id"])
        if work["status"] == "completed" or work["attempt_id"] != intent["attempt_id"]:
            return
        report = await self.workflow.artifacts.put_bytes(json.dumps(delivery, ensure_ascii=False).encode())
        await self.workflow.finish_attempt(intent["attempt_id"], {"fencing_token": work["fencing_token"],
            "input_fingerprint": work["input_fingerprint"], "execution_status": "completed", "quality_result": "passed",
            "satisfied_approval_fingerprint": intent["candidate_fingerprint"]}, f"delivery-finish:{intent['id']}",
            verified_artifacts=[{"digest": report["id"], "name": "delivery.json", "media_type": "application/json"}])

    async def reconcile(self):
        for intent in await self.store.list("delivery_intent"):
            if intent["status"] == "prepared":
                await self._publish(intent)
            elif intent["status"] == "confirmed":
                await self._complete_work(intent, await self.store.read("delivery", intent["id"]))

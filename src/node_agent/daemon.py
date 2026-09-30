"""Single-node pull loop with durable claims and lease watchdogs."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.execution.capabilities import probe_target
from agentflow.execution.manifests import execution_key, file_digest
from agentflow.execution.models import TargetConfig, new_id
from agentflow.execution.pki import sign_receipt
from node_agent.cleanup import cleanup_evidence
from node_agent.client import NodeClient
from node_agent.journal import NodeJournal
from node_agent.maintenance import NodeMaintenance
from node_agent.runner import NodeRunner


class NodeDaemon:
    def __init__(self, directory: Path, targets: list[TargetConfig], *, trusted_project_mode: bool = False,
                 resource_ids: list[str] | None = None):
        self.directory = directory.resolve()
        self.journal = NodeJournal(self.directory / "journal.sqlite")
        try:
            self.client = NodeClient(self.directory)
        except Exception:
            self.journal.close()
            raise
        self.runner = NodeRunner(self.directory / "workspaces", self.journal,
                                 trusted_project_mode=trusted_project_mode)
        self.targets = targets
        self.resource_ids = resource_ids or []
        self.capability_ids: list[str] = []
        self.boot = ""
        self.capability_refresh_seconds = 30 * 60
        self._last_capability_refresh = 0.0

    async def close(self) -> None:
        await self.client.close()
        self.journal.close()

    async def initialize(self) -> list[dict]:
        self._maintain_local_files()
        return await self.refresh_capabilities()

    def _maintain_local_files(self) -> None:
        try:
            NodeMaintenance(self.directory, self.journal).sweep()
        except Exception as error:
            # Maintenance must not invalidate an acknowledged successful job.
            logging.getLogger(__name__).warning('Node transfer-copy cleanup deferred: %s', type(error).__name__)

    async def refresh_capabilities(self) -> list[dict]:
        reports = [await probe_target(target) for target in self.targets]
        augment = getattr(self.runner.executor, "augment_capability_report", None)
        if augment:
            reports = [augment(report) for report in reports]
        if not reports:
            raise DomainError("target_configuration_required", "Node needs explicit owner-approved target configurations", 422)
        self.boot = reports[0].boot_fingerprint
        if any(report.boot_fingerprint != self.boot for report in reports):
            raise DomainError("boot_changed_during_probe", "Static probes did not observe one consistent host boot")
        pending = self.journal.recoverable() + self.journal.undelivered_results()
        await self._heartbeat(sorted({row["job_id"] for row in pending}))
        capability_ids = []
        for report in reports:
            response = await self.client.json_request("POST", f"/executor/v1/nodes/{self.client.config['node_id']}/capabilities",
                payload=report.model_dump(mode="json"), idempotency_key=f"capability:{report.probe_id}")
            capability_ids.append(response.get("capability_id") or response["id"])
        self.capability_ids = capability_ids
        self._last_capability_refresh = time.monotonic()
        return [r.model_dump(mode="json") for r in reports]

    async def _heartbeat(self, jobs: list[str]) -> None:
        payload = {"expected_revision": self.client.config["node_revision"], "heartbeat_sequence": self.journal.next_counter("heartbeat"),
                   "boot_fingerprint": self.boot, "active_job_ids": jobs,
                   "capability_snapshot_ids": self.capability_ids, "local_journal_sequence": self.journal.sequence}
        operation = self.journal.pending_operation("heartbeat", payload)
        response = await self.client.json_request("POST", f"/executor/v1/nodes/{self.client.config['node_id']}/heartbeat",
            payload=operation["payload"], idempotency_key=operation["operation_id"])
        self.client.config["node_revision"] = response["revision"]
        self.journal.finish_operation("heartbeat")

    async def run_once(self) -> dict:
        self._maintain_local_files()
        # Refresh between jobs, so a slow multi-platform probe cannot starve an
        # executing process's lease heartbeat. A long job refreshes before its next claim.
        if time.monotonic() - self._last_capability_refresh >= self.capability_refresh_seconds:
            await self.refresh_capabilities()
        unfinished = self.journal.undelivered_results()
        await self._heartbeat([row["job_id"] for row in self.journal.recoverable() + unfinished])
        if unfinished:
            return await self._execute(unfinished[0]["assignment"], saved_result=unfinished[0]["result"])
        for cleanup in self.journal.pending_cleanups():
            await self._cleanup(cleanup["assignment"], cleanup["result"])
        # Unknown previous executions are observed/quarantined, never replayed as a new process.
        unknown = [row for row in self.journal.recoverable() if row["state"] != "received"]
        if unknown:
            return {"state": "execution_unknown", "job_ids": [r["job_id"] for r in unknown]}
        received = [row for row in self.journal.recoverable() if row["state"] == "received"]
        if received:
            assignment = received[0]["assignment"]
            remote = await self.client.inspect(assignment["job_id"])
            if remote["state"] != "leased" or remote["input_fingerprint"] != assignment["input_fingerprint"]:
                return {"state": "execution_unknown", "job_ids": [assignment["job_id"]]}
        else:
            payload = {"node_revision": self.client.config["node_revision"], "boot_fingerprint": self.boot,
                       "capability_ids": self.capability_ids, "available_resource_ids": self.resource_ids}
            operation = self.journal.pending_operation("claim", payload)
            try:
                response = await self.client.claim({**operation["payload"], "operation_id": operation["operation_id"]})
            except DomainError as error:
                # Replay first: a lost acknowledgement may still return an old
                # assignment. Only an explicit rejection proves no assignment.
                if (error.code != "boot_mismatch" or operation["payload"].get("boot_fingerprint") == self.boot
                        or self.journal.recoverable() or self.journal.undelivered_results()):
                    raise
                self.journal.reject_claim(operation["operation_id"])
                return {"state": "idle"}
            assignment = response.get("assignment")
            if assignment:
                self.journal.record_assignment(assignment)
            self.journal.finish_operation("claim")
            if not assignment:
                return {"state": "idle"}
        if assignment.get("boot_fingerprint") != self.boot:
            return {"state": "execution_unknown", "job_ids": [assignment["job_id"]], "reason": "boot_changed"}
        return await self._execute(assignment)

    async def _execute(self, assignment: dict, saved_result: dict | None = None) -> dict:
        pending_result = self.journal.get_pending_operation(f"result:{assignment['job_id']}")
        if pending_result:
            return await self._submit_result(assignment, pending_result["payload"], saved_result)
        stop, cancel = asyncio.Event(), asyncio.Event()
        mutex = asyncio.Lock()
        process = {"fingerprint": None, "state": "preparing"}
        if saved_result and saved_result.get("process_results"):
            process.update(fingerprint=saved_result["process_results"][-1]["process_fingerprint"], state="process_exited")

        async def renew(state=None, fingerprint=None):
            async with mutex:
                if fingerprint:
                    process.update(fingerprint=fingerprint, state=state)
                payload = {"expected_revision": assignment["lease_revision"], "fencing_token": assignment["fencing_token"],
                           "input_fingerprint": assignment["input_fingerprint"], "boot_fingerprint": self.boot,
                           "process_fingerprint": process["fingerprint"], "observed_state": process["state"],
                           "resource_fences": assignment["resource_leases"], "last_activity_at": utc_now(),
                           "local_journal_sequence": self.journal.sequence}
                response = await self.client.json_request("POST", f"/executor/v1/jobs/{assignment['job_id']}/heartbeat",
                    payload=payload, attempt_token=assignment["attempt_token"], idempotency_key=new_id())
                if response["directive"]["action"] != "continue_observing":
                    cancel.set()
                    raise DomainError("job_stop_requested", response["directive"]["reason"])
                assignment.update(revision=response["job_revision"], lease_revision=response["lease_revision"],
                                  resource_leases=response["resource_leases"], lease_expires_at=response["expires_at"],
                                  attempt_token=response.get("attempt_token", assignment["attempt_token"]))
                self.journal.update_assignment(assignment)

        async def keepalive():
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=10)
                except TimeoutError:
                    try:
                        await self._heartbeat([assignment["job_id"]])
                        await renew()
                    except Exception:
                        # A failed lease renewal immediately prevents further project actions.
                        cancel.set()
                        return

        pulse = asyncio.create_task(keepalive())
        try:
            result = saved_result
            if result is None:
                input_files = {}
                for artifact_id in assignment["download_artifact_version_ids"]:
                    path = self.directory / "inputs" / hashlib.sha256(artifact_id.encode()).hexdigest()
                    input_files[artifact_id] = await self.client.download(assignment, artifact_id, path)
                outcome = await self.runner.run(assignment, input_files, cancel, on_process_event=renew)
                result = outcome.get("result")
                if not result:
                    return {"state": outcome["disposition"], "job_id": assignment["job_id"]}
            uploads = {}
            for item in result["artifact_files"]:
                path = Path(item["path"])
                if path.is_file() and str(path) not in uploads:
                    uploads[str(path)] = await self.client.upload(assignment, path, file_digest(path))
            stop.set()
            await pulse
            current = await self.client.inspect(assignment["job_id"])
            payload = self._result_payload(assignment, current, result, uploads)
            operation = self.journal.pending_operation(f"result:{assignment['job_id']}", payload)
            payload = operation["payload"]
            return await self._submit_result(assignment, payload, result)
        finally:
            stop.set()
            await pulse

    async def _submit_result(self, assignment: dict, payload: dict, result: dict) -> dict:
        receipt = await self.client.json_request("POST", f"/executor/v1/jobs/{assignment['job_id']}/results",
            payload=payload, attempt_token=assignment["attempt_token"], idempotency_key=payload["operation_id"])
        self.journal.record_delivery(assignment["job_id"], receipt)
        self.journal.finish_operation(f"result:{assignment['job_id']}")
        cleaned = await self._cleanup(assignment, result)
        return {"state": result["state"], "job_id": assignment["job_id"], "receipt": receipt,
                "quality_result": result["quality_result"], "cleanup_required": not cleaned}

    async def _cleanup(self, assignment: dict, result: dict) -> bool:
        all_cleaned = True
        for lease in sorted(assignment["resource_leases"], key=lambda value:
                            assignment.get("resource_kinds", {}).get(value["resource_id"]) == "workspace"):
            kind = assignment.get("resource_kinds", {}).get(lease["resource_id"], "unknown")
            if kind == "workspace" and not all_cleaned:
                continue  # Retain frozen application identities until device/session cleanup is proven.
            name = f"cleanup:{assignment['job_id']}:{lease['resource_id']}:{lease['fencing_token']}"
            operation = self.journal.get_pending_operation(name)
            if operation is None:
                proof = await cleanup_evidence(self.directory, assignment, result, kind)
                if proof is None:
                    all_cleaned = False
                    continue
                payload = {**proof, "node_id": self.client.config["node_id"], "job_id": assignment["job_id"],
                           "resource_id": lease["resource_id"], "lease_id": lease["lease_id"],
                           "fencing_token": lease["fencing_token"]}
                operation = self.journal.pending_operation(name, {"payload": payload,
                    "signature": sign_receipt(self.directory / "node.key", payload)})
            await self.client.json_request("POST", f"/executor/v1/nodes/{self.client.config['node_id']}/cleanup",
                                          payload=operation["payload"], idempotency_key=operation["operation_id"])
            # Retain the exact acknowledged operation until all cleanup actions finish, so
            # a crash between resource releases safely replays the original receipt.
        if all_cleaned:
            self.journal.finish_cleanup(assignment["job_id"])
            self._maintain_local_files()
        return all_cleaned

    def _result_payload(self, assignment: dict, current: dict, result: dict, uploads: dict) -> dict:
        source, platform = assignment.get("source_manifest"), assignment.get("platform_artifact_manifest")
        built = []
        for item in result["built_artifacts"]:
            upload = uploads[item["path"]]
            built.append({k: v for k, v in item.items() if k not in {"path", "size"}} | {
                "artifact_version_id": upload["artifact_version_id"], "signing_identity_fingerprint": None})
        checks = []
        for check in result["checks"]:
            report = check["normalized_report"]
            observed_cases = {c["case_id"] for c in report["cases"]}
            for entry in assignment.get("matrix_entries", []):
                key = execution_key(entry["test_case_id"], assignment["target_config"]["target_config_id"], platform["fingerprint"])
                executed = set(entry["framework_case_ids"]).issubset(observed_cases)
                checks.append({"check_id": new_id(), "test_case_id": entry["test_case_id"],
                    "matrix_entry_id": entry["matrix_entry_id"], "app_target": assignment["app_target"],
                    "source_manifest_fingerprint": source["fingerprint"],
                    "platform_artifact_manifest_fingerprint": platform["fingerprint"],
                    "test_package_digest": assignment["test_package_digest"],
                    "matrix_plan_fingerprint": assignment["matrix_plan_fingerprint"],
                    "matrix_binding_fingerprint": assignment["matrix_binding_fingerprint"],
                    "target_config_id": assignment["target_config"]["target_config_id"],
                    "target_config_revision": assignment["target_config"]["revision"],
                    "environment_fingerprint": result.get("observed_environment_fingerprint"),
                    "exit_code": result["process_results"][0]["exit_code"] if result["process_results"] else None,
                    "report_format": check["report_format"], "runner_name": check["report_format"],
                    "expected_execution_keys": [key], "actual_execution_keys": [key] if executed else [],
                    "quality_result": report["quality_result"], "execution_status": result["execution_status"],
                    "executed_case_count": len(report["cases"]),
                    "observed_components": result.get("observed_components", []),
                    "service_observations": result.get("service_observations", []),
                    "raw_report_artifact_version_id": uploads[check["raw_path"]]["artifact_version_id"],
                    "started_at": result["process_results"][0]["started_at"] if result["process_results"] else utc_now(),
                    "finished_at": result["finished_at"]})
        return {"expected_revision": current["revision"], "operation_id": canonical_digest({"job": assignment["job_id"], "result": result})[7:39],
                "job_kind": assignment["kind"], "app_target": assignment["app_target"],
                "fencing_token": assignment["fencing_token"], "input_fingerprint": assignment["input_fingerprint"],
                "execution_status": result["execution_status"], "quality_result": result["quality_result"],
                "observed_source_manifest_fingerprint": result.get("observed_source_manifest_fingerprint"),
                "observed_platform_artifact_manifest_fingerprint": result.get("observed_platform_artifact_manifest_fingerprint"),
                "observed_test_package_digest": result.get("observed_test_package_digest"),
                "built_artifacts": built, "checks": checks,
                "artifact_version_ids": sorted({a["artifact_version_id"] for a in uploads.values()}),
                "cleanup_evidence": [], "summary": result["summary"], "finished_at": result["finished_at"]}

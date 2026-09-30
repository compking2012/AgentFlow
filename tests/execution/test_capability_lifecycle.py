"""Fresh observations may retain genuine proof, never transfer it across environments."""
from datetime import UTC, datetime, timedelta

import pytest
from test_result_validation import prepared_claim

from agentflow.common import DomainError, canonical_digest, utc_now
from agentflow.execution.models import CapabilityReport, JobClaimRequest, new_id
from node_agent import daemon as daemon_module
from node_agent.daemon import NodeDaemon
from node_agent.journal import NodeJournal


async def verified(paired, target):
    service, identity, job, request = await prepared_claim(paired, target)
    result = await service.submit_job_result(identity, job["job_id"], request, job["attempt_token"])
    cap = await service.confirm_functional_capability(job["capability_id"], result["result_id"], "confirm")
    return service, identity, job, cap


def fresh(cap, **changes):
    return CapabilityReport.model_validate({**cap["report"], "probe_id": new_id(), "observed_at": utc_now(), **changes})


async def queue_formal(service, target, job):
    return await service.enqueue_job("run", kind="test", target_config=target,
        source_manifest=job["source_manifest"], platform_manifest=job["platform_artifact_manifest"],
        matrix_entry_ids=job["matrix_entry_ids"], matrix_entries=job["matrix_entries"],
        matrix_plan_fingerprint=job["matrix_plan_fingerprint"], matrix_binding_fingerprint=job["matrix_binding_fingerprint"],
        idempotency_key="formal")


async def test_expired_static_observation_needs_fresh_probe_but_retains_intact_functional_proof(paired, target):
    service, identity, probe_job, cap = await verified(paired, target)
    formal = await queue_formal(service, target, probe_job)
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    await service.store.command("test-expire", "age", {}, lambda tx: tx.put("node_capability", cap["id"],
        {**cap, "observed_at": old}, cap["revision"]))
    request = {"node_revision": 1, "boot_fingerprint": paired[3], "capability_ids": [cap["id"]]}
    assert (await service.claim_job(identity, JobClaimRequest(**request, operation_id="expired")))["assignment"] is None
    refreshed = await service.register_capability(identity.node_id, fresh(cap), "fresh-observation")
    assert refreshed["id"] == cap["id"] and refreshed["verification_state"] == "functional_verified"
    assert refreshed["functional_result_id"] == cap["functional_result_id"]
    assert refreshed["functional_verified_at"] == cap["functional_verified_at"]
    assert refreshed["static_refresh_count"] == 1
    assert (await service.claim_job(identity, JobClaimRequest(**request, operation_id="fresh")))["assignment"]["job_id"] == formal["id"]
    observations = await service.store.list("node_capability_observation")
    assert len(observations) == 2
    assert observations[0]["report"]["probe_id"] != observations[1]["report"]["probe_id"]


async def test_environment_change_supersedes_old_cap_and_returning_environment_does_not_resurrect_it(paired, target):
    service, identity, job, cap = await verified(paired, target)
    await queue_formal(service, target, job)
    changed = await service.register_capability(identity.node_id, fresh(cap, architecture="changed-architecture"), "changed")
    assert changed["id"] != cap["id"] and changed["verification_state"] == "static_verified"
    assert changed["functional_result_id"] is None
    old = await service.store.read("node_capability", cap["id"])
    assert old["verification_state"] == "superseded" and old["functional_result_id"] == cap["functional_result_id"]
    for candidate_id in [cap["id"], changed["id"]]:
        with pytest.raises(DomainError):
            await service.confirm_functional_capability(candidate_id, cap["functional_result_id"], "old-proof:" + candidate_id)
    assert (await service.claim_job(identity, JobClaimRequest(operation_id="both-caps", node_revision=1,
        boot_fingerprint=paired[3], capability_ids=[cap["id"], changed["id"]])))["assignment"] is None
    returned = await service.register_capability(identity.node_id, fresh(cap), "returned")
    assert returned["id"] not in {cap["id"], changed["id"]}
    assert returned["verification_state"] == "static_verified" and returned["functional_result_id"] is None
    assert (await service.store.read("node_result", cap["functional_result_id"]))["assessment_state"] == "validated"


@pytest.mark.parametrize("change", ["boot", "missing_report", "blocked_probe"])
async def test_boot_loss_of_raw_evidence_and_blocked_preflight_never_inherit_functional_pass(paired, target, change):
    service, identity, _, cap = await verified(paired, target)
    alterations = {}
    if change == "boot":
        alterations["boot_fingerprint"] = canonical_digest("new-boot")
    elif change == "blocked_probe":
        alterations.update(state="blocked", blocking_reasons=["permission_unavailable"])
    else:
        result = await service.store.read("node_result", cap["functional_result_id"])
        artifact = await service.store.read("node_artifact", result["verified_checks"][0]["raw_report_artifact_version_id"])
        service.artifacts.object_path(artifact["digest"]).unlink()
    changed = await service.register_capability(identity.node_id, fresh(cap, **alterations), change)
    assert changed["id"] != cap["id"] and changed["functional_result_id"] is None
    assert changed["verification_state"] != "functional_verified"
    assert (await service.store.read("node_capability", cap["id"]))["verification_state"] == "superseded"


async def test_stale_or_future_dated_report_cannot_extend_a_capability(paired, target):
    service, identity, _, cap = await verified(paired, target)
    for delta in [timedelta(hours=-2), timedelta(hours=2)]:
        report = fresh(cap, observed_at=(datetime.now(UTC) + delta).isoformat())
        with pytest.raises(DomainError, match="fresh static probe"):
            await service.register_capability(identity.node_id, report, report.probe_id)
    assert (await service.store.read("node_capability", cap["id"]))["revision"] == cap["revision"]


async def test_daemon_restart_reuses_verified_cap_and_periodically_refreshes_before_new_claim(paired, target, tmp_path, monkeypatch):
    service, identity, _, cap = await verified(paired, target)
    probe_calls = []

    async def actual_probe_contract(_target):
        probe_calls.append(1)
        return fresh(cap)

    class ProtocolClient:
        def __init__(self, _directory):
            self.config = {"node_id": identity.node_id, "node_revision": 1}

        async def json_request(self, method, path, *, payload, idempotency_key):
            assert method == "POST"
            if path.endswith("/capabilities"):
                return await service.submit_capability_report(identity, payload, idempotency_key)
            assert path.endswith("/heartbeat")
            return await service.heartbeat(identity, payload, idempotency_key)

        async def claim(self, payload):
            return await service.claim_job(identity, payload)

        async def close(self):
            pass

    monkeypatch.setattr(daemon_module, "probe_target", actual_probe_contract)
    monkeypatch.setattr(daemon_module, "NodeClient", ProtocolClient)
    directory = tmp_path / "persistent-node"
    journal = NodeJournal(directory / "journal.sqlite")
    journal.next_counter("heartbeat")  # The paired protocol fixture already sent heartbeat one.
    journal.close()
    first = NodeDaemon(directory, [target])
    await first.initialize()
    assert first.capability_ids == [cap["id"]]
    await first.close()
    second = NodeDaemon(directory, [target])
    try:
        await second.initialize()
        assert second.capability_ids == [cap["id"]] and len(probe_calls) == 2
        second._last_capability_refresh -= 3601
        assert (await second.run_once())["state"] == "idle"
        assert len(probe_calls) == 3 and second.capability_ids == [cap["id"]]
        current = await service.store.read("node_capability", cap["id"])
        assert current["verification_state"] == "functional_verified"
        assert current["functional_result_id"] == cap["functional_result_id"]
    finally:
        await second.close()

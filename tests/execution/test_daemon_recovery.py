import json
from pathlib import Path

import pytest

from agentflow.common import DomainError, canonical_digest
from node_agent.daemon import NodeDaemon
from node_agent.journal import NodeJournal


def test_single_supervisor_lock_and_independent_durable_heartbeat(tmp_path):
    path = tmp_path / "node.sqlite"
    journal = NodeJournal(path)
    try:
        assert journal.next_counter("heartbeat") == 1
        assert journal.next_counter("heartbeat") == 2
        assert journal.sequence == 0
        with pytest.raises(DomainError, match="Another node supervisor"):
            NodeJournal(path)
    finally:
        journal.close()
    reopened = NodeJournal(path)
    try:
        assert reopened.next_counter("heartbeat") == 3
    finally:
        reopened.close()


async def test_lost_result_ack_replays_exact_request_without_rerunning_or_uploading(tmp_path):
    journal = NodeJournal(tmp_path / "journal.sqlite")
    assignment = {"job_id": "job", "attempt_id": "attempt", "fencing_token": 1,
                  "input_fingerprint": canonical_digest("inputs"), "attempt_token": "expired-after-first-accept",
                  "resource_leases": []}
    result = {"execution_status": "completed", "quality_result": "passed", "state": "completed"}
    payload = {"operation_id": "stable-result", "expected_revision": 4, "checks": [{"check_id": "same-check"}]}
    journal.record_assignment(assignment)
    journal.record_result("job", result)
    journal.pending_operation("result:job", payload)
    journal.close()
    journal = NodeJournal(tmp_path / "journal.sqlite")

    class LostAckClient:
        config = {"node_id": "node"}

        def __init__(self):
            self.requests = []

        async def json_request(self, method, path, **kwargs):
            assert method == "POST" and path == "/executor/v1/jobs/job/results"
            self.requests.append(json.loads(json.dumps(kwargs["payload"])))
            if len(self.requests) == 1:
                raise ConnectionError("controller accepted result but response was lost")
            return {"result_id": "accepted", "assessment_state": "validated"}

    daemon = object.__new__(NodeDaemon)
    daemon.directory, daemon.journal, daemon.client = Path(tmp_path), journal, LostAckClient()
    try:
        with pytest.raises(ConnectionError):
            await daemon._execute(assignment, saved_result=result)
        assert len(journal.undelivered_results()) == 1
        receipt = await daemon._execute(assignment, saved_result=result)
        assert receipt["receipt"]["result_id"] == "accepted"
        assert daemon.client.requests == [payload, payload]
        assert journal.undelivered_results() == []
        assert journal.pending_cleanups() == []
    finally:
        journal.close()

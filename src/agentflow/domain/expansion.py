"""Bounded same-stage fan-out; proposals cannot change execution policy."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from agentflow.common import DomainError, canonical_digest, utc_now

from .planning import CODING_STEPS, ROLES
from .planning_contract import build_planning_contract, normalize_scope_path, validate_parallel_work
from .review_phase import review_phase_contract, validate_review_focus


def _required(tx, kind: str, identity: str) -> dict:
    value = tx.get(kind, identity)
    if value is None:
        raise DomainError("not_found", f"Unknown {kind}", 404)
    return value


def _path(value: str) -> str:
    return normalize_scope_path(value)


def _within(path: str, scopes: list[str]) -> bool:
    return any(scope == "." or path == scope or path.startswith(scope + "/") for scope in scopes)


def _validate_graph(items: list[dict], maximum_edges: int) -> None:
    by_id = {item["id"]: item for item in items}
    if len(by_id) != len(items) or len({item["key"] for item in items}) != len(items):
        raise DomainError("invalid_graph", "Work items must have unique identities and keys", 422)
    incoming, consumers = {}, {identity: [] for identity in by_id}
    count = 0
    for identity, item in by_id.items():
        dependencies = item.get("dependencies", [])
        if len(dependencies) != len(set(dependencies)) or any(parent not in by_id for parent in dependencies):
            raise DomainError("invalid_graph", "Dependencies must name unique work items in this run", 422)
        incoming[identity] = len(dependencies)
        count += len(dependencies)
        for parent in dependencies:
            consumers[parent].append(identity)
    if count > maximum_edges:
        raise DomainError("graph_limit_exceeded", "Work dependency limit exceeded", 422)
    ready = deque(identity for identity, size in incoming.items() if size == 0)
    visited = 0
    while ready:
        identity = ready.popleft()
        visited += 1
        for consumer in consumers[identity]:
            incoming[consumer] -= 1
            if incoming[consumer] == 0:
                ready.append(consumer)
    if visited != len(items):
        raise DomainError("dependency_cycle", "Expanded work graph contains a cycle", 422)


class _Preview:
    """In-memory graph construction; no store writes or events escape validation."""
    def __init__(self, tx):
        self.tx, self.changed = tx, {}

    def get(self, kind, identity):
        return deepcopy(self.changed.get((kind, identity), self.tx.get(kind, identity)))

    def list(self, kind):
        rows = {row['id']: row for row in self.tx.list(kind)}
        rows.update({identity: value for (k, identity), value in self.changed.items() if k == kind})
        return deepcopy(list(rows.values()))

    def put(self, kind, identity, value, expected_revision=None):
        current = self.get(kind, identity)
        if expected_revision is not None and (current or {}).get('revision') != expected_revision:
            raise DomainError('revision_conflict', 'The graph changed before expansion')
        row = {**deepcopy(value), 'id': identity, 'revision': current['revision'] + 1 if current else 1}
        self.changed[kind, identity] = row
        return deepcopy(row)

    def event(self, *_args, **_kwargs):
        return None


class StageExpander:
    def __init__(self, store, *, max_children: int = 32, max_work_items: int = 512,
                 max_edges: int = 4096):
        self.store = store
        self.max_children = max_children
        self.max_work_items = max_work_items
        self.max_edges = max_edges

    def apply_batch(self, tx, run_id, proposals, *, producer, contract=None):
        run = _required(tx, 'run', run_id)
        author = _required(tx, 'work_item', producer['work_item_id'])
        plan = _required(tx, 'plan', run['plan_id'])
        items = [row for row in tx.list('work_item') if row['run_id'] == run_id and not row.get('archived')]
        current = build_planning_contract(run, author, plan, items,
            max_children=self.max_children, max_work_items=self.max_work_items, max_edges=self.max_edges,
            reused_steps=[a['step'] for identity in plan.get('reused_inputs', [])
                          if (a := tx.get('artifact', identity)) and not a.get('stale')])
        if contract is not None:
            if (contract.get('version') != 1 or contract.get('run_id') != run_id
                    or contract.get('input_fingerprint') != run['input_fingerprint']
                    or contract.get('plan_id') != plan['id'] or contract.get('plan_revision') != plan['revision']
                    or any(contract.get('producer', {}).get(k) != author.get(k)
                           for k in ('id', 'attempt_id', 'fencing_token', 'input_fingerprint'))):
                raise DomainError('stale_planning_contract', '规划输入版本已变化，需重新读取并核验。')
            frozen = {row['stage_key']: row for row in contract.get('stages', [])}
            latest = {row['stage_key']: row for row in current['stages']}
            for proposal in proposals if isinstance(proposals, list) else []:
                key = proposal.get('stage_key') if isinstance(proposal, dict) else None
                if key in frozen and (key not in latest or any(frozen[key].get(k) != latest[key].get(k)
                        for k in ('work_item_id', 'revision', 'generation', 'input_fingerprint'))):
                    raise DomainError('stale_planning_contract', '规划目标阶段版本已变化，需重新读取并核验。')
        normalized = validate_parallel_work(proposals, current)
        by_key = {row['stage_key']: row for row in current['stages']}
        preview = _Preview(tx)
        _validate_graph(items, self.max_edges)
        for target in (preview, tx):
            results = []
            for proposal in normalized:
                spec = by_key[proposal['stage_key']]
                results.append(self._apply(target, run_id, spec['work_item_id'], proposal['children'],
                    spec['revision'], producer, target.get('run', run_id), target.get('work_item', spec['work_item_id'])))
        return results

    async def expand(self, run_id: str, stage_work_item_id: str, children: list[dict],
                     key: str, expected_revision: int, *, producer: dict | None = None) -> dict:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise DomainError("invalid_revision", "Expected stage revision must be a positive integer", 422)
        if not isinstance(children, list) or not 2 <= len(children) <= self.max_children:
            raise DomainError("expansion_limit_exceeded", f"Supply 2 to {self.max_children} stage children", 422)
        proposals = []
        for child in children:
            if (not isinstance(child, dict) or not {"key", "goal", "write_paths"} <= set(child)
                    or set(child) - {"key", "goal", "write_paths", "review_focus", "inspection_paths"}):
                raise DomainError("invalid_expansion", "A child may specify only key, goal, write_paths, review_focus and inspection_paths", 422)
            child_key, goal, paths = child["key"], child["goal"], child["write_paths"]
            if (not isinstance(child_key, str) or not child_key.strip() or len(child_key) > 128
                    or any(ord(char) < 32 for char in child_key) or "/" in child_key or "\\" in child_key):
                raise DomainError("invalid_expansion", "Child keys must be bounded identifiers", 422)
            if not isinstance(goal, str) or not goal.strip() or len(goal) > 16000 or "\0" in goal:
                raise DomainError("invalid_expansion", "Each child needs a bounded concrete goal", 422)
            if not isinstance(paths, list) or len(paths) > 128:
                raise DomainError("invalid_write_scope", "Child write scope is too large", 422)
            normal_paths = sorted(set(_path(path) for path in paths))
            inspection = child.get('inspection_paths', [])
            if not isinstance(inspection, list) or len(inspection) > 128:
                raise DomainError('invalid_inspection_scope', 'Inspection scope must be a bounded array', 422)
            proposals.append({"key": child_key, "goal": goal, "write_paths": normal_paths,
                              **({'inspection_paths': sorted(set(_path(path) for path in inspection))}
                                 if 'inspection_paths' in child else {}),
                              **({"review_focus": child["review_focus"]} if "review_focus" in child else {})})
        if len({child["key"] for child in proposals}) != len(proposals):
            raise DomainError("duplicate_child", "Child keys must be unique", 422)
        proposals.sort(key=lambda child: child["key"])
        payload = {"run_id": run_id, "stage_work_item_id": stage_work_item_id,
                   "children": proposals, "expected_revision": expected_revision, "producer": producer}
        # These snapshots detect a concurrent input revision; they are not part of
        # the idempotency payload, so a successful command remains replayable.
        frozen_run = await self.store.read("run", run_id)
        frozen_stage = await self.store.read("work_item", stage_work_item_id)

        return await self.store.command("stage.expand", key, payload,
            lambda tx: self._apply(tx, run_id, stage_work_item_id, proposals, expected_revision,
                                  producer, frozen_run, frozen_stage))

    def _apply(self, tx, run_id, stage_work_item_id, proposals, expected_revision, producer, frozen_run, frozen_stage):
        payload = {"run_id": run_id, "stage_work_item_id": stage_work_item_id,
                   "children": proposals, "expected_revision": expected_revision, "producer": producer}
        run = _required(tx, "run", run_id)
        stage = _required(tx, "work_item", stage_work_item_id)
        if producer:
            author = _required(tx, "work_item", producer["work_item_id"])
            if (author["run_id"] != run_id or author["status"] != "running"
                    or author["attempt_id"] != producer["attempt_id"]
                    or author["fencing_token"] != producer["fencing_token"]
                    or author["input_fingerprint"] != producer["input_fingerprint"]):
                raise DomainError("stale_producer", "Only the current planning attempt may expand stages")
        if stage["run_id"] != run_id:
            raise DomainError("run_mismatch", "Stage belongs to another run", 422)
        if stage["revision"] != expected_revision:
            raise DomainError("revision_conflict", "The stage changed before expansion")
        if (not frozen_run or not frozen_stage
                or run["input_fingerprint"] != frozen_run["input_fingerprint"]
                or stage["input_fingerprint"] != frozen_stage["input_fingerprint"]):
            raise DomainError("stale_input", "Run or stage inputs changed before expansion")
        if run["execution_state"] not in {"running", "paused"}:
            raise DomainError("invalid_state", "This run cannot expand work")
        if (any(row.get("run_id") == run_id and row.get("run_input_fingerprint", run["input_fingerprint"]) == run["input_fingerprint"] for row in tx.list("candidate"))
                or any(row.get("run_id") == run_id and row.get("status", "prepared") in {"prepared", "confirmed"} for row in tx.list("delivery_intent"))):
            raise DomainError("source_already_frozen", "Frozen candidates or delivery intents require a new revision/run before graph changes")
        if stage["status"] != "pending" or stage.get("attempt_id"):
            raise DomainError("stage_not_pending", "Only an unclaimed pending stage can be expanded")
        if stage["step"] in {"unit_test_execution", "integration_test_execution"}:
            raise DomainError("matrix_controls_parallelism", "Execution parallelism is defined by the frozen target matrix; expand test planning or test implementation roles instead")
        if stage.get("kind") in {"aggregation", "stage_child"} or stage.get("parent_stage_id"):
            raise DomainError("stage_already_expanded", "A stage may be expanded once; children cannot recursively expand")
        plan = _required(tx, "plan", run["plan_id"])
        authorized = any(spec["key"] == stage["key"] and spec["step"] == stage["step"]
                         and spec["role"] == stage["role"] for spec in plan.get("work_specs", []))
        selected = set(plan.get("actual_steps", []))
        mandatory_review = stage["step"] == "code_review" and any(
            stage["key"] == coding_step + ":review" and coding_step in selected
            for coding_step in ("unit_test_implementation", "integration_test_implementation"))
        if (not authorized or (stage["step"] not in selected and not mandatory_review) or stage["role"] == "system"
                or ROLES.get(stage["step"]) != stage["role"]):
            raise DomainError("stage_outside_scope", "Expansion must preserve a stage in the frozen Run plan", 422)
        all_items = [item for item in tx.list("work_item") if item["run_id"] == run_id and not item.get("archived")]
        if len(all_items) + len(proposals) > self.max_work_items:
            raise DomainError("graph_limit_exceeded", "Run work-item limit exceeded", 422)
        phase_contract = review_phase_contract(stage, {item['id']: item for item in all_items},
            reused_steps=[artifact['step'] for identity in plan.get('reused_inputs', [])
                          if (artifact := tx.get('artifact', identity)) and not artifact.get('stale')])
        for proposal in proposals:
            validate_review_focus(proposal.get('review_focus'), phase_contract)
        original_scopes = [_path(path) for path in stage.get("write_paths", [])]
        coding = stage["step"] in CODING_STEPS
        existing_keys = {item["key"] for item in all_items}
        # Model recovery authorizes an existing work identity, not future children.
        child_payload = {name: value for name, value in stage.get('payload', {}).items()
                         if name not in {'recovery_model_binding', 'review_phase_contract', 'review_focus'}}
        if phase_contract is not None:
            child_payload['review_phase_contract'] = phase_contract
        rows = []
        for proposal in proposals:
            child_key = stage["key"] + ":" + proposal["key"]
            if child_key in existing_keys:
                raise DomainError("duplicate_child", "A stage child key already exists")
            paths = proposal["write_paths"]
            if not coding and paths:
                raise DomainError("role_write_scope", "Non-coding stages cannot acquire source write permission", 403)
            if coding and (not paths or any(not _within(path, original_scopes) for path in paths)):
                raise DomainError("write_scope_expansion", "Child writes must remain within the original stage scope", 403)
            identity = str(uuid5(NAMESPACE_URL, f"agentflow:{run_id}:{stage_work_item_id}:{stage['generation']}:{proposal['key']}"))
            body = {name: value for name, value in stage.items() if name not in {"id", "revision"}}
            body.update(key=child_key, kind="stage_child", parent_stage_id=stage_work_item_id,
                        expansion_key=proposal["key"], goal=proposal["goal"],
                        dependencies=list(stage["dependencies"]), status="pending", quality_result="unknown",
                        fencing_token=0, artifact_ids=[], attempt_id=None, output_fingerprint=None,
                        approved_fingerprint=None, write_paths=paths,
                        payload={**child_payload, "goal": proposal["goal"],
                                 **({'inspection_paths': proposal['inspection_paths']} if 'inspection_paths' in proposal else {}),
                                 **({"review_focus": proposal["review_focus"]} if phase_contract else {})},
                        created_at=utc_now())
            rows.append({"id": identity, **body})
        fingerprint = canonical_digest({"request": payload, "run_input": run["input_fingerprint"],
            "stage_input": stage["input_fingerprint"], "generation": stage["generation"],
            "policy": stage["policy_fingerprint"], "dependencies": stage["dependencies"]})
        aggregation = {**stage, "kind": "aggregation", "dependencies": [row["id"] for row in rows],
                       "write_paths": [], "expanded_child_ids": [row["id"] for row in rows],
                       "expansion_fingerprint": fingerprint, "original_dependencies": stage["dependencies"],
                       "original_write_paths": stage.get("write_paths", []),
                       "payload": {**stage.get("payload", {}),
                           **({"review_phase_contract": phase_contract} if phase_contract else {})}}
        graph = [aggregation if item["id"] == stage_work_item_id else item for item in all_items] + rows
        _validate_graph(graph, self.max_edges)
        affected = {stage_work_item_id}
        while True:
            added = {item["id"] for item in all_items if set(item["dependencies"]) & affected} - affected
            if not added:
                break
            affected |= added
        if any(item["id"] in affected - {stage_work_item_id} and item["status"] != "pending" for item in all_items):
            raise DomainError("stale_dependents", "Revise downstream work before expanding its source stage")
        created = [tx.put("work_item", row["id"], {name: value for name, value in row.items() if name != "id"}) for row in rows]
        updated = tx.put("work_item", stage_work_item_id, aggregation, expected_revision)
        tx.put("stage_expansion", str(uuid5(NAMESPACE_URL, fingerprint)), {
            "run_id": run_id, "stage_work_item_id": stage_work_item_id, "original_stage": stage,
            "input_fingerprint": fingerprint, "child_ids": [row["id"] for row in rows], "created_at": utc_now()})
        tx.put("run", run_id, run, run["revision"])
        tx.event("work.expanded", {"stage_work_item_id": stage_work_item_id,
                 "work_item_ids": [row["id"] for row in rows], "input_fingerprint": fingerprint}, run_id=run_id)
        return {"stage": updated, "items": created, "work_item_ids": [row["id"] for row in rows],
                "input_fingerprint": fingerprint}

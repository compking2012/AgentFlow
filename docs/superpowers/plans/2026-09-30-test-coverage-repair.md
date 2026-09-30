# Test Coverage Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Close mixed production/test coverage findings through the existing controlled repair workflow.

**Architecture:** Add one constrained coverage action to the existing disposition protocol. Reuse owner-bound budgets, assembly and diagnostic/review gates; preserve original statements through an additive AST guard.

**Tech Stack:** Python, SQLite Store, Babel parser bundle, React dashboard, pytest and Playwright.

**Spec:** `docs/superpowers/specs/2026-09-30-test-coverage-repair.md`

## Global Constraints

- Retain model configuration, cumulative budgets, original scopes, approvals and formal test gates.
- Existing files and cases remain; no assertion replacement, skip/only/todo or test configuration changes.
- Only stopped, unexecuted triage contexts may be upgraded; keep their prior evidence and identity.
- Generated product edits use AgentFlow repair tasks, not direct operator patches.

## Task 1: Disposition and accepted test-plan evidence

Files: `control/review_disposition.py`, `tests/control/test_review_disposition.py`.

Produces classification `test_coverage_extension`; `ReviewDisposition.validate(result, context)` retains every finding and validates distinct owner assignments.

- [ ] Add failing tests for one finding with production and coverage owners, missing/wrong test-plan citations, duplicate same-owner actions and forbidden migration authority.
- [ ] Add the classification and preserve the existing migration branch:

```python
classification: Literal['production_fix', 'test_contract_migration', 'test_coverage_extension', 'needs_clarification']
action_keys = [(a['finding_id'], a['owner_work_item_id']) for a in checked['actions']]
```

- [ ] Include accepted `unit_test_plan` and `integration_test_strategy` documents with their step provenance; require coverage references to the owner's corresponding test plan.
- [ ] Run `pytest -q tests/control/test_review_disposition.py` and reject any weakening of existing migration checks.

## Task 2: Additive coverage guard

Files: new `control/test_coverage_guard.py`, new `resources/test_migration_guard/coverage.cjs`, existing parser entry/bundle, new `tests/control/test_test_coverage_guard.py`.

Produces `TestCoverageGuard.verify(before_repo, after_repo, paths) -> {ok, errors, evidence}`. Parser operation `coverage` receives file/before/after and returns structural preservation evidence.

- [ ] Add failing source fixtures for allowed appended assertions and fresh HTTP helpers, and rejected changed/deleted assertions, skipped cases, early return, rebinding and modified support files.
- [ ] Normalize AST metadata, match original top-level nodes in order, and permit extension only in original test callbacks:

```javascript
if (old.type === 'BlockStatement') {
  // Original statements remain in the same positions; only append after them.
  return next.body.length >= old.body.length && old.body.every((node, i) => extendsNode(node, next.body[i]));
}
```

- [ ] Reject added control exits and writes/rebindings to protected old identifiers; allow only side-effect-free fresh declarations at program scope.
- [ ] Rebuild with `node scripts/build_test_migration_guard.mjs`; run coverage and existing migration-guard tests.

## Task 3: Lifecycle, context upgrade and current-run recovery

Files: `control/review_contract_repair.py`, `control/review_contract_view.py`, `control/scheduler.py`, protocol docs and lifecycle tests.

Consumes validated coverage actions and the new guard. Produces owner-scoped ordinary test-author tasks, mandatory guard receipts and one verified assembled snapshot.

- [ ] Add failing lifecycle tests for mixed repairs, guard receipt requirements, unchanged budgets/permissions, and stopped-only triage context upgrade.
- [ ] Route coverage actions using the original test owner's step; require `validate_action` and assembled-file identity checks for both coverage and migration kinds.
- [ ] Include an explicit coverage instruction: append within existing cases, preserve all old statements and assertions, add only necessary fresh helpers, and leave formal gates intact.
- [ ] Archive old context before adding test-plan evidence to a stopped triage batch; validate that old source, review and document identities remain unchanged. Reuse the original work item and counters.
- [ ] Add dashboard label/count support for coverage repairs and update the protocol documentation.
- [ ] Run disposition, guard, binding, lifecycle, recovery and browser review-context regressions.
- [ ] At a stopped controller window load changes, refresh the old WordBook triage context through the controller, retry the analysis normally and continue to real delivery.

## Execution note

Implementation remains in the current authorized repair session. Existing untracked repository content is preserved; no broad repository commit is part of this task.

## Cross-review follow-up

- [ ] Reject same-file migration/coverage combinations before creating a batch; retain distinct-file parallel support.
- [ ] Bind delegated execution to shared owner remaining allowance and unsettled reservations in the writer transaction. Apply existing timeout allowance increments to child and owner atomically with audit/idempotency, never reset usage.
- [ ] Reject stopped-context upgrades with active/unverified model requests while preserving acknowledged unknown usage semantics.
- [ ] Verify repair action identity/spec before selecting the diagnostic guard; mutation cannot suppress required receipts.
- [ ] Re-run targeted regression, cross-review, and then restore WordBook normally.

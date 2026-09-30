# AgentFlow Dashboard

English | [简体中文](README.zh-CN.md)

A local, single-owner software delivery dashboard built with React 19, TypeScript, and Vite. The default Create product entry supports both new products and imports, with multiple target selections: Web, API, iOS, Android, Windows, macOS, and Linux. Only supported Web/API projects execute automatically. Native or unrecognized projects can be registered statically but do not start development. Production code has no demo-data branch.

## Build and Run

Run from this directory:

```sh
npm ci
npm run build
```

Build output goes to `../../src/agentflow/web/` and is served by the local AgentFlow controller. Run `agentflow start` to start the background service. While it is running, open `http://127.0.0.1:8787` directly; no startup link is required, including in a new tab or after a controller restart. This does not install login autostart or expose the controller to the LAN. Building replaces that output directory; rebuild after source changes.

`npm run dev` only serves the Vite development page. Verify management operations on the controller's same-origin page. No cross-origin management API, development proxy, or mock backend is configured.

## Views

- **Create product:** Enter a name, long-term goal, and platforms. New products check actual models and execution prerequisites. Import statically inspects a local directory and registers its name and goal without running project code; unknown platforms are not guessed as Web. The view tracks the Product and associated Run. Preparation failures are retryable only before execution starts. Export retry re-exports the original Git delivery without rerunning development; records awaiting recovery reconciliation are not retryable. Downloads and managed local start/stop require completion and complete delivery fields. Unknown launch state blocks another launch.
- **Requirement changes:** Select an existing product, describe a focused change, and optionally supply acceptance criteria while retaining the original goal. The workflow starts with PRD updates, checks architecture and API impacts, then proceeds through development and quality gates. Server-side eligibility is enforced, including preview, active-run, unsupported-platform, and unresolved-recovery restrictions. Unconfirmed requests retain their request version and idempotency key; preparation failure before the new run starts can retry the same requirement.
- **Execution console:** Stage nodes wrap horizontally, with directed edges based on actual dependencies. Selecting a stage reveals its artifacts, parallel tasks, and named summaries. Execution highlighting and failure markers are independent. Local-node waits show actual preparation progress, not-started status, or validation errors. After addressing the cause, explicitly prepare the local executor again; this does not retry work generations or bypass validation. Duplicate preparation is unavailable while preparing or once required targets are ready. Keyboard navigation supports arrow keys, Home/End, Enter/Space, and Escape, with mobile scroll positioning. Run controls remain version-bound.
- **Human review:** Decisions bind to the exact version and fingerprint displayed when opened. Rejection requires a reason and requested changes; stale requests cannot be submitted.
- **Artifacts and quality:** Display verified unit/integration pass rates, categorized review findings, explicitly reported performance measurements, and sample counts. Missing or unmeasured data is not filled with zero. Human-readable Markdown is the default for viewing and downloads; code/test artifacts show actual directories and descriptions. Internal JSON is not the default artifact. Candidates and Git deliveries use named fields.
- **Local settings:** Show actual model acceptance, credential status, and backend probes; expose product environment setup, self-managed node pairing, and device-resource details.

Model settings are submitted only when the user chooses Save and use this model. API keys use password inputs and are not written to persistent browser storage. Inputs are cleared after success or failure and when the provider or endpoint changes. Analysis uses Chat Completions; coding uses Responses. A shared model must support both. Ordinary users are not asked for pricing-evidence configuration by default; unknown cost is not shown as zero.

Persistent configuration lives in `~/.config/agentflow/config.toml`; dashboard model settings write to the same file. Server `product_defaults` initialize only untouched fields. If a file change requires restart, model editing remains available, new product creation is blocked, and the page instructs the user to run `agentflow stop` followed by `agentflow start`. Clearing model configuration does not silently reuse old database defaults.

## Data and Evidence Contracts

The dashboard associates `/runs/{id}/target_matrix`, `/checks`, and `/candidates`. Neither `target_matrix.plan.entries` (the plan) nor `bound_to_platform_manifest` (binding status) alone proves a test passed.

An effective pass requires exactly one candidate for the current run inputs, a matching matrix fingerprint, and every required target check completed with the same candidate fingerprint, a passing conclusion, verified raw reports, and a positive case count. Old candidates, mismatched fingerprints, missing checks, or unverified evidence remain unexecuted or incomplete. Evidence for one platform never substitutes for another.

Delivery lists read saved server receipts. Confirmed delivery requires `confirmed_at`, `commit_oid`, and `delivery_ref`. Test success, human approval, and delivery are displayed separately.

Stage and quality read models use `/runs/{id}/workflow` and `/runs/{id}/quality_summary`, bound to the run-input version and current candidate. Summary stages expose one outer deliverable; internal contributions appear after stage selection. Readable artifacts use dedicated preview/download URLs from the server; derived IDs must not be treated as internal JSON artifact IDs. Verified report provenance is not a passing test result: failure, partial coverage, and unmeasured states remain distinct.

Local-wait explanations read `/product_setup` and `/executor_jobs`. They match only the current run, pending work and its generation, queued jobs, and a complete consistent local target configuration. Matching target IDs alone are insufficient if configuration versions, OS, resources, or tool requirements differ. Object-key order does not affect matching; array order is preserved. Remote targets, other runs, older generations, and finished jobs do not surface as local failures. With partial platform readiness, readiness is evaluated against platforms needed by current queued work. Preparation is explicitly submitted through `/product_setup/local_execution`, disabled while busy, and reuses the same idempotency key when the receipt cannot be confirmed.

## Browser Verification

Install the Python project and test dependencies into the repository-root `.venv` first. From this directory, run:

```sh
npm run build -- --outDir /tmp/agentflow-dashboard-test
PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers npx playwright install chromium
./node_modules/.bin/tsc --noEmit -p ../../tests/browser/tsconfig.json
AGENTFLOW_BROWSER_DASHBOARD_DIR=/tmp/agentflow-dashboard-test npm run test:browser
```

Tests live in `../../tests/browser/`. The isolated build directory is selected through the test-only `AGENTFLOW_BROWSER_DASHBOARD_DIR`, avoiding replacement of live platform assets. Workspace tests use the production owner API, real SQLite, and temporary Git projects; node pairing uses a real NodeService and temporary PKI. `product_entry_server.py` uses actual Application/ProductService components, temporarily committed repositories, and static diagnostics with scheduling disabled. It verifies real registration/preparation states without calling models or manufacturing passing checks. `product_server.py` remains a separate product-UI transport-contract fixture; simulated states exercise display boundaries such as downloads, retries, and launch URLs. Production does not load test routes.

Chromium coverage includes authentication, review races, quality matrices, confirmation receipts, pairing, run controls, artifact boundaries, run switching, and keyboard interaction. Product-entry coverage includes multi-platform selection, disabled native execution, static repository import, unknown-platform handling, incremental PRD requirements and acceptance criteria, unchanged original goals, version/idempotency-preserving recovery, key clearing, authenticated downloads, and local-launch URL boundaries. Screenshots and failure traces go to `tests/browser/artifacts/`; HTML reports go to `tests/browser/report/`. Both are ignored local outputs.

These tests verify dashboard interactions with actual APIs, storage, and Git project management. Fixture platform reports and delivery records verify DTO presentation, not seven-platform native execution, model capabilities, real delivery pipelines, or provider billing.

## Security

Bootstrap codes are removed from the URL before exchange; owner tokens remain in private page memory. Requests are restricted to the page origin and `/api/v1/`, reject redirects, and do not use cookies, Local Storage, Session Storage, or query tokens. Documents render through safe React Markdown nodes without executing raw HTML, scripts, or unsafe links. Document content is limited to 1 MiB, and JSON transport envelopes also have read limits. Downloads create temporary Blobs from authenticated responses. See [SECURITY_REVIEW.md](SECURITY_REVIEW.md) for further review details.

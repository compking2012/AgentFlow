# Strict goal-to-product acceptance (A layer)

`test_goal_to_product.py` runs one real Application and its owner HTTP/IPC services.
An API-only reading-list product is submitted through the actual CLI. A separate
Web/API product is submitted by filling and clicking the actual freshly compiled
WebUI in Chromium. Product API requests are never mocked.

Only upstream LLM HTTP responses are scripted by `model_fixture.py`. The actual
OpenHands SDK writes role artifacts and reads code. The actual Codex CLI applies
patches. Builds, Node unit tests, Playwright tests, report parsing, quality gates,
Git delivery, ZIP export and isolated product launch execute normally. No checks,
jobs, candidates, or passing results are inserted into the database by the test.
The only preconfigured records are two local test model profiles, one per native
protocol. No paid model provider is contacted.

The Web implementation deliberately begins with a label accessibility defect.
Its original integration assertion fails. Only after the controller creates a
repair task from the verified failing report does the scripted model return a
patch fixing `public/index.html`. The test requires another Review and full test
matrix, retains the old failure, and verifies that the repair changed no test,
build tooling, lockfile, or execution recipe. API and Web candidates/checks are
separate; API evidence cannot substitute for Web evidence.

Each delivered product is then tested independently over HTTP, including process
restart and SQLite persistence. The Web delivery also has an independent browser
check. Release contents must remain unchanged while the product is running.

Run on the supported macOS host with the installed Node toolchain, Codex CLI,
OpenHands SDK 1.49.2, and pinned dashboard Playwright/browser dependencies:

```sh
.venv/bin/python -m pytest -q -s --tb=short --show-capture=no tests/e2e/test_goal_to_product.py
```

For a first compatibility check of the complete CLI/API path only, use
`AGENTFLOW_E2E_ENTRY=cli` with the same command. This explicit slice retains all
CLI engineering and delivery assertions and writes `e2e-selection.json`; it does
not claim WebUI coverage. The default remains both paths, including their
cross-product evidence-isolation checks. The evidence-preservation command below
requires the complete default suite.

`AGENTFLOW_E2E_NPM_CACHE=/absolute/path/to/npm-cache` may point to an additional
read-only cache, for example a previous temporary acceptance run. Only exact
public registry tarballs whose SHA-512 matches the committed lockfiles are
copied into the new isolated cache. The real `npm ci`, build and tests still
execute; installed dependencies, command results and quality states are never
copied. Missing tarballs still require registry access.

Only a read-only status observer may reconnect after at most three connection
errors; it records each reconnect. Mutations, tool executions, and test results
are not replayed by that observer. A persistent error fails acceptance.

After the complete test passes, preserve its case directory:

```sh
.venv/bin/python tests/e2e/preserve_evidence.py /absolute/path/to/pytest/case-directory
```

This copies a whitelist of final release archives, receipts, screenshots, raw
check reports, and repair summaries to `validation/goal-to-product/`, with a hash
index. It does not copy controller databases, PKI, tokens, or model request headers.

These tests prove the engineering pipeline executes. They do **not** establish
that a live LLM independently researched, designed, implemented, or repaired the
product. Live-model acceptance is a separate B-layer activity.

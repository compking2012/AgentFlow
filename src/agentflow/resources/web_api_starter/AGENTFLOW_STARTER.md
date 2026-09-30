# Supported Web/API foundation

This is infrastructure, not an implemented user product. Keep business scope in the
goal, PRD and accepted test plans. Do not rename the placeholders and call them a
completed application. All supplied acceptance placeholders deliberately fail.

Node.js 22.13+ runs the product using native HTTP, file APIs and SQLite. The product
has no third-party runtime dependencies. Playwright 1.63.0 is a pinned test-only
dependency in package-lock.json. Coding does not need network access to invent or
refresh a dependency lock: the outer executor runs npm ci from the existing lock.
The coding workspace may not contain node_modules. If a local build reports a
missing Playwright package, do not repeatedly run the build or attempt npm ci in
the isolated coding environment. Use available dependency-free checks such as
node --check for changed JavaScript files, and report which checks still require
the formal executor. Never claim an unexecuted test passed. The later build and
test gates install the pinned dependencies and must still execute every required
case; missing local test dependencies do not authorize changing the lockfile,
build tooling, test support files or assertions.

## Stable contract

- Implement APIs and business behavior in src/*.mjs and UI in public/. Keep
  src/server.mjs as the executable entry point and preserve GET /health.
  Await asynchronous business handlers inside the error boundary (or catch their
  rejections inside the handler). Returning an unawaited Promise from try/catch
  does not catch asynchronous validation/readJson failures; every such failure
  must produce its contracted HTTP response rather than an unhandled rejection.
- PORT accepts 0 for an operating-system allocated port; HOST defaults to
  127.0.0.1. AGENTFLOW_DATA_DIR is writable application data outside frozen output.
- AGENTFLOW_READY_FILE, when supplied, receives the actual URL/port and PID after
  listen succeeds. It must not be placed inside build/product or build/tests.
- npm run build syntax-checks and copies source into build/product, public into
  build/product/public, and tests plus their installed dependencies into build/tests.
- Unit tests execute build/tests/unit.test.mjs and import ../product/<module>.mjs.
  Use flat node:test cases with stable names from the accepted unit plan.
  A unit-test helper that starts an in-process HTTP server must listen on
  Number(process.env.AGENTFLOW_TEST_PORT ?? 0) at 127.0.0.1 and close it during
  teardown before the next case reuses that port. Handle startup errors so a
  failed bind also closes the database and temporary resources. PORT=0 is only
  a fallback outside the isolated executor. Keep this choice in the test helper;
  do not override the product server's standard listen semantics.
  When cases destroy and recreate a server on this same port, isolate their HTTP
  clients too: close a per-case client pool, or use `Connection: close` in the
  test-only requests. Reusing a global keep-alive pool across different server
  instances can cause intermittent ECONNRESET/fetch failures. Preserve product
  keep-alive behavior; fix this isolation in the test helper, not product replies.
- Web and API integration tests import test/expect from ./support/fixtures.mjs.
  Keep api.spec.mjs and web.spec.mjs separate and use flat, stable test names.
  Global setup supplies one temporary product process/data directory for the
  suite, not a fresh database for every case. Establish each case's known baseline
  through the product API in the assigned spec, and clean up its test records.
  Do not let earlier cases' records invalidate exact counts, ordering or sibling
  assertions. Keep assertions and shared test support unchanged.
- The local executor supplies AGENTFLOW_BROWSER_EXECUTABLE for its verified
  Chromium binary. Preserve the launchOptions.executablePath binding in
  tests/support/config.mjs. Use Playwright's temporary browser contexts; never
  open a user's existing browser profile or persist a profile in frozen output.
- Playwright global setup uses the trusted executor's AGENTFLOW_TEST_PORT (or
  AGENTFLOW_WEB_PORT), falling back to PORT=0 only for ordinary unsandboxed test
  invocation. These are runtime parameters, not model-chosen source configuration.
  It uses a new temporary data directory, waits for the real listener, supplies
  baseURL, then stops it.
- Tests that start a second product process while global setup's server is running
  must pass PORT=process.env.AGENTFLOW_TEST_SECONDARY_PORT || '0' and HOST=127.0.0.1
  to that child. The executor allocates this second port separately from the main
  port and keeps both stable across commands in one job. Restart the second process
  on that same port for persistence tests, stopping it before reuse. Each job has
  its own two ports; controller ports and arbitrary loopback ports remain blocked.
  The PORT=0 fallback is only for ordinary unsandboxed test invocation. Keep the
  second process's data and ready file in a writable temporary directory.
  Give each process launch a fresh ready-file path, or remove the previous launch's
  ready file before spawning. Reuse the data directory for persistence assertions,
  but never mistake its old ready file for evidence about the new process. Keep
  checking the new PID, host and port within the existing readiness deadline.
- All test processes must be stopped during teardown.
  Reports, traces and data live outside the product/test package. Do not add build,
  transpilation, npm installation or writes to frozen packages in formal tests.

## Required Agent work

Coding runs with an isolated tool environment. Use an advertised patch/edit tool
when available; do not assume `apply_patch` is a shell executable. Node is the
runtime for this stack and can read JSON or make scoped filesystem edits when a
shell helper is unavailable. Check an optional executable once before relying on
it; do not repeatedly invoke missing `apply_patch`, Python or ripgrep commands,
and do not install unrelated tools to complete a coding step.
Put disposable diagnostic files under the supplied TMPDIR (or os.tmpdir() in
Node), not a hard-coded /tmp path. Filesystem and network restrictions still
apply there. If a diagnostic needs an unavailable listener or dependency, record
that limitation and leave execution to the formal node; do not retry the same
denied operation or broaden permissions.
For a resumed repair, inspect the preserved checkpoint and the specific review
findings first. Reuse unchanged checks already recorded for that checkpoint.
After the assigned issues are addressed and available focused checks are done,
return the required final receipt with actual results and any execution limits.
Do not keep rereading unchanged files, rerunning identical checks, or searching
for unrelated improvements merely to postpone the receipt. Independent review
and formal test execution remain responsible for their own acceptance gates.

The product/architecture stages define the user's actual entities, API contracts,
UI journeys and error cases. Development implements them and replaces the visible
starter placeholder. The test-planning roles independently define normal, invalid,
state-transition and persistence cases against those requirements. The test-coding
roles replace the failing placeholder tests with those exact cases, without
weakening assertions. Each code review follows its actual code-producing stage.
The first implementation review checks production behavior, frozen boundaries,
and regressions or tampering in existing tests and support files. Future test
plans and replacement of untouched starter tests belong to the downstream test
stages; their absence alone cannot block the earlier implementation review.
After each test implementation, its independent review must check complete case
coverage, exact accepted framework IDs, meaningful assertions and removal of
that phase's placeholders. Existing test deletion or weakened assertions remain
reviewable defects at every stage.

The normalized flat Node IDs are test::<test title>. The normalized flat
Playwright IDs are api.spec.mjs::<test title>:: and web.spec.mjs::<test title>::.
The final empty segment represents Playwright's unnamed project. Freeze actual
goal-specific IDs from the accepted plans, not the starter's placeholder titles.

## Execution recipe paths

Both targets build in project_path="." and collect output_paths
{"product":"build/product","test":"build/tests"}. Unit recipes use
test_project_path="build/tests", unit_project="build/tests/unit.test.mjs",
product_path="build/product", report_path="reports/<target>-unit.xml".
Integration recipes use test_project_path="build/tests", the corresponding
framework_config="build/tests/playwright.<api|web>.config.mjs", and
report_path="reports/<target>-integration.json". expected_case_ids must be copied
from the accepted target-specific test plan. API uses test_kind="api" and Web
uses test_kind="integration". No external service_urls are needed for this local
self-starting test configuration.

Launch the exported frozen product with node build/product/server.mjs (or run
node server.mjs inside the product directory) and an explicit writable data path.
A successful build or /health is not evidence that the business goal works.

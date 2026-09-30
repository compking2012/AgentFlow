# Runtime and model protocol evidence

Verified locally on 2026-09-18 using Python 3.12.14, OpenHands SDK 1.49.2,
and the installed Codex executable reporting `0.154.0-alpha.6.2`.

## Execution contract

`RuntimeService.execute_task` creates one supervised backend execution.
The SQLite launch intent precedes process creation; the launcher waits for a
controller acknowledgement before starting the backend. Receipts bind the
attempt, operation, nonce, PID start time, boot fingerprint and fencing token.
Recovery inspects the original process or receipt and never starts a replacement.
Missing identities, corrupt receipts and unavailable frozen configuration produce
`execution_unknown`. Domain review and test validators own quality decisions;
successful runtime output does not set `quality_result=passed`.

The launcher removes its private descriptor after reading it, redacts the scoped
task token in stdout/stderr, bounds logs and wall time, and can cancel observed
descendants. POSIX process groups are **observed containment**, not proof that an
arbitrary detached descendant cannot escape process tracking.

## Isolation and authentication

The macOS controller verifies real allow/deny filesystem and loopback-port probes
before execution. Workspace code is read-only for professional roles. Staged
documents and explicit coding paths are writable. Protected controller state and
personal configuration are outside the sandbox. Unsupported controller platforms
fail closed; no Linux/Windows controller sandbox is claimed. Native test-node
capabilities are a separate component.

Codex runs with a private HOME/CODEX_HOME, `--ignore-user-config`, ephemeral state,
no MCP servers, no nested Agents, disabled web search, and only a scoped task-proxy
credential. Its help explicitly says `--ignore-user-config` alone still reads
CODEX_HOME authentication, so HOME isolation is required.

macOS rejects nested Seatbelt setup (`forbidden-sandbox-reinit`). The CLI therefore
uses its `danger-full-access` inner mode **inside the mandatory outer Seatbelt
sandbox**. The adapter refuses to start unless the outer evidence reports verified
hard filesystem and network enforcement. It never permits sandbox reinitialization
or falls back to an unsandboxed process. A real Codex patch outside the allowed
workspace is denied in the protocol tests.

Codex tool-call counts are observed; requesting hard tool-count enforcement blocks
this backend. OpenHands uses only fixed `agentflow_io` and `finish` tools and checks
the tool quota before side effects, including finish. Default tools, skills, MCP,
plugins, memory and backend spawning are disabled. It cannot write source code or
dispatch proposed work. Both SDK retries and its independent OpenAI transport
retries are disabled; the actual HTTP 500 test asserts one request.

Public research defaults on for newly dispatched research roles through
`[app].research_public_web_enabled = true`. An explicit `false` disables reads.
`research_web_hosts = []` allows any public source; a nonempty list is an optional
exact-host restriction, applied again to every redirect. Changes load on restart;
enabling the setting never expands a previously frozen task authorization.

The role sandbox has no direct public-IP grants. `fetch_url` calls only the
controller's `/internal/v1/research/fetch` endpoint using the existing scoped task
token. The broker validates the live attempt, fence, expiry, role and frozen
dispatch authorization, enforces a durable per-attempt request ceiling derived
from that task's tool allowance, and rechecks authorization after DNS before each
HTTP send. It does not resolve or forward owner/provider credentials.

The broker permits GET on HTTP(S) ports 80/443 only, rejects private, loopback,
link-local, multicast and unsafe transition addresses (including private NAT64
translations), validates every DNS answer, and pins the connection IP and TLS
server name. Each of at most five redirects repeats these checks with a fresh
client: no cookies, authorization headers, ambient proxy, or connection reuse.
Reads retain the previous 2 MiB source bound and 15-second transport timeout,
with a 30-second total fetch bound including DNS and redirects. Malformed URLs,
revocation, unavailable sources and oversized content return controlled errors.
Sources retain original/final URL, redirect chain, retrieval time, full content
and digest; the tool returns a bounded text preview and artifact reference.
Web content remains untrusted data and is never executed as instructions.

## Model routing and accounting

Chat Completions and Responses remain separate native protocols. Configured
profiles determine model, protocol, fixed upstream and local secret reference;
request bodies cannot override them. Pending/unaccepted profiles never dispatch.
Public profile views report only local `credential_status`; the check does not
contact an upstream. Task tokens are reauthorized and profile revisions rechecked
immediately before dispatch. Owner/provider credentials are not given to backends.

Each invocation atomically reserves reviewed input/output cost bounds against both
Run and Iteration ledgers and cumulative request quotas. Unknown outcomes remain
reserved, block fresh calls for that attempt, and require reconciliation. Connection
failures proven unsent can release their reservation. EOF is not stream completion;
missing usage or a different reported model retains uncertain liability. Completed
idempotent calls replay their verified receipt rather than making a second request.

The input bound and provider output control must be explicitly reviewed in the
profile. These are configuration assumptions, not an SDK token-count guarantee.
Unverified bounds block dispatch. Provider overruns are recorded and reported.

## What the tests establish

`tests/models` exercises the real Store and local HTTP servers: atomic double-budget
reservations, concurrency, interruption/reconciliation, cumulative quotas, JSON and
both SSE protocols, replay, partial streams, local credential status, profile
revocation and model mismatch. No paid endpoint is contacted.

`tests/runtime` launches real subprocesses and both real backend implementations.
The OpenHands fixture makes the actual SDK write a staged review and finish using
only the two allowed tools. The Codex fixture returns a native Responses
`custom_tool_call` for apply_patch, verifies the real workspace change and the next
request's `custom_tool_call_output`, then validates the final JSON schema. A second
patch targets outside the workspace and is denied by the OS sandbox.

The Codex fixture uses `gpt-5.4` **only as a local CLI tool-metadata identifier**;
the endpoint is a loopback test server and no such model is invoked. An unknown
fixture model selects fallback CLI metadata without apply_patch. The removed
`apply_patch_freeform` flag is not used. These tests establish backend/protocol
behavior, not reasoning quality or acceptance of any DeepSeek route. Production
profiles are not substituted or automatically confirmed.

## Sources and verification limits

The implementation was checked against the installed CLI help/version/features
and the installed SDK 1.49.2 source, including LLM API mode/options, Conversation,
fixed ToolDefinition registration, AgentContext, and retry behavior. Actual local
protocol tests are the executable evidence.

Official Codex noninteractive/security pages returned HTTP 403 during this session,
and an official-domain search timed out. OpenCLI was unavailable. Those web sources
were not treated as read or verified. No personal subscription authentication,
global configuration changes, paid model calls, or live-model acceptance were used.

## Product-entry transport verification (2026-09-19)

The supported role transport is pinned to OpenHands SDK 1.49.2 and LiteLLM 1.101.0.
SDK `api_mode="chat"` alone did not guarantee Chat on the wire: its default high
reasoning effort made LiteLLM bridge GPT-5.4 function calls to Responses. The role
worker now avoids that implicit default; for the GPT-5.4+ family it explicitly
uses the native Chat-compatible `reasoning_effort="none"`. Other model families
receive no unsolicited reasoning parameter. Coding remains on its separately
configured native Responses backend. A real SDK regression asserts the endpoint,
parameter and tool round-trip for GPT-5.4; this is protocol evidence, not a live
provider/model acceptance result.

Supervisor launch identity remains fail-closed. Each new launch records a
private `identity-verification.json` without credentials. macOS binds the kernel
boot-session UUID and raw `PROC_PIDTBSDINFO` birth data; Linux binds the boot ID
and `/proc/<pid>/stat` start ticks. The wall-clock creation time remains available
for display and legacy receipts, but clock corrections cannot change a new
process's birth token. Receipt fields cannot be removed to downgrade validation.
Legacy live PIDs with mismatching wall times remain uncertain; an absent PID or
a verified changed boot/birth identity can establish that the original process
has stopped.

A verified late failure receipt can reconcile previously unknown coding usage.
The collector and reconciler share the same tool-ID accounting. Original unknown
records remain in the audit trail; only the missing measured duration and tools
are added once, with no extra step, allowance, model settlement or success claim.

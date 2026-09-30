# Test migration parser resource

`parser.bundle.cjs` bundles `@babel/parser` and the checked-in `inspect.cjs` entry
point. It only parses stdin JSON; it never imports or executes inspected code.
Runtime requires Node.js 18+ and does not require development `node_modules`.
The parser license is retained in `BABEL-LICENSE`; `PROVENANCE.json` records its
version. Rebuild from the repository root with
`node scripts/build_test_migration_guard.mjs` after installing dashboard packages.

Python API: `TestMigrationGuard(node_path=...).inspect(relative_path, source)`
returns `file`/`path`, SHA-256 `source_digest`, static test `cases` with stable
`case_id`, hierarchical `name`, exact `title_path`, `framework_operation`, source `range`, and
`assertions`. Each assertion has an `assertion_id`, matcher chain,
`actual_expression`, exact `expected_source`/`old_expected`, decoded
`expected_value`, `static_expected`, `argument_count`, and `expected_range`.
Ranges are half-open Unicode code-point offsets, suitable for Python slicing.
Case IDs bind the relative file, suite/name and same-name occurrence. Assertion
IDs bind the case and source-order occurrence. Comment, regex, and template
contents never create phantom assertions. `test.step` assertions belong to their
original case. Dynamic names and framework aliases are conservatively excluded
from actionable manifests.

`verify(before_repo, after_repo, actions)` accepts approved actions with
`path` (or `file`), `case_id`, `assertion_id`, `matcher`, `old_expected`,
`new_expected`, and nonempty `requirement_refs` (structured references with
`artifact_id`, `requirement_id`, and `quote`; legacy string IDs are also accepted). Both expected fields must be
exact JavaScript **source strings**, including quotes or object syntax, rather
than JSON-decoded values. The caller authenticates approval/requirements and
checks test-path ownership; the guard enforces the bound AST transformation.

Only the bound expected argument may change (first matcher argument for Playwright, second argument for Node strict assertions). All other file bytes and
executable modes, the complete file set, and symlink targets are protected.
Root `.git` metadata is excluded. Formatting/comment changes outside approved
arguments are rejected. New expectations permit scalar literals and static
objects/arrays; executable expressions, regexes, templates, spread, getters,
computed/shorthand/duplicate/prototype keys are rejected. Existing object keys
and array length/types must remain. Migration supports equality matchers
`toBe`, `toEqual`, `toStrictEqual`, `toContainEqual`, `toHaveText`, `toHaveValue`
and `resolves`/`rejects` prefixes; range, negation, substring and unknown matchers
are conservatively rejected. The report contains `ok`, `errors`, affected case
IDs, per-action evidence, repository digests, and semantic protection evidence.

Node assertions are resolved from ESM imports of `node:assert`/`node:assert/strict` (including strict aliases). Only strict equality is supported. Rebound, shadowed, escaped or spread-argument bindings are rejected. Static local test imports are exposed in `imports` for frozen unit-suite discovery.

## Additive coverage verification

`TestCoverageGuard(node_path=...).verify(before_repo, after_repo, paths)` verifies
coverage additions in authorized **existing** test files. It returns `ok`,
`errors`, per-file `evidence`, repository digests and
`requires_independent_review: true`. The caller authenticates accepted test-plan
evidence and original author/path ownership. The guard requires existing static
test/it cases and rejects duplicate or noncanonical paths, symlink targets,
file additions/deletions, every changed file permission bit, and any changed byte
outside the authorized paths. Root `.git` metadata is excluded as for migration.

The JSON parser operation is
`{"operation":"coverage","file":"tests/example.spec.mjs","before":"...","after":"..."}`.
The bundle includes `coverage.cjs`; it parses both sources without importing or
executing them. Migration's default inspection and `literal` operations are
unchanged.

Coverage comparison ignores source positions, comments and formatting. Every
original program node remains in order, with its original imports, helpers,
executable statements, directives, case names, operations, arguments and callback
parameters. Only block bodies within existing test callbacks can grow: each old
statement stays at its original index, and new statements follow it. This rule
recurses into existing `try` blocks and nested callbacks such as `withDb`.
Expression-bodied callbacks cannot be converted into blocks. Suite definitions
cannot acquire new cases or statements.

Additional program nodes must be static imports or fresh named functions and
constants with inert initializers. Calls, getters, spreads, executable statements
and old-name rebinding are rejected at program scope. Added code cannot write to
old/ambient bindings, shadow an old name in an existing scope, escape an assertion
or framework binding, register tests/hooks, change test configuration, access
process/evaluation APIs or mutate shared prototypes. Direct old aliases of
framework and hazardous APIs are also constrained, including newly imported
assertion bindings. Member writes/deletes are conservatively rejected even on
fresh locals, because those locals can contain aliases of original objects.
Computed property accesses must use literal property names or numeric indices.
Fresh helper functions can
use their own parameters/local variables and normal `return`/`try`/`finally`
control flow, enabling HTTP helpers with reliable server cleanup. Direct control
exits in original callback blocks are rejected.

Evidence includes preserved case IDs, original assertion IDs, counts of added
statements/program nodes and both source digests. Assertion IDs retain the
existing migration parser's source-order convention: an addition in an earlier
nested block can shift the later *after* manifest indices. Coverage evidence
therefore records the original IDs whose AST nodes were preserved, rather than
requiring new and original indices to match.

This is a conservative structural guard, not a JavaScript sandbox or a proof of
coverage quality. Static imports and calls into unchanged application/dependency
code can have effects that cannot be established by comparing these two files.
Independent review must assess new imports, helpers, application calls and
assertions against the accepted test plan; diagnostic tests and all original
formal gates remain required. Unsupported dynamic test registration, aliases,
complex binding patterns or top-level initialization should be rewritten into
the supported additive form instead of weakening the guard.

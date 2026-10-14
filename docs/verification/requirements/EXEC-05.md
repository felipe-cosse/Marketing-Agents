# EXEC-05 — APIs and org-chart control surface

## Scope and authority

Prepared from main `bea8deb2d4bfdecd2ecfe7dc5d5ec8bde623ccc5` on
`req/exec-05-api-control-surface`, after the live EXEC-04 M3–M5 qualification.
Machine authority: [EXEC-05.json](EXEC-05.json).

This increment completes the explicit standalone campaign-brief and dedicated
schedule route inventory in plan 09, connects client response models to generated
OpenAPI types, and strengthens the native desktop/mobile control-surface journey.
It qualifies the present M7–M8 surface, not the historical order in which already
merged API/UI features were developed. Existing five-demo, hierarchy, approval,
artifact, identity, accessibility and network tests remain required.

### Dedicated schedule resources

[Seven routes](../../schedules.md) expose one configuration-owned schedule per
instance through the existing configuration service. The same application-level
authorization, configuration ETag/CAS, sealed workflow input, transaction and
audit event remain authoritative. There is no second scheduling state owner.

Disable preserves the instance's enabled state, other trigger kinds, saved input,
durable recurrence and historical occurrences; it does not cancel admitted work.
Enable cannot enable the whole instance. Disabled schedule edits return a conflict.
The existing synchronization path also updates retained inactive schedules after
later configuration edits, so their configuration revision cannot silently lag.
Inspection excludes private input, binding digests and lease owners. Pagination is
bounded and endpoint scoped; previews are recurrence metadata, not execution.
A fixed safe `503` detail explicitly warns that a mutation may have committed.

### Standalone campaign briefs

[Create/read resources](../../campaign-briefs.md) are immutable revision-one
records, not work admission. Distinct installation-keyed HMAC domains bind retry
identity, original normalized content equality, and the entire stored snapshot.
An exact retry returns the same identity/expiry without another creation event;
different content conflicts even if the redacted projections look identical.

Validated restricted input is stored separately from its central-redacted operator
projection under the accepted data policy. Secret-classified structured values
are rejected. The configured admitted-input TTL defaults to seven days; expired
reads return metadata without details. This is projection expiry, not physical
erasure or arbitrary free-text DLP. Every executable workflow retains its
existing forbidden-brief policy; no resolver or execution context was relaxed.

The optional repository participates in the existing write-intent unit of work.
Migration `0010` adds only the exact `campaign_brief.created` shape to two audit
CHECK constraints. Populated `0009` upgrade tests compare preserved rows, indexes,
foreign keys and unchanged constraints, and exercise repeat migration and current
ORM parity. Existing frozen `0008`→`0009` recurrence evidence remains explicit.
Verification migrates only owned temporary fixture databases.

### Generated contract and rendered journey

Client response types derive from generated DTOs; existing runtime validation of
unknown JSON is unchanged. Pure hierarchy contracts remain API-independent and
are checked for compatibility at the API boundary. Compile-time assertions are
included in TypeScript checking, not just the Vitest runtime.

Screenshot review exposed a narrow-screen run-index overflow: long immutable IDs
forced flex rows outside the viewport. The new real-browser bounds assertion
failed with 121 CSS pixels of horizontal overflow before the fix. Run links now
wrap within shrinkable content, and mobile status badges stack below the ID.
The assertion checks actual card/status bounds and scroll width, not just DOM
visibility. No identity text is truncated or hidden to make the test pass.

The real native browser journey now opens the exact persisted run through
**Runs & audit**, follows its timeline and artifact links, and checks paginated
public timeline/audit projections against the separately fetched run resource.
It compares every rendered sequence/type, linked artifact, transition, approval,
action and receipt witness. This is rendered/public-projection consistency;
independent stored-fact completeness still comes from the unchanged AC-14 tests.
No request interception or fabricated API response was added to this journey.

## Qualification command and evidence boundary

```sh
make -j1 verify-exec-05
```

The Make target directly invokes the existing `scripts.ac_16_quality run` wrapper.
It does not introduce another verification framework or select a smaller suite.
The complete 20-gate inventory includes catalog, formatting, lint, typing,
repository governance, generated-contract checks, network canaries, the full
backend suite, unchanged 17-module safety coverage, frontend units/build, and all
16 registered browser runners. Every existing deadline remains unchanged.

The wrapper requires clean committed source, checks source identity throughout,
and writes its record and coverage artifacts outside Git. A matching successful
tree-keyed record and independently observed successful full execution are both
required before merge. The short manifest audit checks trusted-local consistency,
not authenticated execution; synthetic reports or wrapper tests are insufficient.
An identical-tree non-fast-forward merge may consume that feature-tree record.

The control regression restores only Makefile for its connection witness. Its
ordinary assertion must fail when the live target is absent; imports and test
collection remain intact. Mutated/ignored/duplicated/partial hooks, missing new
integration evidence and browser inventory drift are rejected.

## Observed pre-commit checks

- Schedule API and adjacent configuration/scheduler modules: 115 passed in
  31.72 seconds, including real runtime reconstruction and two-runtime CAS.
- Campaign brief, migration and recurrence modules: 62 passed in 39.67 seconds;
  the final expanded campaign module separately passed 20 tests in 16.17 seconds.
- Product-gate controls: 24 passed in 0.89 seconds.
- Final combined schedule/brief/migration/recurrence/control selection: 116 passed
  in 58.94 seconds with socket-denial flags.
- Frontend unit suite: 55 files, 470 tests passed in 37.21 seconds.
- Frontend format, lint, TypeScript and production build passed. Vite reports its
  existing large-chunk warning; this is not a new performance qualification.
- OpenAPI regeneration/check passed for both artifacts; six generator controls
  and three actual-factory contract tests passed. Backend typing passed across
  296 source files; Ruff checked the full selected source/test inventory.
- The unchanged native browser runner passed all six transport canaries and both
  desktop/mobile real-service journeys after the overflow correction (1.6 and
  9.2 seconds of browser tests). Chromium used `http://127.0.0.1:4173`, with
  1536×1024 and 390×844 viewports. Page URL/title, meaningful content, absence of
  framework overlays, console health, exact interactions and external screenshots
  were checked. Browser plugin was not available; existing Playwright was used.
  The initial sandbox-denied listener attempt was a failure, not a qualifying run;
  the successful retry retained the same canaries and guards with local-server
  permission. Screenshot review caught overflow that the original assertions
  missed; the added assertion failed before the CSS correction and passed after.
- Repository documentation, architecture, source, secret and retained-history
  checks passed against baseline main: 119 of 124 completed requirements.
- Independent read-only schedule, brief/migration and frontend-type reviews found
  no remaining concrete blockers.

These focused results are not the full qualification result. The committed-tree
aggregate must pass before the requirement is eligible for merge. Its source-bound
records are external
to this document so recording the result does not change the qualified tree.

## First committed aggregate and bounded correction

The first complete run against feature `66859a5` stopped at the backend gate:
11 preceding gates passed, then pytest reported 4,612 passed, 33 optional
PostgreSQL skips and 35 claim-taxonomy failures in 1,560.30 seconds, within the
unchanged 1,800-second deadline. This failed run is not qualification evidence.
The feature is preserved on `codex/exec-05-checkpoint-66859a5`.

Migration `0010` exposed stale current-revision documentation and AC-17 test
literals still pinned to `0009`. The bounded correction updates the current
architecture source fact and independently reviewed test literals to `0010`,
with hypothetical drift controls advancing to `0011`. The production claim
checker, historical migration evidence, negative controls, network boundaries,
suite inventory and all deadlines are unchanged. The complete aggregate must
restart from its first gate on the corrected clean committed tree.
The complete claim-taxonomy and EXEC-05 control modules passed after correction:
61 tests and 107 subtests in 8.98 seconds; the unchanged documentation checker
independently reported revision `0010` without asserting acceptance verification.

## Limits

- SQLite and deterministic mock integrations only; no live PostgreSQL service,
  production authentication or real external delivery is established.
- Schedule keyset pages are not frozen snapshots. Disable does not retract work,
  and an unconfirmed mutation outcome must be inspected before retrying.
- Briefs remain standalone, projection expiry does not erase storage/backups, and
  structured redaction is not general free-text secret detection.
- Local guard/canary results do not replace new AC-15 kernel-isolated full-suite
  evidence. No network check, Docker setting, coverage policy or deadline changes.
- This is not DEL-01, EXEC-07 clean-state acceptance or final-source release
  acceptance. CI remains disabled; no push or next merge is implied.

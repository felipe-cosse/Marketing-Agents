# OBJ-06 operational organization chart

Requirement: the org chart is a configuration/control surface, not a static diagram.

Status: implemented and locally exercised. Committed-feature gates and a
restored-base connection witness are required before merge.

## System changes

Accepted manual submissions now open the exact run timeline through the existing
guarded router callback. Successful admission consumes only the submitted input;
it does not discard a separate configuration draft. The blocker reads the latest
dirty state synchronously, including edits made while admission is pending.
Operators can keep editing and later use the receipt link to retry the same
navigation without submitting another run. Modified link clicks keep native
browser behavior. Admission errors never navigate; failed post-mutation refreshes
produce a warning without misreporting an accepted request or saved configuration.

Graph cards and tree rows display the observed latest-run state independently of
deployment enabled/disabled state. Missing or failed observations are unavailable,
not invented never-run records. Runtime updates are passed separately from
hierarchy structure so they do not reset graph layout, viewport or selection.
Hierarchy accessibility descriptions include the runtime state; duplicate-role
ordinals and read/write classification remain present.

Pending-action links carry their exact run ID into the approval queue. The queue
subscribes to router search parameters; changing run scope resets selected review,
decision dialog and local filters. Existing exact-action approval, authorization,
redaction, immutable run snapshots and mock-receipt safety labels are preserved.

## Real-system evidence

`node apps/web/scripts/run-obj-06-e2e.mjs` creates a fresh private installation,
empty HOME, migrated/seeded SQLite database and local digest key. It launches the
actual native API, run worker, scheduler and Vite through the existing supervisor.
No application API responses or provider execution results are browser fixtures.
All mutations are made through rendered controls; native same-origin GETs inspect
the resulting persisted run and timeline without bypassing browser transport.

- Desktop, 1536x1024: select the newsletter deployment from the chart; disable and
  save it; close/reopen to verify persistence and admission gating; re-enable and
  save it; submit a mock execution; open its automatically selected timeline;
  verify revision 3, awaiting-approval state, zero tool calls and no receipt;
  follow its exact run-scoped approval; cancel confirmation without dispatch;
  explicitly confirm; inspect the sequence-ordered completed run, exactly one
  tool attempt and one durable mock receipt; return to its completed chart status.
- Mobile, 390x844: use the tree to select a READ role; keep an unsaved configuration
  draft while submitting a real dry run; Keep preserves the configuration while
  clearing submitted sensitive input; activate the receipt and explicitly Discard
  to continue; verify unchanged revision 1, one model call, zero tool calls and
  no actions/approvals; open the resulting artifact's identity, schema, provider
  provenance and authorized bounded payload. Browser storage does not contain
  the draft/input/CSRF value; the artifact page has no horizontal overflow.

Both journeys assert page identity, nonblank content, absent development overlays,
no console warnings/errors and connected interactions. Screenshots cover the
desktop organization, approval wait, completed receipt and returned chart, plus
mobile draft preservation and artifact identity/content. Visual review is local;
artifacts stay in the runner's external temporary evidence directory, not Git.

## Transport and cleanup qualification

The existing fixture's fetch-and-fulfill forwarding cannot preserve Chromium's
own Fetch Metadata for genuine local mutations. This dedicated harness uses
native transport without fabricated security headers. An exact-origin request
guard and response-stage Chromium interception reject redirects before follow.
Additional route mocks/removal/HAR registration are forbidden. Service workers
are disabled; contexts and pages receive the guard; unbound first-popup requests
fail closed. Local tripwires test HTTP, WebSocket and popup denial and all five
redirect statuses to both same-origin and off-origin destinations, with zero
destination hits. Nonlocal URL controls use in-memory delegates, never actual
external canary calls.

A failed initial browser attempt exposed a transport race: Chromium invalidated
response interception for an aborted session read during React rerender. The
guard accepts that exact continue-response error only when an independent CDP
loading-failed event confirms cancellation of the same network request. Evidence
is consumed once, event reordering waits at most 100ms, and final quiescence is
bounded at two seconds. A sixth canary rejects absent/wrong identities, other
failure reasons and errors from other interception commands; no blanket error
suppression or header fabrication is used.

Instrumented real Python child modules retain the prior OBJ-04 socket boundary;
Vite is not a kernel-isolated process. Owned process groups are reaped on success,
failure, timeout and interruption. No existing server may be reused, no dependency
is installed and no production data or host credentials are copied. The installed
Python environment, pinned Node, browser and prewarmed pnpm cache are prerequisites.

The exhaustive browser inventory owns this journey explicitly with one reviewed
custom configuration. Missing/extra specs, unknown configurations, filtered test
selection and allow-zero-test options still fail. The default static-server
configuration ignores only this real-runtime spec; the aggregate invokes its
dedicated runner rather than dropping its evidence.

## Regression and witness gates

The full frontend suite passed 457 tests across 53 files during preflight. Focused
controls cover accepted navigation, configuration edited before/during admission,
refresh failures, status-only layout stability and mounted approval route changes.
The WEB-04 browser retains input-error/retry, geometry, storage and receipt checks
using explicit typed configuration/admission fixtures; it is not the real-system
proof. ARCH-02 retains hierarchy accessibility and responsive-navigation coverage.

Final preflight with the corrected cancellation guard passed the sanitized native
gate in 32.68 seconds: both real-system journeys and all six transport canaries,
followed by successful owned-process cleanup. The ten non-browser manifest gates
also passed under the same sanitized gate environment. WEB-04 and ARCH-02 each
passed their dedicated browser regression. The committed-feature verification
repeats those gates and performs the actual restored-base witness.

An extra legacy ARCH-02 witness run exposed a pre-existing literal check against
the old aggregate script. DEL-07 had moved the runner list to its centralized
inventory. OBJ-06 updates that directly related assertion to validate real
inventory ownership and aggregate consumption; a negative control rejects removing
the architecture runner. The original thirteen committed gates passed before this
compatibility correction; the final fourteen-gate manifest also requires the
updated architecture witness. No historical manifest or policy is loosened.

The dependency-free witness executes all runtime status labels and checks bounded
lexical connections between the chart, cards/tree, guarded admission callback and
run-scoped approval route. Seventeen mutation controls include disconnected props,
commented-out wiring and a dishonest unknown-to-never-run formatter. It runs from
tracked source without node_modules or Git. Restoring only OrgChartPage.tsx to
the branch base must fail an ordinary connection assertion. These lexical checks
are not a TypeScript parser or React execution proof; the unit and browser gates
provide that separate interaction evidence.

Reproduce after activating the installed Python environment and Node 24.20.0:

```text
node apps/web/scripts/run-obj-06-e2e.mjs
node apps/web/scripts/run-obj-06-witness.mjs
make verify-requirement REQUIREMENT=OBJ-06 BASE=cd576cf91a11f6704be33c57c5d59b450c6ecc5a HEAD=<feature-commit> PYTHON=python3
make verify-history PYTHON=python3
```

All manifest gates run in the evidence verifier's sanitized environment. The
browser gates require loopback/process execution permission. CI remains disabled,
no branch is published, and no live provider, vendor SDK, production auth, other
browser or whole-system final acceptance is qualified by this requirement.

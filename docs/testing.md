# Testing and local verification

## Claim status and prerequisites

Claim: **Implemented and verified** for the scoped commands/results listed in
[verification](verification.md), not a claim that the entire release is complete.
The [requirements matrix](implementation-plan/16-requirements-traceability-matrix.md)
tracks the remaining work. GitHub CI is temporarily disabled by user direction;
local validation remains available and CI must not be treated as green.

Use Python 3.12, Node 24.20.0, pnpm 11.24.0, and uv 0.10.7. The first frozen
dependency acquisition requires registry access unless caches are prewarmed.
No required test needs a model/provider/cloud credential. Activate the pinned
Node on `PATH` before frontend/browser commands; the repository checks that
version. Do not silently use a different installed version.

```sh
make bootstrap
```

See [operations](operations.md) for native/Compose requirements. Never point
tests at a developer database or real provider to turn a failed test green.

## Existing command map

These are current targets in the [Makefile](../Makefile), not promised aliases.
Commands are read-only with respect to application installations unless their
documented fixture explicitly creates owned test storage. Builds can generate
ignored caches or build output.

| Command | Scope and important limit |
| --- | --- |
| `make verify-docs` | Required guides, repository links/anchors, documented shell Make targets, catalog counts/hash, and pinned-version consistency; not a semantic proof of all prose. |
| `make test-del-06-docs` | Documentation checker plus omission, broken-link, drift, and false-positive negative controls. |
| `make verify-source` | Original source frames and authority evidence. |
| `make verify-catalog-release` | Compile the authoritative catalog and compare its semantic hash and exact counts with the committed release lock. |
| `make test-catalog-compiler` | Catalog compiler contract tests. |
| `make verify-governance` | Existing source/tooling/architecture/history checks; history permits incomplete requirements. |
| `make verify-backend` | Current Python formatting, lint, mypy, and full pytest discovery under `tests`. |
| `make test-backend` | Full Python pytest discovery, including unit, catalog, API/integration, contract, acceptance, security, source, and tooling modules. |
| `make verify-web` | Frontend formatting/lint/types, unit tests and requirement witnesses, and production build; excludes full browser journeys. |
| `make web-test-e2e` | Existing full browser runner collection against local test services, not live providers. |
| `make test-network` | Separate Python and Node no-network canaries. |
| `make test-del-04-persistence` | SQLite migrations, seed, readiness, and native CLI contracts. |
| `make test-del-05-runtime` | Real local API/worker/scheduler process composition, claims, restart, and mock demos. |
| `make test-del-05-backup` | Paired database/key backup contracts and rejection paths. |
| `make test-del-05-compose-backup` | Scoped Docker round-trip into new storage, distinct from the startup smoke. |
| `make verify-clean REF=HEAD` | Exact committed export, fresh Docker storage, deployed demos/replay, offline suites, production browser smoke, owned cleanup. Does not test uncommitted edits. |
| `make verify` | Ordered catalog-first local static, repository, network, backend coverage, frontend and full browser gates, then source-drift comparison. Stops at the first failure. |
| `make verify-exec-06` | The unchanged complete AC-16 source-bound quality run: all 20 gates, 17 safety-coverage modules, and 16 browser runners. Requires clean committed source; no partial selector or replacement command. |
| `make test` | Catalog validation, full backend discovery, network canaries, frontend units and full browser journeys, sequential even under parallel Make. Static/coverage enforcement belongs to `verify`. |
| `make test-frontend` / `make test-e2e` | Frontend unit suite / all 16 owned browser runners, including OBJ-06. New unowned, missing, empty or filtered browser inventory fails closed. |
| `make test-contract` / `make test-integration` | Independently runnable Python contract / integration directories. Empty pytest collection fails. |
| `make test-del-07-backend` | Catalog-first backend static/full pytest and all 17 coverage thresholds, using the same gates and fresh report isolation as `verify`. |
| `make test-del-07-browser-network` | Actual Chromium positive/negative controls for the automatic exact-origin fixture, using a private loopback tripwire and no external destination. |
| `make api-contract-check` | Regenerate OpenAPI metadata and frontend types in memory; missing or changed artifacts fail without overwriting them. |
| `make api-contract-generate` | Explicitly regenerate the two checked-in API artifacts after an intentional route/schema change. |
| `make verify-repository` | DEL-07 tooling static checks, offline JSON/YAML/Markdown text policy and links, product docs, architecture, source provenance, retained main history (incomplete requirements allowed), tracked secret scan and whitespace checks. |
| `make acceptance REF=HEAD` | Requires source matching the selected commit; runs all local verification, then exact committed clean-state verification and paired Compose backup/restore. Does not automatically complete acceptance matrix rows. |

Claim: **Implemented and verified** — the recorded DEL-07 frozen-worktree
`make verify` run passed all 20 gates without changing source. Its exact counts
and limitations are in the [verification record](verification.md).
The exact committed candidate also passed all eight clean-state phases; its
revision, report hash and environment differences are recorded separately.
Claim: **Acceptance target not yet verified** — final feature branch attestation
remains separate. A `verify` pass is not an `acceptance` pass; do not omit browser
or backup gates when claiming those behaviors.

The local verifier uses a fresh external report directory and records gate
status/timing without environment values. It compares tracked and nonignored
untracked source content, modes and symlinks before/after, including failure
paths. Existing caller edits are preserved; verification does not repair files.
No prior coverage file can satisfy a new run. Pytest catalog tests run before
the remaining backend tests, and both contribute to one measured report.
Nested verification commands inherit offline/frozen dependency settings. The
acceptance precondition compares committed blob bytes, executable bits and
symlink targets directly, independent of Git's index change-detection hints.

## EXEC-06 qualification boundary

```sh
make verify-exec-06
```

This entry point invokes the existing frozen/offline `scripts.ac_16_quality run`
wrapper directly. It retains every gate, coverage threshold, network control,
browser runner, and deadline, including the 1,800-second backend and browser
deadlines. It records the exact committed source tree and a fresh local report;
report consistency is not authenticated execution evidence. The
[gate regressions](../tests/tooling/test_exec_06_product_gate.py) exercise the live
Make connection and failure forwarding, not a substitute partial verifier.

**Acceptance target not yet verified:** adding the command, passing focused
regressions, or inspecting an old report does not establish a current full pass.
The current candidate's observed results belong in the
[verification record](verification.md). Full local quality is distinct from the
four isolated clean-export suites and from
`make verify-clean REF=<commit>` / `make acceptance REF=<commit>` with fresh
Compose storage and paired backup/restore. Do not promote a host quality pass to
AC-15 kernel-isolation, clean-state, real-provider, or complete-release acceptance.
CI remains disabled by operator direction; this target neither enables nor
dispatches a GitHub workflow. Checkpoint branches are not altered by verification.

## Network boundaries and optional compatibility

Claim: **Implemented and verified** within specific tests, with limitations.
The Python pytest [autouse fixture](../tests/conftest.py) blocks external socket
and DNS use while allowing local clients. This is in-process instrumentation,
not a universal firewall for arbitrary subprocesses. Several focused targets
also use pytest-socket with Unix sockets explicitly allowed.

The clean verifier executes prebuilt Python and frontend suites with Docker
`--network none` after acquisition. Its production browser smoke rejects
requests outside the exact local origin. API/workers have no IP network;
the web ingress container retains an outbound route, as disclosed in
[security](security.md). Host Vitest's [setup](../apps/web/src/test/setup.ts)
now installs the Node guard globally: real sockets (including loopback/Unix),
HTTP(S), DNS callbacks/promises/resolvers, TLS/UDP and global fetch are denied.
Tests supply explicit in-memory fetch mocks; this repository does not use MSW.
A swallowed denial still fails the test lifecycle hook. A subprocess canary
exercises the actual setup connection with stub delegates, so even a broken
guard cannot egress during its negative control. This remains application-level
instrumentation, not an OS firewall or protection against arbitrary child tools.

## Current browser verification scope

Claim: **Implemented but not live-tested** by this documentation review — the
current [browser inventory](../apps/web/scripts/browser-evidence-inventory.mjs)
declares 16 browser runners with unfiltered execution. The 15 established browser
runners use the automatic [fixture](../apps/web/e2e/fixtures.ts). It guards default
and explicitly created contexts, blocks service workers,
permits only the configured loopback origin (and matching WebSocket origin),
and fails on denied HTTP/WebSocket attempts even if the page catches the error.
In-memory API route mocks remain usable; registered route handlers cannot
continue an unapproved request around the context guard. URL overrides are
validated, real pass-through fetches disable automatic redirects, and redirect
responses are denied before fulfillment (304 cache responses remain allowed).
This intentionally rejects even same-origin redirects; context reuse is also
unsupported and rejected. The separate actual Chromium canary checks zero
requests reached its unapproved loopback tripwire.
Demo browser mutations in those runners use explicit route mocks.

Claim: **Deterministic mock behavior** — the separate
[OBJ-06 fixture](../apps/web/e2e/obj-06-fixtures.ts) drives the real supervised
local API, workers and UI with native browser transport, not route-mocked
application responses. Its exact-origin and response-stage controls preserve
redirect and external-request denial. The
[OBJ-06 record](verification/requirements/OBJ-06.md) scopes two journeys: desktop
approval and mobile artifact inspection, not browser coverage of every role. Its
providers remain deterministic mocks, so these integrated browser-to-worker
journeys do not prove external delivery.

The historical DEL-07 run on 2026-09-11 passed 15 runners and 32 cases, as recorded
in [verification](verification.md). Those counts are not the current inventory or
a new result for the expanded suite. This documentation review establishes no
fresh full-suite pass, AC-15 no-external-call acceptance, AC-16 quality-gate pass,
or final release acceptance. Browser interception is not a universal
operating-system egress boundary for arbitrary child processes.

## Optional PostgreSQL compatibility

Optional PostgreSQL tests use an opt-in temporary Unix-socket cluster and the
project's optional `postgresql` dependency. Install the extra and supported
PostgreSQL binaries first; see the [fixture contract](../tests/support/postgresql_runtime.py).

```sh
make test-del-04-postgresql
```

Skipped compatibility cases are **skipped**, not passed. A SQLite success is
not PostgreSQL qualification. No live PostgreSQL run was performed for DEL-06.

## Results, witnesses, and unresolved release work

Claim: **Implemented and verified** for evidence validation. A requirement's
JSON manifest binds claims, changed paths, executable gates, and where required
a connection witness. The witness restores selected implementation paths to the
base while retaining the new tests; the gate must then fail. It supplements,
but does not replace, review that failures concern the claimed behavior rather
than missing dependencies. See [requirement workflow](implementation/requirement-branch-workflow.md).

Record the exact source revision, command, toolchain, pass/fail/skip counts, and
limits. Keep sanitized reports outside the repository; never upload database/key
backup bundles, raw payloads, full prompts, or credentials. Failed and interrupted
commands remain failures even if an unchanged retry later passes.

The [safety policy](verification/safety-coverage.json) pins 17 transition, hash,
approval and dispatch modules. The checker requires every measured statement
and branch, validates integer counts against individual arrays, rejects missing
modules and new exclusions, and never uses rounded percentage displays. The
existing two-line domain approval exhaustive-enum fallback is the only explicit
source-pinned exclusion; it is not silently counted as exercised.

The API snapshot retains the factory's schema and numeric bounds. The offline
[generator](../tools/api-contract/generate.mjs) uses pinned
[openapi-typescript](https://openapi-ts.dev/node) and refuses nonlocal references;
the real frontend session type consumes the generated contract while preserving
runtime validation and private CSRF handling. Its supported TypeScript 5.9.3
compiler lives in an isolated tooling workspace; the web app stays on 6.0.3.
Only the generated file's index-signature-versus-Record style preference is
exempted from ESLint; type and safety rules remain enabled.

Repository text formatting means UTF-8/LF, space indentation, final newlines,
trailing-whitespace control, and duplicate-free parsed JSON/YAML. Markdown local
links/anchors and approved external URL syntax are checked without fetching.
Original `references/` inputs are byte-preserved under the source-provenance
gate; historical evidence is not rewritten to impose a different line layout.
Frontend/generated files additionally use their pinned canonical formatter.
The tracked secret scanner targets high-confidence patterns, not every possible
secret; ignored developer files are not proof of secret absence.

Claim: **Acceptance target not yet verified** — final feature attestation and
requirement-by-requirement acceptance remain open. The recorded fresh coverage,
candidate clean-state and current-source backup results do not complete those checks.
Coverage configuration alone does not establish a passing threshold. Disabling
CI is not a waiver of local tests or proof that prior startup/deadline failures
are fixed.

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

Claim: **Acceptance target not yet verified** — the planned all-in-one `make
verify`, `make acceptance`, `make test-frontend`, `make test-contract`, and
`make test-e2e` aliases are not implemented here. `make test` currently runs only
source/tooling/network gates; it is **not** the full test aggregate. DEL-07 owns
the broader entry-point and coverage gaps. Do not omit the separate browser or
backup gate when claiming those behaviors.

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
does not globally install the Node network guard: passing the separate canary
does not prove that every ordinary host frontend test is network-isolated.

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

Claim: **Acceptance target not yet verified** — remaining release work includes
enforced safety-critical branch coverage, comprehensive task-runner aggregation,
tracked secret scanning in the normal gate, broader format/drift coverage, and
full requirement-by-requirement acceptance. Coverage configuration alone does
not establish a threshold. Disabling CI is not a waiver of local tests or proof
that the prior startup/deadline failures are fixed.

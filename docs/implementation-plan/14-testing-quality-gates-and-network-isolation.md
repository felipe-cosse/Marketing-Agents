# 14 — Testing, quality gates, and network isolation

Status: planned

Depends on: scaffold; evolves with every workstream

Unblocks: verified acceptance and release

## Objective

Build verification alongside implementation so every acceptance claim has executable evidence. Tests must be deterministic, use injected time/IDs, exercise real persistence and concurrency where required, and fail if code attempts an external network connection.

## Test pyramid and ownership

| Layer | Scope | Tools/approach | Primary failures caught |
|---|---|---|---|
| Catalog | Raw files, schemas, references, exact counts | Pytest + JSON Schema | Source/count/policy drift |
| Pure domain | State, graph, hashes, policies, provenance | Fast unit/table/property tests | Invalid transitions/invariants |
| Persistence | Migrations, constraints, UoW, races | File-backed SQLite integration | Transaction/concurrency/idempotency bugs |
| Application/runtime | Planner, worker, approval, cancellation | Ports + deterministic adapters | Orchestration/recovery bugs |
| Adapter contracts | Provider/connector behavior | Shared contract suite | Effect/schema/idempotency drift |
| API integration | Routes/auth/errors/idempotency | In-process ASGI transport | Transport/authorization regressions |
| Scheduler | Recurrence, claims, DST, restart | Fake clock + multi-session SQLite | Duplicate/misfire/time bugs |
| Frontend component | UI logic/forms/a11y | Vitest + Testing Library + MSW + axe | Rendering/interaction/error bugs |
| Browser acceptance | Cross-layer user journeys | Playwright against local stack | Wiring/accessibility/state flow bugs |
| Clean-state | Build/migrate/seed/start/restart | Isolated Compose project/volume | Missing setup/race/reproducibility bugs |

## Test directory

```text
tests/
├── conftest.py
├── catalog/
├── unit/
│   ├── architecture/
│   ├── domain/
│   ├── application/
│   ├── security/
│   ├── scheduler/
│   └── webhooks/
├── integration/
│   ├── api/
│   ├── approval/
│   ├── catalog/
│   ├── db/
│   ├── runtime/
│   ├── scheduler/
│   └── security/
├── contract/
├── acceptance/
└── fixtures/
```

Frontend tests remain colocated with features where useful; Playwright specs live under `apps/web/e2e/`.

## Determinism infrastructure

Inject:

- Clock/frozen time.
- ID/UUID generator.
- Retry planner/backoff waiter.
- Provider and connector registries.
- Failure/crash hooks.
- Identity provider/principal.
- Recurrence calculator where boundary-focused.

Tests must not use arbitrary sleeps. Worker/scheduler tests call one-step drain/claim methods and advance fake time. Golden comparisons normalize runtime IDs, timestamps, and correlation metadata while preserving business fields and hashes that should be stable.

## Network isolation

### Python

- Install an autouse `pytest-socket` policy or equivalent that disables sockets.
- API tests use `httpx` ASGI transport in-process.
- Allow no DNS or non-loopback socket by default.
- Tests requiring file-backed SQLite need filesystem access, not network.
- Mock adapters do not import HTTP clients.
- Optional real-adapter tests inject fake transports and run with network blocked.

### Frontend unit tests

- MSW uses `onUnhandledRequest: "error"`.
- Install a Node test guard that fails `net`, `http`, `https`, DNS, and global-fetch attempts outside explicitly allowed in-memory/test transports; MSW alone is not the network boundary.
- Stub browser APIs explicitly.
- No remote images, fonts, analytics, or schema fetches.

### Browser tests

- Permit only the explicit loopback origin for the local test stack.
- Abort/fail every non-loopback request through Playwright routing.
- Record an assertion that no external request was observed.

### Offline CI

- Run prebuilt test images under `--network none` for backend tests and frontend format/lint/type/unit/build after frozen dependencies are installed.
- Run browser tests on a scoped local bridge/loopback and deny external destinations.
- Fail if environment selects a real provider/connector or network opt-in.

## Catalog suite

Required:

- Structural JSON/YAML schema validation.
- Local reference confinement.
- Exact `5/12/36/43` and every function/department count.
- Unique stable IDs and display order.
- Community exactly two instances per seven templates.
- Non-Community exactly one instance per template.
- Template completeness and instance field ownership.
- Input/output schema compilation and positive/negative/boundary fixtures.
- Capability/effect/approval consistency.
- Bounded budgets/retries/timeouts/content.
- Deterministic catalog hash.
- Repeatable seed and local config preservation.

Catalog gate runs before runtime tests in CI.

## Domain and property-focused suite

- Full legal/illegal transition table.
- Cancellation from every nonterminal state.
- Terminal immutability.
- DAG cycle/missing dependency/limit/ordering behavior.
- Canonical action hash equivalence/difference properties.
- Approval state, expiry, consumption, and actor policy.
- Budget/retry/rate-limit bounds.
- Artifact immutability and provenance completeness.
- Schedule/occurrence identity values.

Target 100% branch coverage for transition, canonicalization/hash, approval decision, and action-dispatch guard modules. Set broader repository thresholds only after an initial baseline, but never lower them to land untested safety code.

## Persistence and concurrency suite

Use a temporary file-backed SQLite database with independent connections.

- Fresh migration to head.
- SQLAlchemy metadata/Alembic drift.
- Foreign keys and restrictive history deletion.
- Seed rollback/idempotency/config preservation.
- Work/action/decision/occurrence/receipt uniqueness.
- State plus audit atomicity with injected audit failure.
- Optimistic config/run/action conflicts.
- Concurrent approval decisions and consumption.
- Concurrent run/step/schedule claims.
- Artifact immutability.
- Snapshot stability after catalog/config changes.

An optional CI job may run compatibility tests against PostgreSQL, but SQLite remains the required acceptance database.

## Runtime and failure-injection suite

Inject failures at:

- Before/after worker lease commit.
- Before/after model call.
- Before/after artifact commit.
- Before/after approval decision/consume.
- Before connector call.
- After mock side effect but before response/local success.
- Before/after schedule occurrence transaction.
- During cancellation.

Assert recovery uses persisted state and never repeats a committed artifact/action/occurrence.

## Approval/idempotency suite

Direct acceptance assertions:

- Mutating run creates proposed actions before calls.
- Zero calls before all approvals.
- One of two Email approvals still means zero calls.
- Unauthorized actor cannot decide.
- Expected-hash mismatch fails.
- Payload/action/destination/binding modification fails.
- Expired, rejected, consumed, superseded, or reused request fails.
- Concurrent decisions produce one winner.
- Crash retry returns one mock receipt/side effect.
- Changed action requires new action, key, and approval.
- Cancellation never claims rollback.

## Webhook and scheduler suite

Webhook:

- Valid/invalid/missing/stale/future signature.
- Raw-body verification before parsing.
- Identical replay returns original resources.
- Same source event ID/different body conflicts.
- Oversize/depth/schema rejection.
- Payload cannot alter routing/capability/destination.

Scheduler:

- Original timezone and UTC next time.
- Two-worker claim race.
- Lease expiry and restart before/after commit.
- Stable occurrence/work deduplication.
- `skip` and `run_once` semantics.
- Spring-forward nonexistent and fall-back ambiguous time policy.
- Transactional next-occurrence advance.

Local key and recovery:

- One shared initializer is exercised through Compose and native entry points.
- Native and Compose restart reuse the same key/version/fingerprint.
- Paired database/key backup restores into new scoped storage and preserves webhook replay identity.
- Missing half, fingerprint/version mismatch, interrupted staging, or permissive key mode fails closed.
- No secret-bearing backup bundle is uploaded as test/CI evidence.

## Adapter contract suite

Run the same contract against every registered implementation:

- Typed request/result schema.
- Deadline/cancellation.
- Error classification.
- Determinism for mocks.
- Capability/effect metadata.
- Redaction metadata.
- Write authorization proof.
- Idempotency behavior.
- No implicit retry/fallback.
- No network for mock/fake-transport tests.

## API suite

- Liveness versus readiness semantics.
- Session/local mode/fail-closed configuration.
- Role/permission matrix.
- Exact catalog/hierarchy projection.
- Deployment-only configuration and stale revision.
- Dry-run input validation/idempotency.
- Run/cancellation/timeline/artifact projections.
- Approval request/decision conflicts.
- Webhook and schedule endpoints.
- Audit pagination/redaction.
- Stable `application/problem+json` codes/pointers.
- OpenAPI snapshot and generated client drift.

## Frontend unit/component suite

- Exact hierarchy/source order and Community multiplicity.
- Deterministic layout and ancestor-preserving filters.
- Keyboard roving focus and graph/tree exclusivity.
- Detail completeness/template-instance separation.
- Schema form fields, errors, formats, bounds, and sensitivity.
- Config dirty/save/conflict behavior.
- Approval exact-action display and disabled/conflict states.
- Timeline stable sequence.
- Artifact provenance, advisory labels, escaping, and sanitization.
- Visible safe-mode/local-identity labels.
- Reduced motion and responsive behavior.
- Automated accessibility scans.

## Browser acceptance journeys

1. Clean load shows safe-mode banner and exact catalog counts.
2. Desktop pan/zoom/fit/search opens a role detail.
3. Both Community instances are distinct but share a template.
4. Mobile defaults to semantic tree.
5. Keyboard-only navigation reaches form/run/artifact.
6. Social draft completes with provenance.
7. Blog review completes without write calls.
8. Community draft says not sent/not externally scheduled.
9. Partnership recommendation says advisory.
10. Email waits with two approvals and zero calls.
11. Invalid Email approval paths fail safely.
12. Both valid approvals permit exactly two mock writes and completion.
13. Timeline contains every transition/approval/action.
14. XSS fixture renders inertly.
15. No non-loopback browser request occurs.

## Static quality gates

Backend:

- Ruff format check.
- Ruff lint.
- Strict mypy with documented narrow exceptions.
- Import-boundary test.
- Alembic metadata drift check.

Frontend:

- Prettier check.
- ESLint.
- TypeScript `tsc --noEmit`.
- Production Vite build.
- Generated OpenAPI client clean.

Repository:

- YAML/JSON/Markdown formatting and local-reference link checks. The checker must not fetch external URLs; it validates local targets and external URL syntax/allowlist offline.
- Secret scan on tracked files/diff.
- Docker/Compose config/build checks.
- No uncommitted generated changes after verification.

## Planned root commands

```text
make catalog-validate
make format-check
make lint
make typecheck
make test
make test-backend
make test-frontend
make test-contract
make test-integration
make test-e2e
make acceptance
make verify
make verify-clean
```

`make verify` runs deterministic local gates. `make verify-clean` additionally builds and starts from isolated tracked inputs and fresh storage.

## CI jobs

1. `catalog-and-backend-static`
2. `backend-unit-integration-offline`
3. `frontend-static-and-unit-offline`
4. `browser-acceptance-loopback-only`
5. `clean-state-compose`
6. Optional `postgres-compatibility`

Use frozen locks, minimal GitHub token permissions, SHA-pinned third-party actions, cache keys based on lock hashes, and sanitized reports only.

## Ordered implementation tasks

1. Install the autouse Python socket blocker, Node socket/HTTP/DNS/fetch guard, and frontend unhandled-request failure policy before adapter code is added.
2. Add injected clock/ID/retry/failure/identity fixtures shared across backend suites.
3. Make catalog validation/count/policy tests the first CI hard gate.
4. Add pure-domain state/graph/hash/approval/provenance tests and enforce exhaustive branches for safety-critical modules.
5. Add file-backed SQLite migration, constraint, transaction, concurrency, and seed tests.
6. Add shared provider/connector contract suites and require every mock/optional adapter to pass them.
7. Add application/runtime failure-injection, cancellation, approval, idempotency, webhook, scheduler, and retention suites.
8. Add API integration and generated OpenAPI/client drift checks.
9. Add frontend component, schema-form, accessibility, artifact-safety, and responsive tests.
10. Add loopback-only Playwright journeys for the complete hierarchy and five demos.
11. Add isolated Compose clean-state/restart acceptance and a no-egress test phase using prebuilt images after the separately identified dependency/base-image acquisition phase.
12. Map every passing gate to plan 16 and record exact results without lowering thresholds to hide failures.

## Exit criteria

- Every prompt acceptance item has at least one named automated test or clean-state command.
- Catalog gate passes before runtime/UI gates.
- Safety-critical transition/hash/approval/dispatch modules have exhaustive branch coverage.
- Concurrency tests use independent file-backed SQLite sessions.
- Time/retry tests use injected time, not sleeps.
- Mock/default/CI tests cannot access external network.
- Backend/frontend static, unit, integration, contract, browser, and clean-state gates pass.
- Generated API/migration/catalog artifacts are clean after verification.
- Failures produce reproducible diagnostics without secrets or PII.

# 16 — Requirements traceability matrix

Status: planned baseline

Source: [`IMPLEMENTATION_PROMPT.md`](../../IMPLEMENTATION_PROMPT.md)

## Purpose

Map every material prompt requirement to its detailed plan, intended implementation location, and verification evidence. All rows begin as `planned`. A row becomes `verified` only when the referenced test/command has passed and the result is recorded in `docs/verification.md`.

Allowed status values:

- `planned`
- `implemented`
- `verified`
- `blocked`
- `deferred`

## Source, objective, and catalog

| ID | Requirement | Plan | Planned implementation | Verification | Status |
|---|---|---|---|---|---|
| SRC-01 | Inspect repository guidance and all six local frames. | 00 | Evidence inventory and `docs/assumptions.md` | Evidence checklist/manual review | planned |
| SRC-02 | Treat hierarchy/names/role intent as authoritative; do not infer hidden integrations/duplicate reasons. | 00, 02 | Source references, confidence, implementation notes | Catalog source-note/duplicate tests | verified |
| SRC-03 | Record material assumptions. | 00, 15 | `docs/assumptions.md`, ADRs | Documentation review | verified |
| OBJ-01 | Represent the exact five-department organization. | 02 | Compiled catalog | Exact-count/distribution tests | verified |
| OBJ-02 | Display an interactive source-modeled org chart. | 12 | Org chart canvas/tree | Component + Playwright hierarchy tests | planned |
| OBJ-03 | Safely configure, simulate, approve, and audit work. | 05, 06, 09, 12, 13 | Application services/APIs/UI | Cross-layer acceptance | planned |
| OBJ-04 | Run locally without cloud credentials or real external calls. | 01, 10, 14, 15 | Mock defaults, Compose | No-network + clean-state tests | planned |
| OBJ-05 | Allow later real adapters without redesigning core. | 03, 10 | Ports/registry/layer boundaries | Architecture + adapter-contract tests | planned |
| OBJ-06 | Org chart is configuration/control surface, not a static diagram. | 09, 12 | Details/config/forms/approvals/timeline | Browser acceptance | planned |
| CAT-01 | Seed exactly 5 departments, 12 functions, 36 templates, 43 instances. | 02 | `catalog/v1`, compiler/seeder | `test_exact_counts.py` | verified |
| CAT-02 | Community has 7 templates and 14 instances. | 02, 12 | Shared template refs + ordinal instances | Multiplicity/API/UI tests | verified |
| CAT-03 | Stable unique instance IDs and no invented duplicate purpose. | 00, 02 | ID convention/instance YAML | Stable-ID/field-ownership tests | verified |
| CAT-04 | Preserve every named Social media role and function/count. | 02 | Social media template/instance files | Function distribution/inventory test | verified |
| CAT-05 | Preserve every named Blog & SEO role and function/count. | 02 | Blog & SEO files | Function distribution/inventory test | verified |
| CAT-06 | Preserve every named Email role and function/count. | 02 | Email files | Function distribution/inventory test | verified |
| CAT-07 | Preserve every named Community role twice. | 02 | Community files | Exact ordinal inventory test | verified |
| CAT-08 | Preserve every named Partnerships role and function/count. | 02 | Partnerships files | Function distribution/inventory test | verified |

## Architecture, control plane, and domain

| ID | Requirement | Plan | Planned implementation | Verification | Status |
|---|---|---|---|---|---|
| ARCH-01 | Use Python 3.12/FastAPI/Pydantic/SQLAlchemy/Alembic when greenfield. | 01 | Backend workspace | Toolchain/build checks | verified |
| ARCH-02 | Use React/TypeScript/Vite and accessible graph/tree. | 01, 12 | Web workspace/chart | Build/a11y/browser tests | planned |
| ARCH-03 | SQLite default and configurable PostgreSQL URL without domain redesign. | 04 | DB config/repositories | SQLite suite + optional PostgreSQL job | planned |
| ARCH-04 | Version-controlled validated YAML/JSON catalog. | 02 | `catalog/` + JSON Schema compiler | Catalog suite | implemented |
| ARCH-05 | Framework-independent orchestration core. | 03, 05 | Domain/application layers | Import-boundary tests | planned |
| ARCH-06 | `LLMProvider`, default deterministic mock, opt-in real provider. | 10 | Provider port/registry | Provider contracts/settings tests | planned |
| ARCH-07 | Typed connector interfaces/mocks for all eight families. | 10 | Connector ports/mocks | Connector contract matrix | planned |
| ARCH-08 | Domain/runtime/adapters/API/UI/tests visibly separated. | 01, 17 | Monorepo layout | Architecture test/review | planned |
| ORCH-01 | Visible root remains Marketing Agents; orchestrator is not instance 44. | 00, 12 | Hierarchy/control-plane badge | Count/UI assertions | planned |
| ORCH-02 | Validate incoming work. | 05, 07 | Intake/planner | Schema/intake tests | planned |
| ORCH-03 | Construct explicit dependency graph. | 03, 05 | Workflow/DAG models | Graph/unit/runtime tests | planned |
| ORCH-04 | Select only necessary instances. | 05 | Deterministic router | Selection tests/snapshots | planned |
| ORCH-05 | Pass typed artifacts, not unbounded chat. | 03, 05 | Bindings/artifacts | Binding/provenance tests | planned |
| ORCH-06 | Enforce budgets, timeouts, rate limits, retries, cancellation. | 03, 05 | Policies/executor | Boundary/failure tests | planned |
| ORCH-07 | Deduplicate events and external actions. | 06, 07, 08 | Unique keys/receipts | Replay/crash/race tests | planned |
| ORCH-08 | Pause at approval boundaries. | 06 | Approval/action service | Zero-call tests | planned |
| ORCH-09 | Persist auditable state for every step. | 04, 05, 13 | Transition/audit tables/services | Timeline completeness/fault tests | planned |
| DOM-01 | Model all named core entities. | 03 | Domain entity modules | Entity/invariant tests | planned |
| DOM-02 | Every template has ID/name/department/function/purpose/instructions. | 02 | Template schema/files | Completeness test | verified |
| DOM-03 | Every template has typed input/output schemas. | 02 | Per-template JSON Schemas | Compilation/fixture tests | verified |
| DOM-04 | Every template has tools/triggers/classification/approval/retry/timeout/budget/source notes. | 02 | Template schema/files | Completeness/policy tests | verified |
| DOM-05 | Every instance references one template and adds deployment fields only. | 02, 04 | Instance schema/config split | Field-ownership/config tests | verified |

## Lifecycle, approval, idempotency, and scheduling

| ID | Requirement | Plan | Planned implementation | Verification | Status |
|---|---|---|---|---|---|
| RUN-01 | Persist required `received` through terminal lifecycle. | 03, 04, 05 | State machine/transition rows | Exhaustive transition tests | planned |
| RUN-02 | Read-only may execute directly; writes must await approval. | 03, 05, 06 | Effect-aware planning | Direct/zero-call tests | planned |
| RUN-03 | Cancellation is best effort and cannot reverse completed actions. | 03, 05, 06 | Cancellation service/timeline | Cancellation race tests | planned |
| RUN-04 | Work idempotency uses source/event/instance or stronger. | 04, 07 | Work unique constraint | Manual/webhook replay tests | planned |
| RUN-05 | Every action has a unique persisted key passed to connector. | 04, 06, 10 | External action/dispatcher | Constraint/contract tests | planned |
| RUN-06 | Persist redacted inputs, transitions, selections, attempts, approvals, actions, outputs, errors, timestamps. | 04, 05, 13 | Runtime/audit schema | Timeline/redaction tests | planned |
| RUN-07 | Approval authorizes one immutable proposed action. | 03, 06 | Action hash/request model | Hash/tamper/reuse tests | planned |
| RUN-08 | Store action/destination/redacted payload/hash/run/step/actor/decision/scope/times/expiry/use. | 04, 06, 09 | Tables/API projections | Persistence/API tests | planned |
| RUN-09 | Payload changes invalidate approval. | 03, 06 | Canonical hash recompute | Payload mutation tests | planned |
| RUN-10 | Approval endpoints require authorized actor. | 06, 09 | Identity/authorization | Role matrix tests | planned |
| SCHED-01 | Store original IANA timezone and next UTC run. | 08 | Schedule table/calculator | Timezone tests | planned |
| SCHED-02 | Only one worker claims an occurrence. | 04, 08 | Lease/CAS | Concurrent claim test | planned |
| SCHED-03 | Stable occurrence ID feeds run idempotency. | 08 | Occurrence/work ingress | Duplicate/restart tests | planned |
| SCHED-04 | Support explicit `skip` and `run_once`. | 08 | Misfire service | Misfire tests | planned |
| SCHED-05 | Recompute/persist next occurrence transactionally. | 04, 08 | UoW transaction | Fault-injection test | planned |
| SCHED-06 | Recover due schedules without duplicates. | 08 | Lease expiry/unique keys | Restart tests | planned |

## Backend and frontend functions

| ID | Requirement | Plan | Planned implementation | Verification | Status |
|---|---|---|---|---|---|
| API-01 | Health and readiness endpoints. | 09 | Health routes | Health tests | planned |
| API-02 | Read complete catalog/hierarchy. | 09 | Catalog routes | Exact API count tests | planned |
| API-03 | Configure instances without modifying seeded templates. | 02, 04, 09 | Config table/PATCH | Field/reseed/revision tests | planned |
| API-04 | Manual dry-run endpoint. | 07, 09 | Dry-run route/intake | API + demo tests | planned |
| API-05 | Webhook with signature hooks/idempotency. | 07, 09 | Webhook route/verifier | Signature/replay tests | planned |
| API-06 | Create/inspect/approve/reject immutable approval requests with auth. | 06, 09 | Approval routes/services | Approval API suite | planned |
| API-07 | Inspect runs, artifacts, and audit. | 09 | Run/artifact/audit routes | API/timeline tests | planned |
| API-08 | Validate every input and structured output. | 02, 05, 09, 10 | Schema validation boundaries | Negative schema tests | planned |
| API-09 | Bound retries/timeouts and expose terminal errors. | 05, 09 | Policies/problem responses | Failure tests | planned |
| WEB-01 | Interactive pan/zoom chart with all 5/12/43. | 12 | OrgChart canvas/layout | UI/browser exact-count tests | planned |
| WEB-02 | Search/filter department/function/status/capability. | 12 | Toolbar/query state | Component/browser tests | planned |
| WEB-03 | Agent details include all required metadata/recent runs. | 09, 12 | Detail API/panel | Completeness tests | planned |
| WEB-04 | Dry-run form generated from input schema. | 12 | SchemaForm | Field/server-error tests | planned |
| WEB-05 | Approval queue. | 12 | Approval feature | UI/Email tests | planned |
| WEB-06 | Run timeline and artifact viewer. | 12 | Runs/artifacts features | Timeline/XSS tests | planned |
| WEB-07 | Responsive list/tree fallback. | 12 | OrgTreeFallback | Mobile Playwright test | planned |
| WEB-08 | Keyboard/focus/labels/contrast/reduced motion. | 12, 14 | Accessibility implementation | Axe + keyboard/browser tests | planned |
| WEB-09 | Preserve hierarchy without third-party branding. | 00, 12 | Tokens/neutral icons | Visual/manual/no-remote-assets check | planned |

## Demos and guardrails

| ID | Requirement | Plan | Planned implementation | Verification | Status |
|---|---|---|---|---|---|
| DEMO-01 | Social idea to draft artifact. | 11 | Social workflow | Social acceptance | planned |
| DEMO-02 | Blog metadata to SEO/content review. | 11 | Blog workflow | Blog acceptance | planned |
| DEMO-03 | Email signup to subscriber/onboarding actions. | 11 | Two actions + welcome artifact | Email acceptance | planned |
| DEMO-04 | Community event signup to reminder draft. | 11 | Reminder draft/recommended time | Community acceptance | planned |
| DEMO-05 | Partnership application to structured recommendation. | 11 | Advisory review workflow | Partnerships acceptance | planned |
| DEMO-06 | Writes await exact approval; Email has zero calls before and mocks after. | 06, 11 | All-approvals barrier | Email negative/positive/crash tests | planned |
| SAFE-01 | Default dry-run/mock providers/connectors. | 01, 10 | Settings/registry/UI banner | Startup/session tests | verified |
| SAFE-02 | No publish/send/enroll/unsubscribe/CRM/CMS/calendar/fulfillment without approval. | 02, 06, 10 | Capability/policy/dispatcher | Catalog + action tests | planned |
| SAFE-03 | No unofficial scraping/terms-violating automation. | 00, 10, 11 | No generic fetch; supplied fixtures | Adapter inventory/review | planned |
| SAFE-04 | External content is untrusted, not executable instructions. | 05, 10, 13 | Trust-separated requests | Injection tests | verified |
| SAFE-05 | Separate system instructions/retrieved/tool content. | 10, 13 | `LLMRequest` fields | Provider contract tests | planned |
| SAFE-06 | Enforce allowlists, schemas, URL/content/rate/time bounds. | 02, 05, 13 | Validators/policies | Boundary/security tests | planned |
| SAFE-07 | Minimize/redact PII and configure retention. | 04, 13 | Redactor/retention | Canary/retention tests | planned |
| SAFE-08 | Preserve artifact provenance. | 03, 05, 11 | Artifact/provenance model | Provenance tests | planned |
| SAFE-09 | Partner/churn outputs remain advisory. | 02, 11, 12 | Schemas/UI labels | Demo/UI tests | planned |
| SAFE-10 | Never commit secrets; safe `.env.example`. | 01, 13, 15 | Config/ignore/CI scan | Secret scan/settings tests | verified |
| SAFE-11 | Tests never call real providers/services. | 10, 14 | Mocks/network blockers | Offline/no-network gates | implemented |

## Deliverables

| ID | Requirement | Plan | Planned implementation | Verification | Status |
|---|---|---|---|---|---|
| DEL-01 | Working backend/frontend. | 01–15 | `apps/api`, `apps/web` | Clean-state/browser smoke | planned |
| DEL-02 | Validated 36-template/43-instance catalog. | 02 | `catalog/v1` | Catalog suite | verified |
| DEL-03 | Deterministic mock model/connectors. | 10 | Adapter mocks | Contract/demo tests | planned |
| DEL-04 | Migrations and repeatable seed. | 04 | Alembic/seeder | Fresh/reseed tests | planned |
| DEL-05 | One-command local startup; Docker if useful. | 15 | Compose/Makefile | `make verify-clean` | planned |
| DEL-06 | README, architecture, assumptions, security docs. | 15 | Required docs | Documentation checklist | planned |
| DEL-07 | Required unit/catalog/API/state/idempotency/approval/adapter/frontend tests. | 14 | Full test tree | `make verify` | planned |
| DEL-08 | Format/lint/type/test task runner commands. | 01, 14, 15 | Makefile/configs | Static gates | implemented |

## Acceptance criteria matrix

| ID | Acceptance criterion | Primary plan | Verification evidence required | Status |
|---|---|---|---|---|
| AC-01 | Clean checkout starts with one command/no cloud credentials. | 15 | Successful isolated `make verify-clean` record | planned |
| AC-02 | Catalog API exposes exactly 5/12/36/43. | 02, 09 | Catalog and API exact-count tests | planned |
| AC-03 | Department instance counts are 12/6/5/14/6. | 02, 09 | Distribution tests/API response | planned |
| AC-04 | UI complete hierarchy and 14 Community instances from 7 templates. | 12 | Component + desktop/mobile browser assertions | planned |
| AC-05 | Stable instance IDs are unique. | 02 | Full inventory uniqueness test | planned |
| AC-06 | Every template has schemas, tools, approval policy. | 02 | Template completeness/schema/policy suite | planned |
| AC-07 | Five valid provenance-linked demos; writes wait and execute only after exact approval. | 06, 11 | Five acceptance tests + Email call ledger | planned |
| AC-08 | Mutating action cannot cross approval boundary. | 06 | Dispatcher proof/zero-call negative tests | planned |
| AC-09 | Reused, expired, or changed approval rejected. | 06 | Approval negative tests | planned |
| AC-10 | Identical webhook replay creates/executes no duplicate work. | 07 | Replay test with original resource IDs/call counts | planned |
| AC-11 | Crash/retry after approval cannot repeat same mock action. | 06 | Fault-injection receipt/count test | planned |
| AC-12 | Scheduled occurrence runs once across workers, persists UTC next time, recovers misfire. | 08 | Race/restart/misfire/DST suite | planned |
| AC-13 | Queued cancellation cancels; executing is honest best effort. | 03, 05, 06 | Cancellation state/action timeline tests | planned |
| AC-14 | Every transition and approval appears in audit timeline. | 04, 05, 13 | Timeline completeness + audit rollback test | planned |
| AC-15 | No test performs external network call. | 14 | Socket/MSW/Playwright/offline CI evidence | planned |
| AC-16 | Backend/frontend tests, formatting, lint, typing pass. | 14 | `make verify` record | planned |
| AC-17 | Docs distinguish implementation, mock, targets, assumptions, credentials. | 15 | Documentation claim-taxonomy review | planned |

## Execution-approach compliance

| ID | Prompt approach | Planned gate | Status |
|---|---|---|---|
| EXEC-01 | Inspect references/guidance. | Plan 00 evidence inventory | verified |
| EXEC-02 | Write architecture decision/assumptions before broad implementation. | M0 ADR/document gate | verified |
| EXEC-03 | Catalog schema/seed/count tests first. | M2 hard gate | verified |
| EXEC-04 | State machine/approval/mocks next. | M3–M5 gates | planned |
| EXEC-05 | APIs and org-chart control surface. | M7–M8 | planned |
| EXEC-06 | Demos/tooling/docs. | M8–M9 | planned |
| EXEC-07 | Complete clean-state verification. | `make verify-clean` | planned |
| EXEC-08 | Report files/commands/results/assumptions without overclaim. | `docs/verification.md` + handoff | planned |

## Maintenance rule

When a requirement changes:

1. Update this matrix and the detailed subject plan first.
2. Update catalog/domain/API/test contracts together.
3. Link the implementation change and exact test evidence.
4. Never mark `verified` from code review alone.
5. If deferred, state why, the risk, and whether deferral affects acceptance.

## Exit criteria

- Every material source, objective, catalog, architecture, orchestration, domain, lifecycle, scheduler, API, frontend, demo, safety, deliverable, acceptance, and execution requirement has a row.
- Every row names a detailed plan, implementation destination, and verification path.
- All 17 explicit acceptance criteria remain individually visible.
- Status vocabulary is consistent and no row is marked verified without recorded evidence.
- Deferred or blocked rows state their acceptance impact before release.

# 17 — Proposed file tree and build order

Status: planned

Depends on: all subject plans

Purpose: final integration blueprint and parallel-work guide

## Target repository tree

```text
marketing-agents/
├── .dockerignore
├── .env.example
├── .gitignore
├── .nvmrc
├── .python-version
├── .github/
│   └── workflows/
│       └── ci.yml
├── Makefile
├── README.md
├── compose.yaml
├── pyproject.toml
├── uv.lock
├── pnpm-lock.yaml
├── pnpm-workspace.yaml
├── apps/
│   ├── api/
│   │   ├── alembic.ini
│   │   ├── alembic/
│   │   │   ├── env.py
│   │   │   └── versions/
│   │   │       ├── 0001_catalog_and_deployment.py
│   │   │       ├── 0002_work_runs_steps_and_artifacts.py
│   │   │       ├── 0003_approvals_external_actions_and_audit.py
│   │   │       ├── 0004_webhooks_schedules_and_occurrences.py
│   │   │       └── 0005_runtime_indexes_rate_limits_and_retention.py
│   │   └── src/
│   │       └── marketing_agents/
│   │           ├── __init__.py
│   │           ├── config.py
│   │           ├── domain/
│   │           │   ├── ids.py
│   │           │   ├── enums.py
│   │           │   ├── errors.py
│   │           │   ├── entities/
│   │           │   ├── policies/
│   │           │   ├── state_machine.py
│   │           │   ├── graph.py
│   │           │   ├── canonical_json.py
│   │           │   ├── action_hash.py
│   │           │   └── provenance.py
│   │           ├── application/
│   │           │   ├── commands.py
│   │           │   ├── queries.py
│   │           │   ├── dto.py
│   │           │   ├── ports/
│   │           │   ├── orchestration/
│   │           │   └── services/
│   │           ├── infrastructure/
│   │           │   ├── catalog/
│   │           │   ├── db/
│   │           │   │   ├── models/
│   │           │   │   └── repositories/
│   │           │   ├── adapters/
│   │           │   │   ├── llm/
│   │           │   │   └── connectors/
│   │           │   ├── identity/
│   │           │   ├── scheduling/
│   │           │   └── webhooks/
│   │           ├── security/
│   │           ├── observability/
│   │           ├── api/
│   │           │   ├── app.py
│   │           │   ├── middleware/
│   │           │   ├── schemas/
│   │           │   └── routes/
│   │           ├── workers/
│   │           └── demos/
│   └── web/
│       ├── package.json
│       ├── tsconfig.json
│       ├── vite.config.ts
│       ├── playwright.config.ts
│       ├── index.html
│       ├── e2e/
│       └── src/
│           ├── main.tsx
│           ├── app/
│           ├── api/
│           │   └── generated/
│           ├── features/
│           │   ├── session/
│           │   ├── catalog/
│           │   ├── org-chart/
│           │   ├── instance-detail/
│           │   ├── instance-config/
│           │   ├── dry-run/
│           │   ├── approvals/
│           │   ├── runs/
│           │   ├── artifacts/
│           │   ├── audit/
│           │   └── demos/
│           ├── components/
│           ├── hooks/
│           ├── styles/
│           └── test/
├── catalog/
│   ├── schema/
│   └── v1/
│       ├── manifest.yaml
│       ├── departments.yaml
│       ├── functions.yaml
│       ├── tool-capabilities.yaml
│       ├── approval-policies.yaml
│       ├── templates/
│       ├── instances/
│       ├── prompts/
│       └── schemas/
├── docker/
│   ├── api.Dockerfile
│   ├── web.Dockerfile
│   └── web.conf
├── docs/
│   ├── adr/
│   ├── implementation-plan/
│   ├── architecture.md
│   ├── assumptions.md
│   ├── security.md
│   ├── identity-and-authorization.md
│   ├── data-handling.md
│   ├── adapter-contracts.md
│   ├── catalog-authoring.md
│   ├── demos.md
│   ├── testing.md
│   ├── operations.md
│   └── verification.md
├── references/
├── scripts/
│   ├── entrypoint-api.sh
│   ├── entrypoint-run-worker.sh
│   ├── entrypoint-scheduler.sh
│   ├── migrate-and-seed.sh
│   └── verify_clean_state.sh
└── tests/
    ├── conftest.py
    ├── catalog/
    ├── unit/
    ├── integration/
    ├── contract/
    ├── acceptance/
    └── fixtures/
```

## Ownership boundaries

| Area | Owns | Must not own |
|---|---|---|
| `catalog/` | Source-backed role data, prompt/schema refs, capability/policy declarations | Runtime state or credentials |
| `domain/` | Pure entities/invariants/hashes/transitions/provenance | ORM, HTTP, SDK, environment loading |
| `application/` | Use cases, ports, planner, policies, orchestration | Framework route/ORM details |
| `infrastructure/catalog` | File loading/validation/seed mapping | Source catalog decisions hidden in code |
| `infrastructure/db` | ORM, repositories, UoW, dialect claims | Domain policy decisions |
| `infrastructure/adapters` | Provider/connector implementations and registry | Tool selection or approval policy |
| `api/` | Transport, auth dependency, DTO mapping, errors | Workflow execution/business mutation logic |
| `workers/` | Claim loops and application-service invocation | Duplicate orchestration rules |
| `demos/` | Explicit workflow definitions/fixtures | General routing or hidden integrations |
| `web/` | Presentation, forms, operational control surface | Reimplemented backend policy/DTO authority |
| `tests/` | Executable evidence and failure injection | Network-dependent production checks |

## Sequential build order

### Phase 0 — Evidence and decisions

Files:

- `docs/assumptions.md`
- ADRs 0001–0008
- Traceability baseline

Gate:

- All source roles/duplicates accounted for.
- Lifecycle/Email/identity/scheduler/retention decisions explicit.

### Phase 1 — Scaffold and safe defaults

Files:

- Root manifests, lockfiles, Makefile, `.env.example`, ignore files.
- Minimal backend/frontend apps.
- Config, import-boundary test, Docker/CI skeleton.
- Threat/trust-boundary baseline, sensitivity vocabulary, recursive redactor, redacted audit-metadata contract, local digest-key lifecycle/initializer, Python/Node network guards, and canary tests.

Gate:

- Services boot with mock/offline/local modes.
- Format/lint/type/test/build commands run.
- Redaction canaries, digest-key generation/permission/restart tests, and no-network guards pass before broad audit/logging/adapter work.

### Phase 2 — Catalog first

Files:

- Catalog structural schemas.
- Capabilities/policies.
- All department/function/template/instance/prompt/input/output files.
- Loader/compiler/CLI and tests.

Gate:

- Exact count/reference/schema/policy/hash tests pass.

Do not begin product UI with manually invented fixtures beyond the compiled API projection after this gate.

### Phase 3 — Pure domain

Files:

- IDs/entities/policies/state/graph/hash/provenance/errors.

Gate:

- Exhaustive transition, graph, hash, approval, artifact, and boundary tests pass without DB/framework imports.

### Phase 4 — Persistence

Files:

- ORM records, Alembic revisions, repositories, UoW, seeder, monotonic timeline allocator, digest-key fingerprint/version metadata, and transactional audit implementation using the already tested redactor.

Gate:

- Fresh migration/reseed/config preservation/constraint/transaction tests, digest key/database pairing checks, and audit-redaction rollback canaries pass.

### Phase 5 — Ports and deterministic mocks

Files:

- Clock/identity/LLM/connector/signature/recurrence ports.
- Local identity adapter/principals, mock provider/read-only connector stage, write interfaces/receipt store, and registry.

Gate:

- Contract suite passes with external network blocked.

### Phase 6 — Planner and run worker

Files:

- Workflow registry/router/planner/graph bindings/executor/budgets/retries/cancellation.
- Run worker claim loop.

Gate:

- Read-only run produces one valid artifact and survives restart.

### Phase 7 — Approval and external actions

Files:

- Action proposal/hash, request/decision services, dispatcher, receipt ledger.

Gate:

- Zero-before-approval, tamper/reuse/expiry/auth/concurrency/crash tests pass.

### Phase 8 — Ingress and scheduler

Files:

- Manual/webhook intake, verifier, receipts.
- Schedule recurrence, claims, occurrences, worker.

Gate:

- Replay and two-worker race/restart/misfire/DST tests pass.

### Phase 9 — API and generated client

Files:

- Routes/DTOs/problems, HTTP wiring for the already defined local identity, Host/CSRF/origin defenses, session/readiness.
- OpenAPI snapshot and generated TypeScript types.

Gate:

- API integration/authorization/contract suite passes.

### Phase 10 — Five demos

Files:

- Demo definitions/schemas/fixtures/acceptance tests.

Gate:

- All five valid/provenance-linked; Email exact barrier passes.

### Phase 11 — Frontend control surface

Files:

- Shell, safe banner, hierarchy/layout/tree, detail, config, forms, approvals, runs, artifacts, demos.

Gate:

- Exact hierarchy/Community/accessibility/mobile/browser tests pass.

### Phase 12 — Hardening and release

Files:

- Retention and observability completion plus final cross-layer threat/redaction/network review; foundational redaction/audit controls are not deferred to this phase.
- Compose/CI/clean-state scripts.
- README/product docs/verification record.

Gate:

- `make verify` and isolated `make verify-clean` pass with no provider/cloud credentials. After pinned dependency/base-image acquisition, test and application-runtime phases have no external egress; a fully air-gapped build additionally requires prewarmed verified caches and is not claimed by default.

## Safe parallel work packages

After Phase 1:

- Catalog authors can create separate department files, but one owner controls IDs/schema/count integration.
- Domain state machine, canonical hashing, and graph code can proceed in separate modules after shared ID/error contracts freeze.
- Frontend shell/design tokens may begin against a generated static hierarchy fixture, but must switch to the generated API contract after Phase 9.
- Security redaction and network-test infrastructure should start early and integrate continuously.

After Phase 4:

- Read-only mock adapters can proceed by connector family.
- API read projections can proceed while runtime commands stabilize.
- Scheduler recurrence unit work can proceed independently from database claim integration.

After Phase 9:

- Org chart/details.
- Schema forms/configuration.
- Approval queue.
- Run timeline/artifacts.
- Browser acceptance fixtures.

## Conflict-prone shared files

Assign one integrator at a time for:

- `pyproject.toml` and lockfiles.
- `apps/web/package.json` and frontend lockfile.
- `Makefile`.
- `compose.yaml`.
- `catalog/v1/manifest.yaml`.
- Central capability/policy catalog.
- Domain enums/errors.
- SQLAlchemy metadata and Alembic heads.
- API app/router registration and OpenAPI snapshot.
- CI workflow.

Department-specific catalog/prompt/schema files and feature-specific frontend modules are safer parallel units.

## Integration checkpoints

At each checkpoint:

1. Rebase/merge only after the preceding hard gate is green.
2. Run targeted subject tests.
3. Run catalog/import/API-generation/migration drift checks if affected.
4. Update traceability status/evidence only after verification.
5. Record new assumptions or ADR changes before dependent work continues.
6. Keep mock/default/no-network settings unchanged unless explicitly reviewed.

## Implementation completion checklist

### Catalog/domain

- [ ] Full exact catalog and shared Community templates.
- [ ] Pure domain boundaries.
- [ ] State/graph/hash/provenance invariants.

### Persistence/runtime

- [ ] Migrations and repeat seed.
- [ ] Worker claims/restart recovery.
- [ ] Budgets/retry/rate/cancellation.
- [ ] Transactional transitions/audit.

### Safety/effects

- [ ] Exact action approvals and all-approval barrier.
- [ ] Work/action/occurrence idempotency constraints.
- [ ] Mock receipt crash safety.
- [ ] Redaction/retention/no-network.

### Product

- [ ] Complete API/OpenAPI client.
- [ ] Five deterministic demos.
- [ ] Full org chart and control surfaces.
- [ ] Accessibility/mobile behavior.

### Delivery

- [ ] Compose one-command startup.
- [ ] CI/static/test/browser/clean-state gates.
- [ ] Required docs/ADRs/verification evidence.

## Exit criteria

- Target tree gives every required concern one clear home.
- Dependency order prevents UI/runtime work from bypassing catalog/domain safety gates.
- Parallel work packages avoid shared-file collisions and preserve one integration owner.
- Every phase has an executable hard gate.
- Final completion checklist matches the traceability matrix and clean-state release criteria.

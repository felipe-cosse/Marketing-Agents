# 04 — Persistence, migrations, and seeding

Status: planned

Depends on: [02 — Catalog](02-catalog-schema-seed-and-validation.md), [03 — Domain](03-domain-model-and-invariants.md)

Unblocks: workers, approvals, scheduler, API integration, audit, and clean-state verification

## Objective

Persist catalog projections, mutable deployment configuration, work, execution plans, approvals, external actions, schedules, artifacts, and audit data with transactional invariants that work on SQLite and remain portable to PostgreSQL. Historical runs must remain explainable after catalog or configuration changes.

## Database configuration

Default URL:

```text
sqlite+aiosqlite:////data/marketing_agents.db
```

Optional PostgreSQL URL:

```text
postgresql+asyncpg://user:password@host/database
```

SQLite connection initialization:

- Enable foreign keys for every connection.
- Enable WAL mode for file-backed local use.
- Set a bounded busy timeout.
- Store UTC timestamps as explicit timezone-aware values or normalized ISO values with tested conversion.
- Use a file-backed database for any multi-connection concurrency test; never claim concurrency behavior from `:memory:`.

Use string enums, explicit constraints, JSON columns with application/schema validation, and conservative indexes that work across both databases. Keep dialect-specific claim logic inside repository implementations.

## ORM and repository layout

```text
apps/api/src/marketing_agents/infrastructure/db/
├── base.py
├── session.py
├── types.py
├── models/
│   ├── catalog.py
│   ├── deployment.py
│   ├── work.py
│   ├── run.py
│   ├── artifact.py
│   ├── approval.py
│   ├── external_action.py
│   ├── webhook.py
│   ├── schedule.py
│   ├── rate_limit.py
│   └── audit.py
├── repositories/
│   ├── catalog.py
│   ├── instances.py
│   ├── work.py
│   ├── runs.py
│   ├── approvals.py
│   ├── external_actions.py
│   ├── schedules.py
│   ├── artifacts.py
│   └── audit.py
└── unit_of_work.py
```

ORM models are persistence records, not domain entities. Repositories map explicitly between them and must not leak SQLAlchemy sessions or model instances into application services.

## Table plan

### Catalog and deployment

| Table | Purpose | Important constraints |
|---|---|---|
| `catalog_releases` | Catalog version/hash/import record | Unique version and unique manifest hash |
| `departments` | Seeded department projection | Unique stable ID and display order |
| `function_teams` | Seeded function projection | FK department; unique sibling order |
| `tool_capabilities` | Seeded effect/connector/schema metadata | Unique capability ID |
| `approval_policies` | Seeded approval requirements | Unique policy ID |
| `agent_templates` | Seeded immutable role definition and snapshot refs | Unique template ID; FK function/department/policy |
| `agent_template_capabilities` | Template allowlist | Unique `(template_id, capability_id)` |
| `agent_template_trigger_kinds` | Supported trigger kinds | Unique `(template_id, trigger_kind)` |
| `agent_instances` | Seeded identity and template relation | Unique instance ID; exactly one template FK |
| `agent_instance_configs` | Mutable local deployment overrides | One row per instance; optimistic `version` |
| `trigger_definitions` | Mutable instance trigger binding | Unique ID; validated kind/config; optimistic `version` |

Store resolved catalog fields or immutable schema/prompt hashes needed for API projection. The version-controlled files remain authoritative; the database is an operational projection.

### Work, plans, and execution

| Table | Purpose | Important constraints |
|---|---|---|
| `campaign_briefs` | Structured reusable brief | Immutable input snapshot/hash after use |
| `work_items` | Admitted input, canonical keyed admission digest/key version, and idempotency identity | Unique `(source,event_id,agent_instance_id)`; changed trigger/workflow/mode/brief/config/payload conflicts |
| `runs` | One primary aggregate per WorkItem, state, snapshots, budgets, leases, next timeline sequence | Unique `work_item_id`; optimistic `version`; indexed state/created time |
| `run_agent_selections` | Selected template/instance/config snapshot | Unique `(run_id,instance_id)` |
| `run_steps` | Persisted execution steps | Unique `(run_id,step_key)`; indexed state/lease |
| `run_step_dependencies` | Explicit DAG edges | Unique edge; no self-edge constraint |
| `run_state_transitions` | Ordered lifecycle record | Unique `(run_id,sequence)` |
| `tool_attempts` | Model/connector/transform attempts | Unique `(run_step_id,operation_key,attempt_number)` |
| `rate_limit_windows` | Durable bounded counters where needed | Unique scope/key/window start |

Runs and steps snapshot:

- Catalog release ID/hash.
- Template ID and instruction/schema/policy hashes.
- Instance ID and configuration revision/effective bindings.
- Selected capability and effect.
- Retry, timeout, budget, and rate-limit values.
- Workflow definition version and plan hash.

This prevents later catalog/config edits from changing the meaning of a historical run.

### Artifacts and provenance

| Table | Purpose | Important constraints |
|---|---|---|
| `artifacts` | Immutable schema-valid structured result | Unique ID and payload hash; FK producer/run |
| `artifact_provenance` | Source and parent relationships | Unique provenance edge |

Do not update an artifact payload. Corrections create a new artifact that references the superseded artifact.

### Approval and external effects

| Table | Purpose | Important constraints |
|---|---|---|
| `external_actions` | Immutable proposal and dispatch/outbox state | Unique idempotency key; optimistic version |
| `approval_requests` | One action-scoped request | Unique active request per action or explicit replacement chain |
| `approval_decisions` | Append-only actor decision | Unique request ID for final decision |
| `action_authorization_sets` | Atomic release record for all required writes in one run | Unique active/released set per run; immutable set hash |
| `action_authorization_set_members` | Bound action/request/decision membership | Unique action membership and stable order |
| `connector_action_receipts` | Durable deterministic mock side-effect ledger | Unique connector/idempotency key |

Approval/request/action rows use restrictive deletion. Expiry, consumption, dispatch claim, and state version fields support conditional atomic updates.

### Ingress and scheduling

| Table | Purpose | Important constraints |
|---|---|---|
| `webhook_receipts` | Signed ingress keyed body digest/key version and replay identity | Unique `(source,event_id)` |
| `schedules` | Cron/timezone/next UTC/lease/config | Unique schedule ID; optimistic version |
| `schedule_occurrences` | Stable due/misfire occurrence record | Unique occurrence ID and `(schedule_id,scheduled_for_utc)` |

### Audit and maintenance

| Table | Purpose | Important constraints |
|---|---|---|
| `audit_events` | Append-only redacted events | Unique event ID and `(run_id,run_sequence)` for run-associated events; stable global ordering |
| `maintenance_runs` | Retention/reconciliation job ledger | Unique job occurrence key |

No application route updates or deletes audit events. Direct database-administrator tampering remains a documented local-demo residual risk.

## Uniqueness and integrity constraints

At minimum:

```text
UNIQUE work_items(source, event_id, agent_instance_id)
UNIQUE runs(work_item_id)
UNIQUE external_actions(idempotency_key)
UNIQUE approval_decisions(approval_request_id)
UNIQUE action_authorization_set_members(external_action_id)
UNIQUE run_state_transitions(run_id, sequence)
UNIQUE audit_events(run_id, run_sequence)
UNIQUE run_steps(run_id, step_key)
UNIQUE run_step_dependencies(run_step_id, depends_on_step_id)
UNIQUE schedule_occurrences(occurrence_id)
UNIQUE schedule_occurrences(schedule_id, scheduled_for_utc)
UNIQUE tool_attempts(run_step_id, operation_key, attempt_number)
UNIQUE connector_action_receipts(connector_binding_id, idempotency_key)
```

Additional checks:

- Approval expiry is after creation.
- Consumed time cannot precede decision time.
- Lease expiry is after claim time.
- Retry/attempt/sequence/version values are non-negative.
- State values are known strings.
- Self-dependency is forbidden.
- Artifact payload/schema hashes are nonempty.
- Instance config references a seeded instance.

Cross-row policy constraints still belong in the domain/application transaction because portable SQL constraints cannot express all of them cleanly.

## Migration sequence

Use schema-only Alembic revisions; do not bury the catalog's 43 instances in migration code.

1. `0001_catalog_and_deployment.py`
   - Catalog releases, departments, functions, policies, capabilities, templates, instances, and mutable configs/triggers.
2. `0002_work_runs_steps_and_artifacts.py`
   - Campaign briefs, work items, runs, selections, steps, dependencies, transitions, attempts, artifacts, provenance.
3. `0003_approvals_external_actions_and_audit.py`
   - Proposed actions/dispatch leases, requests, decisions, atomic authorization sets/members, mock receipts, audit events, and run timeline sequence.
4. `0004_webhooks_schedules_and_occurrences.py`
   - Webhook receipts, schedules, occurrences, and leases.
5. `0005_runtime_indexes_rate_limits_and_retention.py`
   - Claim/query indexes, rate limits, maintenance ledger, and retention-support fields.

Every migration requires:

- Upgrade test from the previous revision.
- Fresh upgrade test from empty database to head.
- Metadata drift check against SQLAlchemy models.
- A documented downgrade policy. Destructive downgrade may be explicitly unsupported after data-bearing production use, but the limitation must be honest.

## Unit of work and transaction boundaries

Application services use a `UnitOfWork` port exposing narrow repositories and one commit/rollback boundary.

Same-transaction operations include:

- Work idempotency receipt plus new Run creation.
- Run state change plus state-transition row plus audit event.
- Plan, selected agents, steps, edges, hashes, and approval proposals.
- Approval decision, bound-action state, and audit event.
- Atomic all-action authorization-set creation, all approval consumption, all action reservations, run release, and audit events.
- Individual external-action dispatch claim/lease plus attempt record.
- Schedule occurrence insertion, work creation, next-run update, and audit event.

Allocate every run-associated audit/timeline entry with a concurrency-safe monotonic counter, for example `UPDATE runs SET next_timeline_sequence = next_timeline_sequence + 1 ... RETURNING`. The transition table may retain its own transition ordinal, but the merged UI timeline orders transitions, steps, approvals, actions, artifacts, and cancellations by `audit_events.run_sequence`.

Never keep a transaction open during:

- Model provider calls.
- Connector calls.
- Long schema compilation.
- Browser/API polling.
- Backoff waits.

Workers claim with a short lease, commit, perform bounded work, then persist the result in a new transaction using optimistic versions.

## Repeatable seed algorithm

1. Load and compile the complete catalog before opening the transaction.
2. Compute catalog content/version hashes.
3. Start one unit of work.
4. Upsert catalog-controlled rows by stable ID.
5. Reject an unsafe destructive identity change rather than silently remapping history.
6. Insert missing `agent_instance_configs` from defaults; never overwrite existing local values.
7. Record or reuse the `catalog_releases` row.
8. Requery and verify exact database counts and references inside the transaction.
9. Commit only if the projection matches the compiled catalog.
10. Return inserted/updated/unchanged counts and the applied hash without sensitive data.

`seed --check` follows the same compile/compare path but performs no write and returns nonzero on drift.

## Sensitive execution data

Separate:

- Minimized operational payload needed for restart/retry.
- Redacted payload exposed through approval, audit, and API projections.
- Payload hash used for integrity and provenance.

Catalog schemas mark sensitivity. Persistence serializers apply those annotations before audit/API data is created. The initial SQLite demo does not claim encryption at rest; documentation must identify local file access as a residual risk and use short configurable retention.

## Claim and concurrency patterns

For SQLite, prefer conditional `UPDATE ... WHERE version = :expected AND lease is free` followed by `rowcount == 1`. For PostgreSQL, a repository may use row locking or `SKIP LOCKED`, but application semantics remain identical.

Concurrency-sensitive operations:

- Run/step worker lease.
- Approval decision and consumption.
- External-action dispatch claim.
- Schedule claim and occurrence creation.
- Durable rate-limit increment.
- Retention job lease.

Each operation needs a file-backed SQLite race test with independent sessions.

## Ordered implementation tasks

1. Write persistence naming, timestamp, and portability conventions.
2. Freeze sensitivity annotations, redacted audit metadata contracts, and a tested redactor before broad audit-writing services are implemented.
3. Implement async engine/session factory and SQLite pragmas.
4. Create separate ORM records and domain mappers.
5. Add migrations in the sequence above.
6. Implement repository and unit-of-work ports/adapters.
7. Implement monotonic per-run timeline allocation and transactionally coupled transition/audit writes.
8. Implement catalog seeding and no-write drift check.
9. Implement immutable artifact and provenance repositories.
10. Implement conditional claim helpers for runs, steps, approvals, authorization sets, actions, and schedules.
11. Add snapshot serialization and redacted projection separation.
12. Add metadata/migration drift checks.
13. Add optional PostgreSQL compatibility tests without making PostgreSQL a local requirement.

## Tests

```text
tests/integration/db/test_fresh_migrations.py
tests/integration/db/test_migration_metadata.py
tests/integration/db/test_foreign_keys.py
tests/integration/db/test_catalog_seed.py
tests/integration/db/test_seed_idempotency.py
tests/integration/db/test_config_survives_seed.py
tests/integration/db/test_work_idempotency_constraint.py
tests/integration/db/test_action_idempotency_constraint.py
tests/integration/db/test_transition_audit_atomicity.py
tests/integration/db/test_run_timeline_sequence.py
tests/integration/db/test_one_run_per_work_item.py
tests/integration/db/test_optimistic_concurrency.py
tests/integration/db/test_artifact_immutability.py
tests/integration/db/test_concurrent_approval_decision.py
tests/integration/db/test_concurrent_claims.py
```

Failure injection must prove rollback when:

- Audit insertion fails after a state mutation is staged.
- One catalog row fails during seed.
- A duplicate work/action/occurrence is attempted.
- A stale optimistic version tries to overwrite configuration or state.
- An artifact fails schema validation before persistence.

## Exit criteria

- Empty SQLite upgrades to Alembic head.
- Catalog seed is transactional, idempotent, and exact.
- Local instance configuration survives reseed.
- Domain objects and ORM records remain separate.
- Historical run snapshots remain stable after catalog/config changes.
- Uniqueness constraints enforce work, action, approval-decision, transition, step, attempt, mock receipt, and schedule idempotency.
- Every state/approval transaction writes its audit event atomically.
- Concurrency claim tests pass with independent SQLite sessions.
- Database URL can select PostgreSQL without changing domain/application code.

# 03 — Domain model and invariants

Status: planned

Depends on: [02 — Catalog schema](02-catalog-schema-seed-and-validation.md)

Unblocks: persistence, orchestration, approvals, scheduling, adapters, and API services

## Objective

Define a pure, framework-independent domain that expresses the system's safety and reliability invariants without importing FastAPI, SQLAlchemy, an LLM SDK, or a connector SDK. Invalid state transitions, unsafe action proposals, mutable artifacts, and unbounded policies should be impossible to construct through public domain APIs.

## Planned modules

```text
apps/api/src/marketing_agents/domain/
├── __init__.py
├── ids.py
├── enums.py
├── errors.py
├── entities/
│   ├── catalog.py
│   ├── deployment.py
│   ├── campaign.py
│   ├── work.py
│   ├── run.py
│   ├── approval.py
│   ├── external_action.py
│   ├── artifact.py
│   ├── schedule.py
│   └── audit.py
├── policies/
│   ├── approval.py
│   ├── budget.py
│   ├── retry.py
│   ├── rate_limit.py
│   └── retention.py
├── state_machine.py
├── graph.py
├── canonical_json.py
├── action_hash.py
├── provenance.py
├── redaction_types.py
└── events.py
```

Use immutable dataclasses or similarly small value objects. Pydantic may validate catalog/API boundaries, but it should not become the domain's persistence or transport model.

## Required domain entities

### Organization and deployment

| Entity | Core fields | Critical invariant |
|---|---|---|
| `Department` | ID, display name, order | Stable ID and unique sibling order |
| `FunctionTeam` | ID, department ID, display name, order | References exactly one department |
| `ToolCapability` | ID, connector family, effect, request/result schema IDs, idempotency support | Write effect is explicit and immutable |
| `ApprovalPolicy` | ID, required roles/scopes, expiry, self-approval setting | Every write resolves to an approval requirement |
| `AgentTemplate` | IDs, purpose, instructions/schema refs, capabilities, triggers, classification, policies | No write capability on read-only/no-approval template |
| `AgentInstance` | ID, template ID, enabled/config revision, bindings, trigger config, source ordinal | References exactly one template; contains deployment fields only |
| `TriggerDefinition` | ID, instance, kind, configuration, enabled | Kind is manual/webhook/schedule and config validates for that kind |

### Work and execution

| Entity | Core fields | Critical invariant |
|---|---|---|
| `CampaignBrief` | ID, title/objective, structured constraints, source refs | Validated and bounded; content is untrusted data |
| `WorkItem` | ID, source/event/instance/trigger/workflow/mode, brief/config revisions, input digest, admission digest/key version, timestamps | `(source,event_id,instance_id)` is the key; the canonical keyed admission digest detects any changed routing/mode/context/payload and remains stable through the installed-key lifecycle |
| `Run` | ID, work item, state, catalog/config snapshots, budgets, deadline, version | State changes only through transition service |
| `RunStep` | ID/key, kind, selected agent, dependencies, schemas, capability, effect, state | Dependencies are acyclic; capability is allowlisted |
| `Artifact` | ID/type/schema, immutable payload/hash, producer, provenance, sensitivity | Output schema validates and payload never mutates |
| `ExternalAction` | ID, action envelope/hash, idempotency key, state, connector binding | One immutable effect with one stable dispatch identity |
| `AuditEvent` | ID, sequence/correlation, type, safe metadata, actor, timestamp | Append-only through application service |

### Approval and scheduling

| Entity | Core fields | Critical invariant |
|---|---|---|
| `ApprovalRequest` | ID, action ID/hash, redacted view, policy snapshot, expiry, status | Authorizes one immutable action only |
| `ApprovalDecision` | ID, request ID, actor, decision, expected hash, reason, time | At most one final decision per request |
| `Schedule` | ID, trigger/instance, cron, IANA zone, next UTC run, misfire policy, version | Original timezone retained; next occurrence deterministic |
| `ScheduleOccurrence` | ID, schedule, scheduled UTC time, state, work ID, lease metadata | Stable occurrence ID and uniqueness per scheduled time |

## Value objects

Introduce narrow types for:

- Stable domain IDs and source event IDs.
- UTC timestamps and original IANA timezone names.
- Canonical payload hash and catalog/plan/schema hashes.
- Idempotency keys.
- Correlation IDs.
- Bounded positive counts and durations.
- Connector destination.
- Action type and capability ID.
- Principal/actor ID, role, and scope.
- Redacted payload versus executable payload.
- Sensitivity classification.
- Optimistic version.

Constructors should reject empty IDs, non-UTC runtime timestamps, non-finite numbers, unbounded policy values, unsupported algorithms, and invalid state combinations.

## Run lifecycle

Required states:

```text
received
validated
planned
awaiting_approval
executing
completed
failed
rejected
cancelled
```

Required transitions:

- `received -> validated`
- `validated -> planned`
- `planned -> executing` for a plan with no writes
- `planned -> awaiting_approval` for any plan containing a write
- `awaiting_approval -> executing` only after all required exact-action approvals remain valid
- `awaiting_approval -> rejected` when any required action is rejected
- `executing -> completed`
- `executing -> failed`
- Any nonterminal state to `cancelled`, subject to best-effort semantics
- No transition from a terminal state

ADR-0004 must decide pre-execution failure handling. The proposed extension is `received|validated|planned|awaiting_approval -> failed` for unrecoverable internal processing errors, while schema-invalid ingress is rejected before a Run is admitted. This extension must be documented and cannot weaken any required transition or approval gate.

The only public mutation is a transition function such as:

```text
transition(current_state, command, context) -> TransitionResult
```

The result contains previous/new state, reason, timestamp, domain events, and required audit metadata. Repositories may not set a run state directly.

## Step and action states

Run-level state must not hide granular progress.

Step states:

```text
pending -> ready -> executing -> succeeded
                    |
                    +-----------> failed
pending|ready -> awaiting_approval -> ready|rejected
any nonterminal -> cancelled
dependent work -> skipped when an upstream terminal outcome prevents execution
```

External-action states:

```text
proposed -> awaiting_approval -> approved -> dispatch_reserved -> dispatching -> succeeded
               |                    |                 |             |
               v                    v                 v             +-------> failed
            rejected             cancelled        cancelled       +-------> outcome_unknown
```

`outcome_unknown` is terminal for automatic dispatch and requires reconciliation. It must never be silently retried for an adapter without provider-side idempotency or status lookup.

An `approved -> awaiting_approval` transition is permitted only when the current approval expires before atomic barrier release and a replacement generation is required. `dispatch_reserved` records that the entire run's approval set was consumed atomically; individual dispatches then use leased `dispatching` claims and may become cancelled before a call or recovered according to connector idempotency support.

## Graph invariants

`WorkflowDefinition` and `ExecutionPlan` must enforce:

- Unique step keys within a workflow/run.
- Every dependency refers to a step in the same graph.
- No self-edge or cycle.
- At least one root and one terminal result path.
- Step count no greater than both workflow and global maxima.
- Every selected instance is enabled and eligible for the trigger.
- Every step capability appears on the selected template allowlist.
- Input bindings refer only to admitted work input or declared ancestor artifacts.
- Output schema is known and local.
- A write step has a proposed action and approval policy.
- Plan hash changes when any step, edge, binding, policy, or selection changes.

Use deterministic topological sorting with source/display order as the tie-breaker so the same input and catalog snapshot produce the same semantic plan.

## Canonical action envelope and hash

Define a versioned action envelope containing:

- Canonicalization version.
- Action type and capability ID.
- Connector family and immutable binding identifier.
- Normalized destination.
- Minimized executable payload.
- Run and step IDs.

The idempotency key is deliberately excluded from the authorized envelope. After hashing, derive it with a domain-separated function over the run ID, step key, action type, connector binding, and action hash. Persist and reuse that derived key for every retry; this avoids a circular hash dependency.

Canonicalization requirements:

- Normalize destination before hashing.
- Sort object keys deterministically.
- Use UTF-8 and a defined Unicode normalization policy.
- Reject NaN, infinity, non-string object keys, ambiguous dates, binary objects, and unsupported custom values.
- Preserve array order.
- Serialize timestamps in canonical UTC form.
- Hash with a versioned SHA-256 scheme.

The approval-facing redacted payload is derived from the envelope but is not the object being authorized. Any action type, destination, connector binding, or payload change must change the hash.

## Policy invariants

### Approval

- Read operations may use a no-approval policy.
- Write operations always require a human decision.
- A request binds to one external action and payload hash.
- Expiry is finite.
- Decision actor and request actor are independently recorded.
- Consumption is one-time and action-specific.

### Retry

- Attempts are finite and at least one.
- Only classified transient failures may retry.
- Backoff is bounded and respects the run deadline.
- Validation, authorization, approval, policy, and payload-hash failures are never transient.
- An ambiguous non-idempotent external result never retries automatically.

### Budget and rate limit

- Per-template values cannot exceed global ceilings.
- Planned totals are checked before execution.
- Actual model/tool/step counters are persisted and checked before each call.
- Deadline and cancellation override remaining retry budget.

### Cancellation

- Queued/unclaimed work cancels immediately.
- Executing work sets `cancel_requested_at` and is best effort.
- Workers check before a call and after it returns.
- A completed external action remains succeeded and visible after run cancellation.
- No audit/UI text claims cancellation reversed a completed effect.

## Artifact and provenance invariants

Every artifact records:

- Artifact type and schema version/ID.
- Immutable structured payload and payload hash.
- Producing run, step, template, and instance.
- Catalog release and selected instance-config revision.
- Source `WorkItem` and admitted-input digest.
- Parent artifact IDs and external observation references.
- Provider/mock version and generation metadata.
- Created timestamp and sensitivity class.

The artifact constructor validates the output schema before an artifact can exist. Failed or malformed model output is recorded as an attempt error, never as an artifact.

## Domain errors

Define stable error codes for transport mapping:

- `invalid_transition`
- `terminal_state`
- `graph_cycle`
- `graph_limit_exceeded`
- `capability_not_allowed`
- `write_requires_approval`
- `approval_expired`
- `approval_consumed`
- `approval_hash_mismatch`
- `approval_actor_forbidden`
- `idempotency_conflict`
- `budget_exhausted`
- `deadline_exceeded`
- `cancelled`
- `schema_validation_failed`
- `external_outcome_unknown`
- `schedule_lease_conflict`

Errors carry safe structured context only. Raw prompt content, credentials, PII, and executable payloads must not appear in messages.

## Ordered implementation tasks

1. Write the lifecycle and action-approval ADRs.
2. Add typed IDs, clock-facing UTC values, hashes, and optimistic versions.
3. Implement catalog/deployment entity invariants.
4. Implement pure run/step/action state transition tables.
5. Implement graph construction, cycle detection, deterministic topological ordering, and plan hash.
6. Implement bounded retry/budget/rate-limit policy values.
7. Implement canonical JSON and action-envelope hashing.
8. Implement immutable artifact payload and provenance records.
9. Implement approval request/decision/consumption invariants.
10. Implement schedule/occurrence value objects without database logic.
11. Add stable domain error codes.
12. Add the architecture test proving domain imports remain pure.

## Tests

```text
tests/unit/domain/test_ids.py
tests/unit/domain/test_catalog_invariants.py
tests/unit/domain/test_state_machine.py
tests/unit/domain/test_step_state_machine.py
tests/unit/domain/test_external_action_state.py
tests/unit/domain/test_graph.py
tests/unit/domain/test_action_hash.py
tests/unit/domain/test_approval_policy.py
tests/unit/domain/test_retry_policy.py
tests/unit/domain/test_budget_policy.py
tests/unit/domain/test_cancellation.py
tests/unit/domain/test_artifact_provenance.py
tests/unit/domain/test_schedule_values.py
```

Required coverage includes:

- Every legal and illegal state transition.
- Cancellation from every nonterminal state and rejection from terminal states.
- Graph cycles, duplicate keys, missing dependencies, and graph limits.
- Canonical hash equivalence across object-key order and difference across payload/destination/action changes.
- Unicode, nested arrays, nulls, dates, and rejected non-finite values.
- Write capability without approval cannot be represented.
- Expired, consumed, rejected, or hash-mismatched approval cannot authorize dispatch.
- Artifact payload cannot change and malformed output cannot become an artifact.
- Provenance includes every required reference.
- No domain module imports a framework or SDK.

## Exit criteria

- All required entities exist as pure domain types.
- Required run transitions and documented extensions are explicit and exhaustively tested.
- Graph, policy, action-hash, artifact, and provenance invariants pass without a database.
- No model output can authorize or select a tool through the domain API.
- Every write is structurally tied to one approval policy and proposed action.
- Errors are stable, safe, and transport-independent.
- Domain import boundaries pass.

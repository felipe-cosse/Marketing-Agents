# 05 — Orchestration DAG and run worker

Status: planned

Depends on: [03 — Domain](03-domain-model-and-invariants.md), [04 — Persistence](04-persistence-migrations-and-seeding.md), stage A ports/read-only mocks from [10 — Adapters](10-model-and-connector-adapters.md)

Unblocks: approval dispatch, demos, run APIs, timelines, and acceptance tests

## Objective

Implement a small, explicit orchestration layer that validates work, chooses only necessary instances, constructs and persists an acyclic dependency graph, passes typed artifacts, enforces resource policies, pauses before writes, and recovers after process restarts. It must not become an unrestricted agent chat or a framework-specific workflow engine.

## Planned modules

```text
apps/api/src/marketing_agents/application/
├── commands.py
├── queries.py
├── dto.py
├── ports/
│   ├── clock.py
│   ├── id_generator.py
│   ├── unit_of_work.py
│   ├── repositories.py
│   ├── llm.py
│   └── connectors.py
├── orchestration/
│   ├── workflow.py
│   ├── registry.py
│   ├── router.py
│   ├── planner.py
│   ├── graph.py
│   ├── bindings.py
│   ├── executor.py
│   ├── budgets.py
│   ├── retry.py
│   ├── rate_limit.py
│   └── cancellation.py
└── services/
    ├── intake_service.py
    ├── run_service.py
    └── artifact_service.py

apps/api/src/marketing_agents/workers/
├── run_worker.py
├── claims.py
└── cli.py
```

## Runtime topology

The API does not execute workflows inline:

1. An ingress adapter validates and admits input through `IntakeService`.
2. The service deduplicates the `WorkItem`, creates or returns its `Run`, records `received`, and returns `202`.
3. A run worker claims the persisted `received` run with a short lease.
4. A distinct validation advancement rechecks the admitted envelope, workflow/trigger/instance eligibility, and snapshotted input schema, then persists `received -> validated` with an audit event.
5. The planner claims only a `validated` run, chooses instances, builds the DAG, snapshots policies/configuration, and persists `validated -> planned`.
6. Read-only plans become executable. Plans with writes create external-action proposals and pause.
7. The worker executes ready steps in topological order, releasing the database transaction before every model/connector call.
8. Each result is schema-validated and persisted as a typed artifact or a classified error.
9. Restarted workers reclaim expired leases and continue from persisted state.

The scheduler and webhook paths use the same intake service; they cannot bypass validation, idempotency, routing, budgets, or approvals.

## Workflow definitions

Workflows are immutable code-level or validated declarative definitions registered at startup:

```text
WorkflowDefinition
  id
  version
  eligible_trigger_kinds
  eligible_template_ids
  workflow_input_schema_id
  result_artifact_schema_id
  step_definitions[]
  dependency_edges[]
  maximum_graph_size
```

Each `StepDefinition` contains:

- Stable workflow-local `step_key`.
- Step kind: `validate`, `transform`, `model`, `propose_action`, or `connector_action`.
- Deterministic agent-selection rule.
- Typed input bindings.
- Output schema ID.
- Required capability ID and read/write effect.
- Dependency keys.
- Timeout, retry, rate-limit, and budget requirements.
- Whether its output is final, intermediate, advisory, or proposed action.

The workflow registry fails startup if IDs collide, graphs cycle, referenced schemas/templates/capabilities do not exist, or a declared effect conflicts with capability metadata.

## Deterministic routing

Routing inputs:

- Trigger definition and source.
- Requested workflow or target instance.
- Compiled catalog snapshot.
- Instance enabled state and effective deployment configuration.
- Validated input metadata, not free-form model judgment.

Routing algorithm:

1. Resolve the requested workflow/instance explicitly.
2. Ensure the instance is enabled and supports the trigger kind.
3. Resolve its template and catalog release.
4. Apply workflow-owned selection rules for any additional instances.
5. Sort candidates by explicit priority/display order and stable ID.
6. Select the minimum set required by the workflow.
7. Reject ambiguous ties not resolved by the definition.
8. Persist all selections and snapshots.

Department and function nodes may scope eligible instances but are never invoked as agents. Model output never participates in selection.

## Planning algorithm

1. Require the run to be `validated` and load admitted work, target, catalog release, and snapshotted instance configuration.
2. Reconfirm the input digest/schema snapshot; do not collapse planning with the earlier persisted `received -> validated` advancement.
3. Resolve the workflow definition and exact selected instances.
4. Materialize steps and input bindings.
5. Validate every capability against the selected template allowlist.
6. Validate each step effect against template classification and approval policy.
7. Check graph references, cycles, roots, terminal result, and global/workflow step limit.
8. Sum maximum model/tool calls and reject a plan exceeding the run/template/global budgets.
9. Snapshot instructions/schema/policy/config hashes and workflow version.
10. Canonicalize and hash the plan.
11. Persist selections, steps, dependencies, bindings, budgets, and plan audit metadata atomically.
12. Transition `validated -> planned`.
13. Create every immutable proposed external action before any write execution.
14. If at least one write exists, create approvals and transition to `awaiting_approval`.
15. Otherwise transition to `executing` and mark root steps ready.

Any plan generated from the same admitted input, workflow version, catalog release, and instance configuration should have the same semantic step/edge/selection content. Runtime IDs and timestamps are not part of semantic determinism.

Validation failure after a Run has been admitted follows ADR-0004's explicit pre-execution failure transition and audit path; it must never jump silently from `received` to planning or leave the Run stranded.

## Typed data flow

Steps accept only:

- A declared subset of admitted work input.
- A `CampaignBrief` reference.
- Explicit parent artifact IDs and schema-validated payloads.
- Typed connector read results.
- Trusted policy/configuration fields passed separately.

They never accept:

- Unbounded accumulated chat history.
- Arbitrary database rows.
- Hidden global memory.
- Raw previous model prompts.
- Model-selected tool names or destinations.

Model requests separate trusted system instructions, typed untrusted content parts, typed tool observations, the required output schema, and budget/deadline metadata.

## Run-worker claim loop

The production loop is bounded and testable:

1. Query a small ordered batch of runnable work.
2. Attempt an atomic lease claim using ID/version/state/lease predicates.
3. If no row is claimed, return or wait with bounded jitter in the CLI wrapper.
4. Commit the claim transaction.
5. Load a domain snapshot through a new unit of work.
6. Execute exactly one state/step advancement in `drain_once()`.
7. Persist result, counters, transition, and audit in a short transaction.
8. Renew a lease only for a step whose bounded call may outlive the original lease.
9. Release/expire the lease and continue.

Tests call `drain_once()` with injected clock/IDs/adapters. They do not run infinite loops or sleep.

## Step execution

For each ready step:

1. Recheck run cancellation, deadline, budgets, rate limit, dependency success, and capability allowlist.
2. Persist step claim and attempt metadata.
3. Commit before calling an adapter.
4. Invoke through a port with a deadline/timeout and correlation context.
5. Classify success, validation failure, transient error, permanent error, cancellation, or ambiguous external result.
6. Validate structured output against the snapshotted schema.
7. Persist artifact, provenance, counters, step state, and audit event.
8. Mark newly unblocked children ready.
9. Complete the run only when all required terminal steps succeed and final artifacts exist.

Connector writes additionally follow the approval/action dispatcher in plan 06; the generic executor cannot call a write connector directly.

## Budget enforcement

Persist both policy ceilings and actual counters:

- Maximum graph steps.
- Maximum model calls.
- Maximum connector/tool calls.
- Maximum input bytes and per-field size.
- Maximum output bytes/tokens.
- Maximum attempts per operation.
- Per-step timeout.
- Overall run deadline.
- Rate-limit scope/window/counter.

Enforce budgets:

- At admission for input size.
- At planning for worst-case graph/call totals.
- Immediately before every attempt.
- After every response for output/counter reconciliation.

Budget exhaustion is a clear terminal error with redacted audit context; it never falls back to an unbounded behavior.

## Retry semantics

Retry only classified transient failures such as a mock-injected temporary unavailable response. Rules:

- Attempts and elapsed time are bounded.
- Backoff is capped and deadline-aware.
- Retry state is persisted before each attempt.
- Schema failures, authorization failures, policy denials, hash mismatch, expiry, and invalid input do not retry.
- Read calls may retry when idempotent.
- Write calls retry only through the external-action dispatcher with the identical idempotency key and declared connector support.
- A non-idempotent ambiguous result becomes `outcome_unknown`, not a retry.

Use an injected retry planner and clock so tests advance time without sleeping.

## Cancellation

`request_cancel(run_id, actor)`:

- Rejects a terminal run with a stable conflict response.
- Sets cancellation request metadata and writes an audit event.
- Immediately transitions queued/unclaimed work to `cancelled` when safe.
- For an executing step, preserves its state until the bounded call returns or times out.
- Workers check before and after every adapter call.
- Prevents unclaimed external actions from dispatching.
- Never rewrites a succeeded external action as cancelled or reversed.

The run timeline must show both the cancellation request and any action that completed before cancellation took effect.

## Rate limits

Start with deterministic fixed-window limits keyed by safe bounded identifiers such as template/capability/connector binding. Avoid user-controlled high-cardinality keys.

- Check at plan and attempt time.
- Persist windows when coordination across API/worker processes matters.
- Return retry-after metadata for operator-visible throttling.
- Do not retry past the run deadline.
- Emit a redacted audit event when a capability is denied or deferred.

## Audit coverage

At minimum record:

- Work received/deduplicated.
- Run validated/planned and plan hash.
- Selected templates/instances and policy/config revisions.
- Step ready/claimed/attempted/succeeded/failed/skipped/cancelled.
- Model/tool attempt result classification without raw content.
- Artifact creation and provenance references.
- Budget/rate-limit/capability denial.
- Approval/action events from plan 06.
- Run completed/failed/rejected/cancelled.

Every state transition and its audit event share the same transaction.

## Failure and restart behavior

| Failure window | Expected recovery |
|---|---|
| Worker dies before claim commit | Another worker can claim immediately. |
| Worker dies after claim but before adapter call | Lease expires; next worker sees persisted attempt/step state and resumes safely. |
| Worker dies during a read/model call | Lease expires; bounded retry applies. |
| Worker dies after artifact commit | Next worker observes succeeded step and does not regenerate it. |
| Output fails schema | Attempt fails; no artifact is created; bounded policy decides retry/permanent failure. |
| Dependency fails | Children become skipped; run failure/rejection is explicit. |
| Cancellation during call | Result is recorded honestly; no subsequent work dispatches. |
| Catalog/config changes mid-run | Snapshots preserve the original run; new work sees the new version. |

## Ordered implementation tasks

1. Define workflow/step/binding/plan contracts and a registry startup validator.
2. Implement deterministic routing and minimum-instance selection against compiled catalog/config snapshots.
3. Implement graph materialization, cycle/limit/capability checks, topological order, and plan hashing.
4. Implement and test the distinct `received -> validated` worker advancement and its failure/audit path.
5. Persist run selections, steps, dependencies, bindings, policies, and audit metadata transactionally.
6. Implement worker leases and a single-advancement `drain_once()` method with injected clock/IDs.
7. Implement typed transform and model steps, independent output validation, artifact creation, and provenance.
8. Add budget, deadline, durable rate-limit, retry classification, and cancellation checks before every attempt.
9. Integrate plan 06's action proposal/approval pause without granting the generic executor a write path.
10. Add restart/failure injection around validation, claims, adapter calls, artifact commits, and cancellation.
11. Expose run/timeline queries only after transition/audit atomicity and snapshot-stability tests pass.

## Tests

```text
tests/unit/application/test_workflow_registry.py
tests/unit/application/test_router.py
tests/unit/application/test_planner.py
tests/unit/application/test_bindings.py
tests/unit/application/test_budget_enforcement.py
tests/unit/application/test_retry_classification.py
tests/unit/application/test_cancellation.py
tests/integration/runtime/test_read_only_run.py
tests/integration/runtime/test_worker_restart.py
tests/integration/runtime/test_artifact_schema_failure.py
tests/integration/runtime/test_snapshot_stability.py
tests/integration/runtime/test_transactional_timeline.py
tests/integration/runtime/test_concurrent_run_claim.py
```

Required assertions:

- Same semantic input/config creates the same plan selection/edge hash.
- Every admitted Run records `received -> validated` before planning; validation failure follows the documented terminal path.
- Unallowlisted capability is rejected at planning and again at execution.
- A cycle or oversized graph never persists as planned.
- No step reads a non-ancestor artifact.
- No transaction remains open across an adapter call.
- A successful committed step is not repeated after restart.
- Retry count/deadline/budget are enforced exactly.
- Queued cancellation reaches `cancelled`; executing cancellation remains best effort.
- Every state change appears in an ordered timeline.

## Exit criteria

- API work creation returns quickly and never executes a workflow inline.
- Explicit workflows compile into deterministic persisted DAGs.
- Only necessary enabled instances are selected.
- Typed artifacts replace unbounded chat history.
- Budgets, timeouts, retries, rate limits, and cancellation are enforced before each call.
- Read-only work completes through deterministic mocks and survives worker restart.
- Write work can only proceed through plan 06's approval/action dispatcher.
- Every step and run transition is auditable.

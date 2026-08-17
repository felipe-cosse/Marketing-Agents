# 06 — Approvals, external actions, and idempotency

Status: planned

Depends on: [03 — Domain and principal values](03-domain-model-and-invariants.md), [04 — Persistence](04-persistence-migrations-and-seeding.md), [05 — Orchestration](05-orchestration-dag-and-run-worker.md), the early `IdentityProvider` port/local adapter from phase 5

Unblocks: every mutating workflow and the Email acceptance scenario

## Objective

Guarantee that a human decision authorizes one immutable proposed action, that the decision cannot be substituted, replayed, expired, or consumed twice, and that a simulated crash cannot repeat a deterministic mock side effect. Never describe this as universal distributed exactly-once delivery for future providers that lack idempotency.

## Core model

### External action

An `ExternalAction` is persisted before approval and contains:

- Stable action ID.
- Run and step IDs.
- Selected template/instance and snapshotted capability.
- Action type.
- Connector family and immutable binding ID.
- Normalized destination.
- Minimized executable payload.
- Redacted approval/audit projection.
- Canonicalization version and payload/action hash.
- Deterministic idempotency key.
- Approval policy snapshot.
- State, version, timestamps, and connector receipt/error metadata.

It is the unit of authorization and dispatch. A run is never the unit of authorization.

### Approval request

An `ApprovalRequest` contains:

- Request ID and generation.
- Bound external-action ID.
- Action type, destination summary, redacted payload, and immutable hash.
- Requesting run/step/template/instance.
- Requester principal.
- Required roles/scopes and self-approval policy snapshot.
- Created and expiry timestamps.
- Pending/final/consumed state.
- Superseded request ID when replacing an expired request.

### Approval decision

An append-only `ApprovalDecision` contains:

- Request ID.
- Actor from authenticated principal, never request body/header role claims.
- `approve` or `reject`.
- Expected hash presented by the UI/client.
- Optional bounded reason.
- Decision timestamp and correlation ID.

## Action construction

1. A deterministic workflow step creates a typed connector command.
2. Normalize action type, connector binding, destination, dates, identifiers, and payload.
3. Validate the command against the capability request schema.
4. Verify the capability is write-effect and allowlisted by the template snapshot.
5. Build the versioned canonical action envelope.
6. Calculate the SHA-256 action hash.
7. Derive a stable idempotency key from run ID, step key, action type, binding, and hash.
8. Derive a redacted operator projection using schema-aware rules.
9. Persist the action and first approval request in the plan transaction.
10. Transition the step/run to `awaiting_approval` and append audit events.

No connector object or dispatch authorization is available to the planner/model step.

## Approval decision algorithm

Approve/reject endpoints delegate to one application service:

1. Authenticate the principal.
2. Authorize `approvals:decide` and the policy-required role/scope.
3. Load/conditionally lock the pending request and bound action.
4. Require request pending, undecided, unconsumed, unsuperseded, and unexpired.
5. Require client `expected_payload_hash` to equal the stored request hash.
6. Recompute the current action envelope hash and require equality.
7. Apply self-approval policy.
8. Insert exactly one decision, transition request state, and transition the bound action from `awaiting_approval` to `approved` or `rejected` atomically.
9. Append a redacted audit event in the same transaction.
10. On rejection, reject the bound action and required workflow; invalidate remaining pending approvals for that run.
11. On approval, keep the run awaiting until every required action has a current valid approval.
12. When the all-approvals predicate becomes true, make the run runnable; do not call a connector inside the HTTP request.

Concurrent decisions use an optimistic/conditional update and unique decision constraint. Exactly one can succeed; the loser receives `409` with the final state.

## All-approvals-before-any-call barrier

For a workflow with multiple required writes:

- Persist every proposed action before requesting decisions.
- Create one approval per action.
- Keep every write step non-dispatchable until all required approvals are approved and unexpired.
- Approving one of two actions still produces zero connector calls.
- Rejecting either action rejects the workflow with zero connector calls.
- If an approval expires before atomic barrier release, produce zero connector calls and return the run to an actionable awaiting state.

This is the required Email-demo policy. It is stricter and easier to prove than dispatching each action immediately after its individual approval.

## Expiry and replacement

Check expiry twice:

- At decision time.
- At one-time consumption/dispatch claim.

If an approved request expires before barrier-release reservation:

1. Mark it expired through a conditional transition and audit the event.
2. Leave the immutable action envelope unchanged but transition the action from `approved` back to `awaiting_approval` for a replacement request.
3. Prevent all dispatch under the multi-action barrier.
4. Allow an authorized requester to create a new generation referencing the same action/hash.
5. Link the replacement chain and prevent the prior request from becoming active again.

Any payload/destination/action change creates a new external action and invalidates the old proposal; it is not an approval renewal.

## Atomic multi-action barrier release

Before the first connector call for a run, one transaction must:

1. Lock or conditionally version-check the complete required-action set in stable order.
2. Recheck cancellation, every action envelope/hash, and every current approval's actor policy, decision, expiry, supersession, and unconsumed state.
3. If any check fails, consume none, reserve none, dispatch none, and return the run to an actionable awaiting/rejected/cancelled state as appropriate.
4. Mark every required approval consumed together.
5. Transition every required action from `approved` to `dispatch_reserved` together.
6. Transition the run to `executing`, persist an authorization-set hash, and append ordered audit events.
7. Commit before any connector invocation.

Once reserved, later wall-clock expiry does not revoke already consumed authorization. Actions are dispatched individually with their original keys. A later connector failure can cause partial execution; the timeline must report it honestly and no compensation is implied. The zero-call expiry guarantee applies to expiry detected before barrier release, not to atomicity across independent external systems.

## Individual dispatch claim after barrier release

The dispatcher, not a connector, controls dispatch:

1. Require a committed authorization set containing the unchanged target action/hash.
2. Recheck cancellation and recompute the target action hash; approval validity is represented by the committed one-time reservation, not a now-consumed pending request.
3. Conditional-update the target action from `dispatch_reserved` to `dispatching`, add a dispatch lease owner/expiry, and create its attempt.
4. Commit before connector invocation.
5. Create an internal `ActionAuthorization` proof containing action ID, hash, authorization-set/request/decision IDs, capability, and idempotency key.
6. Call only the connector operation and binding stored in the approved action.
7. Persist succeeded, failed, or `outcome_unknown` result afterward.

Mocks refuse mutating operations unless a dispatcher-created proof is supplied and matches the request.

## Idempotency key rules

- Work idempotency and action idempotency are distinct.
- One semantic action within one run has one key across all retries and restarts.
- A changed envelope/hash produces a new key and requires a new approval.
- A database uniqueness constraint prevents two local action rows with the same key.
- The key is passed to connectors that support provider idempotency.
- Keys may appear in operator timelines but must not contain PII or secrets.

## Crash-window behavior

| Crash point | Recovery |
|---|---|
| Before dispatch claim commit | No connector call occurred; another worker may claim. |
| After claim commit, before connector call | Worker lease expires; retry uses the same action and idempotency key. |
| During connector call | Outcome depends on connector contract. |
| After provider accepted, before local success commit | Retry only with the same provider-supported idempotency key or status lookup. |
| After local success commit | Subsequent workers return the stored receipt and never invoke again. |

Deterministic mocks persist a separate `connector_action_receipts` ledger keyed by connector binding and idempotency key. A repeated invocation returns the original deterministic receipt and does not increment side-effect count.

Stale `dispatching` actions carry a lease. After lease expiry, recovery may reclaim and retry with the identical key only when the connector declares provider idempotency or status reconciliation. If the connector lacks that support and a call may have left the process, recovery marks `outcome_unknown` instead of redispatching.

For a future real connector:

- `idempotency=required|supported`: retry with the identical key and reconcile provider status.
- `idempotency=none` with a known pre-call failure: a bounded retry may be safe if the adapter proves no request left the process.
- `idempotency=none` with timeout/ambiguous result: mark `outcome_unknown`, stop automatic retries, alert the operator, and require reconciliation.

## Rejection and cancellation

- Rejection is final for that approval request/action path.
- A rejected required action moves the run to `rejected` and cancels unclaimed siblings.
- Cancellation before barrier release prevents consumption/reservation.
- Cancellation after barrier release but before an individual call prevents that uncalled action when possible; the approvals remain consumed and the timeline shows the reserved action as cancelled.
- Cancellation during a connector call is best effort; record the actual result.
- A succeeded action remains succeeded even if the run is later cancelled.
- UI/audit text must not imply rollback unless a separately modeled compensating action actually runs and is approved.

Compensating actions are out of v1 scope.

## Authorization rules

- Requester and approver identities come from the identity provider.
- Viewer/operator roles alone cannot decide.
- The local demo can use a fixed approver principal, explicitly labeled insecure/local.
- Arbitrary actor/role headers are ignored or rejected.
- Service principals cannot grant human approval.
- Approval reason length/content is bounded and redacted like other untrusted input.
- `APP_ENV=production` with local auth fails startup.

## API projection requirements

Approval resources expose:

- Request ID, status, generation, and one-time-use state.
- Action type and destination summary.
- Redacted proposed payload.
- Payload hash.
- Run/step/template/instance links.
- Created/expires/decided/consumed timestamps.
- Required actor roles/scopes.
- Decision actor and bounded reason after decision.
- Replacement/supersession metadata.

They never expose secrets or the unrestricted execution envelope.

## Audit events

- `external_action.proposed`
- `approval.requested`
- `approval.approved`
- `approval.rejected`
- `approval.expired`
- `approval.superseded`
- `approval.consumed`
- `approval.barrier_released`
- `external_action.dispatch_reserved`
- `external_action.dispatch_claimed`
- `external_action.succeeded`
- `external_action.failed`
- `external_action.outcome_unknown`
- `external_action.duplicate_suppressed`
- `authorization.denied`
- `approval.hash_mismatch`

Payload values are redacted. The required immutable hash is access-controlled pseudonymous integrity material, not confidential or safe for logs/metrics; IDs, actor, state, and allowlisted summaries preserve auditability.

## Ordered implementation tasks

1. Finalize canonical action and approval ADRs.
2. Implement typed action-envelope normalization and hashing.
3. Add action/request/decision repositories and constraints.
4. Add planner-time action proposal and approval creation.
5. Add authorization-aware decision service and concurrent-decision protection.
6. Implement the all-approvals predicate and run-resume signal.
7. Add expiry checking, replacement generation, and supersession.
8. Implement atomic all-action approval consumption/reservation and run barrier release.
9. Implement individual leased dispatch claims and stale-dispatch recovery by connector idempotency class.
10. Add durable mock receipt ledger and failure injection.
11. Implement result classification and `outcome_unknown` behavior.
12. Add cancellation/rejection/partial-execution interactions.
13. Expose safe API projections and complete audit timeline.

## Tests

```text
tests/unit/domain/test_action_hash.py
tests/unit/application/test_approval_service.py
tests/unit/application/test_all_approvals_barrier.py
tests/integration/approval/test_authorization.py
tests/integration/approval/test_concurrent_decisions.py
tests/integration/approval/test_expiry.py
tests/integration/approval/test_payload_tamper.py
tests/integration/approval/test_one_time_consumption.py
tests/integration/approval/test_atomic_barrier_release.py
tests/integration/approval/test_expiry_between_actions.py
tests/integration/approval/test_rejection.py
tests/integration/runtime/test_email_zero_calls.py
tests/integration/runtime/test_action_crash_recovery.py
tests/contract/test_mock_connector_idempotency.py
```

Must prove:

- Missing/unauthorized principal receives `401/403`.
- Actor is server-derived.
- One approval cannot authorize a second action.
- Modified payload, destination, action type, connector binding, or hash is rejected.
- Expired, rejected, consumed, superseded, or reused approval is rejected.
- Two concurrent decisions produce one decision.
- Before all approvals, connector count is exactly zero.
- Expiry detected before barrier release consumes no approval and produces zero calls; once all approvals are atomically reserved, later expiry cannot interrupt the authorized set.
- After valid approvals, each deterministic mock write produces exactly one receipt.
- A failure between individually dispatched actions is reported as partial execution and never as rollback.
- A crash after mock side effect but before local success does not repeat the side effect.
- Cancelling never claims to reverse a succeeded action.

## Exit criteria

- Every write exists as an immutable proposed action before execution.
- Every approval is action-scoped, payload-bound, authorized, finite, and one-time-use.
- Multi-write workflows remain at zero connector calls until all exact approvals are valid.
- Reuse, expiry, rejection, tamper, actor failure, and concurrent decisions fail safely.
- Mock crash/retry tests produce exactly one side effect per idempotency key.
- Non-idempotent ambiguous real outcomes cannot auto-retry.
- All decisions, consumption, and action state changes appear in the audit timeline.

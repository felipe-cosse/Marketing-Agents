# ADR-0004: Run lifecycle and failure semantics

- Status: Accepted
- Date: 2026-08-18

## Context

The prompt fixes the main state machine but internal validation or planning can fail after a run is admitted.

## Decision

Persist the required transitions: `received -> validated -> planned`, then read-only work moves to `executing`; mutating work moves to `awaiting_approval` and only then to `executing`; execution ends in `completed` or `failed`; approval may end in `rejected`; any non-terminal state may move best-effort to `cancelled`. Additionally, unrecoverable internal processing errors may move `received`, `validated`, `planned`, or `awaiting_approval` to `failed`. Invalid ingress is rejected before creating a run. Terminal states are immutable. Every accepted transition and rejected attempt is represented in audit evidence.

## Consequences

Failures cannot strand admitted runs or bypass approval. Cancellation reports completed effects honestly and never claims rollback of an external action.

## Verification

Exhaustive transition-table tests, terminal immutability, cancellation race tests, and transactional audit tests. Closes ASM-017.

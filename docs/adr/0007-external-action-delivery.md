# ADR-0007: External action delivery and idempotency

- Status: Accepted
- Date: 2026-08-18

## Context

Database commits and remote side effects cannot be made one transaction. Blind retry can duplicate effects when a provider lacks idempotency.

## Decision

Persist every action and unique idempotency key before dispatch. The dispatcher consumes a valid action authorization, acquires a lease, and passes the key to connectors that support provider idempotency. Deterministic mocks write one receipt per key and return the original receipt on replay. On timeout or crash with a real connector, retry only when provider idempotency or a verified lookup resolves the outcome. Otherwise record `outcome_unknown`, stop automatic retry, and require operator reconciliation. No universal exactly-once claim is made.

## Consequences

Mock acceptance can prove exactly one recorded effect. Real adapters must declare and test their delivery capability and may deliberately stop with an unknown outcome.

## Verification

Uniqueness, concurrent dispatch, crash-window, receipt replay, timeout, and no-blind-retry connector contract tests. Relates to ASM-018 through ASM-020.

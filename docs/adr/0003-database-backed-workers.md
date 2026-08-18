# ADR-0003: Database-backed local workers

- Status: Accepted
- Date: 2026-08-18

## Context

The local product needs persistent run and schedule workers but must start without a broker or cloud service.

## Decision

Use the configured relational database as the durable coordination boundary for the API, run worker, and scheduler worker. SQLite is the required local default; repository and unit-of-work ports keep PostgreSQL selectable by URL. Workers claim bounded batches with leases and compare-and-swap updates, renew only while active, and recover expired claims after restart. No in-memory queue is authoritative.

## Consequences

Local operation stays simple and restart-safe. SQLite concurrency is deliberately modest. A future distributed deployment may add a broker behind application ports without changing domain invariants.

## Verification

File-backed multi-session claim races, expired-lease recovery, rollback injection, and optional PostgreSQL contract tests. Relates to ASM-013.

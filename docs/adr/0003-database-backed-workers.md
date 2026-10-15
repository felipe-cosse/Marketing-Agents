# ADR-0003: Database-backed local workers

- Status: Accepted
- Date: 2026-08-18
- Database-default decision superseded by DEL-10 on 2026-10-15; coordination invariants retained.

## Context

The local product needs persistent run and schedule workers but must start without a broker or cloud service.

## Decision

Use the configured relational database as the durable coordination boundary for the API, run worker, and scheduler worker. The original SQLite-default decision remains historical evidence for ARCH-03. The user's DEL-10 instruction promotes PostgreSQL to the primary fresh-seeded Compose and native database; SQLite remains an explicit compatibility selection, never automatic failover. Workers claim bounded batches with leases and compare-and-swap updates, renew only while active, and recover expired claims after restart. No in-memory queue is authoritative.

## Consequences

Compose PostgreSQL uses an immutable PostgreSQL 17 image, a private Unix socket, peer identity, a non-superuser application role, and no IP network or published database port. Separate PostgreSQL data and digest-key volumes cannot overwrite old SQLite volumes. Native startup owns its private PostgreSQL process/socket and preserves persistent state. Existing SQLite state blocks implicit switching. A future distributed deployment may add a broker behind application ports without changing domain invariants.

## Verification

File-backed multi-session claim races, expired-lease recovery and rollback injection remain required. DEL-10 additionally requires live PostgreSQL migration/reseed, workflow/approval, restart, role and network checks, native startup and paired dump/key restore, plus full source-bound quality and production browser qualification. Related assumptions ASM-012 and ASM-013 now describe this explicit user-approved default change; historical SQLite results do not qualify it.

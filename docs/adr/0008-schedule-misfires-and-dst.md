# ADR-0008: Schedule misfires, timezones, and DST

- Status: Accepted
- Date: 2026-08-18

## Context

Persistent schedules must remain stable across restarts, workers, timezones, and daylight-saving transitions.

## Decision

V1 accepts five-field cron plus an IANA timezone. Store the original expression/timezone and persist `next_run_at` in UTC. A stable occurrence ID derives from schedule ID and the persisted due instant. Workers claim occurrences with a lease. `skip` audits missed work and advances to the first future wall-clock occurrence. `run_once` coalesces missed occurrences into one catch-up using the persisted due instant, then advances to the first future occurrence. Occurrence creation, work admission, audit, and next-time advancement commit atomically. Ambiguous wall times choose the earlier UTC instant; nonexistent wall times advance to the next valid local instant.

## Consequences

Restarts do not create duplicate work, and misfire behavior is explicit. Calendar exceptions and seconds are outside v1.

## Verification

DST tables, stable-ID, two-worker race, rollback, restart, `skip`, and `run_once` tests. Closes ASM-014 through ASM-016.

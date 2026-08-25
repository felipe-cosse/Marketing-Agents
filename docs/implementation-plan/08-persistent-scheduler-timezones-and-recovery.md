# 08 — Persistent scheduler, timezones, and recovery

Status: planned

Depends on: [04 — Persistence](04-persistence-migrations-and-seeding.md), [07 — Common ingress](07-triggers-webhooks-and-ingress.md)

Unblocks: schedule trigger acceptance and restart/concurrency verification

## Objective

Implement a small database-backed scheduler that preserves the original IANA timezone, calculates the next occurrence in UTC, allows only one worker to claim a due occurrence, applies explicit `skip` or `run_once` misfire behavior, and recovers after restart without duplicate work.

## Schedule configuration

```text
Schedule
  id
  trigger_definition_id
  agent_instance_id
  workflow_id
  cron_expression
  timezone_name
  next_run_at_utc
  last_scheduled_at_utc
  misfire_policy: skip | run_once
  misfire_grace_seconds
  enabled
  lease_owner
  lease_expires_at_utc
  version
  created_at_utc
  updated_at_utc
```

V1 cron scope:

- Five fields: minute, hour, day of month, month, day of week.
- No seconds, years, provider macros, or arbitrary code.
- Validate expression at configuration time.
- Store expression and original IANA timezone unchanged.
- Calculate and persist `next_run_at_utc`; never store only a local naive timestamp.

Use a recurrence-calculator port around a well-tested cron library plus standard `zoneinfo`. Keep library behavior behind explicit tests so it can be replaced without changing scheduler/application contracts.

## Occurrence identity

For each scheduled wall-clock occurrence:

- Record original schedule ID.
- Record local scheduled representation and timezone.
- Record chosen UTC instant and timezone-fold rule.
- Derive a stable UUIDv5/digest from schedule ID plus scheduled UTC instant and recurrence version.
- Store it as `occurrence_id`.
- Use it as the schedule ingress `event_id`.

Database uniqueness on `occurrence_id` and `(schedule_id,scheduled_for_utc)` prevents duplicate occurrence/work creation.

## Timezone and DST policy

Document and test the initial policy:

- Calculate future times from the original cron expression in its IANA timezone, not `now + interval`.
- Convert each selected valid local occurrence to UTC for persistence/claim.
- For a nonexistent spring-forward local time, record a skipped/nonexistent occurrence reason and advance to the first valid local instant after the gap.
- For an ambiguous fall-back wall time, choose the first occurrence (`fold=0`) in v1 so the same wall-clock label runs once.
- A timezone database update may change future UTC projections; existing recorded occurrence identities never change.

ADR-0008 must confirm these semantics before the recurrence implementation is frozen.

## Claim algorithm

Scheduler workers use an injected clock and bounded batch size:

1. Query enabled schedules with `next_run_at_utc <= now`, ordered by due time and ID.
2. Attempt a conditional update:

```sql
UPDATE schedules
SET lease_owner = :worker_id,
    lease_expires_at_utc = :lease_until,
    version = version + 1
WHERE id = :schedule_id
  AND version = :expected_version
  AND enabled = true
  AND next_run_at_utc <= :now
  AND (lease_expires_at_utc IS NULL OR lease_expires_at_utc < :now)
```

3. Treat `rowcount == 1` as ownership; otherwise move on.
4. Commit the lease before recurrence/ingress work.
5. Reopen a short transaction to create occurrence/work and advance the schedule atomically.
6. Release the lease only through a version-checked update.

PostgreSQL may use `FOR UPDATE SKIP LOCKED` inside its repository, but correctness must not depend on it.

## On-time occurrence transaction

1. Treat persisted `next_run_at_utc` as the scheduled occurrence, not current time.
2. Derive the stable occurrence ID.
3. Insert `schedule_occurrences` as pending; on uniqueness conflict, load the existing record.
4. Admit work through `IntakeService.admit_in_uow(...)` using that occurrence ID and the current transaction; the helper validates/writes but never commits independently.
5. Calculate the first future valid wall-clock occurrence after the scheduled time.
6. Persist occurrence/work/run link, `last_scheduled_at_utc`, new `next_run_at_utc`, audit event, and lease release atomically in that same unit of work.
7. If the transaction fails, no partial occurrence/work/advance is committed.

## Misfire detection

A due schedule is a misfire when it is later than `next_run_at_utc + misfire_grace_seconds` at claim time. Calculate the missed range with a bounded algorithm; never iterate unbounded years of occurrences.

### `skip`

1. Record one skipped occurrence anchored to the persisted due time.
2. Store safe metadata: first missed time, last coalesced missed time, and missed count.
3. Create no work/run.
4. Advance to the first valid future wall-clock occurrence.
5. Persist skip record, next occurrence, audit, and lease release atomically.

### `run_once`

1. Create exactly one catch-up occurrence anchored to the originally persisted due time.
2. Record the coalesced missed range/count.
3. Admit exactly one work item/run using the stable catch-up occurrence ID through `admit_in_uow(...)`.
4. Advance to the first valid future wall-clock occurrence.
5. Persist occurrence/work, next time, audit, and lease release atomically.

Do not create a backlog burst for every missed occurrence.

## Restart behavior

| Restart point | Expected behavior |
|---|---|
| Before lease commit | Another worker claims normally. |
| After lease commit, before occurrence transaction | Lease expiry permits another worker to repeat stable calculation. |
| During occurrence transaction | Rollback leaves no partial work or next-time advance. |
| After transaction commit, before lease-cleanup observation | Existing occurrence/work uniqueness returns the same records. |
| After work creation, before run execution | Run worker resumes independently; scheduler does not recreate work. |

Stable IDs and constraints, not timing assumptions, supply deduplication.

## Scheduler service and CLI

```text
apps/api/src/marketing_agents/application/services/schedule_service.py
apps/api/src/marketing_agents/application/ports/recurrence.py
apps/api/src/marketing_agents/infrastructure/scheduling/cron_recurrence.py
apps/api/src/marketing_agents/infrastructure/db/repositories/schedules.py
apps/api/src/marketing_agents/workers/scheduler_worker.py
```

Expose deterministic methods:

- `create_schedule()`
- `update_schedule(expected_version)`
- `calculate_next()`
- `claim_due_once(worker_id, now)`
- `process_claimed_once(schedule_id, worker_id, now)`
- `recover_expired_leases(now)`

The CLI loop wraps those methods with bounded polling. Tests call methods directly and use a fake clock.

## Configuration API safety

- Require instance/template support for `schedule`.
- Validate timezone through the local IANA database.
- Validate cron and calculate/display the next several preview times before save.
- Require explicit misfire policy and grace.
- Use `If-Match`/configuration version.
- Disable rather than delete schedules with historical occurrences.
- Never let untrusted work payload alter cron, timezone, target instance, workflow, or connector binding.

## Audit and observability

Events:

- `schedule.created`
- `schedule.updated`
- `schedule.enabled` / `schedule.disabled`
- `schedule.claimed`
- `schedule.lease_conflict`
- `schedule.occurrence_created`
- `schedule.misfire_skipped`
- `schedule.misfire_run_once`
- `schedule.duplicate_suppressed`
- `schedule.next_occurrence_persisted`

Metrics use bounded labels only: claimed count, lease conflicts, scheduler lag, misfire policy/count, occurrence result. Do not use schedule IDs or destinations as metric labels.

## Ordered implementation tasks

1. Approve ADR-0008 for cron, DST, and misfire semantics.
2. Implement schedule/occurrence schemas and domain values.
3. Implement recurrence port and tested cron/timezone adapter.
4. Add schedule/occurrence tables and indexes.
5. Implement optimistic conditional claim and lease expiry.
6. Implement on-time occurrence transaction through common intake.
7. Implement bounded missed-range calculation, `skip`, and `run_once`.
8. Implement restart/duplicate recovery paths and prove transaction-aware intake cannot partially commit.
9. Add schedule CRUD/config preview APIs with optimistic concurrency.
10. Add worker CLI, health/metrics, and audit events.

## Tests

```text
tests/unit/scheduler/test_cron_validation.py
tests/unit/scheduler/test_recurrence.py
tests/unit/scheduler/test_dst.py
tests/unit/scheduler/test_misfire.py
tests/integration/scheduler/test_concurrent_claim.py
tests/integration/scheduler/test_occurrence_transaction.py
tests/integration/scheduler/test_restart_recovery.py
tests/integration/scheduler/test_lease_expiry.py
tests/integration/scheduler/test_duplicate_work.py
tests/integration/api/test_schedule_configuration.py
```

Must prove:

- Original IANA timezone is preserved while next UTC time is correct.
- Two independent workers racing create one occurrence and one run.
- A crash before/after transaction commit cannot duplicate work.
- Lease expiry permits recovery.
- `skip` creates no work and persists the next future time.
- `run_once` creates one catch-up work item, not one per miss.
- Spring-forward nonexistent time and fall-back ambiguity follow the documented policy.
- A repeated stable occurrence ID returns the same run.
- Next occurrence and occurrence/work creation commit atomically.
- An injected failure after admission but before next-time persistence rolls back the occurrence, WorkItem, Run, audit event, and schedule advance together.
- Tests use file-backed SQLite and injected time without sleeps.

## Exit criteria

- Persistent schedules store cron, original IANA timezone, and next UTC occurrence.
- Claiming works across concurrent SQLite sessions.
- Occurrence and work idempotency are constraint-backed.
- Both misfire policies and restart recovery are explicit and tested.
- DST behavior is documented and tested.
- Scheduler ingress cannot bypass normal validation/approval/idempotency.
- The scheduler never depends on an in-memory timer as the source of truth.

# Dedicated schedule API

The `/api/v1/schedules` resources expose the existing persistent scheduler. Each
agent instance has at most one configuration-owned schedule; these routes do not
create a separate scheduler, timer, or source of configuration authority.
Mutations use the same configuration compare-and-swap transaction, scheduled-input
validation and binding, audit event, and schedule synchronization as the instance
configuration API. The server derives schedule, trigger, and workflow identities.

## Authorization and transport

Authenticated human principals with `viewer`, `operator`, `approver`, or
`local_admin` may read schedules and occurrences. Mutations require a human
`local_admin`, enforced both at the route and application-service boundaries.
Scheduler/webhook service principals cannot use this human control surface.

Mutations require strict `application/json`, the current process-local
`X-CSRF-Token` obtained from `/api/v1/session`, and the normal trusted Host,
same-origin, and Fetch Metadata checks. The request-body limit is 65,536 bytes.
Duplicate JSON keys, non-finite numbers, unknown fields, and caller-supplied
identity, workflow, trigger, lease, or scheduler-version authority are rejected.
Local identity remains non-production authentication.

## Operations

| Method and path | Result |
| --- | --- |
| `GET /api/v1/schedules` | Bounded page of configuration-owned schedules. |
| `POST /api/v1/schedules` | Create the active schedule for an enabled instance; `201` and `Location`. |
| `GET /api/v1/schedules/{schedule_id}` | Safe metadata, retained recurrence, three UTC previews, and configuration ETag. |
| `PATCH /api/v1/schedules/{schedule_id}` | Replace recurrence and/or explicit saved input for an enabled schedule. |
| `POST /api/v1/schedules/{schedule_id}/enable` | Restore retained recurrence using the existing saved input; body `{}`. |
| `POST /api/v1/schedules/{schedule_id}/disable` | Disable only the schedule binding; body `{}`. |
| `GET /api/v1/schedules/{schedule_id}/occurrences` | Read actual persisted occurrence identities and safe work/run links. |

Creation and edits configure future worker behavior; they do not execute or admit
work inline. The existing scheduler alone claims due occurrences and performs
atomic admission, recurrence advancement, lease handling, and deduplication.

### Revision fence

Every mutation requires one exact strong `If-Match` header, such as
`"instance-configuration-v1-1"`. For creation, obtain it from
`GET /api/v1/agent-instances/{instance_id}/configuration`. Subsequent mutations
can use the ETag returned by schedule GET or a successful mutation.

The validator belongs to **instance configuration**, not the scheduler's worker
version. Configuration changes can invalidate it; normal lease claims and
occurrence advancement do not. Missing, malformed, and stale validators return
`428`, `400`, and `409`, respectively. Read current state before retrying a conflict.

### Create and patch bodies

Example creation body using synthetic business input:

```json
{
  "instanceId": "inst.community.education.course-progress-reminders.01",
  "schedule": {
    "cron": "15 9 * * *",
    "timezone": "America/Los_Angeles",
    "misfirePolicy": "skip",
    "misfireGraceSeconds": 60
  },
  "scheduledInput": {
    "input": {
      "request_id": "request.example.course-reminder",
      "source_content": "A synthetic course has a new practice exercise."
    },
    "executionMode": "dry_run"
  }
}
```

Cron accepts five fields and a locally available IANA timezone. Misfire policy
(`skip` or `run_once`) and grace are explicit. The selected instance must support
schedules, and its real executable workflow validates the saved input. This API
accepts only `dry_run` scheduled input. Existing schedules return `409` on create;
update or explicitly enable the existing resource instead.

PATCH accepts a non-null `schedule` object with all four recurrence fields and/or
a non-null `scheduledInput` object. It cannot replace the target instance or
change immutable template/workflow authority. For example:

```json
{
  "schedule": {
    "cron": "30 9 * * *",
    "timezone": "America/Los_Angeles",
    "misfirePolicy": "run_once",
    "misfireGraceSeconds": 120
  }
}
```

### Enable, disable, and retained history

Disable clears the configuration's schedule binding/parameters while retaining
the durable schedule and its recurrence. It preserves the instance's enabled
state, other trigger kinds, connector bindings, saved input, and occurrence
history. It does **not** cancel work already admitted by the scheduler.

Enable restores the retained recurrence and calculates a future due occurrence.
It never re-enables a disabled agent instance: that returns `409`
(`schedule_instance_disabled`). If saved input was cleared through the explicit
configuration API, enabling returns `409` (`schedule_input_required`). Disabled
schedules reject PATCH with `409` (`schedule_disabled`); explicitly enable before
editing through this dedicated API. No disabled draft-creation or delete operation
is provided.

## Inspection, pagination, and errors

All responses are private/no-store. Schedule and occurrence projections omit
saved business input, input binding digests, and lease-owner facts. The explicitly
administrator-only instance configuration endpoint remains the surface for
inspecting saved input.

The three `previewUtc` values begin at the persisted next-due instant, followed by
two recurrence calculations. They are not execution promises or a claim that the
schedule is enabled; a retained due instant can be in the past. Original timezone,
UTC due time, recurrence version, and historical occurrence identity remain
authoritative under the existing DST/misfire rules.

Both list endpoints default to 20 items and accept at most 100. Schedule pages
order by stable schedule ID; occurrence pages order by `(scheduled UTC instant,
occurrence ID)`. Follow the opaque `nextCursor` with the `cursor` query parameter.
It is omitted when no further rows exist. Cursors are endpoint scoped, and
occurrence cursors are also schedule scoped. They are keyset continuation points,
not frozen snapshots; concurrent changes may be visible. Unknown/duplicate query
parameters and malformed or cross-scope cursors are rejected.

Errors use redacted `application/problem+json`. A `503 schedule_unavailable`
response means the outcome is unconfirmed: inspect the schedule and configuration
before retrying, because a mutation may already have committed. It does not claim
that a timed-out request made no changes.

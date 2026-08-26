# API-04 verification

Status: verified locally

The API now exposes `POST /api/v1/agent-instances/{instance_id}/dry-runs` as a
typed, asynchronous manual-admission boundary. It independently requires an
intact server-authenticated human operator in the FastAPI dependency and the
application service. A route-specific ASGI guard caps raw bodies at 1 MiB and
scans structural depth iteratively before FastAPI buffers or recursively parses
them; the typed route repeats the input-depth check before command normalization.
It accepts one `application/json` body, maps the public `dry_run` and
`mock_execute` values explicitly, and accepts an optional bounded opaque
`Idempotency-Key`. The body cannot supply actor, source, event, trigger, workflow,
configuration revision, capability, connector, work, or run authority.
Successful creation and replay both return non-cacheable `202 accepted` receipts
with authoritative event, work, and run IDs, an existing instance link, and the
reserved run-detail link without waiting for execution.

Inside the same transaction used to create the receipt, the resolver loads and
locks the persisted effective instance configuration, cross-checks it against
the immutable compiled instance and template, validates registered mock binding
IDs, and derives deterministic server-owned manual trigger and workflow IDs.
Because every v1 template supports manual work, an absent mutable manual binding
means the server-owned manual trigger is available; an explicit disabled manual
binding disables it. The compiled template input schema, capability allowlist,
input and output budgets, per-field limit, rate limit, and timeouts construct the
admission validator. A real catalog-schema negative test proves invalid input is
rejected with only a bounded safe JSON pointer before any durable receipt.

A supplied retry key becomes a keyed HMAC event identity rather than persisted
caller text. Exact retries replay the original WorkItem and primary Run across a
database-runtime restart; changed payload or execution mode conflicts. Omitting
the key generates a fresh event and distinct WorkItem and Run on every submission.
The manual-admission unit of work takes SQLite write intent before reading the
effective configuration and retries only bounded recognized lock conflicts. A
one-millisecond-busy-timeout concurrency test proves two simultaneous exact
submissions converge on one creation plus one replay, while changed submissions
produce one creation plus one idempotency conflict. Generic unit-of-work and
PostgreSQL behavior remain unchanged.
The WorkItem, primary Run, initial received transition, and audit event use one
caller-owned unit of work, and an injected failure after the audit flush proves
that all four roll back before a clean retry creates the receipt.

Each authorized schema-valid attempt also appends `ingress.manual_received` and
exactly one `work.created`, `work.duplicate_returned`, or
`work.idempotency_collision` witness on the authoritative Run timeline.
Collision auditing commits no new WorkItem or Run before returning `409`, and its
metadata describes the current
trusted attempted mode, configuration, trigger, and workflow while linking the
original receipt. An authorized schema or bound rejection instead commits one
runless `ingress.schema_rejected` witness and no WorkItem or Run. Database
assertions prove strict sequence, outcome shape, and that caller retry keys and
payload canaries are absent from audit metadata and aggregate identities.

`dry_run` safety is also enforced at the final external-effect boundary, not only
at admission. Before any connector contract lookup, claim, call-start marker, or
gateway execution, the dispatcher resolves and cross-checks the action's Run and
WorkItem. Durable dry-run work is terminally denied and audited with zero connector
calls, dispatch-attempt rows, or connector receipts. `mock_execution` retains the
existing controlled connector path.

Campaign-brief and demo-scenario registries are not present yet, so non-null
optional IDs fail closed instead of inventing trusted revisions. API-07 still
owns the run-detail resource, API-09 owns the unified problem vocabulary and
browser Origin/CSRF policy, and WEB-04 and DEL-07 own UI/client artifacts. DEL-04
owns migrations and insert-only seed bootstrap; DEL-05 owns runnable default
startup and service composition. The integration witness injects the real service,
compiled resolver, and durable unit of work through the application factory and
does not claim the default process constructs them yet. No live PostgreSQL service
or optional PostgreSQL driver is required by the local gate.

Machine authority: [`API-04.json`](API-04.json).

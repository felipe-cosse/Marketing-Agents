# API-07 verification

Status: verified locally

API-07 installs authenticated, read-only inspection for runs, their ordered
timelines and artifacts, individual steps and external actions, the global audit
feed, instance runtime status, and recent runs on instance detail. The transport
and application boundaries both require an intact authenticated human with a
viewer-equivalent local control-plane role before resolving an executor or
opening a unit of work. Service identities and humans without a permitted role
fail before repository access.

`GET /api/v1/runs` supports state, instance, workflow, and UTC creation-time
filters, a 25-item default, and a 100-item maximum. Its opaque versioned cursor is
bound to the exact filter set and the database keyset boundary
`created_at DESC, id DESC`. The repository fetches only `limit + 1` selector
rows, exact-hydrates the selected Runs with their bound WorkItems and complete
transition histories, and issues no operational count query. Run detail then
loads one sealed plan with selected instances, assignments, and steps; bounded
execution control, at most 100 current pending approvals, at most 10 returned
artifact summaries plus a truncation marker, and exact external actions. It
rereads the parent Run to detect parent drift and separately cross-validates the
bounded child bindings. This does not claim a database-wide repeatable-read
snapshot for every child row under a default `READ COMMITTED` transaction.

Step detail uses the inspectable sealed plan rather than a mutation-only private
service. External-action detail cross-checks the immutable action against Run,
plan, step, template, instance, capability, binding, and safe destination/payload
projections. Both action detail and composite Run detail rebind every action to
its sealed WRITE step, including effective timeout, binding revision,
idempotency support, delivery contract, and approval policy roles, scopes,
expiry, and self-approval snapshot. A succeeded result must exactly match its
authoritative durable receipt identity and status, the receipt's bounded output
projection, and the relations `receipt.created_at <= result.completed_at` and
`result.completed_at == action.updated_at`.

The persisted proposal payload is passed through the central conservative
redactor again before projection. Receipt metadata is bounded and exact-matched
internally as part of succeeded-result validation, but is never exposed:
`result_safe_metadata` is null-only in the application resource and live OpenAPI
schema, and transport rejects any non-null executor value with a private `503`.
Stable resource links connect those records to the existing catalog and API-06
approval resources. Unknown Runs are rejected before timeline lookup, and
malformed or path-mismatched executor results become private unavailable
responses.

The instance status endpoint preserves the injected catalog order for all 43
instances, including `never_run`, and reports its scope as
`single-local-installation`. It has a runtime-specific strong ETag and supports
private conditional `304` responses independently of the stable catalog ETag.
The ETag covers both `latest_run_created_at` and `latest_run_updated_at`; a
regression changes creation time while holding update time fixed and observes a
different validator. Instance detail optionally includes an exact recent-run
projection and performs two bounded status reads around composition;
disagreement fails closed rather than returning a drifting catalog/runtime view.

Artifact lists use a 25-item default, 100-item maximum, a Run-bound opaque
cursor, and deterministic `created_at ASC, id ASC` keyset ordering. List entries
contain metadata and complete provenance links but no payload or payload hash.
Detail exact-hydrates and verifies the immutable payload fingerprint, producer
step, schema, template, instance, configuration revision, parents, sources, and
provider provenance. It applies the persisted JSON-pointer redaction set plus a
conservative secret-field pass, and returns a keyed API pseudonym rather than the
stored payload hash. An artifact classification cannot be lower than its
producer step classification, and SECRET-classified detail fails closed because
secret material is non-retainable rather than projected or pseudonymized. The
live OpenAPI detail and source schemas therefore enumerate only retainable
`public`, `internal`, `personal`, and `sensitive` classifications. Returned
payloads remain JSON data and are never served as executable HTML.

Run timelines are ordered by the persisted per-Run `run_sequence`, not by
timestamp. They pseudonymize actor and correlation identifiers, enforce metadata
expiry at projection time, and emit links only for present live resource IDs.
The global `GET /api/v1/audit-events` supports bounded filters for Run, step,
action, approval, event type, and UTC time range. Its opaque cursor binds the
endpoint version, exact filters, descending `feed_sequence` boundary, and the
first page's fixed high watermark, so later appends do not enter an in-progress
walk. A cursor whose decoded filter material is non-ASCII fails as
`audit_cursor_invalid` before opening a unit of work. The historical internal
`global_sequence` is not exposed or reinterpreted.

All single/batch Run and runless audit append methods allocate immutable public
`feed_sequence` values through one counter update in the caller's transaction.
Concurrent allocation is serialized, rollback releases the uncommitted number,
and a committed counter must agree with its event tail. Reads exact-hydrate the
selected bounded page and reject missing selected sequences or sealed canonical
event/metadata fingerprint disagreement. Those fingerprints are unkeyed: a
trusted database administrator can rewrite rows and recompute them. Historical
filtered-out rows outside the selected page are not revalidated on every
request, and no tamper-proof persistence claim is made.

The typed API exposes stable GET operation IDs for Run list/detail/timeline,
step, external action, Run artifact list, artifact detail, global audit list, and
instance status. Duplicate query keys are rejected before executor resolution.
Every API-07 response, including route-local errors and redirects at this
surface, is private and `nosniff`; strong status responses deliberately use
private revalidation instead of `no-store`. Successful runtime-enriched existing
instance detail likewise keeps the catalog's private revalidation contract with
its own strong representation ETag and adds `nosniff`; enrichment failures are
`no-store`. API-07 adds no POST Run mutation and does not implement cancellation.

The bounded executable gate covers the API-07 transport, persistence, and
application suites plus catalog enrichment, API-06 link compatibility,
artifact/timeline persistence, audit redaction, network isolation, portable
database, and framework-boundary regressions. The shared test fixture denies
sockets and DNS, so the manifest records `network_requirement: deny`.

This evidence is deliberately limited. It covers one local installation and
does not establish tenant isolation. DEL-04 owns Alembic migrations, live
PostgreSQL execution, and backfill for the new feed column/counter; the current
database tests use metadata-created SQLite and the optional PostgreSQL check may
skip. DEL-05 owns default runnable composition. DEL-07 owns committed OpenAPI and
generated-client drift artifacts. API-09 owns global problem details,
correlation, CSRF/Origin/Fetch-Metadata, trusted proxy, and timeout/retry policy.
No live provider, connector dispatch, cancellation mutation, repair job,
retention-deletion job, or tamper-proof persistence claim is made. Run detail's
parent-drift check and child cross-validation do not establish portable
repeatable-read isolation; deployment isolation and default composition remain
DEL-04 and DEL-05 concerns. Although proposal payloads are conservatively
re-redacted, opaque destination summaries and neutral-key proposal values still
rely on the producer's persisted redacted-projection contract. Result metadata
is receipt-validated internally but exposed only as a null-only field, not
claimed safe, and this evidence makes no privacy claim for arbitrary connector
metadata.

Machine authority: [`API-07.json`](API-07.json).

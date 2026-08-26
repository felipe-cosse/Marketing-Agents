# API-05 verification

Status: verified locally

The API now exposes `POST /api/v1/webhooks/{source}/{trigger_id}` as a raw-body,
signature-authenticated asynchronous admission boundary. The route accepts one
unencoded `application/json` media type, preserves exact request bytes and the
received header multiset for application verification, and does not consult the
browser or local-human identity provider. Its ASGI guard rejects ambiguous or
oversized declared lengths and caps both buffered and streamed bodies at 1 MiB
before service invocation. The route waits at most five seconds, cancels a timed
out executor, returns non-cacheable fixed errors without reflecting internal
details, and returns a typed `202 accepted` receipt containing every authoritative
instance, WorkItem, and Run link without claiming execution or completion.

The registered source definition binds one source and trigger to a verifier
policy and one immutable mapper-owned version. The static registry rejects a
second trigger for the same source and rejects reuse of a secret reference or
verifier instance across source authorities. The HMAC-SHA256 adapter resolves an opaque
`env:` reference only at verification time, authenticates a canonical timestamp
and exact raw bytes under a domain separator, enforces body, header, freshness,
and future-skew bounds, compares digests in constant time, and issues a sealed
service principal restricted to the exact source and trigger. Signature failure
happens before JSON mapping, configuration resolution, or business persistence.
The strict JSON mapper then accepts only `{eventId,input}`, rejects duplicate
keys, non-finite constants, unexpected authority fields, invalid UTF-8 and
excessive depth, and never treats payload text as routing authority.

After authentication and mapping, the compiled-catalog resolver reads the
transaction-locked effective configuration for every catalog instance and fans
out only to enabled instances that explicitly contain an enabled webhook binding
for the authenticated source. The source registry alone is insufficient to admit
work. Each selected target gets its compiled template schema, workflow,
configuration revision, capability policy, byte budgets, rate window, and
timeouts. A real catalog integration binds both attendee-scheduler instances to
the same source, creates two normal WorkItems and primary Runs at the effective
revision, and proves a signed schema-invalid input creates neither target receipt.

Exact authenticated bytes are reduced to a domain-separated keyed digest with a
persisted key-version label; neither value is exposed by its restricted domain
representation. One immutable `webhook_receipts` row is unique on source and
event identity and retains the trigger, mapper version, digest material, and
received time. Child delivery rows uniquely bind each selected instance to its
normal WorkItem and primary Run. Composite portable foreign keys require each
delivery's WorkItem to belong to that instance and its Run to belong to that
WorkItem; tests prove contradictory and cross-linked triples are rejected by the
database itself. Hydration additionally cross-checks receipt, trigger, event,
and configuration lineage rather than trusting child rows.

The entire fan-out uses one caller-owned webhook admission unit of work. Exact
replay returns the original complete aggregate before consulting later target
configuration, including after database-runtime restart and both bindings drift
to disabled revisions. A correctly signed changed body with the same source and
event returns an idempotency conflict and creates no new WorkItem or Run.
Concurrent identical requests serialize into one creation and one replay of the
same two-target aggregate. After a PostgreSQL waiter acquires the deterministic
configuration locks, the service re-reads and classifies any receipt committed
by the winner before checking for orphan work; a focused service test exercises
that branch. The domain and response share the fixed 43-instance fan-out bound,
so an oversized result cannot commit and then fail transport serialization.
Tests also prove that partial fan-out failure, an omitted outer commit,
contradictory persisted linkage, and a fault after the
final webhook audit flush leave no partial receipt, work, run, transition, or
audit rows before a clean retry succeeds.

Webhook audit evidence preserves the authentication boundary. A rejected
signature is recorded only under a pre-verification system identity. Successful
verification followed by receipt creation, duplicate suppression, collision, or
schema rejection is recorded under the sealed webhook service identity. The
allowed event sequences and portable database constraints are exercised, and
the durable integration asserts that raw bodies, raw event IDs, signatures,
secrets, secret references, body and admission digests, and digest key versions
never enter audit metadata or aggregate identities.

The executable gate runs every API-05 route, service, verifier, mapper, digest,
audit, receipt, and real intake test plus proportionate manual-admission,
incoming-validation, receipt, identity, redaction, network-isolation, portable
database, and architecture regressions. `tests/conftest.py` applies the shared
socket and DNS denial fixture to every Python test, so the manifest records the
gate with `network_requirement: deny` rather than relying on test convention.

API-09 still owns the global `application/problem+json` vocabulary, browser
Origin and CSRF policy, per-source admission rate limits, and global timeout and
retry policy. API-05 supplies a route-local five-second wait, signature freshness
bounds, fixed non-reflective errors, and compiled target policy but does not claim
that larger boundary. Its HMAC environment-reference adapter and static registry
are local implementations, not a secret-rotation or provider-administration
system. The receipt proves admission, not inline execution.

DEL-04 still owns Alembic migrations and repeatable insert-only bootstrap. These
tests construct metadata directly and prove the SQLAlchemy constraints on real
SQLite. PostgreSQL schema portability is source-inspected but unexecuted; the
generic optional driver test is not evidence of API-05 DDL or runtime behavior.
DEL-05 still
owns default runnable composition: the installed route fails closed when no
asynchronous webhook executor is configured, while the witnesses explicitly
inject the real verifier, source registry, compiled resolver, digest key,
repositories, and unit of work. The SQLite write-intent path serializes the
supported default-timeout race, but unusually aggressive lock timeouts can
exhaust its bounded retries and safely return unavailable. New-source resolution
locks the fixed 43-row configuration set to obtain a coherent v1 fan-out
snapshot; a future indexed trigger projection can narrow that bounded lock set.

Machine authority: [`API-05.json`](API-05.json).

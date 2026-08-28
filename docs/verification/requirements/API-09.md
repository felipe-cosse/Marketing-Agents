# API-09 verification

Status: verified locally

API-09 puts one bounded failure boundary around the installed HTTP API and
connects ordinary terminal runtime failures to the durable Run lifecycle.
Requests receive a server-generated `correlation.api.<hex>` identifier before
any route-local middleware runs. Caller-provided correlation is removed. The
same identifier is placed in request state, admitted application commands,
response headers, and error occurrences, so logs and persisted command/audit
facts can be joined without trusting caller metadata.

The outer pure-ASGI middleware validates exactly one canonical trusted `Host`
and rejects generic `Forwarded` or `X-Forwarded-*` metadata in the current
direct-listener mode. It gives each request one configured absolute timeout and
does not retry route execution. A timeout before response start becomes a safe
503 `request_timeout` problem. Once a response has started, the middleware
propagates the timeout instead of sending a second start. All completed
responses receive fixed CSP, referrer, frame, content-type, and correlation
headers.

Application failures are normalized into one strict RFC 9457-style
`application/problem+json` occurrence. The required type, title, status,
detail, instance, code, and correlation fields are server-owned and bounded.
Optional field errors retain only safe root pointers and stable codes; optional
retry and current-version facts are separately bounded. The boundary preserves
only an allowlist of operational headers and never reflects arbitrary
`HTTPException` detail, exception text, validator input, or attacker-supplied
headers. Runtime tests cover 404, 405, validation, route-local, direct JSON, and
unhandled failures. The live FastAPI OpenAPI document exposes the same strict
`ProblemDetails` schema and only that media type for documented application
errors.

The local browser control plane now has an authenticated `GET /api/v1/session`
bootstrap. It returns the bounded local principal/session projection plus one
unpredictable process-start CSRF token under `Cache-Control: no-store`. Unsafe
API mutations require an exact JSON transport, one canonical configured Origin,
`Sec-Fetch-Site: same-origin`, and the current token. Missing, duplicate, stale,
cross-site, malformed, or normalized-origin evidence is rejected before a
handler. Path matching is based on the mounted ASGI path, so a `root_path`
prefix cannot bypass the rule. The exact signature-first webhook route is the
narrow exemption: its raw receive chunks remain untouched so authentication
continues to cover the original bytes.

Webhook admission adds a fixed-window, process-local bound after the exact
source/trigger definition and signature identity have been verified but before
envelope parsing, workflow resolution, Work, Run, or receipt persistence, or
provider work. Invalid signatures do not consume quota. A denied authenticated
attempt persists only redacted `webhook.signature_validated` and
`ingress.rate_limited` audit facts; raw body, event identity, signature, secret,
digests, and workflow data do not enter those rows. Each authenticated configured
source has a bounded call count and window; rollover is exact, backward clock
movement cannot open a new quota, expired windows are pruned, and total tracked-
source cardinality is capped. The v1 static registry enforces exactly one
configured trigger per source, and the quota is keyed by that authenticated
source. Capacity or live policy drift fails closed. An exhausted source receives
a non-reflective 429 problem with a bounded integer `Retry-After` value.

The existing ORCH-06 durable attempt controls remain authoritative for provider
work. READ and WRITE attempts retain persisted maximum-attempt, retry
eligibility, call-deadline, and absolute Run-deadline facts; API-09 does not add
an HTTP or SDK retry loop. A lost Run compare-and-swap keeps its existing
three-attempt rejection-audit bound, but now waits 10 and 20 milliseconds outside
the failed transactions so the winning SQLite writer can commit. The loser then
retains its stable `stale_run_version` result and audit instead of spuriously
ending as `audit_rejection_race`. A terminal READ adapter result, timeout,
cancellation, or exhausted retry deadline now fails the focal step and parent Run atomically.
A terminal WRITE outcome does the same while cancelling only queued or
provably pre-call siblings. The Run transition records a bounded
`terminal_failure_origin` that distinguishes ordinary execution failure from a
runtime-control denial. An external call that already crossed its durable
call-start boundary remains in flight until its sealed deadline. Recovery checks
for an exact durable receipt first and can complete from it without replaying the
provider, even for a legacy failure event. Without a receipt it maps the recorded
origin to a safe outcome; legacy originless audit rows use a generic failed-parent
reason. A missing or incoherent exact failed-Run mutation witness fails closed
without another provider call. Safe attempt/action, step, and Run audit events
commit in the same unit of work.

Failed Run detail now exposes one bounded `terminal_error` projection. It is
derived only from a coherent failed Run, its sealed plan and execution control,
and a validated child lineage. An exact final READ attempt can expose its safe
adapter cause. A recognized pre-call READ or WRITE failure with no attempt/action
uses `source="step"` and keeps the child link without inventing a cause. A
post-call denial must map to the final attempt; unsafe terminal identifiers,
missing attempt history, policy drift, and impossible post-call lineage fail the
resource closed. Operators receive the stable Run code, optional safe cause
code, source, final attempt number, call and Run deadlines, occurrence time, and
`retryable=false`. Provider exception strings, headers, request/output payloads,
secrets, and unvalidated attempt lineage are never part of this resource.

The executable gate runs the API-09 transport, terminal projection, and webhook
rate suites together with configuration, manual-work, webhook, approval,
read-resource, identity, controlled-READ, WRITE-completion, external-action, and
network-isolation regressions. The shared fixture denies sockets and DNS, so the
manifest records `network_requirement: deny`.

This evidence deliberately preserves delivery ownership. The CSRF token and
webhook windows are process-local and reset on restart; they are not a
distributed session or global multi-process quota. Direct mode rejects proxy
metadata rather than interpreting it. DEL-05 still owns default API/worker
composition, listener and reverse-proxy topology, and production process
layout. DEL-07 still owns the committed OpenAPI snapshot, generated TypeScript
client, and drift gate; API-09 verifies only the live schema and runtime
contract. DEL-04 owns migration and upgrade DDL for the new
`ingress.rate_limited` audit-event constraint; API-09 proves only a fresh
metadata-created SQLite schema. The readiness probe's diagnostic 503 retains its
established payload instead of being rewritten as an application problem.

Tests use local ASGI transports, SQLite-backed stores, deterministic adapters,
and no network. They do not claim a reverse proxy, multi-process server,
PostgreSQL race execution, live provider, external webhook sender, or network
fallback.

Machine authority: [`API-09.json`](API-09.json).

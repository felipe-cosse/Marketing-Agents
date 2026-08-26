# API-06 verification

Status: verified locally

The API now installs the complete approval surface: bounded list and detail reads,
explicit existing-action request/renewal, and authenticated approve/reject
mutations. Reads require an intact server-authenticated human with a
viewer-equivalent role and `approvals:read`; explicit request creation requires a
human operator with both `approvals:read` and `approvals:request`. Both checks
happen at the transport dependency and application-service boundary before
repository access. The local identity receives the new request scope explicitly.

`GET /api/v1/approvals` uses a documented 25-item default and 100-item maximum,
optional status/run/action filters, and a versioned opaque cursor bound to the
exact filters. The database orders by `requested_at DESC, id DESC`, fetches only
the bounded `limit + 1` selector rows (at most 101), exact-hydrates only that
selection, and does not issue a count query. List summaries omit both payload and
action hash. Authorized detail uses the historical inspection seam and returns
the safe destination and redacted payload, access-controlled hash, immutable
action/run/step/template/instance and policy lineage, lifecycle and decision
fields, replacement/use metadata, and stable resource links. It omits the
executable envelope, normalized destination, semantic and plan hashes,
idempotency and reservation material, authority snapshots, correlation IDs, and
keyed integrity values.

`POST /api/v1/external-actions/{action_id}/approval-requests` accepts only
`expected_generation` and `expected_payload_hash`; payload, type, destination,
policy, requester, role, and scope authority cannot enter the command. Normal
planning already creates generation one atomically with the complete
authorization set. Generation zero therefore ensures and returns the current
unexpired pending/approved request, while a missing initial chain fails closed
instead of repairing one leaf outside the all-approvals barrier. A positive
generation can renew only that exact expired action/hash. The existing renewal
CAS and unique chain link make the first caller the creator and let an exact retry
or race loser return the authoritative linked replacement. The winning
authenticated renewal actor becomes the new generation's immutable requester and
audit actor, preserving self-approval enforcement per generation. Responses
distinguish `200 existing` from `201 renewed` and carry the approval `Location`
without implying execution.

Approve/reject retain RUN-10's authenticated HUMAN, approver,
`approvals:decide`, policy-role/scope, self-approval, generation, expiry, action
immutability, current-set, and one-winner checks. The existing narrow response is
preserved when no approval query service is composed. With the API-06 resource
service, the response embeds a post-commit full approval only when the optional
reread validates against the decision; contradictory or malformed optional state
is omitted and the narrow response remains valid. The optional reason must
already be trimmed, is bounded at 500 characters, and rejects C0/C1 controls and
surrogate code points. It is stored on the append-only decision, included in its
keyed integrity digest, and exposed only through the authorized full resource. It
remains absent from list summaries, legacy narrow responses, audits, and errors.
Real API tests confirm a decision creates no dispatch attempt or connector
receipt and never describes the action as sent, published, executed, or
completed.

Repository reads do not trust isolated approval rows. List and detail
exact-hydrate each bounded selected row against keyed request/decision integrity
and its action, RunStep, policy, projection, and lifecycle authority. Detail uses
`get_inspectable`, so a linked historical generation remains readable without
passing the current-set mutation gate. Renewal/action lookup still reconstructs
the exact ordered generation chain from full current-set history. Restart tests
prove a renewal and same-generation replay hydrate the same two-generation chain.
A missing generation, a safe projection resealed to hide a disagreement with the
immutable action, and raw decision-reason tampering all fail closed.

All five operations have typed DTOs and stable OpenAPI operation IDs. Mutation
transport accepts exactly one `application/json` media type, extra authority
fields are forbidden, duplicate query parameters are rejected, and every approval
POST is capped at 8,192 raw bytes and JSON structural depth 16 before application
execution. Injected service errors do not reflect internal detail, and malformed
executor results fail closed. A narrowly scoped ASGI response guard adds
`Cache-Control: no-store` and preserves existing `Vary` values while adding
`Authorization` to every approval success and to authentication, authorization,
validation, media-type, conflict, not-found, and unavailable responses. OpenAPI
documents the `200`/`201` successes, stable error statuses, private headers, and
mutation `Location` headers.

The executable gate runs the three API-06 suites plus the existing RUN-08,
RUN-09, RUN-10, ORCH-08, redaction, network-isolation, portable-database, and
framework-boundary regressions. The shared test fixture denies sockets and DNS,
so the manifest records `network_requirement: deny`.

API-09 still owns the process-wide problem-details, shared correlation,
same-origin/CSRF/Fetch-Metadata, proxy, and global timeout policy. DEL-04 owns an
Alembic migration for the optional reason column and live PostgreSQL runtime
parity; these tests use metadata-created SQLite, and the optional PostgreSQL
driver check may skip. DEL-05 owns default process composition. API-07 owns the
resources behind the returned run, step, action, template, and instance links.
The authenticated renewal winner becomes the replacement generation's immutable
requester; first-generation planner requester provenance remains a pre-existing
intake/planning-boundary concern.
The decision service also retains RUN-10's received-state allowance for isolated
fixtures; normal persisted write plans reach `awaiting_approval` before decision.

Machine authority: [`API-06.json`](API-06.json).

# Standalone campaign briefs

`POST /api/v1/campaign-briefs` creates one immutable revision with a server-owned
opaque ID. `GET /api/v1/campaign-briefs/{brief_id}` reads its authorized projection.
These resources are standalone: no shipped workflow consumes them, and existing
manual-work brief restrictions remain unchanged. Creation does not admit a run.

Creation requires an authenticated human operator and one `Idempotency-Key`
(8–240 visible ASCII characters). An exact canonical-content retry returns the
same resource without refreshing its creation/expiry time or duplicating audit;
changed content returns `409`, even when both bodies redact identically. Keys and
body comparisons use distinct installation-keyed HMAC domains, not plaintext
retry-key persistence. Existing viewer-capable human roles may read; service
principals cannot. Browser creation retains the shared Origin/CSRF boundary.

Inputs contain `title` (1–200 characters), `objective` (1–2,000), `constraints`
(bounded strict JSON object), and `sourceRefs` (up to 64 unique strings of at most
240 characters). The complete body is bounded to 64 KiB. Duplicate/ambiguous JSON,
nonfinite values, unsupported content encodings and excessive depth are rejected.
Brief content is untrusted data, never routing, tool, approval, or execution authority.

Following the [accepted data policy](data-handling.md), the immutable snapshot
retains validated restricted input separately from its central-redacted operator
projection. Objectives and source references are marked sensitive; the short
title is an operational label, and sensitive structured constraint keys are
masked. Secret-classified structured data is rejected before persistence. This is
field/schema redaction, not general free-text DLP: use synthetic inputs and do not
put credentials or sensitive prose into the visible title or unmarked fields.
Input, projection, classification, recorded expiry, identity/revision and body
digest are installation-key integrity-bound. Audit stores only identity/revision.

Brief detail uses `RETENTION_ADMITTED_PAYLOAD_DAYS` (default 7 days). After the
recorded expiry, reads and retries return safe identity/revision/timestamp metadata
with `expired: true` and `details: null`. This is projection expiry, **not physical
erasure**: there is no retention sweeper, and restricted database/backup content
can outlive the TTL. Existing host protection and paired-key backup requirements
apply. No edit, delete, or later-revision endpoint is provided in this increment.

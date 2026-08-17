# 13 — Security, privacy, audit, and retention

Status: planned

Depends on: evidence/assumptions and architecture boundaries

Applies to: every implementation workstream

## Objective

Build guardrails around the actual trust boundaries: untrusted content, model/provider calls, connector side effects, approval identity, webhook replay, catalog policy, stored personal data, browser rendering, and network activation. Controls remain acceptance targets until their explicit tests pass.

## Trust boundaries

1. Browser/operator to API: dry runs, approvals, configuration, and sensitive views.
2. Webhook sender to API: signed but attacker-controlled bytes and replay risk.
3. Scheduler/run workers to the orchestrator: duplicate claims, stale leases, and restart races.
4. Version-controlled catalog to runtime: developer-controlled capabilities, schemas, and policies.
5. Orchestrator to model provider: untrusted posts/comments/email/transcripts/web content.
6. Orchestrator to connectors: external/representational side-effect boundary.
7. API/workers to database: PII, execution payloads, approvals, receipts, artifacts, audit.
8. API to browser: generated content and stored-XSS risk.
9. Environment to adapter registry: secrets and unsafe real-network activation.
10. Tests/CI to network: accidental live calls and secret leakage.

## Protected assets

- Approval integrity and authorized decision actor.
- External-action uniqueness and actual outcome.
- Catalog/capability/approval-policy integrity.
- Personal/sensitive data in signups, leads, comments, churn cases, course/community data, and partner applications.
- Model/provider and connector credentials.
- Artifact provenance and audit integrity.
- Scheduler occurrence identity and worker leases.
- Availability budgets, deadlines, rate limits, and retry state.

## Threat register

| ID | Threat | Primary planned controls | Verification |
|---|---|---|---|
| SEC-001 | Local identity exposed publicly | Loopback binding, internal API, visible warning, production fail-closed | Startup/bind tests |
| SEC-002 | Payload substitution, approval replay, expiry bypass, concurrent reuse | Canonical action hash, immutable request, conditional decision/consume, one-time state | Approval negative/race suite |
| SEC-003 | Spoofed/replayed webhook | Raw-body HMAC port, freshness, constant-time compare, unique source event key | Signature/replay/collision tests |
| SEC-004 | Prompt injection selects a tool/action | Trusted/untrusted separation, deterministic planner, no model tool calling | Injection fixtures and capability counters |
| SEC-005 | Capability/approval policy downgrade | Central registry, semantic catalog checks, deployment-only config | Catalog/config rejection tests |
| SEC-006 | Crash repeats a write | Durable action/outbox, stable key, mock receipt ledger, unknown-outcome handling | Crash-window tests |
| SEC-007 | PII/secret leakage | Field-aware redaction, minimized persistence, safe errors, no body logging, retention | Canary leakage tests |
| SEC-008 | SSRF/data exfiltration | No generic fetch, URL policy, scheme/host/IP restrictions, double network opt-in | URL-policy/no-network tests |
| SEC-009 | Stored XSS/generated HTML | React text rendering, sanitized restricted Markdown, CSP, no raw HTML | Browser XSS fixtures |
| SEC-010 | Oversize/fan-out/retry exhaustion | Byte/schema/graph/call/token/time ceilings, rate limit, bounded retry | Boundary/budget tests |
| SEC-011 | Scheduler race duplicates work | Lease/CAS, stable occurrence ID, unique constraints, atomic advance | Concurrent worker/restart tests |
| SEC-012 | Mutable/missing audit hides actions | Same-transaction audit, append-only repository/API, stable sequence | Fault-injection completeness tests |
| SEC-013 | Advisory output becomes automatic decision | Advisory schemas/UI labels; no decision/outreach command | Demo/API/UI assertions |
| SEC-014 | Unsafe real adapter activates silently | Mock defaults, two opt-ins, validated credentials, no fallback | Settings/registry tests |
| SEC-015 | Drive-by browser request or DNS rebinding abuses the fixed local principal | Trusted Host/origin, per-start CSRF token, Fetch Metadata, JSON-only mutations, proxy header stripping | Host/Origin/CSRF/spoof tests |

## Untrusted content and prompt injection

Treat all external text as data:

- Posts, comments, emails, transcripts, article text, webpages, webhook fields, connector observations, and prior generated artifacts are untrusted.
- LLM request objects place trusted system instructions and untrusted content in separate fields.
- Delimit/source-label untrusted parts and include their provenance IDs.
- Model output is parsed strictly against a known JSON Schema.
- The planner, not the model, chooses instances, steps, capabilities, destinations, and approval boundaries.
- Tool names/destinations embedded in user/model text are inert.
- Capability allowlists are rechecked immediately before invocation.
- Denied attempts create redacted audit events.

Add fixtures containing fake system instructions, tool names, approval claims, data-exfiltration URLs, and schema-breaking payloads. Expected denial is a passing security result.

## URL and content policy

V1 has no generic network fetch. URL fields may be preserved as provenance/reference only.

If an adapter later fetches a URL, central policy must:

- Allow HTTPS only unless a narrowly documented local test exception exists.
- Reject userinfo, fragments where inappropriate, unsupported ports, malformed IDNs, and dangerous schemes.
- Resolve and reject loopback, private, link-local, multicast, metadata-service, and other forbidden addresses at connection time.
- Use connector-specific host allowlists.
- Bound redirects and revalidate every target.
- Bound response size/content type/time.
- Avoid returning raw fetched content to a model without source labeling and size controls.

This future policy does not authorize scraping or unofficial automation.

## Data classification

| Class | Examples | Handling |
|---|---|---|
| Secret | API keys, bearer tokens, webhook secrets/signatures | Never persist/log/return; environment or secret source only |
| Direct PII | Email, name, handles, CRM/customer IDs | Minimize, mark schema fields, redact audit/log/API where not required |
| Sensitive content | Comments, transcripts, churn reasons, partner applications | Bounded storage/provider transmission; inherit into artifacts |
| Operational metadata | Stable IDs, states, safe counts, timestamps | Persist/audit with cardinality controls |
| Pseudonymous integrity material | Payload/action hashes, admission/body keyed digests, and idempotency keys | Integrity/dedup only; access control, no metric labels, not inherently safe/confidential |
| Generated artifact | Draft/review/recommendation | Inherit highest source sensitivity; sanitize in browser |

## Redaction design

Planned modules:

```text
apps/api/src/marketing_agents/security/redaction.py
apps/api/src/marketing_agents/security/digests.py
apps/api/src/marketing_agents/infrastructure/secrets/local_digest_key.py
apps/api/src/marketing_agents/workers/local_secret_init.py
apps/api/src/marketing_agents/domain/data_classification.py
apps/api/src/marketing_agents/observability/logging.py
```

Implementation:

1. Annotate input/output/connector DTO fields with sensitivity metadata such as `x-sensitive`.
2. Implement recursive schema-aware redaction for nested objects and arrays.
3. Use suspicious-key patterns only as defense in depth.
4. Apply redaction before operational logging, audit construction, approval projection, problem serialization, and UI-safe projections.
5. Never log request bodies, authorization headers, webhook signatures, prompts, complete model requests, or executable action payloads.
6. Keep only necessary IDs/digests for correlation/provenance and classify hashes as pseudonymous. A hash provides integrity, not confidentiality; low-entropy emails/IDs can be guessed.
7. Capture exception types/codes and safe context, not raw object representations.

Canary tests should place synthetic emails, names, tokens, signatures, partner text, and nested sensitive data in every path and assert they do not appear in captured logs/audit/problem JSON.

## Minimized persistence

Persist only data needed to:

- Resume validated work after restart.
- Bind/execute an approved action.
- Validate artifact/provenance.
- Explain states and decisions.

Keep separate representations:

- Restricted minimized execution envelope.
- Redacted operator/audit projection.
- Stable hash/digest.

The SQLite file is not encrypted by this plan. The product security document must identify host-file access as a residual local-demo risk. Real production storage/encryption/key management is a later deployment decision.

Digest policy:

- The prompt-required approval action hash uses versioned, domain-separated SHA-256 over the complete canonical action and is visible only to authorized approval clients; never put it in logs, metrics, or public cache keys.
- Internal admitted-input/webhook/body digests that do not need client comparison use a domain-separated keyed digest with a per-install secret, or an opaque random identity where equality is unnecessary.
- Never treat a digest of a low-entropy email, customer ID, or destination as anonymized data.

### Local digest-key lifecycle

The keyed-digest policy must remain credential-free and restart-safe:

1. A one-shot `local-secret-init` service creates a CSPRNG key exactly once in a project-scoped named volume when both the database and secret volume are new.
2. Create the directory/file atomically under the backend service UID with directory mode `0700` and key mode `0600`; never accept a key from catalog, webhook input, browser input, or a committed environment file.
3. Mount the key read-only into API, run worker, and scheduler processes. Every process loads the same key/version before becoming ready.
4. Store only a non-secret key fingerprint/version in database metadata and the version on restricted digest-bearing rows. A missing or mismatched key beside an existing database fails readiness and all new admissions; it must never silently generate a replacement.
5. Local backup/restore documentation treats the SQLite data volume and secret volume as one recovery unit. The key is excluded from logs, reports, source control, browser responses, and ordinary exports.
6. V1 performs no automatic key rotation. A later privileged rotation workflow must re-key still-required digests transactionally or expire them under retention policy before switching versions.

Tests create a temporary secret volume and prove atomic first generation, `0600` permissions, stable digests across API/worker/scheduler processes and restarts, identical webhook replay after restart, failure on missing/mismatched key with existing data, and fresh-key generation only for an empty installation.

## Retention

Make TTLs independently configurable for:

- Admitted execution payloads.
- External-action execution payloads.
- Approval redacted payloads.
- Artifacts.
- Connector receipts.
- Audit metadata.

Proposed local defaults must be approved in ADR-0010. A sensible starting point is short retention for execution/action payloads, moderate retention for artifacts/receipts, and longer retention for a non-sensitive audit skeleton. Secrets are never retained.

Retention job requirements:

- Uses injected clock and a leased, idempotent maintenance occurrence.
- Deletes or pseudonymizes only expired eligible fields/rows.
- Preserves state, sequence, timestamps, and a minimum safe provenance/audit skeleton; retains a digest only when still operationally necessary and within its own policy.
- Never deletes active runs/actions/approvals needed for recovery.
- Emits counts only, no deleted content.
- Can run repeatedly and concurrently without widening deletion scope.

## Audit design

Audit is distinct from operational logging.

Fields:

- Event ID and schema version.
- Stable sequence/global ordering fields.
- Event type.
- Aggregate type/ID.
- Run/step/action/approval/artifact IDs where relevant.
- Previous/new state.
- Actor ID and auth method/service source.
- Correlation ID.
- UTC timestamp.
- Allowlisted redacted metadata.

Rules:

- Append-only normal application/repository API.
- No update/delete product endpoints.
- Same database transaction as every state transition and approval decision.
- Stable cursor pagination.
- No raw payload, prompt, credentials, direct PII, or unbounded destination.
- Audit-write failure rolls back the associated state change.

Retention is the sole privileged exception: a narrowly scoped maintenance repository may delete or pseudonymize expired optional audit metadata, but it cannot delete the event row, run sequence, event type, state transition, decision fact, timestamp, or minimum correlation skeleton. It appends a retention-summary event. The timeline guarantee means every transition/approval remains visible as a skeleton after retention; detail may explicitly read `metadata expired/pseudonymized`. Acceptance tests run before TTL expiry and must see the full redacted timeline.

Optional later hardening may add a hash chain/verification CLI, but do not claim tamper-proof audit on local SQLite. Direct database administrators remain trusted/residual risk.

## Secrets and configuration

- `.env.example` contains placeholders only.
- `.env` is ignored.
- Configuration repr/API/log output masks secret fields.
- Webhook secret references resolve at runtime and are not stored in the catalog/database.
- Real provider credentials are loaded only when real mode plus network opt-in is enabled.
- Startup fails on missing/partial unsafe configuration; no silent mock/real fallback.
- No committed test fixture contains a usable real credential.
- The per-start local CSRF token is ephemeral, same-origin readable only, never persisted/logged, and distinct from human or provider credentials.

## Browser safety

- Render generated strings as text.
- Sanitize restricted Markdown and disallow raw HTML/remote embeds.
- Content Security Policy blocks remote scripts/fonts/assets and unsafe inline behavior where the build permits.
- Generic URLs are not rendered as trusted clickable actions without validation.
- Do not store sensitive form inputs in local storage, analytics, or error telemetry.
- No analytics/telemetry export by default.

## Rate limiting and availability

- Bounded request body and JSON depth.
- Per-source webhook admission rate limit.
- Per-template/capability model/tool limits.
- Global graph/step/call/token/output/deadline ceilings.
- Bounded database claim batches and polling intervals.
- Bounded pagination and audit filters.
- Retry-after values never exceed run deadline.
- Avoid user-controlled high-cardinality metrics/log fields.

## Operational telemetry

Structured logs:

- Request/run/step correlation IDs.
- Safe event/error code.
- Bounded state/capability/provider mode.
- Duration/counts without raw content.

Metrics:

- Runs by bounded state/workflow/template.
- Approval queue age and decisions.
- Connector result classification and retries.
- Idempotency conflicts/duplicates.
- Scheduler lag, claims, and misfires.
- Audit-write failures and redaction events.

Do not use actor IDs, emails, payload hashes, run IDs, schedule IDs, or arbitrary destinations as metric labels.

## Ordered implementation tasks

1. Write `docs/security.md` threat model and residual-risk baseline before broad runtime code.
2. Add data classification/sensitivity annotations to schemas/DTOs.
3. Implement redactor and canary tests before enabling broad logging/audit.
4. Implement trusted/untrusted LLM contract and injection fixtures.
5. Implement central content/URL policies and capability guardrails.
6. Implement local identity/public-bind fail-closed settings.
7. Implement the atomic local digest-key initializer, fingerprint/version checks, read-only runtime mounts, and restart/mismatch tests before keyed receipt/admission persistence.
8. Implement same-transaction append-only audit.
9. Add safe structured logging and bounded metrics.
10. Add retention policy/maintenance job and ADR defaults.
11. Add browser sanitizer/CSP/XSS tests.
12. Review optional real adapter activation and secret paths.
13. Run a final threat-model review against implemented data flows.

## Security verification

```text
tests/unit/security/test_redaction.py
tests/unit/security/test_content_policy.py
tests/unit/security/test_url_policy.py
tests/unit/security/test_action_canonicalization.py
tests/integration/security/test_local_digest_key_lifecycle.py
tests/integration/security/test_log_and_audit_canaries.py
tests/integration/security/test_prompt_injection_guardrails.py
tests/integration/security/test_catalog_policy_downgrade.py
tests/integration/security/test_auth_mode_fail_closed.py
tests/integration/security/test_retention.py
apps/web/src/features/artifacts/ArtifactViewer.security.test.tsx
```

## Residual risks to document

- Fixed local identity is not strong authentication.
- Local SQLite is not encrypted and a database administrator can alter records.
- Future real connectors may not guarantee provider idempotency.
- Real LLM transmission adds provider privacy/retention considerations outside offline acceptance.
- URL/DNS controls require revalidation at actual connection time when a real fetch adapter exists.
- Application-layer audit append-only semantics are not cryptographic tamper proof.
- Mock tests are not evidence of production delivery, scale, latency, or reliability.

## Exit criteria

- Threat model and data classification cover every trust boundary.
- Injection content cannot select or invoke a tool.
- Every write is capability-allowlisted and approval-gated.
- Logs, audit, problems, approvals, and configuration pass synthetic secret/PII canary tests.
- Retention is configurable, leased, idempotent, and tested.
- Browser output is sanitized and CSP/no-remote-assets checks pass.
- Real adapters cannot activate accidentally.
- Local digest identity remains stable across processes/restarts and fails closed if the database/key pairing is lost or mismatched.
- Audit events are transactional, ordered, redacted, and append-only through the product API.
- Residual risks are stated plainly without production claims.

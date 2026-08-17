# 09 — API contracts, local identity, and errors

Status: planned

Depends on: [04 — Persistence](04-persistence-migrations-and-seeding.md), [05 — Orchestration](05-orchestration-dag-and-run-worker.md), [06 — Approvals](06-approvals-external-actions-and-idempotency.md), [07 — Ingress](07-triggers-webhooks-and-ingress.md), [08 — Scheduler](08-persistent-scheduler-timezones-and-recovery.md)

Unblocks: generated frontend client, browser control surface, and API acceptance tests

## Objective

Expose a versioned, typed API for the complete catalog, deployment configuration, dry runs, webhooks, schedules, approvals, runs, artifacts, and audit history. All mutable operations must be server-authorized, optimistic-concurrency protected where appropriate, and mapped to stable problem responses. The documented local identity mechanism must work without credentials while being unmistakably non-production.

## API structure

```text
apps/api/src/marketing_agents/api/
├── app.py
├── dependencies.py
├── middleware/
│   ├── correlation.py
│   ├── errors.py
│   ├── limits.py
│   └── security_headers.py
├── schemas/
│   ├── common.py
│   ├── catalog.py
│   ├── instances.py
│   ├── work.py
│   ├── runs.py
│   ├── artifacts.py
│   ├── approvals.py
│   ├── schedules.py
│   ├── audit.py
│   └── problems.py
└── routes/
    ├── health.py
    ├── session.py
    ├── catalog.py
    ├── instances.py
    ├── campaign_briefs.py
    ├── demos.py
    ├── runs.py
    ├── artifacts.py
    ├── approvals.py
    ├── webhooks.py
    ├── schedules.py
    └── audit.py
```

FastAPI DTOs are distinct from domain entities and ORM rows. Routers translate transport input into application commands/queries and never perform orchestration or database mutations directly.

## Local identity design

Default `AUTH_MODE=local` behavior:

- The server produces one fixed `local-operator` principal with viewer, operator, approver, and local-admin roles for browser/API control-plane requests.
- The actor is never accepted from request bodies or arbitrary `X-Actor`/role headers.
- Native API binds to `127.0.0.1`. The Compose API may bind `0.0.0.0` only inside its private Docker network and is never published; Compose publishes only the web/reverse-proxy port on `127.0.0.1`.
- The proxy and FastAPI each enforce an explicit trusted `Host`/forwarded-host allowlist. The proxy strips caller-supplied forwarding and identity headers and sets fixed internal upstream values.
- At process start, the single local API process creates a cryptographically random CSRF token. `GET /api/v1/session` returns it only to the same-origin client with `Cache-Control: no-store`; the token is never written to browser storage, logs, or persistent application state. Every browser/control-plane mutation requires JSON content, `X-CSRF-Token`, a trusted `Origin`, and acceptable Fetch Metadata; webhook routes use HMAC instead and are exempt from browser CSRF handling.
- The UI displays an always-visible `Local identity — not production authentication` banner and current principal/roles.
- Webhooks and schedules use separate service principals created from verified signature/internal scheduler context.
- Tests replace the `IdentityProvider` dependency to exercise missing, viewer-only, operator-only, approver, admin, and service principals.
- Startup rejects `AUTH_MODE=local` when `APP_ENV=production` or public-bind settings are enabled.

This is authorization behavior for a credential-free local demo, not proof of authentication strength. A future production identity adapter implements the same `IdentityProvider` port.

## Roles and permissions

| Role/principal | Permissions |
|---|---|
| `viewer` | Read catalog, instance details, runs, artifacts, approvals, and audit |
| `operator` | Viewer plus create manual/dry runs and request cancellation |
| `approver` | Viewer plus decide action-scoped approvals |
| `local_admin` | Viewer plus deployment-only instance/trigger/schedule configuration |
| `webhook_service` | Submit work for its verified source/trigger only |
| `scheduler_service` | Submit its claimed occurrence only |

Application services authorize commands as defense in depth; route guards alone are insufficient.

## Health and session routes

```text
GET /health/live
GET /health/ready
GET /api/v1/session
```

Liveness is process-only and must not depend on the database.

Readiness checks:

- Database connectivity.
- Alembic revision at expected head.
- Catalog compiled/seeded and exact count/hash available.
- Required deterministic provider/connector registry initialized.
- Worker-facing schema compatibility.

It does not call an external provider or connector.

Session response includes safe actor ID, roles/scopes, auth mode, environment, model mode, connector mode, network permission, the local-mode warning, and—in local mode only—the current `csrfToken` plus `csrfHeaderName: "X-CSRF-Token"`. It is always returned with `Cache-Control: no-store` and must never be included in audit or diagnostics. A future multi-replica API must replace the process-local token source with an explicitly shared token provider before scaling; v1 runs one API process.

## Catalog and hierarchy routes

```text
GET /api/v1/catalog
GET /api/v1/catalog/hierarchy
GET /api/v1/agent-templates
GET /api/v1/agent-templates/{template_id}
GET /api/v1/tool-capabilities
GET /api/v1/approval-policies
GET /api/v1/agent-instances
GET /api/v1/agent-instances/status-summary
GET /api/v1/agent-instances/{instance_id}
GET /api/v1/agent-instances/{instance_id}/configuration-schema
PATCH /api/v1/agent-instances/{instance_id}/configuration
```

`GET /catalog` returns the complete small catalog projection: manifest/version/hash, departments, functions, templates, instances, capability definitions, approval policies, and derived count summaries. The dedicated list/detail routes provide filterable or focused projections without requiring the browser to download the full catalog on every view.

`GET /catalog/hierarchy` returns one ordered UI projection:

```text
catalogVersion
catalogHash
counts { departments, functions, templates, instances }
departmentCounts[]
departments[]
  id, displayName, displayOrder
  functions[]
    id, displayName, displayOrder
    instances[]
      id, templateId, displayName, purpose, displayOrder
      enabled, operationClassification, triggerTypes
      capabilitySummaries { id, displayName, connectorFamily, effect }
      sourceOrdinal
```

Counts are calculated from resolved data, never copied constants. Include a strong ETag based on catalog hash and relevant config projection version.

Recent run state is intentionally separate: `GET /agent-instances/status-summary` returns instance ID, recent state, latest run ID/time, and a runtime-status watermark/ETag for bounded polling. This prevents a dynamic status field from invalidating the otherwise stable hierarchy ETag.

Instance detail includes:

- Instance ID/configuration/revision and shared-template deployment count.
- Full template metadata and source/implementation notes.
- Input/output JSON Schemas.
- Capabilities with display labels/effect/connector family, trigger support/bindings, schedule, and connector binding summaries.
- Effect, approval, retry, timeout, budget, and rate-limit policies.
- A deployment `configurationSchema` link or embedded schema that contains only editable instance fields.
- Recent run summaries.

Configuration PATCH:

- Accepts deployment fields only.
- Requires `If-Match` or explicit expected revision.
- Returns `409` on stale revision.
- Cannot alter prompt, schemas, purpose, capabilities, classification, or approval policy.
- Audits actor, old/new safe configuration, and revision.

## Work and demo routes

```text
POST /api/v1/campaign-briefs
GET  /api/v1/campaign-briefs/{brief_id}
GET  /api/v1/demo-scenarios
POST /api/v1/demo-scenarios/{scenario_id}/runs
POST /api/v1/agent-instances/{instance_id}/dry-runs
```

Manual/demo creation:

- Requires operator permission.
- Uses an `Idempotency-Key` header for UI retries.
- Validates input against the resolved schema.
- Accepts only server-allowed execution modes.
- Returns `202` with work/run IDs and resource links.
- Never waits for the workflow to finish.

The response must say `accepted`, not `published`, `sent`, or `completed`.

## Run, timeline, and artifact routes

```text
GET  /api/v1/runs
GET  /api/v1/runs/{run_id}
GET  /api/v1/runs/{run_id}/timeline
POST /api/v1/runs/{run_id}/cancel
GET  /api/v1/runs/{run_id}/artifacts
GET  /api/v1/artifacts/{artifact_id}
```

Run detail includes state, selected agents, workflow/catalog/config snapshots, budgets/counters, cancellation metadata, pending approvals, artifacts, and safe errors.

Timeline entries include:

- Stable sequence number.
- UTC timestamp.
- Event type.
- Previous/new state where applicable.
- System/actor source.
- Run/step/action/approval/artifact links.
- Redacted summary and correlation ID.

Order by the database-enforced per-run `audit_events.run_sequence`, not timestamps alone. Every run-associated transition, step, approval, action, artifact, and cancellation obtains its sequence from a concurrency-safe monotonic allocator with `UNIQUE(run_id,run_sequence)`.

Artifact responses include schema ID/version, the authorized safe structured projection, access-controlled pseudonymous payload hash, sensitivity classification, and complete provenance references. Never serve artifact HTML as executable markup or emit hashes to logs/metrics.

## Approval routes

```text
GET  /api/v1/approvals
GET  /api/v1/approvals/{approval_id}
POST /api/v1/external-actions/{action_id}/approval-requests
POST /api/v1/approvals/{approval_id}/approve
POST /api/v1/approvals/{approval_id}/reject
```

Normal workflows create approval requests automatically. The explicit creation route is restricted to requesting a first/replacement request for an existing immutable action and must not accept action payload/type/destination mutations.

Approve/reject bodies include:

- `expected_payload_hash`.
- Optional bounded reason.

Server rechecks actor, policy, status, expiry, hash, action immutability, and one-time state atomically. It returns the updated approval and run/action resource links, not a claim that the connector has already executed.

## Webhook and schedule routes

```text
POST  /api/v1/webhooks/{source}/{trigger_id}
GET   /api/v1/schedules
POST  /api/v1/schedules
GET   /api/v1/schedules/{schedule_id}
PATCH /api/v1/schedules/{schedule_id}
POST  /api/v1/schedules/{schedule_id}/enable
POST  /api/v1/schedules/{schedule_id}/disable
GET   /api/v1/schedules/{schedule_id}/occurrences
```

Webhook authentication is source signature verification, not the local browser principal. Schedule mutation requires local admin; occurrence submission is internal scheduler service behavior.

## Audit route

```text
GET /api/v1/audit-events
```

Support cursor pagination and bounded filters for run, step, action, approval, event type, and time range. Reject unbounded page sizes. Return only redacted metadata and stable ordering.

## Pagination and consistency

- Use opaque cursors based on stable ordered keys.
- Use a documented default/max page size.
- Include `next_cursor` only when more rows exist.
- Apply deterministic secondary ordering by ID.
- Avoid count queries on every operational list unless the UI requires them.
- Use ETags/config revisions for mutable resources.

## Error contract

Return `application/problem+json` with stable fields:

```text
type
title
status
detail
instance
code
correlation_id
field_errors[] { pointer, code, message }
retry_after_seconds (when applicable)
current_resource_version (for conflicts)
```

Status mapping:

- `400`: malformed transport or unsupported option.
- `401`: no valid identity/signature.
- `403`: authenticated but insufficient scope/policy.
- `404`: unknown or intentionally undisclosed resource.
- `409`: idempotency collision, stale revision, invalid state, decided/consumed approval, hash mismatch.
- `413`: payload too large.
- `422`: schema/semantic input validation.
- `429`: rate limit.
- `503`: not ready or explicitly configured adapter unavailable.

No problem detail includes raw payload, secret, signature, prompt, stack trace, or direct PII. Development stack traces remain server-side and redacted.

## OpenAPI ownership

- Give every DTO/operation a stable name and operation ID.
- Generate `apps/web/src/api/generated/schema.ts` with `openapi-typescript` or equivalent.
- Centralize calls in `apps/web/src/api/client.ts` and problem mapping in `problems.ts`.
- Commit the OpenAPI snapshot and generated client types, or make deterministic regeneration mandatory.
- `make api-contract-check` fails if regeneration changes tracked files.
- Add API schema tests for required approval, timeline, catalog, and form fields.

## Browser/API security headers

- Exact same-origin deployment through the web proxy.
- Exact development origins only; no wildcard credentialed CORS.
- Trusted external/internal Host allowlists to reject DNS-rebinding-style Host values.
- The proxy strips spoofable `Forwarded`, `X-Forwarded-*`, actor, role, and other internal-trust headers before setting its own upstream values. It forwards the browser's `X-CSRF-Token` value unchanged over the private hop; that value is not an identity assertion, and FastAPI validates it against the process-local token.
- Mutable control-plane routes accept JSON only and require the per-start CSRF token plus strict `Origin`/Fetch-Metadata checks. Reject simple form/text submissions.
- Webhook routes are outside browser CSRF flow and require their configured raw-body signature; they never inherit the fixed local principal.
- Content Security Policy with no remote scripts/fonts/assets and no unsafe raw artifact HTML.
- `X-Content-Type-Options: nosniff`.
- Appropriate frame/referrer policies.
- Bounded request/body middleware and correlation IDs.

## Ordered implementation tasks

1. Freeze route/resource naming and local identity ADR.
2. Implement the early identity port/local adapter, per-start CSRF service/session projection, trusted-host/origin policy, precise proxy header handling, and production/public-bind fail-closed checks.
3. Define common IDs, pagination, timestamps, problem details, and correlation middleware.
4. Implement liveness/readiness/session.
5. Implement complete catalog, stable hierarchy, separately polled instance status summary, template/capability/policy list/detail, instance detail, configuration-schema, and config routes.
6. Implement work/demo/run/timeline/artifact routes.
7. Implement approval request/decision routes.
8. Implement webhook and schedule routes.
9. Implement audit query route.
10. Generate OpenAPI and frontend types.
11. Add security headers, CORS, Host/DNS-rebinding defenses, CSRF/Fetch-Metadata checks, proxy stripping, body bounds, and authorization matrices.

## Tests

```text
tests/integration/api/test_health.py
tests/integration/api/test_session_identity.py
tests/integration/api/test_local_csrf_and_origin.py
tests/integration/api/test_trusted_host.py
tests/integration/api/test_proxy_header_spoofing.py
tests/integration/api/test_catalog.py
tests/integration/api/test_hierarchy_counts.py
tests/integration/api/test_instance_configuration.py
tests/integration/api/test_dry_run.py
tests/integration/api/test_runs_timeline.py
tests/integration/api/test_cancellation.py
tests/integration/api/test_artifacts.py
tests/integration/api/test_approvals.py
tests/integration/api/test_webhooks.py
tests/integration/api/test_schedules.py
tests/integration/api/test_audit.py
tests/contract/test_openapi_snapshot.py
```

Must prove:

- Exact hierarchy/count projection and stable order.
- Stable hierarchy ETag excludes dynamic run state; status-summary has its own runtime watermark/ETag.
- Complete catalog projection includes all templates, instances, capability labels/effects, and approval policies required by the UI.
- Every instance returns or links to a deployment-only configuration schema.
- Template fields cannot be patched through instance configuration.
- Stale revisions conflict.
- Local actor is server-derived and unsafe production combination fails.
- Malicious/missing Origin, unexpected/DNS-rebinding Host, simple form/text mutation, missing/wrong/stale CSRF token, and spoofed forwarding/identity headers are rejected before application handlers run.
- Session is `no-store`; the approved CSRF header survives the proxy while internal trust headers do not; an API restart invalidates the old token and a fresh session token succeeds.
- Native loopback and Compose-internal API binding are distinguished so the canonical private Compose topology remains valid.
- Role matrix returns correct `401/403` behavior.
- Dry-run input errors map to JSON pointers.
- Approval decisions require expected hash and do not claim immediate dispatch.
- Timeline sequence is complete and stable.
- Concurrent timeline writers cannot reuse a per-run sequence.
- Artifact HTML is not executable.
- Pagination is deterministic and bounded.
- OpenAPI regeneration is clean.

## Exit criteria

- Every required backend function has a versioned route and typed DTO.
- Local identity is credential-free, loopback-only, clearly labeled, and production-fail-closed.
- Mutable operations enforce application authorization and optimistic concurrency.
- Errors are stable, field-addressable, and redacted.
- OpenAPI generates the frontend contract without manual duplicate DTOs.
- API integration and contract suites pass.

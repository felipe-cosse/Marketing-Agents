# Local identity and authorization

This document describes one local installation, not production authentication. Claim labels follow [plan 15](implementation-plan/15-local-operations-documentation-and-release.md#documentation-claim-taxonomy). See [security](security.md) for the surrounding trust boundaries and [ADR-0006](adr/0006-local-identity.md) for the accepted local-v1 decision.

## Who is acting?

**Implemented and verified.** [Settings](../apps/api/src/marketing_agents/config.py) default to `AUTH_MODE=local`, actor `local-operator`, roles `viewer`, `operator`, `approver`, `local_admin`, and scopes `approvals:read`, `approvals:request`, `approvals:decide`, `scope.external-write`. These are server configuration, not fields a browser may choose. The [local identity adapter](../apps/api/src/marketing_agents/infrastructure/adapters/identity.py) issues an integrity-checked human principal. Bearer credentials are rejected in local mode; malformed/duplicate authorization and caller-supplied identity headers fail before application commands.

Only local authentication is currently supported by settings. Native listeners must remain loopback-bound, forwarded trust is disabled, and production/local combinations fail closed. Compose uses a non-published Unix-domain API socket and exposes only the loopback web origin. The web proxy forwards an explicit header allowlist, stripping identity/forwarding assertions; direct API requests containing forwarded headers are rejected. Evidence: [identity dependency tests](../tests/integration/api/test_run_10_identity_dependency.py), [transport tests](../tests/integration/api/test_api_09_transport_security.py), [proxy configuration](../docker/web.conf).

**Assumption and residual risk.** A process or person with local access shares this configured identity. There is no per-person login, tenant boundary or production administrator account. “Server-issued” describes where the application gets authority, not cryptographic protection against a malicious host or modified Python runtime. Changing the displayed actor string does not authenticate another person. Future production identity, session lifecycle and multi-tenant authorization are deferred work behind the [identity port](../apps/api/src/marketing_agents/application/ports/identity.py).

## Permission matrix

**Implemented and verified.** Both route dependencies and application policies enforce permission. Roles are not a universal hierarchy: an extra `local_admin` role does not by itself confer the separate approval-decision scopes.

| Operation | Required authority |
|---|---|
| Catalog and installation-wide run/artifact/audit reads | Intact human principal with at least one of `viewer`, `operator`, `approver`, `local_admin` |
| Read approval resources | A human read-capable role plus `approvals:read` |
| Reuse an existing initial approval request or renew an expired request for an unchanged action | Approval-read permission plus `operator` and `approvals:request`; the initial complete request chain must already exist |
| Approve or reject an action | Human `approver` plus `approvals:decide`, every additional role/scope captured by the action policy, and its self-approval rule |
| Submit manual/demo/dry-run work | Human `operator`; execution remains restricted to supported modes and workflow/input policy |
| Update deployment instance configuration | Human `local_admin`; expected revision and deployment-only field validation still apply |
| Submit a webhook | Verified source/trigger service authority; never the browser's local human principal |
| Admit a schedule occurrence | Internal claimed-occurrence path; a caller-supplied actor or “approved” input field grants no scheduler/human-approval authority |

Authority sources: [catalog](../apps/api/src/marketing_agents/application/policies/catalog_authorization.py), [runtime reads](../apps/api/src/marketing_agents/application/policies/runtime_resource_authorization.py), [approval resources](../apps/api/src/marketing_agents/application/policies/approval_resource_authorization.py), [approval decisions](../apps/api/src/marketing_agents/application/policies/approval_authorization.py), [manual work](../apps/api/src/marketing_agents/application/policies/manual_work_authorization.py), [instance configuration](../apps/api/src/marketing_agents/application/policies/instance_configuration_authorization.py), [schedule occurrence admission](../apps/api/src/marketing_agents/application/services/schedule_occurrence_ingress.py).

The read scope is `single-local-installation`, not ownership of individual runs. Service principals cannot grant human approvals or browse the human control plane. Configuration permission cannot rewrite template prompts, schemas, capabilities, classification or approval policy; those remain version-controlled catalog authority.

The request API does not create an initial authorization chain from an arbitrary
action. A missing chain fails closed. Its renewal path replaces only an expired
request for the unchanged action and set epoch; semantic changes require a new
complete authorization-set epoch and are not implemented by the renewal endpoint.
See [request service](../apps/api/src/marketing_agents/application/services/approval_resources.py)
and [approval integrity](../apps/api/src/marketing_agents/application/services/approval_integrity.py).

## Session and mutation flow

**Implemented and verified.** The browser retrieves `GET /api/v1/session` from its local origin. The response uses `Cache-Control: no-store` and includes the actor/roles/scopes, safe modes, the visible “Local identity — not production authentication” warning, `csrfToken` and `csrfHeaderName: "X-CSRF-Token"`. Treat the token as ephemeral secret material: do not persist it in browser storage, copy the response into diagnostics or commit it.

For a browser/control-plane mutation, use the same origin, JSON content, `Sec-Fetch-Site: same-origin`, and the current `X-CSRF-Token`. A missing, duplicate, wrong or stale token, untrusted Origin/Host, forbidden forwarding header, or form/text mutation is rejected before the handler. The token is process-local and rotates when the API restarts; reload the page/session rather than replaying an old token. CSRF defenses do not authenticate a local process that can already read the session.

Webhook routes are specifically exempt from browser CSRF because their raw-body signature is the authentication boundary; an arbitrary route prefix does not gain that exemption. Do not add browser actor headers to a webhook or substitute the CSRF token for an HMAC secret. Sources: [session route](../apps/api/src/marketing_agents/api/routes/session.py), [token provider](../apps/api/src/marketing_agents/api/csrf.py), [transport boundary](../apps/api/src/marketing_agents/api/middleware/transport_security.py).

**Deferred real-adapter work.** The single-process CSRF source is not a shared multi-replica session system. Public exposure, reverse-proxy trust, federated identity and production authorization need their own reviewed implementation; the local mode must not be enabled as a shortcut.

## Approval identity and self-approval

**Implemented and verified.** The server records the authenticated actor and authority that matched the persisted approval policy. It does not accept a decision actor from the request body. Approval requires the expected canonical payload hash, an unchanged action and a valid unconsumed request. Baseline human permission is checked before resource lookup; service, missing-role, missing-scope and disallowed self-approval cases fail closed. See [actor authorization tests](../tests/unit/application/test_run_10_authorized_approval_actor.py) and [durable decision tests](../tests/integration/db/test_run_10_authorized_approval_decisions.py).

**Assumption.** The local external-write policy deliberately allows the same local operator to request and approve an action, as recorded in [catalog approval policies](../catalog/v1/approval-policies.yaml). The authorization code also enforces policies that forbid self-approval; do not describe local-v1 self-approval as dual control. A human decision does not skip capability, scope, expiry, idempotency or the complete approval-set barrier. The Email demo needs both action approvals before either mock write can start.

## When a request is rejected

**Implemented and verified.** Stable problem responses distinguish invalid transport (`400`), absent/rejected identity (`401`), insufficient permission/browser policy (`403`), and stale state/hash/revision conflicts (`409`); validation and readiness failures have their own codes. Inspect the code and refresh only the relevant session or resource. Never “fix” a denial by adding an actor/role header, widening network exposure or disabling approval checks. Share sanitized codes and revision information, not the session token or submitted payload. See [operations](operations.md) and [verification](verification.md) for supported checks and recorded results.

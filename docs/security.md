# Security boundaries and limitations

This is a credential-free, single-installation local demonstration, not a production marketing service. Use synthetic inputs. The labels below follow the [documentation claim taxonomy](implementation-plan/15-local-operations-documentation-and-release.md#documentation-claim-taxonomy). “Implemented and verified” means deterministic repository tests exercise the stated control; it does not mean a production deployment or a live provider was assessed. Release results belong in [verification](verification.md).

## Trust boundaries

| Boundary | Protected asset and control | Claim |
|---|---|---|
| Browser to API | Server-issued local principal, role/scope checks, trusted Host, JSON-only mutations, same-origin CSRF and Fetch Metadata checks | Implemented and verified |
| Webhook sender to admission | Signature verification precedes admission; signed content remains untrusted, with source/trigger binding and replay/collision checks | Implemented and verified by repository webhook tests; not a live provider integration |
| Workers to persisted work | Database claims, leases, conditional state changes, stable occurrence/action identities and transactional audit | Implemented and verified by deterministic repository tests |
| Catalog to execution | Version-controlled schemas, capabilities, approval policies and explicit workflow plans are trusted developer inputs; instance configuration cannot replace them | Implemented and verified |
| External text to model/planner | Immutable source-labeled untrusted content; structured model output is data, not tool-selection authority | Implemented and verified; model results are deterministic mock behavior |
| Dispatcher to connector | Exact immutable action authorization, all-required-approvals barrier, durable delivery identity and receipt handling | Implemented and verified; delivered effects are deterministic mock behavior |
| Database to operator/browser | Redacted bounded projections, artifact provenance and inert rendering | Backend projections implemented and verified; browser implementation has dedicated tests, with live-browser results recorded separately |
| Host to installation | Host filesystem, Docker daemon, source tree, process owner and database administrator remain trusted | Assumption; not a sandbox against a compromised host |

The associated assets include personal input, approval actor/decision integrity, action destination and payload, catalog policy, digest-key material, audit history and availability budgets. The broader planning threat register is in [plan 13](implementation-plan/13-security-privacy-audit-and-retention.md); its proposed controls are not automatically claims of implementation.

## Local identity is not authentication for remote users

**Implemented and verified.** The default `local-operator` is a server-owned human principal with viewer, operator, approver and local-admin roles. Requests cannot supply an actor or elevate roles using arbitrary headers. A bearer token is rejected in local mode; it does not select a different person. The current settings support only `AUTH_MODE=local`, reject production/local combinations and reject public native API binds. See [identity and authorization](identity-and-authorization.md) for the exact permission matrix and CSRF flow.

**Assumption and residual risk.** All users and processes that can legitimately reach this local installation effectively share its configured principal. The local action-approval policy permits self-approval. This is neither multi-tenant isolation nor separation of duties, and a local-admin role is not a production administrative login. Do not expose the listener through a public bind, tunnel or unreviewed proxy. See [ADR-0006](adr/0006-local-identity.md).

## Approval and delivery integrity

**Implemented and verified.** An approval binds one canonical action, including its type, capability, destination, payload, run/step binding, scope and expiry. The server authenticates the decision actor and rechecks roles/scopes, expected action hash, state and expiry. Conditional persistence prevents concurrent reuse. Changing an action requires a valid replacement approval rather than inheriting an earlier decision. Workflows with multiple required actions reserve the complete unchanged approval set before dispatch; approving only one of the Email demo's two actions cannot release either call. A successful approval response is a decision record, not a claim that delivery completed.

**Deterministic mock behavior.** Durable mock connectors replay one receipt for the same delivery key. Tests exercise denied calls, tampering, reuse and concurrency; see [write authorization tests](../tests/security/test_safe_02_external_write_authorization.py), [approval repository tests](../tests/integration/db/test_api_06_approval_repository.py), [ADR-0005](adr/0005-action-scoped-approval.md) and [ADR-0007](adr/0007-external-action-delivery.md).

**Implemented and verified, with a narrow renewal boundary.** The request API
reuses an existing initial request or renews an expired request for the exact
unchanged action; it does not manufacture a missing initial approval chain.
Semantic action changes fail with `full_set_epoch_required` in the integrity
replacement path and need a new complete authorization-set epoch. The renewal
endpoint is not a semantic replanning implementation. See
[approval integrity](../apps/api/src/marketing_agents/application/services/approval_integrity.py).

**Deferred real-adapter work and residual risk.** A database commit and a remote effect are not one transaction. Provider idempotency or verified lookup must resolve retry safety; an ambiguous effect may require `outcome_unknown` and operator reconciliation. Do not blindly replay it or claim universal exactly-once delivery. Mock receipt tests do not prove email was sent, CRM was updated, or content was published to an external service. See [adapter contracts](adapter-contracts.md).

## Untrusted content, tools and URLs

**Implemented and verified.** Posts, comments, email, transcripts, webpages, webhook input, connector observations and prior artifacts enter as untrusted data. Prompt text saying “approved” or naming a tool does not issue authority. The deterministic planner and capability/write guards select and authorize operations; model output must satisfy the declared schema. Capability, byte, step, call and time budgets are separate controls, not instructions the model may override. Evidence: [untrusted-content tests](../tests/security/test_safe_04_untrusted_content.py), [runtime policy tests](../tests/security/test_safe_06_runtime_policy.py), [content contract](../apps/api/src/marketing_agents/security/content_trust.py).

**Implemented and verified.** There is no generic crawler, browser, scraper or arbitrary-URL fetch adapter in the supported execution path. Supplied inputs and committed fixtures cannot request network access. Official-API declarations require explicit resource IDs and a terms-review reference. Backend reference URLs are inert provenance, with HTTPS/host/port restrictions; validation does not resolve DNS or fetch the URL. Evidence: [source-access policy](../apps/api/src/marketing_agents/security/source_access.py), [URL policy](../apps/api/src/marketing_agents/security/url_policy.py), [source-access tests](../tests/security/test_safe_03_compliant_source_access.py).

**Deferred real-adapter work.** A future fetch adapter needs connection-time DNS/IP checks, redirect revalidation, connector-specific destinations, size/time limits and actual provider/terms review. The existing provenance validator is not an SSRF-safe HTTP client. Injection tests establish deterministic guard behavior, not that every future model output will be truthful or harmless.

## Secrets, defaults and network isolation

**Implemented and verified in local controls.** The supported runtime requires mock model and connector modes, `ALLOW_EXTERNAL_NETWORK=false` and non-production local identity. Settings contain independent real-model/real-connector opt-ins, but those gates are not a shipped live integration: [runtime composition](../apps/api/src/marketing_agents/workers/runtime/composition.py) rejects non-mock/network-enabled operation. No provider credential is needed for `make up`. Credentials and webhook signing material must not enter catalog content, request diagnostics, source control or ordinary reports.

**Implemented but not live-tested by this documentation change.** In [Compose](../compose.yaml), API, run worker, scheduler worker and initialization services use `network_mode: none`. The API communicates with the web proxy through a local Unix-domain socket. Only the web port is published on `127.0.0.1`; runtime users are non-root, root filesystems are read-only and capabilities are dropped. Registry access during frozen image/dependency acquisition is distinct from application-provider egress. [Clean-state verification](../scripts/del_05_clean_state.py) checks actual network modes and scoped resources; consult its recorded run rather than inferring a deployment result from YAML alone.

**Residual risk.** The web container uses a NAT ingress bridge and has an outbound route; not every container is network-isolated. It has no database or digest-key mount and proxies only the fixed local socket. Native development likewise relies on application controls and host networking, not Docker's `network_mode: none`. A compromised web container or host is outside the no-backend-egress guarantee.

## Data, audit and browser safety

**Implemented and verified.** Central redaction uses sensitivity metadata, bounded JSON-pointer policies and key-name heuristics. Operator/API/audit projections are distinct from restricted data needed for execution. Audit metadata is event-specific and bounded; associated state/audit writes are transactional, and product APIs provide no audit update/delete operation. Expired optional audit metadata is hidden in projections while the event skeleton remains visible. See [data handling](data-handling.md) for the precise retention limitation and [audit redaction tests](../tests/security/test_orch_09_audit_redaction.py).

**Implemented but not live-tested by this documentation change.** [Artifact rendering](../apps/web/src/features/artifacts/ArtifactPayloadView.tsx) uses React text and a restricted Markdown renderer, not executable raw HTML or remote embeds. Links are separately validated HTTP(S) navigation with `noopener noreferrer` and no referrer; they are not provider requests authorized by the application. [Web CSP](../docker/web.conf) restricts scripts/assets/connections to the local origin, denies framing and permits inline styles—not arbitrary inline scripts. [Renderer tests](../apps/web/src/features/artifacts/ArtifactPayloadView.test.tsx) cover hostile markup and links. Clicking an allowed external link still leaves the local application; it is not a content-safety endorsement.

**Residual risks.** SQLite and paired backup bundles are not application-encrypted. A host/database administrator can read restricted content or alter records; append-only application audit is not tamper-proof storage. Redaction is schema/field based, not universal PII discovery in arbitrary text. Configured TTLs are not automatic deletion: no physical retention sweeper is wired into the shipped workers. Use synthetic data, protect the database/key/backup pair and do not attach it to issues. Production authentication, encryption/key management, retention enforcement, provider privacy and operational reliability remain separate work.

## Reporting a problem

Stop local use if the mode warning is wrong, an approval hash/destination differs from what you reviewed, or readiness reports a database/key mismatch. Preserve only sanitized error codes, source revision, test command and relevant safe IDs; never share credentials, full prompts, session/CSRF responses, raw webhook bodies or backup bundles. Use [operations](operations.md) for scoped shutdown and recovery. Do not “repair” a missing digest key by generating another beside existing data, or bypass a failed authorization check to continue a demo.

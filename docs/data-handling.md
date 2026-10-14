# Data handling, retention and local recovery

Use synthetic data for the local demo. Claim labels follow [plan 15](implementation-plan/15-local-operations-documentation-and-release.md#documentation-claim-taxonomy); the accepted policy is [ADR-0010](adr/0010-data-retention.md). This document distinguishes logical field erasure from projection expiry and physical media sanitization. It is not a production privacy, deletion or backup guarantee.

## Classification and representations

**Implemented and verified.** [Data classifications](../apps/api/src/marketing_agents/domain/data_classification.py) are ordered `public`, `internal`, `personal`, `sensitive`, `secret`. Structured values and artifacts inherit relevant source sensitivity; classification does not itself encrypt data.

| Class/material | Examples | Handling boundary |
|---|---|---|
| Public | Approved catalog descriptions, committed synthetic examples | May appear in product/source documentation; external text is still untrusted |
| Internal | States, bounded counts, schema versions, operational IDs | Minimize and authorize views; identifiers are not unrestricted telemetry labels |
| Personal | Email, names, handles or customer references | Apply schema/field redaction to operator/audit projections; restricted execution data can still require host protection |
| Sensitive | Partner application text, comments, transcripts and derived artifacts | Bound inputs, preserve classification/provenance, expose only the permitted projection |
| Secret | Provider credentials, passwords, signing material | Never retain as application payload/audit/artifact content; dedicated runtime secret sources only |
| Pseudonymous integrity material | Action hashes, keyed admission/body digests, idempotency and correlation identities | Integrity/replay use, not anonymization; keep out of unbounded logs/metric labels and unauthorized exports |

The application separates validated data needed for execution/recovery from the redacted representation intended for operators. Do not assume a redacted API response means the original is absent from the database. Likewise, an artifact classification is not evidence that every free-text personal detail was detected.

**Implemented and verified.** The [central redactor](../apps/api/src/marketing_agents/security/redaction.py) copies nested structures, respects `x-sensitive`/`x-data-classification`, supports bounded explicit JSON-pointer redaction and uses suspicious-key masking as defense in depth. `SecretValue` and secret settings mask their display representations. Artifact reads apply persisted producer redaction fields and reject secret-classified artifacts; audit metadata has event-specific allowed fields and limits. Evidence: [redaction/TTL tests](../tests/security/test_safe_07_redaction_retention.py), [input projection tests](../tests/security/test_run_06_input_redaction.py), [timeline canaries](../tests/security/test_run_06_timeline_redaction.py), [artifact projection](../apps/api/src/marketing_agents/application/services/artifact_resources.py).

**Residual risk.** Field/schema redaction is not a general data-loss-prevention classifier for arbitrary text. Mark sensitive fields when authoring schemas, use synthetic canaries in regression tests, and review new projections/diagnostics before adding real data. A developer must not bypass redaction by serializing raw request, provider, connector or exception objects.

## Logs, audit and exports

**Implemented and verified.** Audit construction accepts typed, bounded, redacted event metadata. State transitions and approval decisions use transactional audit writes; failure to persist their audit fact must not leave an unaudited successful transition. The normal repository/product surface is append-only. Audit/run query projections omit expired optional metadata and preserve the visible event skeleton with expiry indicators. See [audit metadata](../apps/api/src/marketing_agents/security/audit_metadata.py), [audit resource projection](../apps/api/src/marketing_agents/application/services/audit_resources.py), [transactional audit tests](../tests/integration/db/test_orch_09_audited_step_state.py) and [expiry projection test](../tests/unit/application/test_api_07_audit_resources.py).

Do not include full prompts, admitted input, executable action bodies, raw webhook bytes/signatures, credentials, the session/CSRF response, or secret-bearing backups in ordinary logs, issue attachments or verification evidence. Prefer source revision, safe error code, bounded status/count/duration and the minimal required correlation reference. An action hash provides integrity, not confidentiality; low-entropy values can be guessed. Even pseudonymous material remains access-controlled operational data.

**Residual risk.** Application append-only semantics are not tamper-proof storage. A local database administrator can alter rows. This release does not claim cryptographic audit-chain verification or a deployed production logging/metrics/retention service. Local structured diagnostics and process-local metrics are described in [observability](observability.md); they never replace transactional audit facts.

## Recorded expiry and bounded retention maintenance

The six independent settings below accept whole days from 1 through 3,650. [RetentionPolicy](../apps/api/src/marketing_agents/domain/retention.py) calculates UTC expiry and rejects secret retention; [settings](../apps/api/src/marketing_agents/config.py) and [.env.example](../.env.example) define the defaults.

| Setting | Default | Intended detail category |
|---|---:|---|
| `RETENTION_ADMITTED_PAYLOAD_DAYS` | 7 | Admitted execution/input detail |
| `RETENTION_EXTERNAL_ACTION_PAYLOAD_DAYS` | 7 | External-action execution detail |
| `RETENTION_APPROVAL_DETAIL_DAYS` | 7 | Redacted approval detail |
| `RETENTION_ARTIFACT_DETAIL_DAYS` | 30 | Artifact detail |
| `RETENTION_CONNECTOR_RECEIPT_DETAIL_DAYS` | 30 | Connector/mock receipt detail |
| `RETENTION_AUDIT_METADATA_DAYS` | 90 | Optional audit metadata |

The real runtime records each new detail's expiry transactionally. Existing work, audit and standalone brief expiries remain authoritative. Migration `0011` is frozen schema-only DDL; maintenance separately registers a bounded batch of legacy details with their original explicit expiry or the approved historical default. New settings never retroactively change those legacy defaults. Changing settings affects new records, not old expiry values; historical approval/scheduler witness reconstruction uses the original event's TTL. Approval validity expiry remains a separate authorization rule.

**Implemented; candidate verification required.** The scheduler process invokes a [bounded maintenance service](../apps/api/src/marketing_agents/application/services/retention_maintenance.py) with an injected clock. One durable UTC-minute occurrence processes at most 50 details (the service's hard maximum is 100), with a lease and frozen cutoff/scope. A restarted/concurrent process must respect that occurrence's claim and completion. Field erasure, authenticated retained skeletons, completion and one count-only audit summary commit together; a failed audit or lost lease rolls back the batch.

Only expired, eligible detail is erased. Active runs, unresolved actions, incomplete/retry-needed attempts and live worker claims protect recovery material. Approval and receipt detail is removed from its duplicate storage locations too. Required transition/decision audit facts, ordering and timestamps remain; only optional metadata expires. Eligibility is rechecked in the maintenance transaction. Never manually delete rows needed for recovery.

Deleted detail is explicitly non-executable. Existing run indexes and transition timelines retain their safe history; detail reads may return `410 detail_expired`, and `/api/v1/retained-details/{record_kind}/{record_id}` provides an authenticated identity-only skeleton. Exact expired work retries still match keyed input/admission and projection witnesses before returning expiry; a different payload still conflicts. No retry creates replacement work or revives deleted content. Binding digests are retained internally for replay fencing, not returned by this endpoint or used as metric labels.

**Residual risk.** This is logical field erasure, not a secure overwrite of SQLite pages, WAL, filesystem snapshots or backups. Bounded throughput and recovery exclusions mean TTL is an eligibility threshold, not a deletion SLA. A stopped scheduler does not purge. Preserved operational identities/digests can remain pseudonymous data; no automatic lifecycle for the entire installation is claimed. Use synthetic inputs and control host and backup lifetimes. Full qualification and scope are recorded in [EXEC-06](verification/requirements/EXEC-06.md), not inferred from these configuration values.

## Local digest key and storage

**Implemented and verified by local lifecycle tests.** A per-installation random digest key is stored separately from SQLite; the database stores only its non-secret fingerprint/version. The shared [initializer](../apps/api/src/marketing_agents/workers/local_secret_init.py) and [installation checks](../apps/api/src/marketing_agents/infrastructure/db/local_installation.py) create it atomically only for an empty installation, enforce owner-only directory/file permissions (`0700`/`0600`) and never replace it beside existing data. API/workers load the same key, and migrations/readiness verify pairing before accepting work. Compose mounts the key read-only into the running backend services. See [key creation](../apps/api/src/marketing_agents/security/digest_key.py) and [repeat-initialization tests](../tests/integration/db/test_del_04_database_cli.py).

The stored key is an intentional, protected local secret—not an exception permitting provider credentials in application data. Keyed admission/webhook digests provide stable internal equality/replay checks; canonical approval hashes serve a different action-integrity purpose. Neither is encryption or anonymization. No automatic key rotation is implemented. Losing the original key beside existing data is a recovery failure; do not generate a substitute and continue.

**Residual risk.** SQLite is not application-encrypted. Host filesystem owners, sufficiently privileged local users and Docker administrators can access restricted execution content and key material. Protect the host and its backups; do not describe a private file mode as protection from its administrator.

## Paired backups and restore

**Implemented and verified by local backup tests; not a production recovery claim.** The local [backup tooling](../scripts/local_backup.py) treats the database and digest key as one recovery unit. Protected bundles include checksums and identity/schema metadata, validate the pair and publish through staged operations. Restore targets must be explicitly new empty storage; invalid/missing components, unsafe permissions or a mismatched pair fail closed. Native and Compose backup use the SQLite online-backup implementation, which includes committed WAL contents; the helper does not automatically stop the running application. Commands and exact target choices are documented in [operations](operations.md), with evidence in [backup tests](../tests/integration/db/test_del_05_backup.py) and [transport tests](../tests/tooling/test_del_05_backup_transport.py).

Treat every bundle as secret-bearing and potentially personal/sensitive. Keep it out of Git, ordinary exports and CI reports; apply its own controlled lifetime because application TTL settings do not delete backups. Never copy only a live SQLite file while ignoring WAL/paired-key requirements, overwrite an active installation, or use broad Docker/file cleanup. No measured production RPO/RTO, remote encrypted backup service or disaster-recovery SLA is claimed.

## Models, connectors and future data transfers

**Deterministic mock behavior.** Supported runtime execution uses local deterministic model/connector adapters without transmitting inputs to a provider. This establishes local behavior only, not consent, provider data handling or production service outcomes.

**Deferred real-adapter work.** Before enabling a real provider, implement and verify the adapter and network boundary, minimize transmitted fields, review provider storage/retention and terms, establish authorization/consent and secret management, and resolve retry/unknown-outcome behavior. Existing configuration opt-ins alone do not supply a live adapter. See [adapter contracts](adapter-contracts.md) and [security boundaries](security.md).

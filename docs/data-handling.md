# Data handling, retention and local recovery

Use synthetic data for the local demo. Claim labels follow [plan 15](implementation-plan/15-local-operations-documentation-and-release.md#documentation-claim-taxonomy); the accepted policy is [ADR-0010](adr/0010-data-retention.md). This document distinguishes current protection and projection behavior from a future retention maintenance service. It is not a production privacy, deletion or backup guarantee.

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

**Residual risk.** Application append-only semantics are not tamper-proof storage. A local database administrator can alter rows. This release does not claim cryptographic audit-chain verification or a deployed production logging/metrics/retention service.

## Configured retention is not automatic deletion

**Implemented and verified — policy and projection only.** The six independent settings below accept whole days from 1 through 3,650. [RetentionPolicy](../apps/api/src/marketing_agents/domain/retention.py) calculates UTC expiry and rejects secret retention; [settings](../apps/api/src/marketing_agents/config.py) and [.env.example](../.env.example) define the defaults.

| Setting | Default | Intended detail category |
|---|---:|---|
| `RETENTION_ADMITTED_PAYLOAD_DAYS` | 7 | Admitted execution/input detail |
| `RETENTION_EXTERNAL_ACTION_PAYLOAD_DAYS` | 7 | External-action execution detail |
| `RETENTION_APPROVAL_DETAIL_DAYS` | 7 | Redacted approval detail |
| `RETENTION_ARTIFACT_DETAIL_DAYS` | 30 | Artifact detail |
| `RETENTION_CONNECTOR_RECEIPT_DETAIL_DAYS` | 30 | Connector/mock receipt detail |
| `RETENTION_AUDIT_METADATA_DAYS` | 90 | Optional audit metadata |

These are configured policy values, not promises that every corresponding row is physically removed after that interval. Stored metadata carries its recorded expiry; changing a setting is not a retroactive purge. Audit projections hide expired metadata while preserving event type, transition/decision fact, sequence/time and the safe correlation skeleton. Hiding an API field is not disk erasure.

**Acceptance target not yet verified / unimplemented maintenance.** There is no physical retention sweeper wired into the shipped API, run worker or scheduler worker. The plan's leased, concurrent/idempotent maintenance job, active-work exclusions, count-only summary and physical deletion/pseudonymization across eligible payload classes remain work to implement and verify. Do not infer that the 7/30/90-day settings establish a complete deletion SLA or legal retention policy. Approval validity expiry is a separate authorization rule; it is not evidence that expired approval detail was purged.

**Residual risk.** Until maintenance exists, stored detail and backups can outlive configured TTLs. Use synthetic inputs and control access/lifetime of the entire scoped installation. Do not manually delete live rows needed for approval binding, idempotency, audit or crash recovery; follow reviewed local lifecycle/recovery procedures instead.

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

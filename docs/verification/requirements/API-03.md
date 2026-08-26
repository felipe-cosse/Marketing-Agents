# API-03 verification

Status: verified locally

Agent-instance deployment configuration is now separate from the immutable compiled
template catalog. The authenticated structural schema route describes only `enabled`,
optional operator `variantLabel`, trigger bindings, registered mock connector bindings,
and schedule/misfire fields, and identifies the semantic consistency checks retained by
PATCH because JSON Schema cannot compare values across duplicated schedule fields. The
PATCH boundary rejects unknown or template-owned fields,
requires one exact strong revision ETag, preserves omitted values, supports explicit
null clearing only for nullable fields, and advances a material update by exactly one
revision. A no-op retains the existing revision and writes no audit event.

Configuration rows are integrity-sealed and persisted independently under stable
instance IDs. A validated 43-instance catalog is converted completely before the
transaction opens, then missing defaults are inserted without updating or deleting an
existing operator row. SQLite restart and repeat-seed tests prove that an override and
its revision survive. Registered connector families and binding IDs, supported trigger
kinds, IANA timezones, recurrence expressions, and schedule/trigger consistency are
validated before commit.

Every material mutation uses one compare-and-swap transaction for the configuration
replacement and the append-only `instance.configuration_changed` event. The audit
witness is runless, pseudonymizes the actor, validates raw old/new deployment snapshots
before transformation, and replaces every operator-controlled text value with a
field-domain-separated HMAC under the restart-stable installation digest key. Hydration
separately validates the stored pseudonym representation, while database checks require
non-null previous/observed/new revision witnesses. Stale writers receive `409` with the
current revision; an injected failure after the audit flush rolls back both the
configuration and audit row.

API-02 aggregate, hierarchy, instance-list, and instance-detail reads can consume a
complete persisted snapshot on every request while retaining one cached immutable
compiled catalog. Effective instance fields and their representation ETags change
coherently after configuration updates; template, capability, policy, prompt, schema,
purpose, classification, and source-order data remain source-owned and unchanged.

DEL-04 still owns an Alembic migration, full catalog relational persistence/FKs, and
production bootstrap that creates/migrates then invokes the seed before serving. API-09
still owns the unified problem+json vocabulary and browser Origin/CSRF protections, so
this JSON-only local-admin mutation must not yet be described as browser-CSRF-safe.
Ingress/planner/scheduler consumption, schedule materialization, real connector
registration, UI controls, and generated clients remain with their later requirements.

Machine authority: [`API-03.json`](API-03.json).

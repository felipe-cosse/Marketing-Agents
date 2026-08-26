# API-02 verification

Status: verified locally

The authenticated static read surface now includes the complete catalog and ordered
hierarchy plus the focused template, capability, approval-policy, and agent-instance
list/detail routes enumerated in Plan 09. Explicit API DTOs expose safe camel-case
metadata and resolved input/output JSON Schemas on detail routes without serializing
system prompts, compiler path fields, provider configuration, or runtime state.

The hierarchy is a direct department-to-function-to-instance projection. It preserves
compiler/source order, resolves shared template name, purpose, trigger support,
classification, and capability summaries for every instance, and derives rather than
copies the exact `5/12/36/43` counts and `12/6/5/14/6` department distribution. The
seven Community templates each remain two distinct `.01`/`.02` deployments with source
ordinals one and two.

Each response is serialized into sealed canonical bytes before publication. A strong,
representation-specific ETag covers the exact bytes served; both strong and weak
`If-None-Match` validators can produce `304` without letting nested mutable schema data
drift under an old tag. Compilation and projection run together off the event loop as
one lazy single-flight operation, and only a complete immutable result is cached.
Missing, blocking, malformed, mislabeled, or throwing dependencies fail closed with a
sanitized no-store `503`.

All routes authorize the server-issued human control-plane principal before loading.
Viewer, operator, approver, and local-admin roles retain the documented viewer-equivalent
read permission; unrelated humans and service principals do not. These GET routes do
not mutate catalog files, open a database transaction, invoke a provider or connector,
or require external network access.

API-03 and DEL-04 own persisted deployment configuration, configuration schema/PATCH,
seeding, and database parity. API-07 owns dynamic status and recent-run fields. API-09
owns the unified problem response vocabulary, and DEL-07 owns the committed OpenAPI and
generated-client drift gate.

Machine authority: [`API-02.json`](API-02.json).

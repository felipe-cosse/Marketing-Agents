# Model and connector adapter contracts

Adapters implement application-owned ports. Catalog role names and product logos
do not establish a working vendor integration. The labels below distinguish
recorded contract verification, deterministic behavior, and work that still
requires a real provider. Current aggregate status is in [verification](verification.md).

## Shipped mode and composition

Claim: Deterministic mock behavior — the shipped local runtime uses
`LLM_PROVIDER=mock`, `CONNECTOR_MODE=mock`, and `ALLOW_EXTERNAL_NETWORK=false`,
without provider credentials. The
[local composition root](../apps/api/src/marketing_agents/workers/runtime/composition.py)
rejects non-mock modes. Mocks exercise schemas, policies, approvals, persistence,
and restart behavior; they do not send messages, update remote contacts, crawl
websites, or measure live provider performance. See
[DEL-03](verification/requirements/DEL-03.md) and
[DEL-05](verification/requirements/DEL-05.md).

Claim: Implemented and verified — application/domain code depends on ports, not
vendor SDKs. Concrete implementations belong under
`infrastructure/adapters`, enforced by
[ARCH-08](verification/requirements/ARCH-08.md) and its
[boundary tests](../tests/unit/architecture/test_arch_08_repository_boundaries.py).

## Structured model boundary

Claim: Implemented and verified —
[`LLMProvider.generate_structured`](../apps/api/src/marketing_agents/application/ports/llm.py)
accepts an immutable, strict `LLMRequest` with separate trust classes:

- Catalog-owned `TrustedSystemInstructions`, including template/catalog identity.
- Retrieved `UntrustedContentPart` items and `UntrustedToolResult` observations.
- Exact output schema ID, hash, and schema plus bounded run/step/correlation,
  UTC deadline, and output-token context.

`LLMResponse` contains a structured payload and bounded provider/model/version,
finish-reason, and usage metadata. It has no executable tool-call field. The
[validator](../apps/api/src/marketing_agents/infrastructure/adapters/llm/validation.py)
checks request/output schema identity and central bounds rather than trusting the
adapter’s declarations. Evidence:
[ARCH-06](verification/requirements/ARCH-06.md) and
[model contract tests](../tests/contract/test_arch_06_llm_provider.py).

Claim: Deterministic mock behavior — the
[deterministic provider](../apps/api/src/marketing_agents/infrastructure/adapters/llm/deterministic.py)
resolves registered exact template/output-schema pairs. The five demos use
explicit renderers; a valid catalog template does not automatically acquire a
renderer. Mock content/usage is fixture behavior, not model quality or billing
evidence.

Claim: Deferred real-adapter work — the
[factory seam](../apps/api/src/marketing_agents/infrastructure/adapters/llm/factory.py)
supports an explicitly registered, case-sensitive provider ID only with external
network permission, independent real-LLM opt-in, and a nonempty credential. Missing
registration, factory errors, malformed responses, or changed provider identity
fail closed; there is no silent switch to a mock. This is an extension boundary,
not a shipped, credential-tested real model integration. The local executable
composition remains mock-only even if individual factory tests use injected
real-provider test doubles.

## Connector operation matrix

Claim: Implemented and verified — the immutable
[registry](../apps/api/src/marketing_agents/infrastructure/adapters/connectors/registry.py)
cross-checks 20 operation registrations against the compiled catalog. Each declares
effect, exact request/result DTOs, schema IDs, idempotency support, timeout,
classification, rate scope, and redaction fields. The
[family ports](../apps/api/src/marketing_agents/application/ports/connector_families.py)
and [ARCH-07 contract tests](../tests/contract/test_arch_07_connector_contract_matrix.py)
are the authority; a family’s presence does not enable every possible operation.

Claim: Deterministic mock behavior — enabled registrations below execute against
local mock adapters, not external systems.

| Family | Registered READ operations | Registered WRITE operations |
| --- | --- | --- |
| Social | Posts, comments, metrics, profiles | None |
| Newsletter | None | Subscribe, unsubscribe; email send is disabled |
| CRM | Customer summary | Contact upsert |
| CMS | Supplied content metadata | None |
| Events | Sessions, attendance | Enroll attendee |
| Community | Membership, course progress | Send message, share material |
| Spreadsheet | Range | Row update is disabled |
| Fulfillment | Status | None |

`cap.email.send-message` and `cap.spreadsheet.update-rows` are explicitly disabled
as `unassigned_in_v1`. Reserved CMS/fulfillment mutation contracts have no
registered execution path. `cap.model.generate-structured` and
`cap.artifact.transform-deterministic` are non-connector families, not additional
external connector operations. Evidence:
[ARCH-07](verification/requirements/ARCH-07.md).

## Reads, writes, and durable receipts

Claim: Implemented and verified — a
[connector read request](../apps/api/src/marketing_agents/application/ports/connectors.py)
binds capability, configured binding, run/step/correlation, UTC deadline, timeout,
and provenance to a typed parameter object. Its observation retains
`untrusted_tool_result`, classification, and provenance when passed to an LLM.
A successful read never upgrades provider content into trusted instructions.

WRITE commands pair a typed payload with a sealed `AuthorizedExternalWrite`;
mutating adapters independently recheck exact authorization. A caller’s boolean
“approved” flag or arbitrary payload is not write authority. Planning, complete-set
approval release, dispatch claims, and call-start fences remain application
responsibilities; constructing a connector bundle grants none of them. See
[architecture](architecture.md), [ORCH-08](verification/requirements/ORCH-08.md),
and [RUN-05](verification/requirements/RUN-05.md).

Claim: Deterministic mock behavior —
[`build_durable_connector_bundle`](../apps/api/src/marketing_agents/infrastructure/adapters/connectors/composition.py)
requires the caller’s persistent unit-of-work factory and clock and supplies one
database-backed receipt ledger to write families. Construction performs no I/O or
schema creation. Reconstructing with the same database replays the original
receipt for the same key. The low-level process-local bundle is useful for isolated
tests but is ineligible for dispatcher writes. Evidence:
[DEL-03](verification/requirements/DEL-03.md) and
[durable composition tests](../tests/contract/test_del_03_durable_mock_composition.py).

Claim: Residual risk — a remote call and database commit are not one transaction.
Dispatch recovery reconciles durable receipts and permits same-key replay only
when the snapshotted connector contract supports it. An unresolved outcome without
safe provider idempotency/lookup becomes `outcome_unknown`, not an automatic second
effect. Mock receipt uniqueness is not proof of real-world exactly-once delivery.
See [ADR-0007](adr/0007-external-action-delivery.md).

## Adding a real integration

Claim: Deferred real-adapter work — real connector mode is explicitly unimplemented
and does not fall back to mocks. A production integration requires a separately
reviewed concrete adapter and composition path, provider credentials, restricted
network policy, and provider-specific verification. Do not commit credentials into
catalog bindings or treat changing an environment variable as integration completion.

Claim: Acceptance target not yet verified — before qualifying a real adapter,
verify exact schema/effect/binding parity, deadline and rate enforcement, redaction
and error handling, approval tamper/refusal, provider idempotency or outcome lookup,
concurrency, lost-response/crash recovery, and live side-effect reconciliation.
Preserve the existing mock/no-network contract suite. Contract tests and injected
test doubles alone do not establish those live-provider guarantees.

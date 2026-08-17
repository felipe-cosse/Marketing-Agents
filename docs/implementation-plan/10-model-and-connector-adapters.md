# 10 — Model and connector adapters

Status: planned

Depends on: [02 — Capability catalog](02-catalog-schema-seed-and-validation.md), [03 — Domain](03-domain-model-and-invariants.md), the early baseline from [13 — Security policies](13-security-privacy-audit-and-retention.md)

Unblocks: orchestration execution, demos, and adapter-contract acceptance

## Objective

Define provider- and SDK-independent ports, deterministic offline implementations, and a fail-closed adapter registry. Model output must be schema-valid data and must never choose tools. Mutating connectors must accept only a dispatcher-issued authorization proof and stable idempotency key.

## LLM provider port

```text
LLMProvider.generate_structured(request: LLMRequest) -> LLMResponse
```

`LLMRequest` separates trust classes:

- Trusted system instructions.
- Typed untrusted content parts with source/provenance IDs.
- Typed untrusted tool/read observations.
- Required output JSON Schema and schema ID.
- Provider/model selection from server configuration, not work content.
- Deadline, output/token budget, run/step/correlation metadata.

`LLMResponse` contains:

- Structured JSON payload only.
- Provider/mock/model/version metadata.
- Usage/counter data where available.
- Finish/result classification.
- Safe diagnostic metadata.

The application validates the output schema independently even if the provider claims structured-output enforcement.

The port exposes no model tool-calling interface in v1. The deterministic planner is the sole tool/capability selector.

## Deterministic mock model

Planned path:

```text
apps/api/src/marketing_agents/infrastructure/adapters/llm/deterministic.py
```

Behavior:

- No HTTP/client SDK import.
- Selects a registered fixture/renderer by workflow/template/output schema ID.
- Normalizes admitted input and computes a fixture key/hash.
- Produces deterministic business fields for the same input and mock version.
- Uses injected clock/ID values only for explicitly volatile metadata.
- Validates its own result before returning.
- Supports deterministic failure injection: timeout, transient error, malformed output, oversized output.
- Records attempts through the runtime, not by logging raw prompts.

Unknown workflow/schema pairs fail closed; the mock never improvises an arbitrary shape.

## Optional real LLM adapter

After mock acceptance passes, add one optional real-provider adapter behind an extra dependency and lazy import. The ADR should choose the provider and current official structured-output API at implementation time.

Activation requires all of:

- Explicit provider selection, for example `LLM_PROVIDER=openai`.
- `ALLOW_EXTERNAL_NETWORK=true`.
- Non-local test environment policy permitting network.
- Valid provider configuration and credential loaded from environment/secret source.
- Requested model in a server allowlist.

It must never silently fall back between real and mock modes. Tests use a fake transport/SDK and never make a live call. A live smoke test, if ever run, is manual, separately authorized, and not part of offline acceptance.

## Connector ports

Use small async protocols with discriminated request/result types:

### Social connector

- Read posts/comments/metrics by explicit supplied IDs.
- No publish operation registered in v1 catalog.

### Newsletter/email connector

- Subscribe a contact.
- Unsubscribe a contact.
- Optional send operation exists in the port but is not used by initial demos.

### CRM connector

- Read a contact/customer summary.
- Upsert a contact.

### CMS connector

- Read supplied content metadata.
- Upload/update command reserved for future approved workflows.

### Calendar/events connector

- Read sessions/attendance.
- Enroll an attendee.

### Community/messaging connector

- Read membership/course progress.
- Send a message or share material.

### Spreadsheet connector

- Read a bounded range.
- Update bounded typed rows/points.

### Fulfillment connector

- Read fulfillment status.
- Define a create-fulfillment command only as an unregistered/reserved future operation; no v1 template, including Swag Tracker, receives that write capability.

Do not add a generic HTTP, browser, arbitrary SQL, shell, or scraping connector.

## Connector operation contract

Every operation declares:

- Stable capability ID.
- Read or write effect.
- Request/result schema IDs.
- Connector family and binding ID.
- Timeout and rate-limit scope.
- Sensitive/redacted fields.
- Provider idempotency support: `required`, `supported`, or `none`.
- Retry-safe error classifications.

Read request context contains deadline, correlation, provenance, and bounded parameters.

Write request context additionally requires:

- External-action ID/hash.
- Dispatcher-issued `ActionAuthorization` proof.
- Stable idempotency key.
- Approved exact typed command.

The adapter rechecks proof/capability/action consistency and rejects a write invoked directly by a planner, model, API route, or test without dispatcher context.

Implement this plan in two dependency-safe stages:

1. **Stage A, before orchestration:** provider/connector ports, typed DTOs, registry metadata, deterministic LLM, read-only mocks, write-operation interfaces, and a durable receipt-store port.
2. **Stage B, after plan 06:** concrete dispatcher authorization-proof validation, mutating mock integration, and crash/idempotency contract tests.

Stage A unblocks plan 05 without depending on approvals. Plan 06 consumes the Stage A write interfaces. Stage B then integrates the completed dispatcher, avoiding a circular dependency.

## Adapter registry

```text
apps/api/src/marketing_agents/infrastructure/adapters/registry.py
apps/api/src/marketing_agents/application/ports/llm.py
apps/api/src/marketing_agents/application/ports/connectors.py
apps/api/src/marketing_agents/infrastructure/adapters/llm/
apps/api/src/marketing_agents/infrastructure/adapters/connectors/mock/
```

Startup registry validation:

1. Load configured mode.
2. Register deterministic provider and mock bindings.
3. Reject duplicate capability/binding IDs.
4. Confirm every catalog capability has a compatible registered operation or is explicitly disabled.
5. Confirm effect/schema/idempotency metadata matches the catalog.
6. Reject a real binding when network opt-in is false.
7. Reject partial real configuration; never fall back silently.
8. Expose safe mode/capability status to readiness/session without secrets.

## Deterministic mock connectors

Mocks must:

- Never import an HTTP client.
- Return typed deterministic reads from fixtures.
- Validate all request/result schemas.
- Persist write receipts in `connector_action_receipts`.
- Deduplicate writes by binding plus idempotency key.
- Return the original receipt on duplicate invocation.
- Expose test-only counters through injected test ledger queries, not a public production endpoint.
- Support failure injection before side effect, after side effect/before response, transient error, permanent error, and timeout.
- Reject writes without valid action authorization.

Business fields derive from canonical input and mock version. Receipt IDs/timestamps may be injected and normalized in golden tests.

## Capability enforcement

Check allowlists at three layers:

1. Catalog compilation: template policy is internally safe.
2. Planning: requested step capability is on selected template snapshot.
3. Immediately before adapter invocation: capability, effect, binding, and action proof still match persisted snapshots.

Any denial produces a safe domain error and redacted audit event. Model output cannot name a capability field that bypasses these checks.

## Network policy

Defaults:

```text
LLM_PROVIDER=mock
CONNECTOR_MODE=mock
ALLOW_EXTERNAL_NETWORK=false
```

- Default containers should have no external egress where practical.
- Tests block non-loopback sockets and fail unhandled HTTP requests.
- No remote fonts, scripts, icons, schemas, or fixtures.
- Optional real adapters use official APIs only and require explicit profile/configuration review.
- URL validation never becomes a generic fetching permission.
- Provider/connector credentials never enter database, audit, logs, artifacts, or API responses.

## Timeouts and errors

Use async ports and a single timeout mechanism such as `anyio.fail_after` around calls.

Normalize adapter errors:

- `transient_unavailable`
- `rate_limited` with bounded retry metadata
- `deadline_exceeded`
- `invalid_request`
- `unauthorized_configuration`
- `permanent_failure`
- `ambiguous_outcome`
- `schema_invalid_response`

Adapters never decide retry themselves; they report classification and the application policy decides.

## Ordered implementation tasks

1. Freeze provider/connector DTOs and capability metadata.
2. Implement async ports with trusted/untrusted separation.
3. Implement registry and fail-closed settings validation.
4. Implement deterministic LLM fixture registry and failure modes.
5. Implement mock read operations for every required connector family.
6. Implement the mock write receipt-store primitive/interface; after plan 06, integrate dispatcher-proof validation and mutating mock calls as Stage B.
7. Add cross-checks between catalog and registered adapter metadata.
8. Add normalized errors, deadlines, and rate-limit metadata.
9. Implement optional real LLM adapter only after core mock gates pass.
10. Document the review checklist for any future real connector.

## Contract tests

```text
tests/contract/test_llm_provider.py
tests/contract/test_connector_registry.py
tests/contract/test_connector_read_operations.py
tests/contract/test_connector_write_authorization.py
tests/contract/test_connector_idempotency.py
tests/contract/test_connector_timeouts.py
tests/contract/test_adapter_errors.py
tests/contract/test_no_http_imports_in_mocks.py
tests/integration/runtime/test_capability_recheck.py
```

Each adapter contract implementation must prove:

- Request/result schema validity.
- Determinism for mock business fields.
- Deadline and cancellation propagation.
- Correct effect/capability metadata.
- Redaction metadata completeness.
- Write rejection without authorization proof.
- Duplicate idempotency key returns original receipt.
- Classified failure semantics.
- No network in default/test mode.

## Exit criteria

- Domain/application code imports only ports, never provider SDKs.
- Deterministic model and all connector-family mocks are complete.
- Model output cannot invoke or select a tool.
- Write adapters require exact dispatcher authorization and idempotency key.
- Adapter/catalog capability metadata matches.
- Real mode requires explicit provider plus network opt-in and valid configuration.
- No real write connector or accidental network path ships in default mode.
- All contract and no-network tests pass.

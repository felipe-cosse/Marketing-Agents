# 07 — Triggers, webhooks, and ingress

Status: planned

Depends on: [02 — Catalog](02-catalog-schema-seed-and-validation.md), [04 — Persistence](04-persistence-migrations-and-seeding.md), [05 — Orchestration](05-orchestration-dag-and-run-worker.md), and the early digest-key/redaction baseline from [13 — Security](13-security-privacy-audit-and-retention.md)

Unblocks: webhook API, scheduled work, replay acceptance, and deployment configuration

## Objective

Provide one admission path for manual, webhook, and schedule work so every source receives the same schema validation, instance eligibility, input bounds, work idempotency, audit, and orchestration behavior. Webhook payloads are signed but still untrusted content.

## Trigger definitions

`TriggerDefinition` contains:

- Stable trigger ID.
- Agent instance ID.
- Kind: `manual`, `webhook`, or `schedule`.
- Enabled state and optimistic configuration version.
- Kind-specific validated configuration.
- Source name and event-ID extraction rule where applicable.
- Input mapping version.
- Signature verifier reference for webhooks.
- Schedule reference for scheduled triggers.

The template lists supported trigger kinds; an instance binds actual trigger configuration. An instance cannot enable an unsupported trigger through the configuration API.

## Common intake contract

```text
IntakeRequest
  source
  event_id
  agent_instance_id
  trigger_id
  workflow_id
  execution_mode
  campaign_brief_id_and_revision
  instance_configuration_revision
  admitted_payload
  payload_digest
  admission_digest
  digest_key_version
  received_at
  principal_or_service_identity
  correlation_id
```

`IntakeService.admit()`:

1. Resolve and validate enabled trigger/instance/template/workflow.
2. Enforce source-specific body/input size limits.
3. Validate the input JSON Schema.
4. Apply URL/content safety policies.
5. Normalize the event ID and compute a versioned, domain-separated keyed admission digest over source/event/instance/trigger/workflow/execution mode/campaign-brief reference and revision/instance-config revision/canonical payload.
6. Insert or retrieve `WorkItem` under `(source,event_id,instance_id)` uniqueness.
7. If an identical admission digest under the installed key version exists, return its single original work/run IDs.
8. If the source/event/instance key exists with a different admission digest, reject with `409` and audit a collision. A changed workflow, trigger, execution mode, brief revision, config revision, or payload is a collision even when the raw payload bytes match.
9. Create exactly one primary Run and initial audit/transition atomically for a new WorkItem; `runs.work_item_id` is unique. A deliberate re-run requires a new source event/idempotency key rather than a second Run for the same WorkItem.
10. Return a stable `202` resource projection.

Expose two transaction forms:

- `admit(request)`: owns a unit of work for API/manual/webhook callers.
- `admit_in_uow(unit_of_work, request)`: performs the same validation/idempotency writes without committing, for the scheduler's occurrence/work/next-time atomic transaction.

The second form must never start or commit a nested transaction.

## Manual ingress

Endpoint shape:

```text
POST /api/v1/agent-instances/{instance_id}/dry-runs
Idempotency-Key: <caller-provided stable key>
```

Request fields:

- Typed `input` matching the template/workflow schema.
- Optional `campaign_brief_id`.
- Explicit execution mode: `dry_run` or `mock_execute` subject to server policy.
- Optional demo scenario ID only for registered demos.

Rules:

- Default mode is `dry_run`.
- `mock_execute` is permitted only while mock connectors are active.
- A caller-provided idempotency key is strongly encouraged and required for UI retries.
- If absent for an ad hoc manual run, generate a new unique event ID and return it; do not pretend repeated submissions deduplicate.
- Mutating dry runs may create proposed-action artifacts but cannot dispatch connectors.
- Required approval-boundary demos use `mock_execute` and still pause for human approval.

## Webhook signature port

```text
WebhookSignatureVerifier.verify(
  source,
  raw_body,
  received_headers,
  received_at,
  verifier_config
) -> VerifiedWebhookIdentity
```

Provide a local HMAC-SHA256 verifier for tests and documented local use:

- Verify raw bytes before parsing JSON.
- Require a signed timestamp and bounded freshness window.
- Use constant-time comparison.
- Bind signature to timestamp plus raw body.
- Reject missing, malformed, stale, or future-skewed signatures. A repeated valid signature remains authentic and proceeds to receipt/work idempotency; signature reuse alone is not rejected.
- Resolve secret by environment/config reference; never store it in catalog/database/audit.
- Apply a strict raw-body byte limit before buffering.
- Never provide a production fallback that silently disables signature verification.

Clean local startup may have no enabled webhook binding. The route then rejects an unconfigured source/trigger clearly. Tests inject a non-production secret; `.env.example` includes only an unset example value.

## Webhook processing algorithm

1. Match a configured trigger and source without trusting payload fields.
2. Read bounded raw bytes.
3. Validate timestamp and signature.
4. Compute the versioned, domain-separated keyed body digest using the installed local digest key.
5. Parse JSON with depth/size safeguards.
6. Validate the provider-envelope schema.
7. Extract/normalize a stable source event ID using the configured mapper.
8. Insert `webhook_receipts(source,event_id,body_digest,digest_key_version)` under a unique constraint after authentication.
9. For identical replay, return the prior outcome and do not create work.
10. For same event ID/different authenticated body digest, return `409` and create no new run. Keep keyed digest comparison inside the restricted receipt record; audit the collision and receipt IDs without treating or exposing raw hashes as safe metadata.
11. Map only allowlisted fields into the workflow input schema.
12. Route only to explicitly bound enabled instances.
13. Admit through `IntakeService` once per selected instance.
14. Return `202` with created/existing resource links; do not execute inline.

## Webhook security

- Payload text remains untrusted even after signature verification.
- Never use payload content to select arbitrary templates, capabilities, connector bindings, or destinations.
- Reject unsupported content types and encodings.
- Do not log raw body or signature headers.
- Bound nested object/array size and string lengths through envelope and workflow schemas.
- Apply per-trigger/source rate limits before expensive work.
- Reject URLs that fail the central URL policy; no generic fetch connector exists in v1.
- Distinguish authentication failure (`401`) from known-but-forbidden trigger (`403`) and payload/schema problems (`422`) without leaking secrets.

## Schedule ingress

The scheduler derives:

- `source = schedule`
- `event_id = occurrence_id`
- Bound instance/trigger/workflow IDs from trusted schedule configuration
- Scheduled time and catch-up metadata

It then calls `admit_in_uow(...)` inside the occurrence transaction. Unique occurrence, admission, and one-run-per-work constraints ensure retries/restarts return the same run without an inner commit.

## Deployment configuration API constraints

Instance configuration may:

- Enable/disable an allowed trigger kind.
- Bind a supported source mapper/signature verifier reference.
- Configure non-secret source identifiers.
- Create/update a validated schedule.
- Select a registered mock connector binding.

It may not:

- Add a trigger kind the template does not support.
- Embed a webhook secret.
- Change input/output schemas or mapping code.
- Change template capabilities, classification, or approval policy.
- Select an unregistered real connector when external network is disabled.

Use optimistic revision/`If-Match` semantics.

## Audit events

- `ingress.manual_received`
- `webhook.signature_validated`
- `webhook.signature_rejected`
- `webhook.received`
- `webhook.duplicate_suppressed`
- `webhook.idempotency_collision`
- `ingress.schema_rejected`
- `ingress.rate_limited`
- `work.created`
- `work.duplicate_returned`
- `trigger.configuration_changed`

Do not include raw bodies, signatures, secret references, or direct PII.

## Ordered implementation tasks

1. Add trigger schemas and catalog/instance semantic checks.
2. Define input mappers as registered deterministic code, not dynamic expressions.
3. Implement common intake DTO/service and work-idempotency handling.
4. Implement manual dry-run admission and idempotency header behavior.
5. Define signature-verifier port and HMAC implementation.
6. Add bounded raw-body middleware/helper.
7. Implement webhook receipt/collision logic and source mapping.
8. Route schedule occurrences through the same intake service.
9. Add deployment-only trigger configuration with optimistic concurrency.
10. Add rate limits, redacted audit, and stable problem responses.

## Tests

```text
tests/unit/application/test_intake_service.py
tests/unit/webhooks/test_hmac_verifier.py
tests/unit/webhooks/test_input_mapping.py
tests/integration/api/test_manual_idempotency.py
tests/integration/api/test_webhook_signature.py
tests/integration/api/test_webhook_replay.py
tests/integration/api/test_webhook_collision.py
tests/integration/api/test_webhook_bounds.py
tests/integration/api/test_trigger_configuration.py
tests/integration/scheduler/test_occurrence_ingress.py
```

Must prove:

- Identical manual idempotency key/payload returns the original Run.
- Same key with changed payload, workflow, trigger, execution mode, brief/config revision, or instance configuration conflicts.
- Invalid/missing/stale/future signatures fail before JSON processing/work creation.
- Identical webhook replay creates one receipt, work item, and run.
- Same source event ID with different body creates no second run.
- One event routed to multiple configured instances gets one work item per instance, not duplicates.
- Oversized/deep payload is rejected before provider invocation.
- Payload instruction text cannot alter routing/capability/destination.
- Trigger override cannot weaken template policy.
- No raw payload/signature appears in logs or audit.
- Scheduler `admit_in_uow` rolls back work/run creation when next-occurrence persistence fails.

## Exit criteria

- Manual, webhook, and schedule paths converge on one intake service.
- Signature verification is a required hook with no unsafe fallback.
- Work idempotency returns original resources for identical replay and conflicts on changed payload.
- Only enabled, explicitly bound instances receive work.
- Every input is bounded and schema-validated before planning.
- Trigger configuration remains deployment-only and optimistic-concurrency protected.
- Replay, collision, signature, and no-leak tests pass.

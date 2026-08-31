// WEB-06 dependency-free witness executes the production run, artifact, and rendering boundaries.
import "./require-pinned-node.mjs";

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { registerHooks } from "node:module";

registerHooks({
  resolve(specifier, context, nextResolve) {
    try {
      return nextResolve(specifier, context);
    } catch (error) {
      if (specifier.startsWith(".") && !specifier.endsWith(".ts")) {
        return nextResolve(`${specifier}.ts`, context);
      }
      throw error;
    }
  },
});

const [
  apiModule,
  timelineModule,
  presentationModule,
  payloadModule,
  markdownSafetyModule,
] = await Promise.all([
  import("../src/api/runArtifacts.ts"),
  import("../src/features/runs/timelineModel.ts"),
  import("../src/features/runs/artifactPresentation.ts"),
  import("../src/features/artifacts/artifactPayload.ts"),
  import("../src/features/artifacts/restrictedMarkdownSafety.ts"),
]);
const {
  RunArtifactsContractError,
  artifactResourceQueryKey,
  normalizeArtifactResource,
  normalizeExternalAction,
  normalizeRunArtifactsPage,
  normalizeRunResource,
  normalizeRunTimelinePage,
  runArtifactsQueryKey,
  runResourceQueryKey,
  runTimelineQueryKey,
} = apiModule;
const { isTerminalRunState, mergeTimelinePages, runRefreshInterval } =
  timelineModule;
const { artifactMarkdownValue, isAdvisoryArtifact } = presentationModule;
const { prepareArtifactValue } = payloadModule;
const { safeArtifactLinkHref } = markdownSafetyModule;

const [timelinePageSource, artifactsPanelSource] = await Promise.all([
  readFile(
    new URL("../src/features/runs/RunTimelinePage.tsx", import.meta.url),
    "utf8",
  ),
  readFile(
    new URL("../src/features/runs/RunArtifactsPanel.tsx", import.meta.url),
    "utf8",
  ),
]);
assert.equal(
  timelinePageSource.match(/refetchIntervalInBackground:\s*false/gu)?.length,
  2,
  "both run and timeline polling must opt out of background intervals",
);
assert.equal(
  artifactsPanelSource.match(/refetchIntervalInBackground:\s*false/gu)?.length,
  1,
  "artifact polling must opt out of background intervals",
);

const RUN_ID = "run.web-06.witness";
const WORK_ID = "work.web-06.witness";
const STEP_ID = "step.web-06.witness";
const ACTION_ID = "action.web-06.witness";
const RECEIPT_ID = "receipt.web-06.witness";
const ARTIFACT_ID = "artifact.web-06.witness";
const INSTANCE_ID = "inst.email.lifecycle-marketing.churned-user-monitor.01";
const TEMPLATE_ID = "tpl.email.lifecycle-marketing.churned-user-monitor";
const WORKFLOW_ID = "workflow.email.churn-review.v1";
const OUTPUT_SCHEMA_ID = `urn:marketing-agents:catalog:v1:${TEMPLATE_ID}:output`;
const HOSTILE_MARKDOWN = [
  "# Advisory review",
  "<script>globalThis.__web06WitnessPwned = true</script>",
  "![remote](https://attacker.invalid/pixel.png)",
  "[unsafe](javascript:alert(1))",
  "[safe](https://example.test/review)",
].join("\n\n");

function artifactSummary() {
  return {
    id: ARTIFACT_ID,
    work_item_id: WORK_ID,
    run_id: RUN_ID,
    step_id: STEP_ID,
    workflow_id: WORKFLOW_ID,
    workflow_version: "1",
    template_id: TEMPLATE_ID,
    instance_id: INSTANCE_ID,
    output_schema_id: OUTPUT_SCHEMA_ID,
    output_schema_version: "1",
    classification: "internal",
    created_at: "2026-08-31T12:00:03.000000000Z",
    artifact_url: `/api/v1/artifacts/${ARTIFACT_ID}`,
    run_url: `/api/v1/runs/${RUN_ID}`,
    step_url: `/api/v1/runs/${RUN_ID}/steps/${STEP_ID}`,
    template_url: `/api/v1/agent-templates/${TEMPLATE_ID}`,
    instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
  };
}

function artifactResource() {
  return {
    ...artifactSummary(),
    catalog_hash: "c".repeat(64),
    instance_config_revision: 7,
    sources: [
      {
        kind: "work_input",
        source_id: "source.web-06.witness",
        classification: "internal",
      },
      {
        kind: "parent_artifact",
        source_id: "artifact.web-06.parent",
        classification: "internal",
      },
    ],
    parent_artifact_ids: ["artifact.web-06.parent"],
    providers: [
      {
        provider_kind: "connector",
        mode: "mock",
        name: "deterministic-newsletter",
        version: "1.0.0",
      },
    ],
    output_schema_hash: `schema-sha256-v1:${"d".repeat(64)}`,
    redacted_payload: {
      artifact: HOSTILE_MARKDOWN,
      email: "[REDACTED]",
    },
    payload_digest: `artifact-hmac-sha256-v1:${"e".repeat(64)}`,
  };
}

function runtimePolicy() {
  return {
    operation_key: "write-advisory",
    attempt_kind: "tool",
    max_attempts: 3,
    backoff: "bounded_exponential",
    step_timeout_seconds: 60,
    template_run_timeout_seconds: 300,
    max_steps: 5,
    max_model_calls: 2,
    max_tool_calls: 3,
    max_input_bytes: 65_536,
    max_input_field_bytes: 32_768,
    max_output_bytes: 262_144,
    max_model_output_tokens: 4_096,
    rate_limit_scope: "connector",
    rate_limit_key: "mock.newsletter.default",
    rate_limit_max_calls: 10,
    rate_limit_window_seconds: 60,
  };
}

function runStep() {
  return {
    id: STEP_ID,
    run_id: RUN_ID,
    key: "write-advisory",
    kind: "external_action",
    selected_instance_id: INSTANCE_ID,
    template_id: TEMPLATE_ID,
    dependency_keys: [],
    capability_id: "cap.newsletter.subscribe",
    effect: "write",
    state: "succeeded",
    ordinal: 1,
    source_order: 1,
    configuration_revision: 7,
    connector_family: "newsletter",
    routing_slot_key: null,
    binding_id: "mock.newsletter.default",
    binding_configuration_revision: 7,
    request_schema_id: "schema.newsletter.subscribe.request.v1",
    result_schema_id: OUTPUT_SCHEMA_ID,
    result_schema_hash: `schema-sha256-v1:${"d".repeat(64)}`,
    data_classification: "internal",
    idempotency_support: "required",
    timeout_seconds: 60,
    runtime_policy: runtimePolicy(),
    approval_policy_id: "policy.external-write.default",
    approval_required_roles: ["approver"],
    approval_required_scopes: ["scope.external-write"],
    approval_expires_after_seconds: 3_600,
    approval_allow_self_approval: true,
    terminal_result: true,
    created_at: "2026-08-31T12:00:00.000000000Z",
    updated_at: "2026-08-31T12:00:03.000000000Z",
    version: 1,
    terminal_reason_code: "step_succeeded",
    transitions: [],
    step_url: `/api/v1/runs/${RUN_ID}/steps/${STEP_ID}`,
    run_url: `/api/v1/runs/${RUN_ID}`,
    instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
    template_url: `/api/v1/agent-templates/${TEMPLATE_ID}`,
  };
}

function externalAction() {
  return {
    id: ACTION_ID,
    run_id: RUN_ID,
    step_id: STEP_ID,
    step_key: "write-advisory",
    template_id: TEMPLATE_ID,
    instance_id: INSTANCE_ID,
    proposal_revision: 1,
    action_type: "newsletter.subscribe",
    capability_id: "cap.newsletter.subscribe",
    connector_family: "newsletter",
    binding_id: "mock.newsletter.default",
    destination_summary: "Mock newsletter · witness audience",
    redacted_payload: { email: "[REDACTED]" },
    payload_schema_id: "schema.newsletter.subscribe.request.v1",
    state: "succeeded",
    created_at: "2026-08-31T12:00:01.000000000Z",
    updated_at: "2026-08-31T12:00:03.000000000Z",
    version: 1,
    delivery_attempt_count: 1,
    delivery_attempt_limit: 3,
    approval_policy_id: "policy.external-write.default",
    approval_required_roles: ["approver"],
    approval_required_scopes: ["scope.external-write"],
    approval_expires_after_seconds: 3_600,
    approval_allow_self_approval: true,
    terminal_reason_code: null,
    superseded_by_action_id: null,
    superseded_at: null,
    receipt_id: RECEIPT_ID,
    result_status: "succeeded",
    result_safe_metadata: null,
    completed_at: "2026-08-31T12:00:03.000000000Z",
    action_url: `/api/v1/external-actions/${ACTION_ID}`,
    run_url: `/api/v1/runs/${RUN_ID}`,
    step_url: `/api/v1/runs/${RUN_ID}/steps/${STEP_ID}`,
    instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
    template_url: `/api/v1/agent-templates/${TEMPLATE_ID}`,
  };
}

function runResource() {
  return {
    id: RUN_ID,
    work_item_id: WORK_ID,
    instance_id: INSTANCE_ID,
    workflow_id: WORKFLOW_ID,
    trigger_id: "trigger.web-06.witness",
    source: "manual",
    mode: "mock_execution",
    state: "completed",
    catalog_hash: "c".repeat(64),
    configuration_revision: 7,
    approval_required: true,
    terminal_reason_code: "run_completed",
    created_at: "2026-08-31T12:00:00.000000000Z",
    updated_at: "2026-08-31T12:00:04.000000000Z",
    version: 1,
    run_url: `/api/v1/runs/${RUN_ID}`,
    timeline_url: `/api/v1/runs/${RUN_ID}/timeline`,
    artifacts_url: `/api/v1/runs/${RUN_ID}/artifacts`,
    instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
    transitions: [
      {
        sequence: 1,
        command: "complete",
        previous_state: null,
        new_state: "completed",
        reason_code: "run_completed",
        occurred_at: "2026-08-31T12:00:04.000000000Z",
        expected_version: 0,
        resulting_version: 1,
        completed_effect_count: 1,
        outcome_unknown_effect_count: 0,
      },
    ],
    plan: {
      plan_hash: "1".repeat(64),
      workflow_id: WORKFLOW_ID,
      workflow_version: 1,
      workflow_definition_hash: "2".repeat(64),
      catalog_content_hash: `catalog-sha256-v1:${"3".repeat(64)}`,
      graph_hash: "4".repeat(64),
      routing_hash: "5".repeat(64),
      approval_required: true,
      step_count: 1,
      runtime_policy: {
        max_steps: 5,
        max_model_calls: 2,
        max_tool_calls: 3,
        run_timeout_seconds: 300,
      },
      created_at: "2026-08-31T12:00:00.000000000Z",
      selected_instances: [
        {
          instance_id: INSTANCE_ID,
          template_id: TEMPLATE_ID,
          configuration_revision: 7,
          display_order: 1,
          source_ordinal: 1,
          selection_order: 1,
          target: true,
          instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
          template_url: `/api/v1/agent-templates/${TEMPLATE_ID}`,
        },
      ],
      routing_assignments: [],
      steps: [runStep()],
    },
    execution_control: null,
    pending_approvals: [],
    artifact_summaries: [artifactSummary()],
    artifacts_truncated: false,
    external_actions: [externalAction()],
    terminal_error: null,
  };
}

function timelineEvent(sequence, eventType, links = {}) {
  const stepId = links.stepId ?? null;
  const actionId = links.actionId ?? null;
  const artifactId = links.artifactId ?? null;
  return {
    id: `event.web-06.witness.${String(sequence)}`,
    sequence,
    schema_version: 1,
    event_type: eventType,
    aggregate_type: "run",
    aggregate_id: RUN_ID,
    outcome: "accepted",
    actor_id: `actor-hmac-sha256-v1:${"a".repeat(64)}`,
    actor_source: "local_session",
    auth_method: "local_session",
    correlation_id: `correlation-hmac-sha256-v1:${"b".repeat(64)}`,
    occurred_at: `2026-08-31T12:00:0${String(sequence)}.000000000Z`,
    step_id: stepId,
    action_id: actionId,
    approval_request_id: null,
    artifact_id: artifactId,
    attempted_command: null,
    previous_state: eventType === "run.completed" ? "executing" : null,
    new_state: eventType === "run.completed" ? "completed" : null,
    reason_code: null,
    metadata: { safe_summary: "persisted witness event" },
    metadata_classification: "internal",
    metadata_expires_at: "2099-08-31T12:00:00.000000000Z",
    metadata_expired: false,
    run_url: `/api/v1/runs/${RUN_ID}`,
    step_url: stepId === null ? null : `/api/v1/runs/${RUN_ID}/steps/${stepId}`,
    action_url:
      actionId === null ? null : `/api/v1/external-actions/${actionId}`,
    approval_url: null,
    artifact_url:
      artifactId === null ? null : `/api/v1/artifacts/${artifactId}`,
  };
}

const normalizedRun = normalizeRunResource(runResource(), RUN_ID);
assert.equal(normalizedRun.state, "completed");
assert.equal(normalizedRun.plan?.steps[0]?.idempotencySupport, "required");
assert.equal(normalizedRun.externalActions[0]?.receiptId, RECEIPT_ID);
assert.equal(normalizedRun.externalActions[0]?.resultStatus, "succeeded");
assert.equal(
  normalizedRun.externalActions[0]?.completedAt,
  "2026-08-31T12:00:03.000000000Z",
);
assert.equal(
  Object.hasOwn(normalizedRun.externalActions[0] ?? {}, "idempotencyKey"),
  false,
);

const timelineBody = {
  run_id: RUN_ID,
  items: [
    timelineEvent(1, "action.succeeded", {
      stepId: STEP_ID,
      actionId: ACTION_ID,
    }),
    timelineEvent(2, "artifact.created", {
      stepId: STEP_ID,
      artifactId: ARTIFACT_ID,
    }),
    timelineEvent(3, "run.completed"),
  ],
  next_cursor: null,
};
const normalizedTimeline = normalizeRunTimelinePage(timelineBody, RUN_ID, {
  limit: 100,
});
assert.deepEqual(
  normalizedTimeline.items.map(({ sequence }) => sequence),
  [1, 2, 3],
);
assert.deepEqual(
  mergeTimelinePages(RUN_ID, [normalizedTimeline]).events.map(
    ({ sequence }) => sequence,
  ),
  [1, 2, 3],
);
assert.equal(isTerminalRunState(normalizedRun.state), true);
assert.equal(runRefreshInterval(normalizedRun.state, false), false);

const normalizedArtifactPage = normalizeRunArtifactsPage(
  { run_id: RUN_ID, items: [artifactSummary()], next_cursor: null },
  RUN_ID,
  { limit: 25 },
);
assert.equal(normalizedArtifactPage.items[0]?.id, ARTIFACT_ID);

const normalizedArtifact = normalizeArtifactResource(
  artifactResource(),
  ARTIFACT_ID,
);
assert.equal(normalizedArtifact.redactedPayload.email, "[REDACTED]");
assert.equal(isAdvisoryArtifact(normalizedArtifact), true);
assert.equal(artifactMarkdownValue(normalizedArtifact), HOSTILE_MARKDOWN);
const mismatchedPresentationArtifact = Object.freeze({
  ...normalizedArtifact,
  outputSchemaId:
    "urn:marketing-agents:catalog:v1:tpl.social-media.new-content.linkedin-post-drafter:output",
});
assert.equal(isAdvisoryArtifact(mismatchedPresentationArtifact), false);
assert.equal(artifactMarkdownValue(mismatchedPresentationArtifact), null);
const unknownTemplate = "tpl.unknown.attacker-controlled";
const selfMatchedUnknownPresentation = Object.freeze({
  ...normalizedArtifact,
  templateId: unknownTemplate,
  outputSchemaId: `urn:marketing-agents:catalog:v1:${unknownTemplate}:output`,
});
assert.equal(isAdvisoryArtifact(selfMatchedUnknownPresentation), false);
assert.equal(artifactMarkdownValue(selfMatchedUnknownPresentation), null);
assert.equal(Object.hasOwn(normalizedArtifact, "downloadUrl"), false);

const cyclic = {};
cyclic.self = cyclic;
const preparedCycle = prepareArtifactValue(cyclic);
assert.equal(
  JSON.stringify(preparedCycle).includes("circular reference"),
  true,
);
assert.equal(safeArtifactLinkHref("javascript:alert(1)"), null);
assert.equal(safeArtifactLinkHref("data:text/html,attack"), null);
assert.equal(safeArtifactLinkHref("//attacker.invalid/pixel"), null);
assert.equal(
  safeArtifactLinkHref("https://example.test/review"),
  "https://example.test/review",
);

const queryState = JSON.stringify([
  runResourceQueryKey(RUN_ID),
  runTimelineQueryKey(RUN_ID, { limit: 100 }),
  runArtifactsQueryKey(RUN_ID, { limit: 25 }),
  artifactResourceQueryKey(ARTIFACT_ID),
]);
assert.equal(queryState.includes(RUN_ID), true);
assert.equal(queryState.includes(ARTIFACT_ID), true);
assert.equal(queryState.includes(HOSTILE_MARKDOWN), false);
assert.equal(queryState.includes("[REDACTED]"), false);

assert.throws(
  () =>
    normalizeRunTimelinePage(
      { ...timelineBody, items: [...timelineBody.items].reverse() },
      RUN_ID,
      { limit: 100 },
    ),
  RunArtifactsContractError,
);
assert.throws(
  () =>
    normalizeRunArtifactsPage(
      { run_id: "run.web-06.other", items: [], next_cursor: null },
      RUN_ID,
    ),
  RunArtifactsContractError,
);
assert.throws(
  () =>
    normalizeArtifactResource(
      { ...artifactResource(), download_url: "/api/v1/artifacts/raw" },
      ARTIFACT_ID,
    ),
  RunArtifactsContractError,
);
assert.throws(
  () =>
    normalizeExternalAction(
      { ...externalAction(), idempotency_key: "must-not-be-exposed" },
      ACTION_ID,
      RUN_ID,
    ),
  RunArtifactsContractError,
);

process.stdout.write("WEB-06 run and artifact witness passed.\n");

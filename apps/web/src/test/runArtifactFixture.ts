import type {
  ArtifactPage,
  ArtifactResource,
  ArtifactSummary,
  RunResource,
  RunTimelineEvent,
  RunTimelinePage,
} from "../api/runArtifacts";

export const WEB_06_RUN_ID = "run.web06.mock.01";
export const WEB_06_ARTIFACT_ID = "artifact.web06.advisory.01";
export const WEB_06_PARENT_ARTIFACT_ID = "artifact.web06.parent.01";
export const WEB_06_STEP_ID = "step.web06.delivery.01";
export const WEB_06_ACTION_ID = "action.web06.mock.01";

export const WEB_06_TEMPLATE_ID =
  "tpl.email.lifecycle-marketing.churned-user-monitor";
export const WEB_06_INSTANCE_ID = "inst.email.churn-monitor.01";
export const WEB_06_OUTPUT_SCHEMA_ID = `urn:marketing-agents:catalog:v1:${WEB_06_TEMPLATE_ID}:output`;
export const WEB_06_SCHEMA_HASH = `schema-sha256-v1:${"a".repeat(64)}`;
export const WEB_06_PAYLOAD_DIGEST = `artifact-hmac-sha256-v1:${"b".repeat(64)}`;
export const WEB_06_CATALOG_HASH = `catalog-sha256-v1:${"c".repeat(64)}`;

const CREATED_AT = "2026-08-31T16:00:00Z";
const UPDATED_AT = "2026-08-31T16:02:00Z";

export function makeArtifactSummary(
  overrides: Partial<ArtifactSummary> = {},
): ArtifactSummary {
  return Object.freeze({
    id: WEB_06_ARTIFACT_ID,
    workItemId: "work.web06.01",
    runId: WEB_06_RUN_ID,
    stepId: WEB_06_STEP_ID,
    workflowId: "workflow.web06.lifecycle-review",
    workflowVersion: "1",
    templateId: WEB_06_TEMPLATE_ID,
    instanceId: WEB_06_INSTANCE_ID,
    outputSchemaId: WEB_06_OUTPUT_SCHEMA_ID,
    outputSchemaVersion: "1.0.0",
    classification: "sensitive",
    createdAt: UPDATED_AT,
    artifactUrl: `/api/v1/artifacts/${WEB_06_ARTIFACT_ID}`,
    runUrl: `/api/v1/runs/${WEB_06_RUN_ID}`,
    stepUrl: `/api/v1/runs/${WEB_06_RUN_ID}/steps/${WEB_06_STEP_ID}`,
    templateUrl: `/api/v1/catalog/templates/${WEB_06_TEMPLATE_ID}`,
    instanceUrl: `/api/v1/agent-instances/${WEB_06_INSTANCE_ID}`,
    ...overrides,
  });
}

export function makeArtifactResource(
  overrides: Partial<ArtifactResource> = {},
): ArtifactResource {
  return Object.freeze({
    ...makeArtifactSummary(),
    classification: "sensitive",
    catalogHash: WEB_06_CATALOG_HASH,
    instanceConfigRevision: 7,
    sources: Object.freeze([
      Object.freeze({
        kind: "work_input" as const,
        sourceId: "work.web06.input.01",
        classification: "personal" as const,
      }),
      Object.freeze({
        kind: "parent_artifact" as const,
        sourceId: WEB_06_PARENT_ARTIFACT_ID,
        classification: "sensitive" as const,
      }),
    ]),
    parentArtifactIds: Object.freeze([WEB_06_PARENT_ARTIFACT_ID]),
    providers: Object.freeze([
      Object.freeze({
        providerKind: "llm" as const,
        mode: "mock" as const,
        name: "mock-llm",
        version: "2026.08",
      }),
      Object.freeze({
        providerKind: "planner" as const,
        mode: "local" as const,
        name: "deterministic-planner",
        version: "1.4.0",
      }),
    ]),
    outputSchemaHash: WEB_06_SCHEMA_HASH,
    redactedPayload: Object.freeze({
      artifact: "# Churn review\nHuman review remains required.",
      score: 0.82,
      recommendation: Object.freeze({
        action: "Review the retained account signals",
        approved: false,
      }),
    }),
    payloadDigest: WEB_06_PAYLOAD_DIGEST,
    ...overrides,
  });
}

export function makeArtifactPage(
  overrides: Partial<ArtifactPage> = {},
): ArtifactPage {
  return Object.freeze({
    runId: WEB_06_RUN_ID,
    items: Object.freeze([makeArtifactSummary()]),
    nextCursor: null,
    ...overrides,
  });
}

export function makeRunResource(
  overrides: Partial<RunResource> = {},
): RunResource {
  const state = overrides.state ?? "completed";
  const terminalReasonCode = Object.prototype.hasOwnProperty.call(
    overrides,
    "terminalReasonCode",
  )
    ? (overrides.terminalReasonCode ?? null)
    : ["completed", "failed", "rejected", "cancelled"].includes(state)
      ? "all_steps_succeeded"
      : null;
  return Object.freeze({
    id: WEB_06_RUN_ID,
    workItemId: "work.web06.01",
    instanceId: WEB_06_INSTANCE_ID,
    workflowId: "workflow.web06.lifecycle-review",
    triggerId: "trigger.web06.manual.01",
    source: "manual",
    mode: "mock_execution",
    state,
    catalogHash: WEB_06_CATALOG_HASH,
    configurationRevision: 7,
    approvalRequired: true,
    terminalReasonCode,
    createdAt: CREATED_AT,
    updatedAt: UPDATED_AT,
    version: 5,
    runUrl: `/api/v1/runs/${WEB_06_RUN_ID}`,
    timelineUrl: `/api/v1/runs/${WEB_06_RUN_ID}/timeline`,
    artifactsUrl: `/api/v1/runs/${WEB_06_RUN_ID}/artifacts`,
    instanceUrl: `/api/v1/agent-instances/${WEB_06_INSTANCE_ID}`,
    transitions: Object.freeze([
      Object.freeze({
        sequence: 1,
        command: "complete",
        previousState: "executing" as const,
        newState: "completed" as const,
        reasonCode: "all_steps_succeeded",
        occurredAt: UPDATED_AT,
        expectedVersion: 4,
        resultingVersion: 5,
        completedEffectCount: 1,
        outcomeUnknownEffectCount: 0,
      }),
    ]),
    plan: Object.freeze({
      planHash: "d".repeat(64),
      workflowId: "workflow.web06.lifecycle-review",
      workflowVersion: 3,
      workflowDefinitionHash: "e".repeat(64),
      catalogContentHash: WEB_06_CATALOG_HASH,
      graphHash: "f".repeat(64),
      routingHash: "1".repeat(64),
      approvalRequired: true,
      stepCount: 1,
      runtimePolicy: Object.freeze({
        maxSteps: 4,
        maxModelCalls: 2,
        maxToolCalls: 3,
        runTimeoutSeconds: 180,
      }),
      createdAt: CREATED_AT,
      selectedInstances: Object.freeze([
        Object.freeze({
          instanceId: WEB_06_INSTANCE_ID,
          templateId: WEB_06_TEMPLATE_ID,
          configurationRevision: 7,
          displayOrder: 1,
          sourceOrdinal: 12,
          selectionOrder: 1,
          target: true,
          instanceUrl: `/api/v1/agent-instances/${WEB_06_INSTANCE_ID}`,
          templateUrl: `/api/v1/catalog/templates/${WEB_06_TEMPLATE_ID}`,
        }),
      ]),
      routingAssignments: Object.freeze([
        Object.freeze({
          slotKey: "delivery",
          instanceId: WEB_06_INSTANCE_ID,
          templateId: WEB_06_TEMPLATE_ID,
          requiredCapabilityIds: Object.freeze(["cap.crm.upsert-contact"]),
          assignmentOrder: 1,
          instanceUrl: `/api/v1/agent-instances/${WEB_06_INSTANCE_ID}`,
          templateUrl: `/api/v1/catalog/templates/${WEB_06_TEMPLATE_ID}`,
        }),
      ]),
      steps: Object.freeze([
        Object.freeze({
          id: WEB_06_STEP_ID,
          runId: WEB_06_RUN_ID,
          key: "deliver_review_record",
          kind: "connector",
          selectedInstanceId: WEB_06_INSTANCE_ID,
          templateId: WEB_06_TEMPLATE_ID,
          dependencyKeys: Object.freeze([]),
          capabilityId: "cap.crm.upsert-contact",
          effect: "write" as const,
          state: "succeeded" as const,
          ordinal: 1,
          sourceOrder: 1,
          configurationRevision: 7,
          connectorFamily: "crm",
          routingSlotKey: "delivery",
          bindingId: "mock.crm.local",
          bindingConfigurationRevision: 7,
          requestSchemaId: "schema.crm.upsert-contact.request.v1",
          resultSchemaId: "schema.crm.upsert-contact.result.v1",
          resultSchemaHash: WEB_06_SCHEMA_HASH,
          dataClassification: "personal" as const,
          idempotencySupport: "required" as const,
          timeoutSeconds: 45,
          runtimePolicy: Object.freeze({
            operationKey: "crm.upsert-contact",
            attemptKind: "tool" as const,
            maxAttempts: 2,
            backoff: "bounded_exponential" as const,
            stepTimeoutSeconds: 45,
            templateRunTimeoutSeconds: 180,
            maxSteps: 4,
            maxModelCalls: 2,
            maxToolCalls: 3,
            maxInputBytes: 65_536,
            maxInputFieldBytes: 16_384,
            maxOutputBytes: 131_072,
            maxModelOutputTokens: 2_048,
            rateLimitScope: "instance",
            rateLimitKey: WEB_06_INSTANCE_ID,
            rateLimitMaxCalls: 5,
            rateLimitWindowSeconds: 60,
          }),
          approvalPolicyId: "policy.human-external-write",
          approvalRequiredRoles: Object.freeze(["approver"]),
          approvalRequiredScopes: Object.freeze(["external-write:approve"]),
          approvalExpiresAfterSeconds: 3_600,
          approvalAllowSelfApproval: false,
          terminalResult: true,
          createdAt: CREATED_AT,
          updatedAt: UPDATED_AT,
          version: 2,
          terminalReasonCode: "step_succeeded",
          transitions: Object.freeze([
            Object.freeze({
              sequence: 1,
              command: "succeed",
              previousState: "executing" as const,
              newState: "succeeded" as const,
              reasonCode: "step_succeeded",
              occurredAt: UPDATED_AT,
              expectedVersion: 1,
              resultingVersion: 2,
            }),
          ]),
          stepUrl: `/api/v1/runs/${WEB_06_RUN_ID}/steps/${WEB_06_STEP_ID}`,
          runUrl: `/api/v1/runs/${WEB_06_RUN_ID}`,
          instanceUrl: `/api/v1/agent-instances/${WEB_06_INSTANCE_ID}`,
          templateUrl: `/api/v1/catalog/templates/${WEB_06_TEMPLATE_ID}`,
        }),
      ]),
    }),
    executionControl: Object.freeze({
      runTimeoutSeconds: 180,
      maxModelCalls: 2,
      maxToolCalls: 3,
      modelCalls: 1,
      toolCalls: 1,
      remainingModelCalls: 1,
      remainingToolCalls: 2,
      startedAt: CREATED_AT,
      deadlineAt: "2026-08-31T16:03:00Z",
      cancelRequestedAt: null,
      createdAt: CREATED_AT,
      updatedAt: UPDATED_AT,
      version: 2,
    }),
    pendingApprovals: Object.freeze([]),
    artifactSummaries: Object.freeze([makeArtifactSummary()]),
    artifactsTruncated: false,
    externalActions: Object.freeze([
      Object.freeze({
        id: WEB_06_ACTION_ID,
        runId: WEB_06_RUN_ID,
        stepId: WEB_06_STEP_ID,
        stepKey: "deliver_review_record",
        templateId: WEB_06_TEMPLATE_ID,
        instanceId: WEB_06_INSTANCE_ID,
        proposalRevision: 1,
        actionType: "crm.upsert-contact",
        capabilityId: "cap.crm.upsert-contact",
        connectorFamily: "crm",
        bindingId: "mock.crm.local",
        destinationSummary: "Mock CRM · retained account review",
        redactedPayload: Object.freeze({
          contact: "[REDACTED]",
          advisory: true,
        }),
        payloadSchemaId: "schema.crm.upsert-contact.request.v1",
        state: "succeeded" as const,
        createdAt: CREATED_AT,
        updatedAt: UPDATED_AT,
        version: 4,
        deliveryAttemptCount: 1,
        deliveryAttemptLimit: 2,
        approvalPolicyId: "policy.human-external-write",
        approvalRequiredRoles: Object.freeze(["approver"]),
        approvalRequiredScopes: Object.freeze(["external-write:approve"]),
        approvalExpiresAfterSeconds: 3_600,
        approvalAllowSelfApproval: false,
        terminalReasonCode: null,
        supersededByActionId: null,
        supersededAt: null,
        receiptId: "receipt.web06.mock.01",
        resultStatus: "accepted",
        resultSafeMetadata: null,
        completedAt: UPDATED_AT,
        actionUrl: `/api/v1/external-actions/${WEB_06_ACTION_ID}`,
        runUrl: `/api/v1/runs/${WEB_06_RUN_ID}`,
        stepUrl: `/api/v1/runs/${WEB_06_RUN_ID}/steps/${WEB_06_STEP_ID}`,
        instanceUrl: `/api/v1/agent-instances/${WEB_06_INSTANCE_ID}`,
        templateUrl: `/api/v1/catalog/templates/${WEB_06_TEMPLATE_ID}`,
      }),
    ]),
    terminalError: null,
    ...overrides,
  });
}

export function makeTimelineEvent(
  overrides: Partial<RunTimelineEvent> = {},
): RunTimelineEvent {
  const sequence = overrides.sequence ?? 1;
  const eventType = overrides.eventType ?? "run.received";
  const stepId = overrides.stepId ?? null;
  const actionId = overrides.actionId ?? null;
  const approvalRequestId = overrides.approvalRequestId ?? null;
  const artifactId = overrides.artifactId ?? null;
  return Object.freeze({
    id: `event.web06.${String(sequence)}.${eventType.replaceAll(".", "-")}`,
    sequence,
    schemaVersion: 1,
    eventType,
    aggregateType: "run",
    aggregateId: WEB_06_RUN_ID,
    outcome: "recorded",
    actorId: "principal.local.worker",
    actorSource: "runtime_worker",
    authMethod: "local_session",
    correlationId: "correlation.web06.01",
    occurredAt: `2026-08-31T16:00:${String(sequence).padStart(2, "0")}Z`,
    stepId,
    actionId,
    approvalRequestId,
    artifactId,
    attemptedCommand: null,
    previousState: null,
    newState: null,
    reasonCode: null,
    metadata: Object.freeze({ attempt_number: sequence }),
    metadataClassification: "internal",
    metadataExpiresAt: "2099-08-31T16:00:00Z",
    metadataExpired: false,
    runUrl: `/api/v1/runs/${WEB_06_RUN_ID}`,
    stepUrl:
      stepId === null ? null : `/api/v1/runs/${WEB_06_RUN_ID}/steps/${stepId}`,
    actionUrl:
      actionId === null ? null : `/api/v1/external-actions/${actionId}`,
    approvalUrl:
      approvalRequestId === null
        ? null
        : `/api/v1/approval-requests/${approvalRequestId}`,
    artifactUrl: artifactId === null ? null : `/api/v1/artifacts/${artifactId}`,
    ...overrides,
  });
}

export function makeTimelinePage(
  items: readonly RunTimelineEvent[],
  nextCursor: string | null = null,
): RunTimelinePage {
  return Object.freeze({
    runId: WEB_06_RUN_ID,
    items: Object.freeze([...items]),
    nextCursor,
  });
}

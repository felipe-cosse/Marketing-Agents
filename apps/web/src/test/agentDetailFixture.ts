export interface AgentDetailFixtureIdentity {
  readonly instanceId: string;
  readonly templateId: string;
  readonly departmentId: string;
  readonly functionId: string;
  readonly sourceOrdinal?: number;
  readonly sharedTemplateDeploymentCount?: number;
  readonly displayName?: string;
  readonly purpose?: string;
  readonly runtime?: "static" | "never_run" | "completed";
}

const CATALOG_HASH = `catalog-sha256-v1:${"a".repeat(64)}`;
const RUNTIME_WATERMARK = `instance-status-sha256-v1:${"d".repeat(64)}`;

export const AGENT_DETAIL_ETAG = `"${"c".repeat(64)}"`;

export function makeAgentDetailPayload({
  instanceId,
  templateId,
  departmentId,
  functionId,
  sourceOrdinal = 1,
  sharedTemplateDeploymentCount = 1,
  displayName = "Agent 1",
  purpose = "Completes source-backed local marketing work.",
  runtime = "completed",
}: AgentDetailFixtureIdentity): Record<string, unknown> {
  const inputSchemaId = `urn:marketing-agents:catalog:v1:${templateId}:input`;
  const outputSchemaId = `urn:marketing-agents:catalog:v1:${templateId}:output`;
  const capabilityId = "cap.local.catalog-read";
  const body: Record<string, unknown> = {
    catalogVersion: "1.0.0",
    catalogHash: CATALOG_HASH,
    instance: {
      id: instanceId,
      templateId,
      displayOrder: sourceOrdinal * 10,
      enabled: true,
      sourceOrdinal,
      variantLabel: null,
      triggerBindings: [
        {
          type: "manual",
          enabled: true,
          eventSource: null,
          cron: null,
          timezone: null,
          misfirePolicy: null,
          misfireGraceSeconds: null,
        },
      ],
      connectorBindings: {
        local: {
          connectorFamily: "local",
          bindingId: "local-catalog",
          enabled: true,
        },
      },
      schedule: null,
      configurationRevision: 1,
      configurationEtag: '"instance-configuration-v1-1"',
    },
    template: {
      id: templateId,
      displayName,
      departmentId,
      functionId,
      displayOrder: 10,
      purpose,
      inputSchemaId,
      outputSchemaId,
      allowedToolCapabilityIds: [capabilityId],
      supportedTriggerTypes: ["manual"],
      operationClassification: "read_only",
      outputHandling: "standard",
      approvalPolicyId: "policy.no-approval.read-only.v1",
      retryPolicy: { maxAttempts: 2, backoff: "bounded_exponential" },
      timeoutPolicy: { stepSeconds: 30, runSeconds: 120 },
      budgetPolicy: {
        maxSteps: 6,
        maxModelCalls: 1,
        maxToolCalls: 2,
        maxInputBytes: 65_536,
        maxInputFieldBytes: 16_384,
        maxOutputBytes: 262_144,
        maxModelOutputTokens: 4_096,
      },
      rateLimitPolicy: { maxCalls: 20, windowSeconds: 60 },
      sourceConfidence: "high",
      sourceReferences: ["IMPLEMENTATION_PROMPT.md#agent-details"],
      implementationNotes: "Produces a bounded local artifact.",
    },
    sharedTemplateDeploymentCount,
    capabilities: [
      {
        id: capabilityId,
        displayName: "Catalog read",
        description: "Read source-authoritative local catalog data.",
        effect: "read",
        connectorFamily: "local",
        idempotencySupport: "not_applicable",
        defaultTimeoutSeconds: 30,
        dataClassification: "internal",
      },
    ],
    approvalPolicy: {
      id: "policy.no-approval.read-only.v1",
      kind: "none",
      requiredRoles: [],
      expirySeconds: 3_600,
      allowSelfApproval: false,
    },
    inputSchema: {
      $schema: "https://json-schema.org/draft/2020-12/schema",
      $id: inputSchemaId,
      type: "object",
      additionalProperties: false,
      properties: { request_id: { type: "string" } },
    },
    outputSchema: {
      $schema: "https://json-schema.org/draft/2020-12/schema",
      $id: outputSchemaId,
      type: "object",
      additionalProperties: false,
      properties: { artifact_id: { type: "string" } },
    },
    templateSourceReferences: ["IMPLEMENTATION_PROMPT.md#agent-details"],
    templateImplementationNotes: "Produces a bounded local artifact.",
    configurationSchema: `/api/v1/agent-instances/${instanceId}/configuration-schema`,
  };

  if (runtime === "static") return body;
  if (runtime === "never_run") {
    return {
      ...body,
      runtimeWatermark: RUNTIME_WATERMARK,
      runtimeStatus: {
        status: "never_run",
        latestRunId: null,
        latestRunState: null,
        latestRunCreatedAt: null,
        latestRunUpdatedAt: null,
        latestRunUrl: null,
      },
      recentRuns: [],
    };
  }
  return {
    ...body,
    runtimeWatermark: RUNTIME_WATERMARK,
    runtimeStatus: {
      status: "completed",
      latestRunId: "run.web-03.latest",
      latestRunState: "completed",
      latestRunCreatedAt: "2026-08-28T18:00:00Z",
      latestRunUpdatedAt: "2026-08-28T18:02:00Z",
      latestRunUrl: "/api/v1/runs/run.web-03.latest",
    },
    recentRuns: [
      {
        id: "run.web-03.latest",
        state: "completed",
        workflowId: "workflow.web-03.latest",
        createdAt: "2026-08-28T18:00:00Z",
        updatedAt: "2026-08-28T18:02:00Z",
        runUrl: "/api/v1/runs/run.web-03.latest",
      },
    ],
  };
}

export function makeViewerSessionPayload(): Record<string, unknown> {
  return {
    actorId: "principal.local.viewer",
    roles: ["viewer"],
    scopes: [],
    authMode: "local",
    environment: "local",
    modelMode: "mock",
    connectorMode: "mock",
    networkPermission: false,
    warning: "Local identity — not production authentication",
    csrfToken: "a".repeat(32),
    csrfHeaderName: "X-CSRF-Token",
  };
}

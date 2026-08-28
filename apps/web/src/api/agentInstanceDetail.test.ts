import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiRequestError } from "./client";
import {
  AgentInstanceDetailContractError,
  fetchAgentInstanceDetail,
  normalizeAgentInstanceDetail,
  type AgentInstanceDetailIdentity,
} from "./agentInstanceDetail";
import {
  clearLocalSession,
  updateInstanceConfiguration,
} from "./instanceConfiguration";

const INSTANCE_ID = "inst.blog-seo.new-content.blog-post-writer.01";
const TEMPLATE_ID = "tpl.blog-seo.new-content.blog-post-writer";
const DEPARTMENT_ID = "dept.blog-seo";
const FUNCTION_ID = "func.blog-seo.new-content";
const CATALOG_HASH = `catalog-sha256-v1:${"a".repeat(64)}`;
const RUNTIME_WATERMARK = `instance-status-sha256-v1:${"b".repeat(64)}`;
const ETAG = `"${"c".repeat(64)}"`;
const INPUT_SCHEMA_ID = `urn:marketing-agents:catalog:v1:${TEMPLATE_ID}:input`;
const OUTPUT_SCHEMA_ID = `urn:marketing-agents:catalog:v1:${TEMPLATE_ID}:output`;
const CAPABILITY_IDS = Object.freeze([
  "cap.cms.read-content",
  "cap.model.generate-structured",
]);

const EXPECTED: AgentInstanceDetailIdentity = Object.freeze({
  instanceId: INSTANCE_ID,
  templateId: TEMPLATE_ID,
  departmentId: DEPARTMENT_ID,
  functionId: FUNCTION_ID,
  sourceOrdinal: 1,
  sharedTemplateDeploymentCount: 1,
  catalogVersion: "1.0.0",
  catalogHash: CATALOG_HASH,
});

function staticBody(): Record<string, unknown> {
  return {
    catalogVersion: "1.0.0",
    catalogHash: CATALOG_HASH,
    instance: {
      id: INSTANCE_ID,
      templateId: TEMPLATE_ID,
      displayOrder: 10,
      enabled: true,
      sourceOrdinal: 1,
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
        cms: {
          connectorFamily: "cms",
          bindingId: "local-cms",
          enabled: true,
        },
      },
      schedule: null,
      configurationRevision: 1,
      configurationEtag: '"instance-configuration-v1-1"',
    },
    template: {
      id: TEMPLATE_ID,
      displayName: "Blog Post Writer",
      departmentId: DEPARTMENT_ID,
      functionId: FUNCTION_ID,
      displayOrder: 10,
      purpose: "Draft, review, and prepare new blog posts for upload.",
      inputSchemaId: INPUT_SCHEMA_ID,
      outputSchemaId: OUTPUT_SCHEMA_ID,
      allowedToolCapabilityIds: [...CAPABILITY_IDS],
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
      sourceReferences: ["IMPLEMENTATION_PROMPT.md#new-content--3"],
      implementationNotes: "Produces a bounded local artifact.",
    },
    sharedTemplateDeploymentCount: 1,
    capabilities: [
      {
        id: CAPABILITY_IDS[0],
        displayName: "Read content",
        description: "Read supplied CMS content.",
        effect: "read",
        connectorFamily: "cms",
        idempotencySupport: "not_applicable",
        defaultTimeoutSeconds: 30,
        dataClassification: "internal",
      },
      {
        id: CAPABILITY_IDS[1],
        displayName: "Generate structured output",
        description: "Generate a bounded local artifact.",
        effect: "read",
        connectorFamily: "llm",
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
      $id: INPUT_SCHEMA_ID,
      type: "object",
      additionalProperties: false,
      properties: { request_id: { type: "string" } },
    },
    outputSchema: {
      $schema: "https://json-schema.org/draft/2020-12/schema",
      $id: OUTPUT_SCHEMA_ID,
      type: "object",
      additionalProperties: false,
      properties: { artifact_id: { type: "string" } },
    },
    templateSourceReferences: ["IMPLEMENTATION_PROMPT.md#new-content--3"],
    templateImplementationNotes: "Produces a bounded local artifact.",
    configurationSchema: `/api/v1/agent-instances/${INSTANCE_ID}/configuration-schema`,
  };
}

function disabledScheduleBody(): Record<string, unknown> {
  const body = staticBody();
  const instance = objectAt(body, "instance");
  instance.triggerBindings = [
    ...(instance.triggerBindings as unknown[]),
    {
      type: "schedule",
      enabled: false,
      eventSource: null,
      cron: null,
      timezone: null,
      misfirePolicy: null,
      misfireGraceSeconds: null,
    },
  ];
  instance.configurationRevision = 2;
  instance.configurationEtag = '"instance-configuration-v1-2"';
  objectAt(body, "template").supportedTriggerTypes = ["manual", "schedule"];
  return body;
}

function recentRun(
  id: string,
  state: string,
  createdAt: string,
  updatedAt: string,
): Record<string, unknown> {
  return {
    id,
    state,
    workflowId: `workflow.${id}`,
    createdAt,
    updatedAt,
    runUrl: `/api/v1/runs/${encodeURIComponent(id)}`,
  };
}

function runtimeBody(): Record<string, unknown> {
  return {
    ...staticBody(),
    runtimeWatermark: RUNTIME_WATERMARK,
    runtimeStatus: {
      status: "executing",
      latestRunId: "run.latest",
      latestRunState: "executing",
      latestRunCreatedAt: "2026-08-28T18:00:00Z",
      latestRunUpdatedAt: "2026-08-28T18:01:00.123456Z",
      latestRunUrl: "/api/v1/runs/run.latest",
    },
    recentRuns: [
      recentRun(
        "run.latest",
        "executing",
        "2026-08-28T18:00:00Z",
        "2026-08-28T18:01:00.123456Z",
      ),
      recentRun(
        "run.previous",
        "completed",
        "2026-08-27T18:00:00Z",
        "2026-08-27T18:02:00Z",
      ),
    ],
  };
}

function neverRunBody(): Record<string, unknown> {
  return {
    ...staticBody(),
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

function clone(value: Record<string, unknown>): Record<string, unknown> {
  return structuredClone(value);
}

function objectAt(
  value: Record<string, unknown>,
  key: string,
): Record<string, unknown> {
  const child = value[key];
  if (typeof child !== "object" || child === null || Array.isArray(child)) {
    throw new Error(`fixture ${key} is not an object`);
  }
  return child as Record<string, unknown>;
}

function arrayAt(value: Record<string, unknown>, key: string): unknown[] {
  const child = value[key];
  if (!Array.isArray(child)) throw new Error(`fixture ${key} is not an array`);
  return child;
}

function responseFor(
  value: Record<string, unknown> = runtimeBody(),
  etag = ETAG,
): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json", ETag: etag },
  });
}

afterEach(() => {
  clearLocalSession();
  vi.unstubAllGlobals();
});

describe("WEB-03 agent instance detail normalization", () => {
  it("accepts, cross-binds, camel-cases, and freezes the exact static detail", () => {
    const detail = normalizeAgentInstanceDetail(staticBody(), EXPECTED, ETAG);

    expect(detail).toMatchObject({
      etag: ETAG,
      catalogHash: CATALOG_HASH,
      sharedTemplateDeploymentCount: 1,
      runtimeAvailable: false,
      runtimeWatermark: null,
      runtimeStatus: null,
      recentRuns: [],
    });
    expect(detail.instance).toMatchObject({
      id: INSTANCE_ID,
      templateId: TEMPLATE_ID,
      configurationRevision: 1,
    });
    expect(detail.template.allowedToolCapabilityIds).toEqual(CAPABILITY_IDS);
    expect(detail.inputSchema.$id).toBe(INPUT_SCHEMA_ID);
    expect(Object.isFrozen(detail)).toBe(true);
    expect(Object.isFrozen(detail.instance)).toBe(true);
    expect(Object.isFrozen(detail.capabilities)).toBe(true);
    expect(Object.isFrozen(detail.inputSchema.properties)).toBe(true);
  });

  it("accepts a coherent bounded runtime overlay and distinguishes never-run", () => {
    const detail = normalizeAgentInstanceDetail(runtimeBody(), EXPECTED, ETAG);
    const neverRun = normalizeAgentInstanceDetail(
      neverRunBody(),
      EXPECTED,
      ETAG,
    );

    expect(detail.runtimeAvailable).toBe(true);
    if (!detail.runtimeAvailable) throw new Error("runtime fixture was static");
    expect(detail.runtimeWatermark).toBe(RUNTIME_WATERMARK);
    expect(detail.runtimeStatus.status).toBe("executing");
    expect(detail.recentRuns.map((run) => run.id)).toEqual([
      "run.latest",
      "run.previous",
    ]);
    expect(Object.isFrozen(detail.recentRuns[0])).toBe(true);
    expect(neverRun).toMatchObject({
      runtimeAvailable: true,
      runtimeStatus: { status: "never_run" },
      recentRuns: [],
    });
  });

  it("round-trips a disabled schedule update into a reusable agent detail", async () => {
    const detailBody = disabledScheduleBody();
    const instance = objectAt(detailBody, "instance");
    const configurationEtag = '"instance-configuration-v1-2"';
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            actorId: "local-operator",
            roles: ["local_admin"],
            scopes: [],
            authMode: "local",
            environment: "local",
            modelMode: "mock",
            connectorMode: "mock",
            networkPermission: false,
            warning: "Local identity — not production authentication",
            csrfToken: "a".repeat(43),
            csrfHeaderName: "X-CSRF-Token",
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            projectionVersion: "instance-configuration-v1",
            configuration: {
              instanceId: instance.id,
              enabled: instance.enabled,
              variantLabel: instance.variantLabel,
              triggerBindings: instance.triggerBindings,
              connectorBindings: instance.connectorBindings,
              schedule: instance.schedule,
              configurationRevision: instance.configurationRevision,
            },
          }),
          {
            headers: {
              "Content-Type": "application/json",
              ETag: configurationEtag,
            },
          },
        ),
      )
      .mockResolvedValueOnce(responseFor(detailBody));
    vi.stubGlobal("fetch", fetchMock);

    const updated = await updateInstanceConfiguration({
      instanceId: INSTANCE_ID,
      configurationEtag: '"instance-configuration-v1-1"',
      patch: {
        triggerBindings: [
          { type: "manual", enabled: true },
          { type: "schedule", enabled: false },
        ],
        schedule: null,
      },
    });
    const detail = await fetchAgentInstanceDetail(EXPECTED);

    expect(updated.configuration).toMatchObject({
      triggerBindings: [
        expect.objectContaining({ type: "manual", enabled: true }),
        {
          type: "schedule",
          enabled: false,
          eventSource: null,
          cron: null,
          timezone: null,
          misfirePolicy: null,
          misfireGraceSeconds: null,
        },
      ],
      schedule: null,
      configurationRevision: 2,
    });
    expect(detail.instance).toMatchObject({
      triggerBindings: updated.configuration.triggerBindings,
      schedule: null,
      configurationRevision: 2,
      configurationEtag,
    });
    expect(
      normalizeAgentInstanceDetail(
        JSON.parse(JSON.stringify(detailBody)) as unknown,
        EXPECTED,
        ETAG,
      ).instance.triggerBindings,
    ).toEqual(detail.instance.triggerBindings);
  });

  it.each([
    [
      "catalog",
      (value: Record<string, unknown>) => (value.catalogVersion = "2.0.0"),
    ],
    [
      "instance",
      (value: Record<string, unknown>) =>
        (objectAt(value, "instance").id = `${INSTANCE_ID}.other`),
    ],
    [
      "template",
      (value: Record<string, unknown>) =>
        (objectAt(value, "template").id = `${TEMPLATE_ID}.other`),
    ],
    [
      "department",
      (value: Record<string, unknown>) =>
        (objectAt(value, "template").departmentId = "dept.other"),
    ],
    [
      "function",
      (value: Record<string, unknown>) =>
        (objectAt(value, "template").functionId = "func.other"),
    ],
    [
      "ordinal",
      (value: Record<string, unknown>) =>
        (objectAt(value, "instance").sourceOrdinal = 2),
    ],
    [
      "deployment count",
      (value: Record<string, unknown>) =>
        (value.sharedTemplateDeploymentCount = 2),
    ],
  ])("rejects a mismatched %s hierarchy identity", (_label, mutate) => {
    const value = clone(staticBody());
    mutate(value);
    expect(() => normalizeAgentInstanceDetail(value, EXPECTED, ETAG)).toThrow(
      AgentInstanceDetailContractError,
    );
  });

  it.each([
    ["root field", (value: Record<string, unknown>) => (value.extra = true)],
    [
      "instance field",
      (value: Record<string, unknown>) =>
        (objectAt(value, "instance").extra = true),
    ],
    [
      "template policy field",
      (value: Record<string, unknown>) =>
        (objectAt(objectAt(value, "template"), "retryPolicy").extra = true),
    ],
    [
      "capability field",
      (value: Record<string, unknown>) => {
        const capability = arrayAt(value, "capabilities")[0] as Record<
          string,
          unknown
        >;
        capability.extra = true;
      },
    ],
    [
      "partial runtime overlay",
      (value: Record<string, unknown>) =>
        (value.runtimeWatermark = RUNTIME_WATERMARK),
    ],
  ])("rejects an unknown or %s", (_label, mutate) => {
    const value = clone(staticBody());
    mutate(value);
    expect(() => normalizeAgentInstanceDetail(value, EXPECTED, ETAG)).toThrow(
      AgentInstanceDetailContractError,
    );
  });

  it.each([
    [
      "capability order",
      (value: Record<string, unknown>) =>
        arrayAt(value, "capabilities").reverse(),
    ],
    [
      "capability duplicate",
      (value: Record<string, unknown>) => {
        const capabilities = arrayAt(value, "capabilities");
        capabilities[1] = structuredClone(capabilities[0]);
      },
    ],
    [
      "approval policy reference",
      (value: Record<string, unknown>) =>
        (objectAt(value, "approvalPolicy").id = "policy.other"),
    ],
    [
      "input schema reference",
      (value: Record<string, unknown>) =>
        (objectAt(value, "inputSchema").$id = "urn:other"),
    ],
    [
      "source references",
      (value: Record<string, unknown>) =>
        (value.templateSourceReferences = ["OTHER.md"]),
    ],
  ])("rejects an incoherent %s", (_label, mutate) => {
    const value = clone(staticBody());
    mutate(value);
    expect(() => normalizeAgentInstanceDetail(value, EXPECTED, ETAG)).toThrow(
      AgentInstanceDetailContractError,
    );
  });

  it.each([
    [
      "configuration schema URL",
      (value: Record<string, unknown>) =>
        (value.configurationSchema = "https://attacker.invalid/config"),
    ],
    [
      "latest-run URL",
      (value: Record<string, unknown>) =>
        (objectAt(value, "runtimeStatus").latestRunUrl =
          "https://attacker.invalid/run"),
    ],
    [
      "recent-run URL",
      (value: Record<string, unknown>) => {
        const run = arrayAt(value, "recentRuns")[0] as Record<string, unknown>;
        run.runUrl = "//attacker.invalid/run";
      },
    ],
  ])("rejects an unsafe %s", (_label, mutate) => {
    const value = clone(runtimeBody());
    mutate(value);
    expect(() => normalizeAgentInstanceDetail(value, EXPECTED, ETAG)).toThrow(
      AgentInstanceDetailContractError,
    );
  });

  it.each([
    [
      "invalid timestamp",
      (value: Record<string, unknown>) => {
        const run = arrayAt(value, "recentRuns")[0] as Record<string, unknown>;
        run.createdAt = "yesterday";
      },
    ],
    [
      "invalid calendar date",
      (value: Record<string, unknown>) => {
        const run = arrayAt(value, "recentRuns")[0] as Record<string, unknown>;
        run.createdAt = "2026-02-30T18:00:00Z";
      },
    ],
    [
      "updated-before-created timestamp",
      (value: Record<string, unknown>) => {
        const run = arrayAt(value, "recentRuns")[0] as Record<string, unknown>;
        run.updatedAt = "2026-08-28T17:59:59Z";
      },
    ],
    [
      "newest-first order",
      (value: Record<string, unknown>) =>
        arrayAt(value, "recentRuns").reverse(),
    ],
    [
      "latest-run coherence",
      (value: Record<string, unknown>) =>
        (objectAt(value, "runtimeStatus").latestRunState = "completed"),
    ],
    [
      "five-run bound",
      (value: Record<string, unknown>) => {
        const runs = arrayAt(value, "recentRuns");
        while (runs.length < 6) runs.push(structuredClone(runs[1]));
      },
    ],
  ])("rejects invalid recent-run %s", (_label, mutate) => {
    const value = clone(runtimeBody());
    mutate(value);
    expect(() => normalizeAgentInstanceDetail(value, EXPECTED, ETAG)).toThrow(
      AgentInstanceDetailContractError,
    );
  });
});

describe("WEB-03 conditional agent instance detail transport", () => {
  it("issues an encoded same-origin GET and retains the strong response ETag", async () => {
    const fetchMock = vi.fn().mockResolvedValue(responseFor());
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    const result = await fetchAgentInstanceDetail(EXPECTED, {
      signal: controller.signal,
    });

    expect(result.etag).toBe(ETAG);
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/agent-instances/${encodeURIComponent(INSTANCE_ID)}`,
      {
        method: "GET",
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      },
    );
  });

  it("sends If-None-Match and returns prior object identity on a matching 304", async () => {
    const previous = normalizeAgentInstanceDetail(
      runtimeBody(),
      EXPECTED,
      ETAG,
    );
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(null, { status: 304, headers: { ETag: ETAG } }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchAgentInstanceDetail(EXPECTED, { previous });

    expect(result).toBe(previous);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({
        headers: { Accept: "application/json", "If-None-Match": ETAG },
      }),
    );
  });

  it.each([
    ["missing prior", undefined, ETAG],
    [
      "mismatched ETag",
      normalizeAgentInstanceDetail(runtimeBody(), EXPECTED, ETAG),
      `"${"d".repeat(64)}"`,
    ],
  ])("rejects a 304 with %s", async (_label, previous, responseEtag) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(null, {
          status: 304,
          headers: { ETag: responseEtag },
        }),
      ),
    );
    const options = previous === undefined ? {} : { previous };
    await expect(
      fetchAgentInstanceDetail(EXPECTED, options),
    ).rejects.toMatchObject({
      name: "ApiRequestError",
      status: 304,
      code: "invalid_agent_instance_detail_response",
    });
  });

  it.each([
    ["missing", ""],
    ["weak", `W/${ETAG}`],
    ["malformed", '"short"'],
  ])("rejects a successful response with a %s ETag", async (_label, etag) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(responseFor(runtimeBody(), etag)),
    );
    await expect(fetchAgentInstanceDetail(EXPECTED)).rejects.toMatchObject({
      name: "ApiRequestError",
      status: 200,
      code: "invalid_agent_instance_detail_response",
    });
  });

  it("maps invalid JSON, network failure, and safe API problems", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("private")));
    await expect(fetchAgentInstanceDetail(EXPECTED)).rejects.toMatchObject({
      name: "ApiRequestError",
      status: 0,
      code: "api_unreachable",
    });

    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response("not json", { status: 200, headers: { ETag: ETAG } }),
        ),
    );
    await expect(fetchAgentInstanceDetail(EXPECTED)).rejects.toMatchObject({
      name: "ApiRequestError",
      status: 200,
      code: "invalid_json_response",
    });

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            code: "catalog_unavailable",
            message: "The catalog is unavailable.",
            private: { secret: "do not reflect" },
          }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    const failure = await fetchAgentInstanceDetail(EXPECTED).catch(
      (error: unknown) => error,
    );
    expect(failure).toBeInstanceOf(ApiRequestError);
    expect(failure).toMatchObject({
      status: 503,
      code: "catalog_unavailable",
      message: "The catalog is unavailable.",
    });
    expect(String(failure)).not.toContain("do not reflect");
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";

import { clearLocalSession } from "./localSession";
import {
  createDemoScenarioRun,
  fetchDemoScenarios,
  SOCIAL_DRAFT_INSTANCE_ID,
  SOCIAL_DRAFT_SCENARIO_ID,
  SOCIAL_DRAFT_TEMPLATE_ID,
} from "./demoScenarios";

const SESSION_TOKEN = "a".repeat(43);
const IDEMPOTENCY_KEY = "demo-social-draft-retry-0001";
const EVENT_ID = `manual-event-hmac-sha256-v1:${"d".repeat(64)}`;
const WORK_ID = "work.demo.social.01";
const RUN_ID = "run.demo.social.01";

const INPUT_SCHEMA = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: "schema.demo.social-media.content-draft.input.v1",
  type: "object",
  additionalProperties: false,
  required: ["idea", "audience", "tone", "key_points"],
  properties: {
    idea: { type: "string", minLength: 1, maxLength: 1_200 },
    audience: { type: "string", minLength: 1, maxLength: 160 },
    tone: {
      type: "string",
      enum: ["professional", "conversational", "educational", "bold"],
    },
    key_points: {
      type: "array",
      minItems: 1,
      maxItems: 6,
      items: { type: "string", minLength: 1, maxLength: 250 },
    },
    call_to_action: { type: "string", minLength: 1, maxLength: 250 },
    source_urls: {
      type: "array",
      maxItems: 5,
      items: { type: "string", minLength: 1, maxLength: 2_048 },
    },
  },
} as const;

const PRESET = {
  idea: "Share how governed AI workflows turn a raw marketing idea into a reviewable draft.",
  audience: "Marketing and platform leaders",
  tone: "professional",
  key_points: [
    "Treat external content as untrusted data.",
    "Keep generation separate from publishing authority.",
    "Persist a traceable artifact for review.",
  ],
  source_urls: ["https://example.com/governed-ai"],
} as const;

function scenarioBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: SOCIAL_DRAFT_SCENARIO_ID,
    version: 1,
    displayName: "Social content draft",
    description:
      "Turn a supplied content idea into an inert, reviewable LinkedIn draft artifact.",
    workflowId: SOCIAL_DRAFT_SCENARIO_ID,
    effect: "read_only",
    mode: "deterministic_mock",
    selectedAgents: [
      {
        templateId: SOCIAL_DRAFT_TEMPLATE_ID,
        instanceId: SOCIAL_DRAFT_INSTANCE_ID,
      },
    ],
    inputSchema: INPUT_SCHEMA,
    preset: PRESET,
    safeSubmitVerb: "Create draft",
    expected: {
      statePath: ["received", "validated", "planned", "executing", "completed"],
      modelCalls: 1,
      connectorCalls: 0,
      externalActions: 0,
      approvals: 0,
      externalWrites: 0,
    },
    ...overrides,
  };
}

function receiptBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    status: "accepted",
    disposition: "created",
    scenarioId: SOCIAL_DRAFT_SCENARIO_ID,
    eventId: EVENT_ID,
    workId: WORK_ID,
    runId: RUN_ID,
    executionMode: "dry_run",
    instanceUrl: `/api/v1/agent-instances/${SOCIAL_DRAFT_INSTANCE_ID}`,
    runUrl: `/api/v1/runs/${RUN_ID}`,
    timelineUrl: `/api/v1/runs/${RUN_ID}/timeline`,
    artifactsUrl: `/api/v1/runs/${RUN_ID}/artifacts`,
    ...overrides,
  };
}

function sessionBody(): Record<string, unknown> {
  return {
    actorId: "local-operator",
    roles: ["approver", "local_admin", "operator", "viewer"],
    scopes: ["approvals:decide", "approvals:read"],
    authMode: "local",
    environment: "local",
    modelMode: "mock",
    connectorMode: "mock",
    networkPermission: false,
    warning: "Local identity — not production authentication",
    csrfToken: SESSION_TOKEN,
    csrfHeaderName: "X-CSRF-Token",
  };
}

function jsonResponse(value: Record<string, unknown>, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

afterEach(() => {
  clearLocalSession();
  vi.unstubAllGlobals();
});

describe("DEMO-01 demo scenario transport", () => {
  it("DEMO-01 discovers and freezes the exact Social safe-preset projection", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({
        items: [scenarioBody()],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    const scenarios = await fetchDemoScenarios(controller.signal);

    expect(scenarios).toHaveLength(1);
    expect(scenarios[0]).toMatchObject({
      id: SOCIAL_DRAFT_SCENARIO_ID,
      mode: "deterministic_mock",
      effect: "read_only",
      safeSubmitVerb: "Create draft",
      selectedAgents: [
        {
          templateId: SOCIAL_DRAFT_TEMPLATE_ID,
          instanceId: SOCIAL_DRAFT_INSTANCE_ID,
        },
      ],
    });
    expect(Object.isFrozen(scenarios)).toBe(true);
    expect(Object.isFrozen(scenarios[0]?.inputSchema)).toBe(true);
    expect(Object.isFrozen(scenarios[0]?.preset.key_points)).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/demo-scenarios", {
      method: "GET",
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
  });

  it("DEMO-01 keeps the shared discovery decoder future-safe without weakening bounds", async () => {
    const future = scenarioBody({
      id: "demo.email.campaign-draft.v2",
      version: 2,
      workflowId: "workflow.demo.email.campaign-draft.v2",
      effect: "mutating",
      selectedAgents: [
        {
          templateId: "tpl.email.campaign.email-writer",
          instanceId: "inst.email.campaign.email-writer.01",
        },
        {
          templateId: "tpl.email.campaign.email-reviewer",
          instanceId: "inst.email.campaign.email-reviewer.01",
        },
      ],
      safeSubmitVerb: "Create review draft",
      expected: {
        statePath: ["received", "validated", "completed"],
        modelCalls: 2,
        connectorCalls: 1,
        externalActions: 1,
        approvals: 1,
        externalWrites: 1,
      },
    });
    vi.stubGlobal(
      "fetch",
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(jsonResponse({ items: [future] })),
    );

    await expect(fetchDemoScenarios()).resolves.toMatchObject([
      {
        id: "demo.email.campaign-draft.v2",
        version: 2,
        effect: "mutating",
        selectedAgents: [{}, {}],
        safeSubmitVerb: "Create review draft",
      },
    ]);
  });

  it.each([
    ["an unknown field", () => scenarioBody({ extra: true })],
    [
      "duplicate selected instances",
      () =>
        scenarioBody({
          selectedAgents: [
            {
              templateId: SOCIAL_DRAFT_TEMPLATE_ID,
              instanceId: SOCIAL_DRAFT_INSTANCE_ID,
            },
            {
              templateId: SOCIAL_DRAFT_TEMPLATE_ID,
              instanceId: SOCIAL_DRAFT_INSTANCE_ID,
            },
          ],
        }),
    ],
    [
      "a dangerous submit label",
      () => scenarioBody({ safeSubmitVerb: "Create\nthing" }),
    ],
    [
      "an unsafe advertised action",
      () => scenarioBody({ safeSubmitVerb: "Publish draft" }),
    ],
    [
      "an unbounded expected count",
      () =>
        scenarioBody({
          expected: {
            ...(scenarioBody().expected as Record<string, unknown>),
            externalWrites: 1_001,
          },
        }),
    ],
  ])("DEMO-01 rejects discovery with %s", async (_label, mutate) => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn<typeof fetch>()
        .mockResolvedValue(jsonResponse({ items: [mutate()] })),
    );

    await expect(fetchDemoScenarios()).rejects.toMatchObject({
      code: "invalid_demo_scenarios_response",
    });
  });

  it("DEMO-01 posts complete overrides with idempotency and private CSRF, then cross-binds the receipt", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(jsonResponse(receiptBody(), 202));
    vi.stubGlobal("fetch", fetchMock);

    const receipt = await createDemoScenarioRun({
      scenarioId: SOCIAL_DRAFT_SCENARIO_ID,
      instanceId: SOCIAL_DRAFT_INSTANCE_ID,
      overrides: PRESET,
      idempotencyKey: IDEMPOTENCY_KEY,
    });

    expect(receipt).toEqual(receiptBody());
    expect(Object.isFrozen(receipt)).toBe(true);
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `/api/v1/demo-scenarios/${SOCIAL_DRAFT_SCENARIO_ID}/runs`,
      {
        method: "POST",
        cache: "no-store",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "Idempotency-Key": IDEMPOTENCY_KEY,
          "X-CSRF-Token": SESSION_TOKEN,
        },
        body: JSON.stringify({ overrides: PRESET }),
      },
    );
  });

  it("DEMO-01 rejects a receipt whose scenario, mode, or links drift", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(
        jsonResponse(
          receiptBody({
            timelineUrl: "/api/v1/runs/run.other/timeline",
          }),
          202,
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createDemoScenarioRun({
        scenarioId: SOCIAL_DRAFT_SCENARIO_ID,
        instanceId: SOCIAL_DRAFT_INSTANCE_ID,
        overrides: PRESET,
        idempotencyKey: IDEMPOTENCY_KEY,
      }),
    ).rejects.toMatchObject({ code: "invalid_demo_scenario_response" });
  });

  it("DEMO-01 rejects unsafe IDs and non-JSON request shapes before any network call", async () => {
    const sparse = new Array<string>(1);
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createDemoScenarioRun({
        scenarioId: "demo/social-content-draft.v1",
        instanceId: SOCIAL_DRAFT_INSTANCE_ID,
        overrides: PRESET,
        idempotencyKey: IDEMPOTENCY_KEY,
      }),
    ).rejects.toMatchObject({ code: "invalid_demo_scenario_request" });
    await expect(
      createDemoScenarioRun({
        scenarioId: SOCIAL_DRAFT_SCENARIO_ID,
        instanceId: SOCIAL_DRAFT_INSTANCE_ID,
        overrides: { source_urls: sparse },
        idempotencyKey: IDEMPOTENCY_KEY,
      }),
    ).rejects.toMatchObject({ code: "invalid_demo_scenario_request" });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

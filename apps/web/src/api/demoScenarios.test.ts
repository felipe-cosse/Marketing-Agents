import { afterEach, describe, expect, it, vi } from "vitest";

import { clearLocalSession } from "./localSession";
import {
  BLOG_CONTENT_REVIEW_INSTANCE_ID,
  BLOG_CONTENT_REVIEW_SCENARIO_ID,
  BLOG_CONTENT_REVIEW_TEMPLATE_ID,
  COMMUNITY_REMINDER_INSTANCE_ID,
  COMMUNITY_REMINDER_SCENARIO_ID,
  COMMUNITY_REMINDER_TEMPLATE_ID,
  createDemoScenarioRun,
  EMAIL_NEWSLETTER_INSTANCE_ID,
  EMAIL_NEWSLETTER_TEMPLATE_ID,
  EMAIL_ONBOARDING_INSTANCE_ID,
  EMAIL_ONBOARDING_TEMPLATE_ID,
  EMAIL_SIGNUP_SCENARIO_ID,
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

const BLOG_INPUT_SCHEMA = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: "schema.demo.blog-seo.content-review.input.v1",
  type: "object",
  additionalProperties: false,
  required: [
    "article_title",
    "canonical_url",
    "supplied_excerpt",
    "last_updated_at",
    "assessment_at",
    "target_keywords",
    "current_product_metadata",
  ],
  properties: {
    article_title: { type: "string", minLength: 1, maxLength: 240 },
    canonical_url: {
      type: "string",
      format: "uri",
      minLength: 1,
      maxLength: 2_048,
    },
    supplied_excerpt: { type: "string", minLength: 1, maxLength: 8_000 },
    last_updated_at: {
      type: "string",
      format: "date-time",
      maxLength: 40,
    },
    assessment_at: {
      type: "string",
      format: "date-time",
      maxLength: 40,
    },
    target_keywords: {
      type: "array",
      minItems: 1,
      maxItems: 8,
      items: { type: "string", minLength: 1, maxLength: 80 },
    },
    current_product_metadata: {
      type: "object",
      additionalProperties: false,
      required: ["features", "integrations"],
      properties: {
        features: {
          type: "array",
          minItems: 0,
          maxItems: 6,
          items: {
            type: "object",
            additionalProperties: false,
            required: ["name", "summary"],
            properties: {
              name: { type: "string", minLength: 1, maxLength: 120 },
              summary: { type: "string", minLength: 1, maxLength: 500 },
            },
          },
        },
        integrations: {
          type: "array",
          minItems: 0,
          maxItems: 6,
          items: {
            type: "object",
            additionalProperties: false,
            required: ["name", "summary"],
            properties: {
              name: { type: "string", minLength: 1, maxLength: 120 },
              summary: { type: "string", minLength: 1, maxLength: 500 },
            },
          },
        },
      },
    },
  },
} as const;

const BLOG_PRESET = {
  article_title: "Governed AI workflows for marketing teams",
  canonical_url: "https://example.com/blog/governed-ai-workflows",
  supplied_excerpt:
    "Governed AI helps marketing teams create reviewable drafts with artifact provenance.",
  last_updated_at: "2025-12-01T00:00:00Z",
  assessment_at: "2026-08-31T00:00:00Z",
  target_keywords: ["governed AI", "marketing teams", "approval workflows"],
  current_product_metadata: {
    features: [
      {
        name: "Artifact provenance",
        summary: "Generated artifacts retain source and provider provenance.",
      },
      {
        name: "Exact approval gates",
        summary: "External writes require approval of the exact payload.",
      },
    ],
    integrations: [
      {
        name: "CMS review export",
        summary:
          "Review artifacts can be prepared for a later human-controlled CMS workflow.",
      },
    ],
  },
} as const;

const EMAIL_INPUT_SCHEMA = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: "schema.demo.email.signup-onboarding.input.v1",
  type: "object",
  additionalProperties: false,
  required: [
    "contact_id",
    "name",
    "email",
    "newsletter_list_ref",
    "consent",
    "signup_at",
    "welcome_context",
  ],
  properties: {
    contact_id: {
      type: "string",
      minLength: 1,
      maxLength: 200,
      pattern: "^demo-contact-[a-z0-9-]+$",
    },
    name: {
      type: "string",
      minLength: 1,
      maxLength: 120,
      "x-sensitive": true,
    },
    email: {
      type: "string",
      format: "email",
      minLength: 3,
      maxLength: 254,
      "x-sensitive": true,
    },
    newsletter_list_ref: {
      type: "string",
      const: "list.demo.email.signup-onboarding.v1",
    },
    consent: {
      type: "object",
      additionalProperties: false,
      required: ["granted", "source", "captured_at"],
      properties: {
        granted: { type: "boolean", const: true },
        source: { type: "string", const: "demo_signup_form" },
        captured_at: {
          type: "string",
          format: "date-time",
          maxLength: 40,
        },
      },
    },
    signup_at: { type: "string", format: "date-time", maxLength: 40 },
    welcome_context: { type: "string", minLength: 1, maxLength: 2_000 },
  },
} as const;

const EMAIL_PRESET = {
  contact_id: "demo-contact-0001",
  name: "Avery Demo",
  email: "avery.demo@example.test",
  newsletter_list_ref: "list.demo.email.signup-onboarding.v1",
  consent: {
    granted: true,
    source: "demo_signup_form",
    captured_at: "2026-08-31T16:00:00Z",
  },
  signup_at: "2026-08-31T16:05:00Z",
  welcome_context:
    "Welcome the subscriber to governed AI updates for marketing teams.",
} as const;

const COMMUNITY_INPUT_SCHEMA = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: "schema.demo.community.reminder-draft.input.v1",
  type: "object",
  additionalProperties: false,
  required: [
    "event_id",
    "event_name",
    "signup_event_id",
    "admitted_source",
    "signup_at",
    "session_local_start",
    "session_timezone",
    "reminder_offset_minutes",
    "attendee_display_name",
    "channel_label",
    "event_details",
  ],
  properties: {
    event_id: {
      type: "string",
      minLength: 1,
      maxLength: 120,
      pattern: "^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$",
    },
    event_name: { type: "string", minLength: 1, maxLength: 160 },
    signup_event_id: {
      type: "string",
      minLength: 1,
      maxLength: 120,
      pattern: "^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$",
    },
    admitted_source: {
      type: "string",
      minLength: 1,
      maxLength: 80,
      pattern: "^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$",
    },
    signup_at: { type: "string", format: "date-time", maxLength: 40 },
    session_local_start: {
      type: "string",
      pattern: "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$",
      maxLength: 19,
    },
    session_timezone: {
      type: "string",
      minLength: 1,
      maxLength: 64,
      pattern: "^[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*$",
    },
    reminder_offset_minutes: {
      type: "integer",
      minimum: 1,
      maximum: 10_080,
    },
    attendee_display_name: {
      type: "string",
      minLength: 1,
      maxLength: 120,
      "x-sensitive": true,
    },
    channel_label: {
      type: "string",
      enum: ["email", "community", "in_app"],
    },
    event_details: { type: "string", minLength: 1, maxLength: 2_000 },
  },
} as const;

const COMMUNITY_PRESET = {
  event_id: "event.community-live-session.2026-09-17",
  event_name: "Marketing operators live session",
  signup_event_id: "signup.community-demo-0001",
  admitted_source: "fixture.community-signup",
  signup_at: "2026-09-01T16:30:00Z",
  session_local_start: "2026-09-17T09:00:00",
  session_timezone: "America/Los_Angeles",
  reminder_offset_minutes: 1_440,
  attendee_display_name: "Demo Attendee",
  channel_label: "email",
  event_details:
    "A live session on governed marketing automation and approval-safe workflows.",
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

function blogScenarioBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: BLOG_CONTENT_REVIEW_SCENARIO_ID,
    version: 1,
    displayName: "Blog & SEO content review",
    description:
      "Review supplied article and product metadata for deterministic SEO and content gaps without fetching or updating a CMS.",
    workflowId: BLOG_CONTENT_REVIEW_SCENARIO_ID,
    effect: "read_only",
    mode: "deterministic_mock",
    selectedAgents: [
      {
        templateId: BLOG_CONTENT_REVIEW_TEMPLATE_ID,
        instanceId: BLOG_CONTENT_REVIEW_INSTANCE_ID,
      },
    ],
    inputSchema: BLOG_INPUT_SCHEMA,
    preset: BLOG_PRESET,
    safeSubmitVerb: "Create review",
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

function emailScenarioBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: EMAIL_SIGNUP_SCENARIO_ID,
    version: 1,
    displayName: "Email signup onboarding",
    description:
      "Prepare approved mock newsletter and CRM onboarding actions, then create a welcome-message draft that is never sent.",
    workflowId: EMAIL_SIGNUP_SCENARIO_ID,
    effect: "mutating",
    mode: "deterministic_mock",
    selectedAgents: [
      {
        templateId: EMAIL_NEWSLETTER_TEMPLATE_ID,
        instanceId: EMAIL_NEWSLETTER_INSTANCE_ID,
      },
      {
        templateId: EMAIL_ONBOARDING_TEMPLATE_ID,
        instanceId: EMAIL_ONBOARDING_INSTANCE_ID,
      },
    ],
    inputSchema: EMAIL_INPUT_SCHEMA,
    preset: EMAIL_PRESET,
    safeSubmitVerb: "Propose onboarding actions",
    expected: {
      statePath: [
        "received",
        "validated",
        "planned",
        "awaiting_approval",
        "executing",
        "completed",
      ],
      modelCalls: 1,
      connectorCalls: 2,
      externalActions: 2,
      approvals: 2,
      externalWrites: 2,
    },
    ...overrides,
  };
}

function communityScenarioBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: COMMUNITY_REMINDER_SCENARIO_ID,
    version: 1,
    displayName: "Community reminder draft",
    description:
      "Create a deterministic reminder draft and recommended UTC time from supplied event signup details without scheduling or sending.",
    workflowId: COMMUNITY_REMINDER_SCENARIO_ID,
    effect: "read_only",
    mode: "deterministic_mock",
    selectedAgents: [
      {
        templateId: COMMUNITY_REMINDER_TEMPLATE_ID,
        instanceId: COMMUNITY_REMINDER_INSTANCE_ID,
      },
    ],
    inputSchema: COMMUNITY_INPUT_SCHEMA,
    preset: COMMUNITY_PRESET,
    safeSubmitVerb: "Create reminder draft",
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

function emailReceiptBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  const runId = "run.demo.email.01";
  return {
    status: "accepted",
    disposition: "created",
    scenarioId: EMAIL_SIGNUP_SCENARIO_ID,
    eventId: `manual-event-hmac-sha256-v1:${"e".repeat(64)}`,
    workId: "work.demo.email.01",
    runId,
    executionMode: "mock_execute",
    instanceUrl: `/api/v1/agent-instances/${EMAIL_NEWSLETTER_INSTANCE_ID}`,
    runUrl: `/api/v1/runs/${runId}`,
    timelineUrl: `/api/v1/runs/${runId}/timeline`,
    artifactsUrl: `/api/v1/runs/${runId}/artifacts`,
    ...overrides,
  };
}

function communityReceiptBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  const runId = "run.demo.community.01";
  return {
    status: "accepted",
    disposition: "created",
    scenarioId: COMMUNITY_REMINDER_SCENARIO_ID,
    eventId: `manual-event-hmac-sha256-v1:${"f".repeat(64)}`,
    workId: "work.demo.community.01",
    runId,
    executionMode: "dry_run",
    instanceUrl: `/api/v1/agent-instances/${COMMUNITY_REMINDER_INSTANCE_ID}`,
    runUrl: `/api/v1/runs/${runId}`,
    timelineUrl: `/api/v1/runs/${runId}/timeline`,
    artifactsUrl: `/api/v1/runs/${runId}/artifacts`,
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

  it("DEMO-02 discovers and freezes Blog alongside the Social scenario", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({
        items: [blogScenarioBody(), scenarioBody()],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const scenarios = await fetchDemoScenarios();

    expect(scenarios.map((scenario) => scenario.id)).toEqual([
      BLOG_CONTENT_REVIEW_SCENARIO_ID,
      SOCIAL_DRAFT_SCENARIO_ID,
    ]);
    expect(scenarios[0]).toMatchObject({
      id: BLOG_CONTENT_REVIEW_SCENARIO_ID,
      effect: "read_only",
      mode: "deterministic_mock",
      safeSubmitVerb: "Create review",
      selectedAgents: [
        {
          templateId: BLOG_CONTENT_REVIEW_TEMPLATE_ID,
          instanceId: BLOG_CONTENT_REVIEW_INSTANCE_ID,
        },
      ],
    });
    expect(Object.isFrozen(scenarios)).toBe(true);
    expect(Object.isFrozen(scenarios[0])).toBe(true);
    expect(Object.isFrozen(scenarios[0]?.inputSchema)).toBe(true);
    expect(Object.isFrozen(scenarios[0]?.preset.target_keywords)).toBe(true);
    expect(Object.isFrozen(scenarios[0]?.preset.current_product_metadata)).toBe(
      true,
    );
  });

  it("DEMO-03 discovers and freezes the exact two-agent Email approval-boundary projection", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse({
          items: [emailScenarioBody(), blogScenarioBody(), scenarioBody()],
        }),
      ),
    );

    const scenarios = await fetchDemoScenarios();
    const email = scenarios[0];

    expect(email).toMatchObject({
      id: EMAIL_SIGNUP_SCENARIO_ID,
      effect: "mutating",
      safeSubmitVerb: "Propose onboarding actions",
      selectedAgents: [
        {
          templateId: EMAIL_NEWSLETTER_TEMPLATE_ID,
          instanceId: EMAIL_NEWSLETTER_INSTANCE_ID,
        },
        {
          templateId: EMAIL_ONBOARDING_TEMPLATE_ID,
          instanceId: EMAIL_ONBOARDING_INSTANCE_ID,
        },
      ],
      expected: {
        statePath: [
          "received",
          "validated",
          "planned",
          "awaiting_approval",
          "executing",
          "completed",
        ],
        modelCalls: 1,
        connectorCalls: 2,
        externalActions: 2,
        approvals: 2,
        externalWrites: 2,
      },
    });
    expect(Object.isFrozen(email)).toBe(true);
    expect(Object.isFrozen(email?.selectedAgents)).toBe(true);
    expect(Object.isFrozen(email?.inputSchema)).toBe(true);
    expect(Object.isFrozen(email?.preset.consent)).toBe(true);
  });

  it("DEMO-04 discovers and freezes the exact Community UTC reminder projection", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse({
          items: [
            communityScenarioBody(),
            emailScenarioBody(),
            blogScenarioBody(),
            scenarioBody(),
          ],
        }),
      ),
    );

    const scenarios = await fetchDemoScenarios();
    const community = scenarios[0];
    expect(community).toMatchObject({
      id: COMMUNITY_REMINDER_SCENARIO_ID,
      effect: "read_only",
      safeSubmitVerb: "Create reminder draft",
      selectedAgents: [
        {
          templateId: COMMUNITY_REMINDER_TEMPLATE_ID,
          instanceId: COMMUNITY_REMINDER_INSTANCE_ID,
        },
      ],
      expected: {
        statePath: [
          "received",
          "validated",
          "planned",
          "executing",
          "completed",
        ],
        modelCalls: 1,
        connectorCalls: 0,
        externalActions: 0,
        approvals: 0,
        externalWrites: 0,
      },
    });
    expect(Object.isFrozen(community)).toBe(true);
    expect(Object.isFrozen(community?.inputSchema)).toBe(true);
    expect(Object.isFrozen(community?.preset)).toBe(true);
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
      expectedExecutionMode: "dry_run",
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

  it("DEMO-03 cross-binds the Email receipt to mock execution and its primary instance", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(jsonResponse(emailReceiptBody(), 202));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createDemoScenarioRun({
        scenarioId: EMAIL_SIGNUP_SCENARIO_ID,
        instanceId: EMAIL_NEWSLETTER_INSTANCE_ID,
        overrides: EMAIL_PRESET,
        idempotencyKey: "demo-email-signup-retry-0001",
        expectedExecutionMode: "mock_execute",
      }),
    ).resolves.toEqual(emailReceiptBody());

    clearLocalSession();
    const driftedFetch = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(
        jsonResponse(emailReceiptBody({ executionMode: "dry_run" }), 202),
      );
    vi.stubGlobal("fetch", driftedFetch);
    await expect(
      createDemoScenarioRun({
        scenarioId: EMAIL_SIGNUP_SCENARIO_ID,
        instanceId: EMAIL_NEWSLETTER_INSTANCE_ID,
        overrides: EMAIL_PRESET,
        idempotencyKey: "demo-email-signup-retry-0002",
        expectedExecutionMode: "mock_execute",
      }),
    ).rejects.toMatchObject({ code: "invalid_demo_scenario_response" });
  });

  it("DEMO-04 cross-binds the Community receipt to dry-run and its reminder instance", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(jsonResponse(communityReceiptBody(), 202));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createDemoScenarioRun({
        scenarioId: COMMUNITY_REMINDER_SCENARIO_ID,
        instanceId: COMMUNITY_REMINDER_INSTANCE_ID,
        overrides: COMMUNITY_PRESET,
        idempotencyKey: "demo-community-reminder-retry-0001",
        expectedExecutionMode: "dry_run",
      }),
    ).resolves.toEqual(communityReceiptBody());
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
        expectedExecutionMode: "dry_run",
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
        expectedExecutionMode: "dry_run",
      }),
    ).rejects.toMatchObject({ code: "invalid_demo_scenario_request" });
    await expect(
      createDemoScenarioRun({
        scenarioId: SOCIAL_DRAFT_SCENARIO_ID,
        instanceId: SOCIAL_DRAFT_INSTANCE_ID,
        overrides: { source_urls: sparse },
        idempotencyKey: IDEMPOTENCY_KEY,
        expectedExecutionMode: "dry_run",
      }),
    ).rejects.toMatchObject({ code: "invalid_demo_scenario_request" });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

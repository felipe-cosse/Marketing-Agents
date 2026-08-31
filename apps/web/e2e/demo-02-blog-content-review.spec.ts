// DEMO-02 browser evidence exercises the production build through the strict local API boundary.
import {
  expect,
  test,
  type Page,
  type Request,
  type Route,
  type TestInfo,
} from "@playwright/test";

type JsonObject = Record<string, unknown>;

interface DemoObservation {
  readonly externalRequests: string[];
  readonly posts: {
    readonly body: JsonObject;
    readonly headers: Record<string, string>;
    readonly path: string;
  }[];
  discoveryRequests: number;
}

const SOCIAL_SCENARIO_ID = "demo.social-media.content-draft.v1";
const SOCIAL_TEMPLATE_ID = "tpl.social-media.new-content.linkedin-post-drafter";
const SOCIAL_INSTANCE_ID =
  "inst.social-media.new-content.linkedin-post-drafter.01";
const BLOG_SCENARIO_ID = "demo.blog-seo.content-review.v1";
const BLOG_TEMPLATE_ID = "tpl.blog-seo.new-content.blog-post-updater";
const BLOG_INSTANCE_ID = "inst.blog-seo.new-content.blog-post-updater.01";
const RUN_ID = "run.demo-02.browser";
const WORK_ID = "work.demo-02.browser";
const EDITED_EXCERPT =
  "Governed workflows keep supplied marketing evidence reviewable without granting publishing authority.";
const LOCAL_HOSTS = new Set(["127.0.0.1", "localhost"]);
const DIRECT_STATE_PATH = [
  "received",
  "validated",
  "planned",
  "executing",
  "completed",
] as const;

const SOCIAL_PRESET = {
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

function socialScenario(): JsonObject {
  return {
    id: SOCIAL_SCENARIO_ID,
    version: 1,
    displayName: "Social content draft",
    description:
      "Turn a supplied content idea into an inert, reviewable LinkedIn draft artifact.",
    workflowId: SOCIAL_SCENARIO_ID,
    effect: "read_only",
    mode: "deterministic_mock",
    selectedAgents: [
      { templateId: SOCIAL_TEMPLATE_ID, instanceId: SOCIAL_INSTANCE_ID },
    ],
    inputSchema: {
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
        call_to_action: {
          type: "string",
          minLength: 1,
          maxLength: 250,
        },
        source_urls: {
          type: "array",
          maxItems: 5,
          items: { type: "string", minLength: 1, maxLength: 2_048 },
        },
      },
    },
    preset: SOCIAL_PRESET,
    safeSubmitVerb: "Create draft",
    expected: {
      statePath: DIRECT_STATE_PATH,
      modelCalls: 1,
      connectorCalls: 0,
      externalActions: 0,
      approvals: 0,
      externalWrites: 0,
    },
  };
}

function blogScenario(overrides: JsonObject = {}): JsonObject {
  return {
    id: BLOG_SCENARIO_ID,
    version: 1,
    displayName: "Blog & SEO content review",
    description:
      "Review supplied article and product metadata for deterministic SEO and content gaps without fetching or updating a CMS.",
    workflowId: BLOG_SCENARIO_ID,
    effect: "read_only",
    mode: "deterministic_mock",
    selectedAgents: [
      { templateId: BLOG_TEMPLATE_ID, instanceId: BLOG_INSTANCE_ID },
    ],
    inputSchema: {
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
        supplied_excerpt: {
          type: "string",
          minLength: 1,
          maxLength: 8_000,
        },
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
    },
    preset: BLOG_PRESET,
    safeSubmitVerb: "Create review",
    expected: {
      statePath: DIRECT_STATE_PATH,
      modelCalls: 1,
      connectorCalls: 0,
      externalActions: 0,
      approvals: 0,
      externalWrites: 0,
    },
    ...overrides,
  };
}

function receipt(): JsonObject {
  return {
    status: "accepted",
    disposition: "created",
    scenarioId: BLOG_SCENARIO_ID,
    eventId: `manual-event-hmac-sha256-v1:${"e".repeat(64)}`,
    workId: WORK_ID,
    runId: RUN_ID,
    executionMode: "dry_run",
    instanceUrl: `/api/v1/agent-instances/${BLOG_INSTANCE_ID}`,
    runUrl: `/api/v1/runs/${RUN_ID}`,
    timelineUrl: `/api/v1/runs/${RUN_ID}/timeline`,
    artifactsUrl: `/api/v1/runs/${RUN_ID}/artifacts`,
  };
}

function requestBody(route: Route): JsonObject {
  const value: unknown = route.request().postDataJSON();
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("DEMO-02 expected one JSON object request body");
  }
  return value as JsonObject;
}

async function installBoundary(
  page: Page,
  discovery: JsonObject = {
    items: [blogScenario(), socialScenario()],
  },
): Promise<DemoObservation> {
  const observation: DemoObservation = {
    externalRequests: [],
    posts: [],
    discoveryRequests: 0,
  };
  page.on("request", (request: Request) => {
    const url = new URL(request.url());
    if (!LOCAL_HOSTS.has(url.hostname)) {
      observation.externalRequests.push(request.url());
    }
  });
  await page.route("**/api/v1/demo-scenarios", async (route) => {
    expect(route.request().method()).toBe("GET");
    observation.discoveryRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Cache-Control": "no-store" },
      body: JSON.stringify(discovery),
    });
  });
  await page.route("**/api/v1/demo-scenarios/*/runs", async (route) => {
    const request = route.request();
    observation.posts.push({
      body: requestBody(route),
      headers: request.headers(),
      path: new URL(request.url()).pathname,
    });
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      headers: { "Cache-Control": "no-store" },
      body: JSON.stringify(receipt()),
    });
  });
  return observation;
}

async function expectNoDocumentOverflow(
  page: Page,
  expectedWidth: number,
): Promise<void> {
  await expect
    .poll(() =>
      page.evaluate(() => ({
        documentClientWidth: document.documentElement.clientWidth,
        documentScrollWidth: document.documentElement.scrollWidth,
        bodyClientWidth: document.body.clientWidth,
        bodyScrollWidth: document.body.scrollWidth,
        demoClientWidth:
          document.querySelector<HTMLElement>(".demo-page")?.clientWidth ?? -1,
        demoScrollWidth:
          document.querySelector<HTMLElement>(".demo-page")?.scrollWidth ?? -1,
      })),
    )
    .toEqual({
      documentClientWidth: expectedWidth,
      documentScrollWidth: expectedWidth,
      bodyClientWidth: expectedWidth,
      bodyScrollWidth: expectedWidth,
      demoClientWidth: expectedWidth,
      demoScrollWidth: expectedWidth,
    });
}

async function capture(
  page: Page,
  testInfo: TestInfo,
  filename: string,
): Promise<void> {
  await page.screenshot({ path: testInfo.outputPath(filename) });
}

test("DEMO-02 selects the Blog preset, admits one advisory review, and exposes durable resources", async ({
  page,
}, testInfo) => {
  const observation = await installBoundary(page);
  await page.goto("/demos", { waitUntil: "networkidle" });

  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "Social idea to draft artifact",
    }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: /Blog & SEO content review/iu })
    .click();

  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "Blog metadata to SEO/content review",
    }),
  ).toBeVisible();
  await expect(page.getByText("Read-only", { exact: true })).toBeVisible();
  await expect(
    page.getByText("0 external writes", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("No crawling or CMS actions", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(BLOG_TEMPLATE_ID, { exact: true })).toBeVisible();
  await expect(page.getByText(BLOG_INSTANCE_ID, { exact: true })).toBeVisible();
  await expect(
    page.getByText(/canonical URL is provenance text and is never fetched/iu),
  ).toBeVisible();

  const form = page.getByRole("form", {
    name: "Blog & SEO content review preset",
  });
  await expect(form).toBeVisible();
  await expect(
    form.getByRole("textbox", { name: /^article title/iu }),
  ).toHaveValue(BLOG_PRESET.article_title);
  await expect(
    form.getByRole("textbox", { name: /^canonical url/iu }),
  ).toHaveValue(BLOG_PRESET.canonical_url);
  await expect(form.getByText(/does not fetch it/iu)).toBeVisible();
  await expect(
    form.getByRole("button", { name: /crawl|update cms|upload|publish/iu }),
  ).toHaveCount(0);
  expect(observation.discoveryRequests).toBe(1);
  expect(observation.posts).toEqual([]);

  await form
    .getByRole("textbox", { name: /^supplied excerpt/iu })
    .fill(EDITED_EXCERPT);
  await form
    .getByRole("button", { name: "Create review", exact: true })
    .click();
  await expect.poll(() => observation.posts.length).toBe(1);
  await expect(
    page.getByRole("heading", { name: "Review run accepted" }),
  ).toBeVisible();
  await expect(page.getByText(WORK_ID, { exact: true })).toBeVisible();
  await expect(page.getByText(RUN_ID, { exact: true })).toBeVisible();

  const post = observation.posts[0];
  expect(post?.path).toBe(`/api/v1/demo-scenarios/${BLOG_SCENARIO_ID}/runs`);
  expect(post?.body).toEqual({
    overrides: { ...BLOG_PRESET, supplied_excerpt: EDITED_EXCERPT },
  });
  expect(post?.headers.accept).toBe("application/json");
  expect(post?.headers["content-type"]).toBe("application/json");
  expect(post?.headers["idempotency-key"]).toMatch(/^[\x21-\x7e]{8,240}$/u);
  expect(post?.headers["x-csrf-token"]).toMatch(/^[A-Za-z0-9_-]{32,128}$/u);

  await expect(
    page.getByRole("link", { name: "Open accepted run" }),
  ).toHaveAttribute("href", `/runs/${RUN_ID}`);
  await expect(
    page.getByRole("link", { name: "Open timeline" }),
  ).toHaveAttribute("href", `/runs/${RUN_ID}#timeline-title`);
  await expect(
    page.getByRole("link", { name: "Open artifacts" }),
  ).toHaveAttribute("href", `/runs/${RUN_ID}#run-artifacts-title`);

  const persistedBrowserState = await page.evaluate(() =>
    JSON.stringify({
      localStorage: Object.entries(localStorage),
      sessionStorage: Object.entries(sessionStorage),
      cookie: document.cookie,
    }),
  );
  expect(persistedBrowserState).not.toContain(EDITED_EXCERPT);
  expect(persistedBrowserState).not.toContain(post?.headers["idempotency-key"]);
  expect(observation.externalRequests).toEqual([]);
  await expectNoDocumentOverflow(page, 1_536);

  await page
    .getByRole("heading", { name: "Review run accepted" })
    .scrollIntoViewIfNeeded();
  await capture(page, testInfo, "demo-02-blog-content-review-1536x1024.png");

  await page.setViewportSize({ width: 426, height: 923 });
  await expect(form).toBeVisible();
  await expectNoDocumentOverflow(page, 426);
  await capture(page, testInfo, "demo-02-blog-content-review-426x923.png");
});

test("DEMO-02 fails closed when the Blog safety contract drifts", async ({
  page,
}) => {
  const drifted = blogScenario({
    expected: {
      statePath: DIRECT_STATE_PATH,
      modelCalls: 1,
      connectorCalls: 0,
      externalActions: 0,
      approvals: 0,
      externalWrites: 1,
    },
  });
  const observation = await installBoundary(page, {
    items: [drifted, socialScenario()],
  });
  await page.goto("/demos", { waitUntil: "networkidle" });

  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "Social idea to draft artifact",
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Blog & SEO content review/iu }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Create review", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Create draft", exact: true }),
  ).toBeVisible();
  expect(observation.posts).toEqual([]);
  expect(observation.externalRequests).toEqual([]);
});

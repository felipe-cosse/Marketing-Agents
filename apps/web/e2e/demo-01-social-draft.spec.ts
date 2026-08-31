// DEMO-01 browser evidence exercises the production build through the strict local API boundary.
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

const SCENARIO_ID = "demo.social-media.content-draft.v1";
const TEMPLATE_ID = "tpl.social-media.new-content.linkedin-post-drafter";
const INSTANCE_ID = "inst.social-media.new-content.linkedin-post-drafter.01";
const RUN_ID = "run.demo-01.browser";
const WORK_ID = "work.demo-01.browser";
const EDITED_IDEA =
  "Explain why durable AI drafts are easier for a marketing team to review.";
const LOCAL_HOSTS = new Set(["127.0.0.1", "localhost"]);

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

function scenario(overrides: JsonObject = {}): JsonObject {
  return {
    id: SCENARIO_ID,
    version: 1,
    displayName: "Social content draft",
    description:
      "Turn a supplied content idea into an inert, reviewable LinkedIn draft artifact.",
    workflowId: SCENARIO_ID,
    effect: "read_only",
    mode: "deterministic_mock",
    selectedAgents: [{ templateId: TEMPLATE_ID, instanceId: INSTANCE_ID }],
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

function receipt(): JsonObject {
  return {
    status: "accepted",
    disposition: "created",
    scenarioId: SCENARIO_ID,
    eventId: `manual-event-hmac-sha256-v1:${"d".repeat(64)}`,
    workId: WORK_ID,
    runId: RUN_ID,
    executionMode: "dry_run",
    instanceUrl: `/api/v1/agent-instances/${INSTANCE_ID}`,
    runUrl: `/api/v1/runs/${RUN_ID}`,
    timelineUrl: `/api/v1/runs/${RUN_ID}/timeline`,
    artifactsUrl: `/api/v1/runs/${RUN_ID}/artifacts`,
  };
}

function requestBody(route: Route): JsonObject {
  const value: unknown = route.request().postDataJSON();
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("DEMO-01 expected one JSON object request body");
  }
  return value as JsonObject;
}

async function installBoundary(
  page: Page,
  discovery: JsonObject = { items: [scenario()] },
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

test("DEMO-01 discovers a safe Social preset, admits one draft, and exposes durable resources", async ({
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
  await expect(page.getByText("Read-only", { exact: true })).toBeVisible();
  await expect(
    page.getByText("0 external writes", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("No approval required", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(TEMPLATE_ID, { exact: true })).toBeVisible();
  await expect(page.getByText(INSTANCE_ID, { exact: true })).toBeVisible();
  await expect(
    page.getByText("Deterministic mock mode", { exact: true }).last(),
  ).toBeVisible();

  const form = page.getByRole("form", { name: "Social draft demo preset" });
  await expect(form).toBeVisible();
  await expect(form.getByRole("textbox", { name: /^idea/iu })).toHaveValue(
    PRESET.idea,
  );
  await expect(form.getByRole("textbox", { name: /^audience/iu })).toHaveValue(
    PRESET.audience,
  );
  await expect(form.getByRole("combobox", { name: /^tone/iu })).toHaveValue(
    PRESET.tone,
  );
  await expect(
    form.getByRole("button", { name: "Create draft", exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/publish/iu)).toHaveCount(0);
  expect(observation.discoveryRequests).toBe(1);
  expect(observation.posts).toEqual([]);

  await form.getByRole("textbox", { name: /^idea/iu }).fill(EDITED_IDEA);
  await form.getByRole("button", { name: "Create draft", exact: true }).click();
  await expect.poll(() => observation.posts.length).toBe(1);
  await expect(
    page.getByRole("heading", { name: "Draft run accepted" }),
  ).toBeVisible();
  await expect(page.getByText(WORK_ID, { exact: true })).toBeVisible();
  await expect(page.getByText(RUN_ID, { exact: true })).toBeVisible();

  const post = observation.posts[0];
  expect(post?.path).toBe(`/api/v1/demo-scenarios/${SCENARIO_ID}/runs`);
  expect(post?.body).toEqual({
    overrides: { ...PRESET, idea: EDITED_IDEA },
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
  expect(persistedBrowserState).not.toContain(EDITED_IDEA);
  expect(persistedBrowserState).not.toContain(post?.headers["idempotency-key"]);
  expect(observation.externalRequests).toEqual([]);
  await expectNoDocumentOverflow(page, 1_536);

  await page
    .getByRole("heading", { name: "Draft run accepted" })
    .scrollIntoViewIfNeeded();
  await capture(page, testInfo, "demo-01-social-draft-1536x1024.png");

  await page.setViewportSize({ width: 426, height: 923 });
  await expect(form).toBeVisible();
  await expectNoDocumentOverflow(page, 426);
  await capture(page, testInfo, "demo-01-social-draft-426x923.png");
});

test("DEMO-01 fails closed when the discovered Social safety contract drifts", async ({
  page,
}) => {
  const observation = await installBoundary(page, {
    items: [scenario({ safeSubmitVerb: "Review draft" })],
  });
  await page.goto("/demos", { waitUntil: "networkidle" });

  await expect(
    page.getByText("Demo unavailable", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/Nothing can be submitted/iu)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Create draft", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByText(/publish/iu)).toHaveCount(0);
  expect(observation.posts).toEqual([]);
  expect(observation.externalRequests).toEqual([]);
});

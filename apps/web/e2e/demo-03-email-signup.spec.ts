// DEMO-03 browser evidence exercises the production build and live discovery contract.
import {
  expect,
  test,
  type Page,
  type Request,
  type Route,
  type TestInfo,
} from "@playwright/test";

type JsonObject = Record<string, unknown>;

const EMAIL_SCENARIO_ID = "demo.email.signup-onboarding.v1";
const EMAIL_NEWSLETTER_TEMPLATE_ID =
  "tpl.email.newsletter.newsletter-subscriber";
const EMAIL_NEWSLETTER_INSTANCE_ID =
  "inst.email.newsletter.newsletter-subscriber.01";
const EMAIL_ONBOARDING_TEMPLATE_ID =
  "tpl.email.lifecycle-marketing.customer-onboarder";
const EMAIL_ONBOARDING_INSTANCE_ID =
  "inst.email.lifecycle-marketing.customer-onboarder.01";
const RUN_ID = "run.demo-03.browser";
const WORK_ID = "work.demo-03.browser";
const LOCAL_HOSTS = new Set(["127.0.0.1", "localhost"]);

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

interface Observation {
  readonly externalRequests: string[];
  readonly posts: {
    readonly body: JsonObject;
    readonly headers: Record<string, string>;
    readonly path: string;
  }[];
}

function requestBody(route: Route): JsonObject {
  const value: unknown = route.request().postDataJSON();
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("DEMO-03 expected one JSON object request body");
  }
  return value as JsonObject;
}

async function installPostBoundary(page: Page): Promise<Observation> {
  const observation: Observation = { externalRequests: [], posts: [] };
  page.on("request", (request: Request) => {
    const url = new URL(request.url());
    if (!LOCAL_HOSTS.has(url.hostname)) {
      observation.externalRequests.push(request.url());
    }
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
      body: JSON.stringify({
        status: "accepted",
        disposition: "created",
        scenarioId: EMAIL_SCENARIO_ID,
        eventId: `manual-event-hmac-sha256-v1:${"e".repeat(64)}`,
        workId: WORK_ID,
        runId: RUN_ID,
        executionMode: "mock_execute",
        instanceUrl: `/api/v1/agent-instances/${EMAIL_NEWSLETTER_INSTANCE_ID}`,
        runUrl: `/api/v1/runs/${RUN_ID}`,
        timelineUrl: `/api/v1/runs/${RUN_ID}/timeline`,
        artifactsUrl: `/api/v1/runs/${RUN_ID}/artifacts`,
      }),
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

test("DEMO-03 proposes Email signup actions and exposes approval-gated durable resources", async ({
  page,
}, testInfo) => {
  const observation = await installPostBoundary(page);
  await page.goto("/demos", { waitUntil: "networkidle" });

  await page.getByRole("button", { name: /Email signup onboarding/iu }).click();
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "Email signup approval boundary",
    }),
  ).toBeVisible();
  await expect(
    page.getByText("2 exact approvals required", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("awaiting_approval", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(EMAIL_NEWSLETTER_TEMPLATE_ID, { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(EMAIL_NEWSLETTER_INSTANCE_ID, { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(EMAIL_ONBOARDING_TEMPLATE_ID, { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(EMAIL_ONBOARDING_INSTANCE_ID, { exact: true }),
  ).toBeVisible();

  const form = page.getByRole("form", {
    name: "Email signup onboarding preset",
  });
  const name = form.getByRole("textbox", { name: /^name/iu });
  const email = form.getByRole("textbox", { name: /^email/iu });
  await expect(name).toHaveAttribute("autocomplete", "off");
  await expect(name).toHaveAttribute("spellcheck", "false");
  await expect(email).toHaveAttribute("autocomplete", "off");
  await expect(email).toHaveAttribute("spellcheck", "false");
  await expect(
    form.getByRole("combobox", { name: /^newsletter list ref/iu }),
  ).toHaveValue(EMAIL_PRESET.newsletter_list_ref);
  await expect(
    form.getByRole("button", { name: /approve|subscribe|send|enroll/iu }),
  ).toHaveCount(0);

  await form
    .getByRole("button", { name: "Propose onboarding actions", exact: true })
    .click();
  await expect.poll(() => observation.posts.length).toBe(1);
  await expect(
    page.getByRole("heading", { name: "Approval-gated run accepted" }),
  ).toBeVisible();
  const post = observation.posts[0];
  expect(post?.path).toBe(`/api/v1/demo-scenarios/${EMAIL_SCENARIO_ID}/runs`);
  expect(post?.body).toEqual({ overrides: EMAIL_PRESET });
  expect(post?.headers["idempotency-key"]).toMatch(/^[\x21-\x7e]{8,240}$/u);
  expect(post?.headers["x-csrf-token"]).toMatch(/^[A-Za-z0-9_-]{32,128}$/u);
  await expect(
    page.getByRole("link", { name: "Open approval queue" }),
  ).toHaveAttribute("href", `/approvals?run_id=${RUN_ID}`);
  await expect(
    page.getByText(/receipt does not prove zero calls or execution/iu),
  ).toBeVisible();

  const persisted = await page.evaluate(() =>
    JSON.stringify({
      localStorage: Object.entries(localStorage),
      sessionStorage: Object.entries(sessionStorage),
      cookie: document.cookie,
      location: location.href,
    }),
  );
  expect(persisted).not.toContain(EMAIL_PRESET.email);
  expect(persisted).not.toContain(EMAIL_PRESET.name);
  expect(persisted).not.toContain(post?.headers["idempotency-key"]);
  expect(observation.externalRequests).toEqual([]);
  await expectNoDocumentOverflow(page, 1_536);
  await capture(page, testInfo, "demo-03-email-signup-1536x1024.png");

  await page.setViewportSize({ width: 426, height: 923 });
  await expect(form).toBeVisible();
  await expectNoDocumentOverflow(page, 426);
  await capture(page, testInfo, "demo-03-email-signup-426x923.png");
});

test("DEMO-03 fails closed when the Email approval boundary drifts", async ({
  page,
}) => {
  const observation = await installPostBoundary(page);
  await page.route("**/api/v1/demo-scenarios", async (route) => {
    const response = await route.fetch();
    const body = (await response.json()) as { items: JsonObject[] };
    const items = body.items.map((item) =>
      item.id === EMAIL_SCENARIO_ID
        ? {
            ...item,
            expected: {
              ...(item.expected as JsonObject),
              externalActions: 1,
            },
          }
        : item,
    );
    await route.fulfill({ response, json: { items } });
  });

  await page.goto("/demos", { waitUntil: "networkidle" });
  await expect(
    page.getByRole("button", { name: /Email signup onboarding/iu }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Create draft", exact: true }),
  ).toBeVisible();
  expect(observation.posts).toEqual([]);
  expect(observation.externalRequests).toEqual([]);
});

import { expect, test, type Page, type Route } from "@playwright/test";

import { installPlaywrightNetworkGuard } from "../../../scripts/browser-network-policy.mjs";

const WEB_ORIGIN = "http://127.0.0.1:4173";
const SCENARIO_ID = "demo.partnerships.application-review.v1";
const TEMPLATE_ID =
  "tpl.partnerships.implementation-partners.partner-application-reviewer";
const INSTANCE_ID =
  "inst.partnerships.implementation-partners.partner-application-reviewer.01";

interface Observation {
  readonly posts: { readonly body: unknown; readonly path: string }[];
}

async function capturePost(page: Page): Promise<Observation> {
  const observation: Observation = { posts: [] };
  await page.route(
    `${WEB_ORIGIN}/api/v1/demo-scenarios/${SCENARIO_ID}/runs`,
    async (route: Route) => {
      observation.posts.push({
        body: route.request().postDataJSON(),
        path: new URL(route.request().url()).pathname,
      });
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        headers: { "Cache-Control": "no-store" },
        body: JSON.stringify({
          status: "accepted",
          disposition: "created",
          scenarioId: SCENARIO_ID,
          eventId: `manual-event-hmac-sha256-v1:${"a".repeat(64)}`,
          workId: "work.demo-05.browser",
          runId: "run.demo-05.browser",
          executionMode: "dry_run",
          instanceUrl: `/api/v1/agent-instances/${INSTANCE_ID}`,
          runUrl: "/api/v1/runs/run.demo-05.browser",
          timelineUrl: "/api/v1/runs/run.demo-05.browser/timeline",
          artifactsUrl: "/api/v1/runs/run.demo-05.browser/artifacts",
        }),
      });
    },
  );
  return observation;
}

async function expectNoOverflow(page: Page, width: number): Promise<void> {
  await expect
    .poll(() =>
      page.evaluate(() => ({
        client: document.documentElement.clientWidth,
        scroll: document.documentElement.scrollWidth,
      })),
    )
    .toEqual({ client: width, scroll: width });
}

test("DEMO-05 creates advisory-only review without external research or decision controls", async ({
  page,
}) => {
  const guard = await installPlaywrightNetworkGuard(page);
  const observation = await capturePost(page);
  await page.goto("/demos", { waitUntil: "networkidle" });
  await expect(page.locator(".demo-switchboard__rail button")).toHaveCount(5);
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "Social idea to draft artifact",
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Social content draft/iu }),
  ).toHaveAttribute("aria-pressed", "true");
  await page
    .getByRole("button", { name: /Partnership application review/iu })
    .click();
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "Partner application to advisory review",
    }),
  ).toBeVisible();
  await expect(page.getByText("Advisory only", { exact: true })).toBeVisible();
  await expect(
    page.getByText("No automated decision", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("No external research or notification", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(TEMPLATE_ID, { exact: true })).toBeVisible();
  await expect(page.getByText(INSTANCE_ID, { exact: true })).toBeVisible();
  const form = page.getByRole("form", {
    name: "Partnership application review preset",
  });
  await expect(
    form.getByRole("button", { name: /accept|reject|notify|research/iu }),
  ).toHaveCount(0);
  await form.getByRole("button", { name: "Create advisory review" }).click();
  await expect.poll(() => observation.posts.length).toBe(1);
  await expect(
    page.getByRole("heading", { name: "Advisory review run accepted" }),
  ).toBeVisible();
  await expect(
    page.getByText(/acceptance is not an applicant decision/iu),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Open accepted run" }),
  ).toHaveAttribute("href", "/runs/run.demo-05.browser");
  await expect(
    page.getByRole("link", { name: "Open timeline" }),
  ).toHaveAttribute("href", "/runs/run.demo-05.browser#timeline-title");
  await expect(
    page.getByRole("link", { name: "Open artifacts" }),
  ).toHaveAttribute("href", "/runs/run.demo-05.browser#run-artifacts-title");
  await expect(
    page.getByRole("link", { name: "Open approval queue" }),
  ).toHaveCount(0);
  expect(observation.posts[0]?.path).toBe(
    `/api/v1/demo-scenarios/${SCENARIO_ID}/runs`,
  );
  expect(observation.posts[0]?.body).toEqual(
    expect.objectContaining({
      overrides: expect.objectContaining({
        applicant_id: "applicant.partnership-demo-0001",
      }),
    }),
  );
  expect(guard.blocked).toEqual([]);
  guard.assertNoExternalAttempts();
  await expectNoOverflow(page, 1536);

  await page.setViewportSize({ width: 426, height: 923 });
  const lowerField = form
    .getByRole("textbox", { name: /^description/iu })
    .last();
  await lowerField.focus();
  await lowerField.scrollIntoViewIfNeeded();
  const fieldBox = await lowerField.boundingBox();
  const actionBox = await form
    .locator(".demo-scenario-form__actions")
    .boundingBox();
  expect(fieldBox).not.toBeNull();
  expect(actionBox).not.toBeNull();
  if (fieldBox === null || actionBox === null)
    throw new Error("DEMO-05 requires measurable field and action bounds");
  expect(fieldBox.y + fieldBox.height).toBeLessThanOrEqual(actionBox.y);
  await expectNoOverflow(page, 426);
});

test("DEMO-05 fails closed when advisory zero-action contract drifts", async ({
  page,
}) => {
  const guard = await installPlaywrightNetworkGuard(page);
  const observation = await capturePost(page);
  await page.route(`${WEB_ORIGIN}/api/v1/demo-scenarios`, async (route) => {
    const response = await route.fetch();
    const body = (await response.json()) as {
      items: Record<string, unknown>[];
    };
    await route.fulfill({
      response,
      json: {
        items: body.items.map((item) =>
          item.id === SCENARIO_ID
            ? {
                ...item,
                expected: {
                  ...(item.expected as Record<string, unknown>),
                  externalActions: 1,
                },
              }
            : item,
        ),
      },
    });
  });
  await page.goto("/demos", { waitUntil: "networkidle" });
  await expect(
    page.getByRole("button", { name: /Partnership application review/iu }),
  ).toHaveCount(0);
  expect(observation.posts).toEqual([]);
  expect(guard.blocked).toEqual([]);
  guard.assertNoExternalAttempts();
});

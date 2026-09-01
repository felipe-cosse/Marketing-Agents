// DEMO-04 browser evidence exercises the production build and live discovery contract.
import { expect, test, type Page, type Route } from "@playwright/test";

import { installPlaywrightNetworkGuard } from "../../../scripts/browser-network-policy.mjs";

type JsonObject = Record<string, unknown>;

const SCENARIO_ID = "demo.community.reminder-draft.v1";
const TEMPLATE_ID = "tpl.community.events.live-session-reminder";
const INSTANCE_ID = "inst.community.events.live-session-reminder.01";
const RUN_ID = "run.demo-04.browser";
const WEB_ORIGIN = "http://127.0.0.1:4173";
const PRESET = {
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

interface Observation {
  readonly posts: { readonly body: JsonObject; readonly path: string }[];
}

async function installBoundaries(page: Page): Promise<Observation> {
  const observation: Observation = { posts: [] };
  await page.route(
    `${WEB_ORIGIN}/api/v1/demo-scenarios/*/runs`,
    async (route: Route) => {
      const value: unknown = route.request().postDataJSON();
      if (typeof value !== "object" || value === null || Array.isArray(value)) {
        throw new Error("DEMO-04 expected one JSON request object");
      }
      observation.posts.push({
        body: value as JsonObject,
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
          eventId: `manual-event-hmac-sha256-v1:${"f".repeat(64)}`,
          workId: "work.demo-04.browser",
          runId: RUN_ID,
          executionMode: "dry_run",
          instanceUrl: `/api/v1/agent-instances/${INSTANCE_ID}`,
          runUrl: `/api/v1/runs/${RUN_ID}`,
          timelineUrl: `/api/v1/runs/${RUN_ID}/timeline`,
          artifactsUrl: `/api/v1/runs/${RUN_ID}/artifacts`,
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
        bodyClient: document.body.clientWidth,
        bodyScroll: document.body.scrollWidth,
      })),
    )
    .toEqual({
      client: width,
      scroll: width,
      bodyClient: width,
      bodyScroll: width,
    });
}

test("DEMO-04 creates an inert reminder draft with UTC provenance and no external request", async ({
  page,
}, testInfo) => {
  const networkGuard = await installPlaywrightNetworkGuard(page);
  const observation = await installBoundaries(page);
  await page.goto("/demos", { waitUntil: "networkidle" });
  await page
    .getByRole("button", { name: /Community reminder draft/iu })
    .click();

  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "Event signup to reminder draft",
    }),
  ).toBeVisible();
  await expect(
    page.getByText("Recommended UTC time", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Not sent · not scheduled", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("No calendar or enrollment", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(TEMPLATE_ID, { exact: true })).toBeVisible();
  await expect(page.getByText(INSTANCE_ID, { exact: true })).toBeVisible();

  const form = page.getByRole("form", {
    name: "Community reminder draft preset",
  });
  await expect(
    form.getByRole("textbox", { name: /^session timezone/iu }),
  ).toHaveValue("America/Los_Angeles");
  await expect(
    form.getByRole("spinbutton", { name: /^reminder offset minutes/iu }),
  ).toHaveValue("1440");
  const attendee = form.getByRole("textbox", {
    name: /^attendee display name/iu,
  });
  await expect(attendee).toHaveAttribute("autocomplete", "off");
  await expect(attendee).toHaveAttribute("spellcheck", "false");
  await expect(
    form.getByRole("button", { name: /send|schedule|calendar|enroll/iu }),
  ).toHaveCount(0);

  await form.getByRole("button", { name: "Create reminder draft" }).click();
  await expect.poll(() => observation.posts.length).toBe(1);
  await expect(
    page.getByRole("heading", { name: "Reminder draft run accepted" }),
  ).toBeVisible();
  await expect(
    page.getByText(/scheduled_reminder_draft artifact/iu),
  ).toBeVisible();
  expect(observation.posts[0]).toEqual({
    path: `/api/v1/demo-scenarios/${SCENARIO_ID}/runs`,
    body: { overrides: PRESET },
  });
  expect(networkGuard.blocked).toEqual([]);
  networkGuard.assertNoExternalAttempts();
  await expectNoOverflow(page, 1_536);
  await page.screenshot({
    path: testInfo.outputPath("demo-04-community-1536x1024.png"),
  });

  await page.setViewportSize({ width: 426, height: 923 });
  await expect(form).toBeVisible();
  const lowerRequiredField = form.getByRole("textbox", {
    name: /^signup event id/iu,
  });
  await lowerRequiredField.focus();
  await expect(lowerRequiredField).toBeFocused();
  await lowerRequiredField.scrollIntoViewIfNeeded();
  const fieldBox = await lowerRequiredField.boundingBox();
  const actionBox = await form
    .locator(".demo-scenario-form__actions")
    .boundingBox();
  expect(fieldBox).not.toBeNull();
  expect(actionBox).not.toBeNull();
  if (fieldBox === null || actionBox === null) {
    throw new Error(
      "DEMO-04 requires measurable focused-field and action-bar bounds",
    );
  }
  expect(fieldBox.y + fieldBox.height).toBeLessThanOrEqual(actionBox.y);
  await expectNoOverflow(page, 426);
  await page.screenshot({
    path: testInfo.outputPath("demo-04-community-426x923.png"),
  });
});

test("DEMO-04 fails closed when the zero-connector boundary drifts", async ({
  page,
}) => {
  const networkGuard = await installPlaywrightNetworkGuard(page);
  const observation = await installBoundaries(page);
  await page.route(`${WEB_ORIGIN}/api/v1/demo-scenarios`, async (route) => {
    const response = await route.fetch();
    const body = (await response.json()) as { items: JsonObject[] };
    await route.fulfill({
      response,
      json: {
        items: body.items.map((item) =>
          item.id === SCENARIO_ID
            ? {
                ...item,
                expected: {
                  ...(item.expected as JsonObject),
                  connectorCalls: 1,
                },
              }
            : item,
        ),
      },
    });
  });
  await page.goto("/demos", { waitUntil: "networkidle" });
  await expect(
    page.getByRole("button", { name: /Community reminder draft/iu }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Create draft" }),
  ).toBeVisible();
  expect(observation.posts).toEqual([]);
  expect(networkGuard.blocked).toEqual([]);
  networkGuard.assertNoExternalAttempts();
});

// WEB-04 browser evidence uses the production Vite preview and real hierarchy, detail, and session APIs.
import {
  expect,
  test,
  type Page,
  type Request,
  type Route,
  type TestInfo,
} from "@playwright/test";

interface DryRunObservation {
  readonly posts: {
    readonly body: Record<string, unknown>;
    readonly headers: Record<string, string>;
  }[];
  readonly externalRequests: string[];
  detailRequests: number;
  statusRequests: number;
}

const INSTANCE_ID = "inst.email.newsletter.newsletter-subscriber.01";
const CORRELATION_ID = `correlation.api.${"4".repeat(32)}`;
const RUN_ID = "run.web-04.browser";

function problem(): Record<string, unknown> {
  return {
    type: "urn:marketing-agents:problem:dry_run_input_invalid",
    title: "Dry-run input invalid",
    status: 422,
    detail: "One or more input fields were rejected.",
    instance: `urn:marketing-agents:request:${CORRELATION_ID}`,
    code: "dry_run_input_invalid",
    correlation_id: CORRELATION_ID,
    field_errors: [
      {
        pointer: "/input/source_content",
        code: "source_content_rejected",
        message: "The submitted source contained a private canary.",
      },
      {
        pointer: "/request",
        code: "request_rejected",
        message: "This request-level error must remain in the summary.",
      },
    ],
  };
}

function receipt(): Record<string, unknown> {
  return {
    status: "accepted",
    disposition: "created",
    eventId: `manual-event-hmac-sha256-v1:${"5".repeat(64)}`,
    workId: "work.web-04.browser",
    runId: RUN_ID,
    executionMode: "dry_run",
    instanceUrl: `/api/v1/agent-instances/${INSTANCE_ID}`,
    runUrl: `/api/v1/runs/${RUN_ID}`,
  };
}

function requestBody(route: Route): Record<string, unknown> {
  const value: unknown = route.request().postDataJSON();
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("WEB-04 expected a JSON object request body");
  }
  return value as Record<string, unknown>;
}

async function installDryRunBoundary(page: Page): Promise<DryRunObservation> {
  const observation: DryRunObservation = {
    posts: [],
    externalRequests: [],
    detailRequests: 0,
    statusRequests: 0,
  };

  page.on("request", (request: Request) => {
    const url = new URL(request.url());
    if (!new Set(["127.0.0.1", "localhost"]).has(url.hostname)) {
      observation.externalRequests.push(request.url());
    }
    if (url.pathname === `/api/v1/agent-instances/${INSTANCE_ID}`) {
      observation.detailRequests += 1;
    }
    if (url.pathname === "/api/v1/agent-instances/status-summary") {
      observation.statusRequests += 1;
    }
  });

  await page.route("**/api/v1/agent-instances/*/dry-runs", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    expect(pathname).toBe(`/api/v1/agent-instances/${INSTANCE_ID}/dry-runs`);
    observation.posts.push({
      body: requestBody(route),
      headers: request.headers(),
    });
    if (observation.posts.length === 1) {
      await route.fulfill({
        status: 422,
        contentType: "application/problem+json",
        headers: { "X-Correlation-ID": CORRELATION_ID },
        body: JSON.stringify(problem()),
      });
      return;
    }
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify(receipt()),
    });
  });
  return observation;
}

async function waitForCatalog(page: Page): Promise<void> {
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(43);
}

async function capture(
  page: Page,
  testInfo: TestInfo,
  name: string,
): Promise<void> {
  await page.screenshot({ path: testInfo.outputPath(name) });
}

test("WEB-04 schema form preserves input, maps server errors, and creates one strict dry run", async ({
  page,
}, testInfo) => {
  const observation = await installDryRunBoundary(page);
  await page.goto("/");
  await waitForCatalog(page);
  await page.locator(`[data-instance-id="${INSTANCE_ID}"]`).click();

  const inspector = page.locator("#agent-inspector");
  const form = inspector.getByRole("form", { name: "Manual dry-run input" });
  await expect(form).toBeVisible();
  await expect(
    inspector.getByText("Each external action still requires", {
      exact: false,
    }),
  ).toBeVisible();
  await expect(
    form.getByRole("button", { name: "Create dry run" }),
  ).toBeVisible();
  await form.getByLabel("Mock execution", { exact: false }).check();
  await expect(
    form.getByRole("button", { name: "Run with mocks" }),
  ).toBeVisible();
  await form.getByLabel("Dry run", { exact: false }).check();

  await form.getByRole("button", { name: "Create dry run" }).click();
  expect(observation.posts).toHaveLength(0);
  const clientSummary = form.locator(".schema-form__error-summary");
  await expect(clientSummary).toBeFocused();
  await clientSummary.getByRole("button").first().click();
  await expect(form.getByLabel(/^Request id/u)).toBeFocused();

  const requestId = form.getByLabel(/^Request id/u);
  const sourceContent = form.getByLabel(/^Source content/u);
  await requestId.fill("web-04-browser");
  await sourceContent.fill("WEB-04 private browser content");

  await inspector.locator(".agent-inspector__close").click();
  const discardDialog = page.getByRole("alertdialog", {
    name: "Discard dry-run input?",
  });
  await expect(discardDialog).toBeVisible();
  await discardDialog.getByRole("button", { name: "Keep editing" }).click();
  await expect(requestId).toHaveValue("web-04-browser");
  await expect(sourceContent).toHaveValue("WEB-04 private browser content");

  await form.getByRole("button", { name: "Create dry run" }).click();
  await expect.poll(() => observation.posts.length).toBe(1);
  await expect(form.locator(".schema-form__error-summary")).toBeFocused();
  await expect(
    form
      .locator(".schema-form__field-error")
      .getByText("The server rejected this field.", { exact: true }),
  ).toBeVisible();
  await expect(requestId).toHaveValue("web-04-browser");
  await expect(sourceContent).toHaveValue("WEB-04 private browser content");

  const detailBaseline = observation.detailRequests;
  const statusBaseline = observation.statusRequests;
  await form.getByRole("button", { name: "Create dry run" }).click();
  await expect.poll(() => observation.posts.length).toBe(2);
  await expect(
    inspector.getByRole("heading", { name: "Dry run accepted" }),
  ).toBeVisible();
  await expect(inspector.getByText(RUN_ID, { exact: true })).toBeVisible();
  await expect(requestId).toHaveValue("");
  await expect(sourceContent).toHaveValue("");
  await expect
    .poll(() => observation.detailRequests)
    .toBeGreaterThan(detailBaseline);
  await expect
    .poll(() => observation.statusRequests)
    .toBeGreaterThan(statusBaseline);

  const [first, second] = observation.posts;
  expect(first?.body).toEqual({
    input: {
      request_id: "web-04-browser",
      source_content: "WEB-04 private browser content",
    },
    executionMode: "dry_run",
  });
  expect(second?.body).toEqual(first?.body);
  expect(first?.headers.accept).toBe("application/json");
  expect(first?.headers["content-type"]).toBe("application/json");
  expect(first?.headers["idempotency-key"]).toMatch(/^[\x21-\x7e]{8,240}$/u);
  expect(second?.headers["idempotency-key"]).toBe(
    first?.headers["idempotency-key"],
  );
  expect(first?.headers["x-csrf-token"]).toMatch(/^[A-Za-z0-9_-]{32,128}$/u);

  const persistedBrowserState = await page.evaluate(() =>
    JSON.stringify({
      localStorage: Object.keys(localStorage).map((key) => [
        key,
        localStorage.getItem(key),
      ]),
      sessionStorage: Object.keys(sessionStorage).map((key) => [
        key,
        sessionStorage.getItem(key),
      ]),
      cookie: document.cookie,
      location: location.href,
    }),
  );
  expect(persistedBrowserState).not.toContain("WEB-04 private browser content");
  expect(observation.externalRequests).toEqual([]);

  await inspector
    .getByRole("heading", { name: "Manual dry run" })
    .scrollIntoViewIfNeeded();
  const wideInspector = await inspector.boundingBox();
  expect(wideInspector?.width).toBeGreaterThanOrEqual(329);
  expect(wideInspector?.width).toBeLessThanOrEqual(333);
  expect(
    await form.locator(".schema-form__actions").evaluate((element) => {
      const bounds = element.getBoundingClientRect();
      return bounds.top < innerHeight && bounds.bottom > 0;
    }),
  ).toBe(true);
  await capture(page, testInfo, "web-04-dry-run-1536x1024.png");

  await page.setViewportSize({ width: 1280, height: 800 });
  await expect(inspector).toBeVisible();
  const compactInspector = await inspector.boundingBox();
  expect(compactInspector?.width).toBeGreaterThanOrEqual(358);
  expect(compactInspector?.width).toBeLessThanOrEqual(362);
  expect(
    await form.locator(".schema-form__actions").evaluate((element) => {
      const bounds = element.getBoundingClientRect();
      return bounds.top < innerHeight && bounds.bottom > 0;
    }),
  ).toBe(true);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await capture(page, testInfo, "web-04-dry-run-1280x800.png");
});

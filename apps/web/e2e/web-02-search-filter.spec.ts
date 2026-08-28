// WEB-02 browser evidence uses the production Vite preview and real hierarchy API.
import {
  expect,
  test,
  type Page,
  type Request,
  type TestInfo,
} from "@playwright/test";

interface CapabilityBody {
  readonly id: string;
  readonly displayName: string;
}

interface InstanceBody {
  readonly id: string;
  readonly templateId: string;
  readonly displayName: string;
  readonly purpose: string;
  readonly enabled: boolean;
  readonly capabilitySummaries: readonly CapabilityBody[];
}

interface FunctionBody {
  readonly id: string;
  readonly instances: readonly InstanceBody[];
}

interface DepartmentBody {
  readonly id: string;
  readonly functions: readonly FunctionBody[];
}

interface HierarchyBody {
  readonly departments: readonly DepartmentBody[];
}

interface BoundaryObservation {
  hierarchyRequestCount: number;
  statusRequestHeaders: (string | null)[];
  readonly externalRequests: string[];
  hierarchyBody: HierarchyBody | null;
}

const STATUS_WATERMARK = `instance-status-sha256-v1:${"c".repeat(64)}`;
const STATUS_ETAG = `"${STATUS_WATERMARK}"`;

function instanceIds(body: HierarchyBody): readonly string[] {
  return body.departments.flatMap((department) =>
    department.functions.flatMap((agentFunction) =>
      agentFunction.instances.map((instance) => instance.id),
    ),
  );
}

function stringAt(values: readonly string[], index: number): string {
  const value = values[index];
  if (value === undefined) throw new Error("expected source ID is missing");
  return value;
}

function runtimeItem(
  instanceId: string,
  index: number,
): Record<string, unknown> {
  const status =
    index === 0
      ? "completed"
      : index === 1
        ? "failed"
        : index === 2
          ? "executing"
          : "never_run";
  const neverRun = status === "never_run";
  const runId = neverRun
    ? null
    : `run.web-02.${String(index + 1).padStart(2, "0")}`;
  return {
    instance_id: instanceId,
    status,
    latest_run_id: runId,
    latest_run_state: neverRun ? null : status,
    latest_run_created_at: neverRun ? null : "2026-08-28T18:00:00Z",
    latest_run_updated_at: neverRun ? null : "2026-08-28T18:01:00Z",
    instance_url: `/api/v1/agent-instances/${encodeURIComponent(instanceId)}`,
    latest_run_url:
      runId === null ? null : `/api/v1/runs/${encodeURIComponent(runId)}`,
  };
}

async function installDeterministicBoundary(
  page: Page,
): Promise<BoundaryObservation> {
  const observation: BoundaryObservation = {
    hierarchyRequestCount: 0,
    statusRequestHeaders: [],
    externalRequests: [],
    hierarchyBody: null,
  };
  let resolveHierarchy: ((body: HierarchyBody) => void) | undefined;
  const hierarchyReady = new Promise<HierarchyBody>((resolve) => {
    resolveHierarchy = resolve;
  });

  page.on("request", (request: Request) => {
    const url = new URL(request.url());
    if (!new Set(["127.0.0.1", "localhost"]).has(url.hostname)) {
      observation.externalRequests.push(request.url());
    }
    if (url.pathname === "/api/v1/catalog/hierarchy") {
      observation.hierarchyRequestCount += 1;
    }
  });
  page.on("response", async (response) => {
    if (new URL(response.url()).pathname !== "/api/v1/catalog/hierarchy")
      return;
    const body = (await response.json()) as HierarchyBody;
    observation.hierarchyBody = body;
    resolveHierarchy?.(body);
    resolveHierarchy = undefined;
  });

  await page.route(
    "**/api/v1/agent-instances/status-summary",
    async (route) => {
      const body = await hierarchyReady;
      const conditional = route.request().headers()["if-none-match"] ?? null;
      observation.statusRequestHeaders.push(conditional);
      const headers = {
        "Cache-Control": "private, no-cache, max-age=0",
        ETag: STATUS_ETAG,
        Vary: "Authorization",
        "X-Content-Type-Options": "nosniff",
      };
      if (conditional === STATUS_ETAG) {
        await route.fulfill({ status: 304, headers });
        return;
      }
      const ids = instanceIds(body);
      expect(ids).toHaveLength(43);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers,
        body: JSON.stringify({
          scope: "single-local-installation",
          runtime_watermark: STATUS_WATERMARK,
          items: ids.map(runtimeItem),
        }),
      });
    },
  );
  return observation;
}

async function waitForCatalog(page: Page): Promise<void> {
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(43);
  await expect(page.locator(".catalog-result-count")).toHaveText("43 agents");
}

function requireHierarchy(observation: BoundaryObservation): HierarchyBody {
  if (observation.hierarchyBody === null)
    throw new Error("hierarchy was not observed");
  return observation.hierarchyBody;
}

async function expectVisibleInstanceIds(
  page: Page,
  expectedIds: readonly string[],
): Promise<void> {
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(
    expectedIds.length,
  );
  expect(
    await page
      .locator('[data-node-kind="instance"]')
      .evaluateAll((nodes) =>
        nodes.map((node) => node.getAttribute("data-instance-id")),
      ),
  ).toEqual(expectedIds);
}

async function openFilters(page: Page): Promise<void> {
  const trigger = page.getByRole("button", { name: /^Filters/u });
  if ((await trigger.getAttribute("aria-expanded")) !== "true")
    await trigger.click();
  await expect(
    page.getByRole("dialog", { name: "Catalog filters" }),
  ).toBeVisible();
}

async function expectSceneInsideViewport(page: Page): Promise<void> {
  const viewport = page.getByTestId("org-chart-viewport");
  const viewportBox = await viewport.boundingBox();
  expect(viewportBox).not.toBeNull();
  if (viewportBox === null) return;
  const nodes = await page
    .locator(
      '[data-node-kind="root"], .department-header, .function-header, [data-node-kind="instance"]',
    )
    .evaluateAll((elements) =>
      elements.map((element) => {
        const box = element.getBoundingClientRect();
        return {
          left: box.left,
          top: box.top,
          right: box.right,
          bottom: box.bottom,
        };
      }),
    );
  expect(nodes).toHaveLength(61);
  for (const box of nodes) {
    expect(box.left).toBeGreaterThanOrEqual(viewportBox.x - 1);
    expect(box.top).toBeGreaterThanOrEqual(viewportBox.y - 1);
    expect(box.right).toBeLessThanOrEqual(
      viewportBox.x + viewportBox.width + 1,
    );
    expect(box.bottom).toBeLessThanOrEqual(
      viewportBox.y + viewportBox.height + 1,
    );
  }
}

async function screenshot(
  page: Page,
  testInfo: TestInfo,
  name: string,
): Promise<void> {
  await page.screenshot({ path: testInfo.outputPath(name), fullPage: true });
}

test("WEB-02 canonical deep links and presentation-safe search preserve source order", async ({
  page,
}, testInfo) => {
  const observation = await installDeterministicBoundary(page);
  const rawQuery = [
    "capability=cap.model.generate-structured",
    "run=completed",
    "deployment=enabled",
    "function=func.social-media.new-content",
    "department=dept.social-media",
    `q=${encodeURIComponent("ＬｉｎｋｅｄＩｎ")}`,
  ].join("&");
  await page.goto(`/?${rawQuery}`);
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(1);
  await expect(page).toHaveURL(
    /\?q=LinkedIn&department=dept\.social-media&function=func\.social-media\.new-content&deployment=enabled&run=completed&capability=cap\.model\.generate-structured$/u,
  );
  const sourceIds = instanceIds(requireHierarchy(observation));
  await expectVisibleInstanceIds(page, [stringAt(sourceIds, 0)]);
  await expect(page.locator('[data-node-kind="department"]')).toHaveCount(1);
  await expect(page.locator('[data-node-kind="function"]')).toHaveCount(1);
  await screenshot(page, testInfo, "web-02-canonical-deep-link.png");

  await page.reload();
  await expectVisibleInstanceIds(page, [stringAt(sourceIds, 0)]);
  await page.getByRole("button", { name: "Clear all" }).last().click();
  await waitForCatalog(page);
  const search = page.getByRole("searchbox", { name: "Search agents" });
  const searches = [
    ["Newsletter Subscriber", 1],
    ["configured newsletter system", 1],
    ["inst.email.newsletter.newsletter-subscriber.01", 1],
    ["tpl.community.events.attendee-scheduler", 2],
    ["Newsletter: Subscribe", 1],
    ["Loops", 0],
  ] as const;
  for (const [query, count] of searches) {
    await search.fill(query);
    await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(
      count,
    );
  }
  await expect(page.locator("body")).not.toContainText("Loops");
  expect(observation.externalRequests).toEqual([]);
});

test("WEB-02 filters compose with AND semantics, clear empty results, and restore focus safely", async ({
  page,
}) => {
  const observation = await installDeterministicBoundary(page);
  await page.goto("/");
  await waitForCatalog(page);
  await openFilters(page);

  await page
    .getByRole("combobox", { name: "Department" })
    .selectOption("dept.email");
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(5);
  await page
    .getByRole("combobox", { name: "Function" })
    .selectOption("func.email.newsletter");
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(2);
  await page
    .getByRole("combobox", { name: "Deployment state" })
    .selectOption("enabled");
  await page
    .getByRole("combobox", { name: "Recent run state" })
    .selectOption("never_run");
  await page
    .getByRole("combobox", { name: "Capability" })
    .selectOption("cap.newsletter.subscribe");
  await expectVisibleInstanceIds(page, [
    "inst.email.newsletter.newsletter-subscriber.01",
  ]);
  await expect(page.locator('[data-node-kind="department"]')).toHaveAttribute(
    "data-department-id",
    "dept.email",
  );
  await expect(page.locator('[data-node-kind="function"]')).toHaveAttribute(
    "data-function-id",
    "func.email.newsletter",
  );
  await expect(page.locator(".catalog-result-count")).toHaveText(
    "1 of 43 agents",
  );

  await page
    .getByRole("combobox", { name: "Deployment state" })
    .selectOption("disabled");
  await expect(
    page.getByText("No agents match", { exact: true }).first(),
  ).toBeVisible();
  await page.getByRole("button", { name: "Clear search and filters" }).click();
  await waitForCatalog(page);
  await expect(page).toHaveURL(/\/$/u);

  const firstCard = page.locator('[data-node-kind="instance"]').first();
  await firstCard.click();
  await expect(firstCard).toHaveAttribute("aria-pressed", "true");
  await page.evaluate(() => {
    const input = document.querySelector<HTMLInputElement>(
      'input[type="search"]',
    );
    const descriptor = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    );
    if (input === null || descriptor?.set === undefined)
      throw new Error("search input unavailable");
    // The native setter is intentionally rebound so React observes input without moving focus.
    // eslint-disable-next-line @typescript-eslint/unbound-method
    Reflect.apply(descriptor.set, input, ["LinkedIn Comment Replier"]);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(1);
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          (document.activeElement as HTMLElement | null)?.dataset.nodeId ??
          null,
      ),
    )
    .toBe("func.social-media.new-content");
  expect(observation.externalRequests).toEqual([]);
});

test("WEB-02 history and conditional polling never refetch the hierarchy", async ({
  page,
}) => {
  const observation = await installDeterministicBoundary(page);
  await page.goto("/");
  await waitForCatalog(page);
  await openFilters(page);
  await page
    .getByRole("combobox", { name: "Department" })
    .selectOption("dept.email");
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(5);
  await page
    .getByRole("combobox", { name: "Function" })
    .selectOption("func.email.newsletter");
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(2);

  await page.goBack();
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(5);
  await expect(page).toHaveURL(/department=dept\.email$/u);
  await page.goForward();
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(2);
  await expect(page).toHaveURL(
    /department=dept\.email&function=func\.email\.newsletter$/u,
  );

  await expect
    .poll(() => observation.statusRequestHeaders.length, { timeout: 8_000 })
    .toBeGreaterThanOrEqual(2);
  expect(observation.statusRequestHeaders[0]).toBeNull();
  expect(observation.statusRequestHeaders[1]).toBe(STATUS_ETAG);
  expect(observation.hierarchyRequestCount).toBe(1);
  expect(observation.externalRequests).toEqual([]);
});

test("WEB-02 full hierarchy remains fitted at wide and compact desktop sizes", async ({
  page,
}, testInfo) => {
  const observation = await installDeterministicBoundary(page);
  await page.goto("/");
  await waitForCatalog(page);
  await expectSceneInsideViewport(page);
  await openFilters(page);
  await page
    .getByRole("combobox", { name: "Department" })
    .selectOption("dept.email");
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(5);
  await page.setViewportSize({ width: 1280, height: 800 });
  await expect(page.locator(".catalog-active-summary")).toBeVisible();
  await expect(page.locator(".catalog-active-summary")).toHaveText("1 active");
  await expect(page.locator(".catalog-active-filters")).toBeHidden();
  const [filterBox, searchBox, resultBox, controlsBox, panelBox] =
    await Promise.all([
      page.locator(".catalog-filter-trigger").boundingBox(),
      page.locator(".catalog-search").boundingBox(),
      page.locator(".catalog-result-count").boundingBox(),
      page.locator(".canvas-controls").boundingBox(),
      page.locator(".catalog-filter-panel").boundingBox(),
    ]);
  expect(filterBox).not.toBeNull();
  expect(searchBox).not.toBeNull();
  expect(resultBox).not.toBeNull();
  expect(controlsBox).not.toBeNull();
  expect(panelBox).not.toBeNull();
  if (
    filterBox !== null &&
    searchBox !== null &&
    resultBox !== null &&
    controlsBox !== null &&
    panelBox !== null
  ) {
    expect(filterBox.x + filterBox.width).toBeLessThanOrEqual(searchBox.x);
    expect(searchBox.x + searchBox.width).toBeLessThanOrEqual(resultBox.x);
    expect(resultBox.x + resultBox.width).toBeLessThanOrEqual(controlsBox.x);
    expect(panelBox.x).toBeGreaterThanOrEqual(0);
    expect(panelBox.y).toBeGreaterThanOrEqual(0);
    expect(panelBox.x + panelBox.width).toBeLessThanOrEqual(1280);
    expect(panelBox.y + panelBox.height).toBeLessThanOrEqual(800);
  }
  expect(
    await page.evaluate(() => document.body.scrollWidth <= window.innerWidth),
  ).toBe(true);
  await screenshot(page, testInfo, "web-02-compact-filter-panel.png");
  await page
    .getByRole("dialog", { name: "Catalog filters" })
    .getByRole("button", { name: "Clear all" })
    .click();
  await waitForCatalog(page);
  await page.getByRole("button", { name: "Fit hierarchy" }).click();
  await expectSceneInsideViewport(page);
  await screenshot(page, testInfo, "web-02-compact-filter-toolbar.png");
  expect(observation.hierarchyRequestCount).toBe(1);
  expect(observation.externalRequests).toEqual([]);
});

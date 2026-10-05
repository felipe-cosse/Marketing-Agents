// WEB-07 browser evidence exercises the production responsive hierarchy against the real local catalog API.
// AC-04 covers every Community deployment and complete source hierarchy on desktop/mobile.
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  expect,
  test,
  type Locator,
  type Page,
  type Response,
} from "./fixtures";

interface HierarchyInstance {
  readonly id: string;
  readonly templateId: string;
  readonly sourceOrdinal: number;
}

interface HierarchyFunction {
  readonly id: string;
  readonly instances: readonly HierarchyInstance[];
}

interface HierarchyDepartment {
  readonly id: string;
  readonly functions: readonly HierarchyFunction[];
}

interface HierarchyBody {
  readonly counts: {
    readonly departments: number;
    readonly functions: number;
    readonly templates: number;
    readonly instances: number;
  };
  readonly departments: readonly HierarchyDepartment[];
}

const MOBILE_VIEWPORT = { width: 426, height: 923 } as const;
const LOCAL_HOSTS = new Set(["127.0.0.1", "localhost"]);

function first<T>(values: readonly T[], label: string): T {
  const value = values[0];
  if (value === undefined) throw new Error(`Missing ${label}`);
  return value;
}

function treeItem(page: Page, nodeId: string): Locator {
  return page.locator(`[role="treeitem"][data-node-id="${nodeId}"]`);
}

async function expectTouchTarget(target: Locator): Promise<void> {
  const box = await target.boundingBox();
  expect(box).not.toBeNull();
  if (box === null) return;
  expect(box.width).toBeGreaterThanOrEqual(44);
  expect(box.height).toBeGreaterThanOrEqual(44);
}

async function expectNoPageOverflow(
  page: Page,
  expectedWidth: number = MOBILE_VIEWPORT.width,
): Promise<void> {
  const metrics = await page.evaluate(
    (viewportWidth) => ({
      documentClientWidth: document.documentElement.clientWidth,
      documentScrollWidth: document.documentElement.scrollWidth,
      bodyClientWidth: document.body.clientWidth,
      bodyScrollWidth: document.body.scrollWidth,
      offenders: [...document.querySelectorAll<HTMLElement>("body *")]
        .filter((element) => element.closest(".org-chart-viewport") === null)
        .map((element) => {
          const rect = element.getBoundingClientRect();
          return {
            tag: element.tagName.toLocaleLowerCase("en-US"),
            className:
              typeof element.className === "string" ? element.className : "",
            ariaLabel: element.getAttribute("aria-label"),
            left: Math.round(rect.left),
            right: Math.round(rect.right),
          };
        })
        .filter(({ left, right }) => left < -1 || right > viewportWidth + 1)
        .slice(0, 12),
    }),
    expectedWidth,
  );
  expect(
    {
      documentClientWidth: metrics.documentClientWidth,
      documentScrollWidth: metrics.documentScrollWidth,
      bodyClientWidth: metrics.bodyClientWidth,
      bodyScrollWidth: metrics.bodyScrollWidth,
    },
    `Page overflow diagnostics: ${JSON.stringify(metrics.offenders)}`,
  ).toEqual({
    documentClientWidth: expectedWidth,
    documentScrollWidth: expectedWidth,
    bodyClientWidth: expectedWidth,
    bodyScrollWidth: expectedWidth,
  });
}

test("WEB-07 responsive tree and graph journey", async ({ page }, testInfo) => {
  const externalRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (!LOCAL_HOSTS.has(url.hostname)) externalRequests.push(request.url());
  });

  await page.setViewportSize(MOBILE_VIEWPORT);
  const hierarchyResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/catalog/hierarchy" &&
      response.request().method() === "GET",
  );
  await page.goto("/");
  const hierarchy = (await (await hierarchyResponse).json()) as HierarchyBody;
  expect(hierarchy.counts).toEqual({
    departments: 5,
    functions: 12,
    templates: 36,
    instances: 43,
  });

  const tree = page.getByRole("tree", {
    name: "Marketing Agents organization tree",
  });
  await expect(tree).toBeVisible();
  await expect(page.getByTestId("org-chart-viewport")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Tree view" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );

  const initialIds = [
    "root",
    ...hierarchy.departments.flatMap((department) => [
      department.id,
      ...department.functions.map((agentFunction) => agentFunction.id),
    ]),
  ];
  await expect(page.getByRole("treeitem")).toHaveCount(18);
  expect(
    await page
      .getByRole("treeitem")
      .evaluateAll((nodes) =>
        nodes.map((node) => node.getAttribute("data-node-id")),
      ),
  ).toEqual(initialIds);
  await expect(treeItem(page, "root")).toHaveAttribute("aria-level", "1");
  await expect(treeItem(page, "root")).toHaveAttribute("aria-expanded", "true");
  const firstDepartment = first(hierarchy.departments, "department");
  const firstFunction = first(firstDepartment.functions, "function");
  const firstInstance = first(firstFunction.instances, "instance");
  await expect(treeItem(page, firstDepartment.id)).toHaveAttribute(
    "aria-posinset",
    "1",
  );
  await expect(treeItem(page, firstDepartment.id)).toHaveAttribute(
    "aria-setsize",
    "5",
  );
  await expect(treeItem(page, firstFunction.id)).toHaveAttribute(
    "aria-expanded",
    "false",
  );
  expect(
    await page
      .getByRole("treeitem")
      .evaluateAll(
        (nodes) =>
          nodes.filter((node) => node.getAttribute("tabindex") === "0").length,
      ),
  ).toBe(1);

  await expect(page.getByRole("link", { name: "Org chart" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Approvals/u })).toBeVisible();
  await expect(page.getByRole("link", { name: "Runs & audit" })).toBeVisible();
  await expect(page.getByText("Demos", { exact: true })).toBeVisible();
  await expect(
    page
      .getByRole("listitem")
      .filter({ hasText: "Local identity — not production authentication" }),
  ).toBeVisible();
  await expect(
    page.getByRole("searchbox", { name: "Search agents" }),
  ).toBeVisible();
  await expectNoPageOverflow(page);
  await expectTouchTarget(page.getByRole("button", { name: /^Filters/u }));
  await expectTouchTarget(page.getByRole("button", { name: "Tree view" }));
  await expectTouchTarget(treeItem(page, "root"));

  await treeItem(page, "root").focus();
  await page.keyboard.press("ArrowDown");
  await expect(treeItem(page, firstDepartment.id)).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect(treeItem(page, firstFunction.id)).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect(treeItem(page, firstFunction.id)).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  await page.keyboard.press("ArrowRight");
  await expect(treeItem(page, firstInstance.id)).toBeFocused();
  await page.keyboard.press("Enter");

  const inspector = page.locator(".agent-inspector");
  await expect(inspector).toBeVisible();
  const inspectorBox = await inspector.boundingBox();
  expect(inspectorBox).not.toBeNull();
  if (inspectorBox !== null) {
    expect(inspectorBox.x).toBe(0);
    expect(inspectorBox.y).toBe(0);
    expect(inspectorBox.width).toBe(MOBILE_VIEWPORT.width);
    expect(inspectorBox.height).toBe(MOBILE_VIEWPORT.height);
  }
  const closeInspector = page.getByRole("button", {
    name: /^Close details for /u,
  });
  await expectTouchTarget(closeInspector);
  await page.screenshot({
    path: testInfo.outputPath("web-07-mobile-detail-sheet.png"),
    fullPage: true,
  });
  await closeInspector.click();
  await expect(inspector).toHaveCount(0);
  await expect(treeItem(page, firstInstance.id)).toBeFocused();

  await treeItem(page, "root").focus();
  await page.keyboard.press("c");
  await expect(treeItem(page, "dept.community")).toBeFocused();
  await page.keyboard.press("/");
  await expect(
    page.getByRole("searchbox", { name: "Search agents" }),
  ).toBeFocused();

  await page.getByRole("button", { name: /^Filters/u }).click();
  const filterSheet = page.getByRole("dialog", { name: "Catalog filters" });
  await expect(filterSheet).toBeVisible();
  const filterBox = await filterSheet.boundingBox();
  expect(filterBox).not.toBeNull();
  if (filterBox !== null) {
    expect(filterBox.x).toBe(0);
    expect(filterBox.width).toBe(MOBILE_VIEWPORT.width);
    expect(Math.round(filterBox.y + filterBox.height)).toBe(
      MOBILE_VIEWPORT.height,
    );
  }
  await expectTouchTarget(
    filterSheet.getByRole("combobox", { name: "Department" }),
  );
  await page.keyboard.press("Escape");
  await expect(filterSheet).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^Filters/u })).toBeFocused();

  await page.getByRole("button", { name: "Graph view" }).click();
  const graph = page.getByTestId("org-chart-viewport");
  await expect(graph).toBeVisible();
  await expect(tree).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Graph view" })).toBeFocused();
  expect(
    Number(await graph.getAttribute("data-viewport-zoom")),
  ).toBeGreaterThanOrEqual(0.72);
  await expectTouchTarget(page.getByRole("button", { name: "Fit hierarchy" }));
  await expectNoPageOverflow(page);

  await page.getByRole("button", { name: "Tree view" }).click();
  await expect(tree).toBeVisible();
  await expect(graph).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Tree view" })).toBeFocused();
  await page.screenshot({
    path: testInfo.outputPath("web-07-mobile-tree.png"),
    fullPage: true,
  });

  await page.setViewportSize({ width: 800, height: 900 });
  await expect(tree).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("org-chart-viewport")).toBeVisible();
  await expect(page.getByRole("tree")).toHaveCount(0);
  await expectNoPageOverflow(page, 800);

  await page.setViewportSize(MOBILE_VIEWPORT);
  await page.getByRole("link", { name: /Approvals/u }).click();
  await expect(page).toHaveURL(/\/approvals$/u);
  await expect(
    page.getByRole("heading", { level: 1, name: "Approval queue" }),
  ).toBeVisible();
  await expect(page.locator(".approval-page__body")).toBeVisible();
  await expectTouchTarget(
    page.getByRole("combobox", { name: "Approval status" }),
  );
  await expectNoPageOverflow(page);
  expect(externalRequests).toEqual([]);
});

const AC04_COMMUNITY_TEMPLATES = [
  "tpl.community.events.attendee-scheduler",
  "tpl.community.events.live-session-reminder",
  "tpl.community.events.event-stats-tracker",
  "tpl.community.education.course-cohort-onboarder",
  "tpl.community.education.material-builder",
  "tpl.community.education.course-progress-reminders",
  "tpl.community.discussion.new-member-onboarder",
] as const;

const AC04_UNAVAILABLE_RUNTIME_URLS = [
  "http://127.0.0.1:4173/api/v1/approvals?status=pending&limit=100",
  "http://127.0.0.1:4173/api/v1/agent-instances/status-summary",
] as const;

async function expectAc04ContainedText(
  text: Locator,
  container: Locator,
): Promise<void> {
  await expect(text).toBeVisible();
  const dimensions = await text.evaluate((node) => ({
    width: node.clientWidth,
    height: node.clientHeight,
    scrollWidth: node.scrollWidth,
    scrollHeight: node.scrollHeight,
  }));
  expect(dimensions.width).toBeGreaterThan(0);
  expect(dimensions.height).toBeGreaterThan(0);
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.width);
  expect(dimensions.scrollHeight).toBeLessThanOrEqual(dimensions.height);
  const textBox = await text.boundingBox();
  const containerBox = await container.boundingBox();
  if (textBox === null || containerBox === null) {
    throw new Error("AC-04 visible text geometry missing");
  }
  expect(textBox.x).toBeGreaterThanOrEqual(containerBox.x - 1);
  expect(textBox.y).toBeGreaterThanOrEqual(containerBox.y - 1);
  expect(textBox.x + textBox.width).toBeLessThanOrEqual(
    containerBox.x + containerBox.width + 1,
  );
  expect(textBox.y + textBox.height).toBeLessThanOrEqual(
    containerBox.y + containerBox.height + 1,
  );
}

for (const [mode, viewport] of [
  ["desktop", { width: 1536, height: 1024 }],
  ["mobile", MOBILE_VIEWPORT],
] as const) {
  test(`AC-04 complete hierarchy and all seven Community pairs on ${mode}`, async ({
    page,
  }) => {
    test.setTimeout(60_000);
    const pageErrors: string[] = [];
    const consoleProblems: { type: string; text: string; url: string }[] = [];
    const failedResponses: Response[] = [];
    const mutationMethods: string[] = [];
    const detailIds: string[] = [];
    let hierarchyRequests = 0;
    page.on("pageerror", (error) => pageErrors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "warning" || message.type() === "error") {
        consoleProblems.push({
          type: message.type(),
          text: message.text(),
          url: message.location().url,
        });
      }
    });
    page.on("response", (response) => {
      if (response.status() >= 400) failedResponses.push(response);
    });
    page.on("request", (request) => {
      const path = new URL(request.url()).pathname;
      if (path === "/api/v1/catalog/hierarchy") hierarchyRequests += 1;
      if (
        path.startsWith("/api/") &&
        !["GET", "HEAD"].includes(request.method())
      ) {
        mutationMethods.push(request.method());
      }
    });

    const evidenceDirectory = await mkdtemp(
      join(tmpdir(), `marketing-agents-ac04-${mode}-`),
    );
    const screenshots: string[] = [];
    const screenshot = async (name: string): Promise<void> => {
      const path = join(evidenceDirectory, name);
      await page.screenshot({ path, fullPage: false });
      screenshots.push(path);
    };
    // Report the external directory immediately so a failed run remains inspectable.
    process.stdout.write(`AC-04 ${mode} screenshots: ${evidenceDirectory}\n`);

    await page.setViewportSize(viewport);
    const hierarchyResponsePromise = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/v1/catalog/hierarchy" &&
        response.request().method() === "GET",
    );
    await page.goto("/");
    const hierarchyResponse = await hierarchyResponsePromise;
    expect(hierarchyResponse.status()).toBe(200);
    const hierarchy = (await hierarchyResponse.json()) as HierarchyBody;
    expect(hierarchy.counts).toEqual({
      departments: 5,
      functions: 12,
      templates: 36,
      instances: 43,
    });
    await expect(page).toHaveURL("http://127.0.0.1:4173/");
    await expect(page).toHaveTitle("Organization chart | Marketing Agents");
    await expect(
      page.getByRole("heading", {
        name: "Marketing agent organization",
        exact: true,
      }),
    ).toBeVisible();
    await expect(page.locator("vite-error-overlay")).toHaveCount(0);
    await expect(
      page.getByRole("searchbox", { name: "Search agents" }),
    ).toBeVisible();

    const sourceInstances = hierarchy.departments.flatMap((department) =>
      department.functions.flatMap((agentFunction) =>
        agentFunction.instances.map((instance) => ({
          id: instance.id,
          templateId: instance.templateId,
          ordinal: String(instance.sourceOrdinal),
          departmentId: department.id,
          functionId: agentFunction.id,
        })),
      ),
    );
    expect(sourceInstances).toHaveLength(43);
    expect(new Set(sourceInstances.map(({ id }) => id)).size).toBe(43);
    expect(
      new Set(sourceInstances.map(({ templateId }) => templateId)).size,
    ).toBe(36);
    const sourceFunctionIds = hierarchy.departments.flatMap((department) =>
      department.functions.map(({ id }) => id),
    );
    expect(sourceFunctionIds).toHaveLength(12);
    const community = sourceInstances.filter(
      ({ departmentId }) => departmentId === "dept.community",
    );
    expect(community).toHaveLength(14);
    expect([...new Set(community.map(({ templateId }) => templateId))]).toEqual(
      AC04_COMMUNITY_TEMPLATES,
    );
    expect(community.map(({ id }) => id)).toEqual(
      AC04_COMMUNITY_TEMPLATES.flatMap((templateId) =>
        ["01", "02"].map(
          (ordinal) => `${templateId.replace(/^tpl\./u, "inst.")}.${ordinal}`,
        ),
      ),
    );

    const tree = page.getByRole("tree", {
      name: "Marketing Agents organization tree",
    });
    const graph = page.getByTestId("org-chart-viewport");
    if (mode === "mobile") {
      await expect(tree).toBeVisible();
      await expect(graph).toHaveCount(0);
      await expect(
        page.getByRole("button", { name: "Tree view" }),
      ).toHaveAttribute("aria-pressed", "true");
      await expect(tree.getByRole("treeitem")).toHaveCount(18);
      await screenshot("initial-mobile-tree.png");
      for (const functionId of sourceFunctionIds) {
        const branch = treeItem(page, functionId);
        await expect(branch).toHaveAttribute("aria-expanded", "false");
        await branch.click();
        await expect(branch).toHaveAttribute("aria-expanded", "true");
      }
      await expect(tree.getByRole("treeitem")).toHaveCount(61);
    } else {
      await expect(graph).toBeVisible();
      await expect(tree).toHaveCount(0);
      const initialZoom = await graph.getAttribute("data-viewport-zoom");
      if (initialZoom === null) throw new Error("AC-04 graph zoom missing");
      await page.getByRole("button", { name: "Zoom in" }).click();
      await expect(graph).not.toHaveAttribute(
        "data-viewport-zoom",
        initialZoom,
      );
      await page.getByRole("button", { name: "Fit hierarchy" }).click();
      await expect(graph).toHaveAttribute("data-viewport-zoom", initialZoom);
    }

    await expect(page.locator('[data-node-kind="root"]')).toHaveCount(1);
    await expect(page.locator('[data-node-kind="department"]')).toHaveCount(5);
    await expect(page.locator('[data-node-kind="function"]')).toHaveCount(12);
    await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(43);
    expect(
      await page
        .locator('[data-node-kind="department"]')
        .evaluateAll((nodes) =>
          nodes.map((node) => node.getAttribute("data-department-id")),
        ),
    ).toEqual(hierarchy.departments.map(({ id }) => id));
    expect(
      await page
        .locator('[data-node-kind="function"]')
        .evaluateAll((nodes) =>
          nodes.map((node) => node.getAttribute("data-function-id")),
        ),
    ).toEqual(sourceFunctionIds);
    expect(
      await page.locator('[data-node-kind="instance"]').evaluateAll((nodes) =>
        nodes.map((node) => ({
          id: node.getAttribute("data-instance-id"),
          templateId: node.getAttribute("data-template-id"),
          ordinal: node.getAttribute("data-source-ordinal"),
          departmentId:
            node.getAttribute("data-department-id") ??
            node
              .closest('[data-node-kind="department"]')
              ?.getAttribute("data-department-id"),
          functionId:
            node.getAttribute("data-function-id") ??
            node
              .closest('[data-node-kind="function"]')
              ?.getAttribute("data-function-id"),
        })),
      ),
    ).toEqual(sourceInstances);
    if (mode === "mobile") {
      expect(
        await tree
          .getByRole("treeitem")
          .evaluateAll((nodes) =>
            nodes.map((node) => node.getAttribute("data-node-id")),
          ),
      ).toEqual([
        "root",
        ...hierarchy.departments.flatMap((department) => [
          department.id,
          ...department.functions.flatMap((agentFunction) => [
            agentFunction.id,
            ...agentFunction.instances.map(({ id }) => id),
          ]),
        ]),
      ]);
    }
    const controlPlane = page.locator(
      '[data-node-kind="control-plane"][data-control-plane-id="control-plane.marketing-orchestrator"]',
    );
    await expect(controlPlane).toHaveCount(1);
    await expect(controlPlane).toHaveAttribute(
      "data-counts-as-instance",
      "false",
    );
    await expect(
      page.locator('[data-instance-id="control-plane.marketing-orchestrator"]'),
    ).toHaveCount(0);

    const communityHeading =
      mode === "mobile"
        ? treeItem(page, "dept.community")
        : page.locator(
            '[data-node-kind="department"][data-department-id="dept.community"] .department-header',
          );
    const summary = communityHeading.locator(
      mode === "mobile" ? ".org-tree-item__summary" : "small",
    );
    await expect(summary).toHaveText(
      "14 deployed instances · 7 reusable templates",
    );
    await expectAc04ContainedText(summary, communityHeading);
    if (mode === "mobile") await communityHeading.scrollIntoViewIfNeeded();
    await expectNoPageOverflow(page, viewport.width);
    await screenshot("complete-hierarchy.png");

    for (const templateId of AC04_COMMUNITY_TEMPLATES) {
      const pair = community.filter(
        (instance) => instance.templateId === templateId,
      );
      expect(pair.map(({ ordinal }) => ordinal)).toEqual(["1", "2"]);
      const renderedPair = page.locator(
        `[data-node-kind="instance"][data-template-id="${templateId}"]`,
      );
      await expect(renderedPair).toHaveCount(2);
      expect(
        await renderedPair.evaluateAll((nodes) =>
          nodes.map((node) => node.getAttribute("data-instance-id")),
        ),
      ).toEqual(pair.map(({ id }) => id));
      for (const instance of pair) {
        const card = page.locator(
          `[data-node-kind="instance"][data-instance-id="${instance.id}"]`,
        );
        const ordinal = `Instance ${instance.ordinal} of 2`;
        await expect(card).toHaveAccessibleName(new RegExp(ordinal, "u"));
        const chip =
          mode === "mobile"
            ? card.getByText(ordinal, { exact: true })
            : card.locator(".ordinal-chip");
        await expect(chip).toHaveText(ordinal);
        // Inline mobile span dimensions may be zero even when its line box is visible;
        // card bounds/touch-target semantics are checked by existing WEB-07 coverage.
        if (mode === "desktop") await expectAc04ContainedText(chip, card);
        const detailPromise = page.waitForResponse(
          (response) =>
            new URL(response.url()).pathname ===
              `/api/v1/agent-instances/${instance.id}` &&
            response.request().method() === "GET",
        );
        await card.click();
        expect((await detailPromise).status()).toBe(200);
        detailIds.push(instance.id);
        const selectionAttribute =
          mode === "mobile" ? "aria-selected" : "aria-pressed";
        await expect(card).toHaveAttribute(selectionAttribute, "true");
        await expect(
          page.locator(
            `[data-node-kind="instance"][${selectionAttribute}="true"]`,
          ),
        ).toHaveCount(1);
        const inspector = page.locator("#agent-inspector");
        await expect(inspector).toBeVisible();
        await expect(
          inspector.getByRole("heading", { level: 2 }),
        ).toContainText(ordinal);
        await expect(
          inspector
            .getByRole("region", { name: "Deployment & configuration" })
            .getByText(instance.id, { exact: true }),
        ).toBeVisible();
        await expect(
          inspector
            .getByRole("region", { name: "Template", exact: true })
            .getByText(instance.templateId, { exact: true }),
        ).toBeVisible();
        if (templateId === AC04_COMMUNITY_TEMPLATES[0]) {
          await screenshot(
            `community-instance-${instance.ordinal}-details.png`,
          );
          await inspector
            .getByRole("region", { name: "Template", exact: true })
            .scrollIntoViewIfNeeded();
          await screenshot(
            `community-instance-${instance.ordinal}-shared-template.png`,
          );
        }
        await expectNoPageOverflow(page, viewport.width);
        await inspector
          .getByRole("button", { name: /^Close details for /u })
          .click();
        await expect(inspector).toHaveCount(0);
        await expect(card).toBeFocused();
      }
    }
    expect(detailIds).toEqual(community.map(({ id }) => id));
    expect(new Set(detailIds).size).toBe(14);
    expect(hierarchyRequests).toBe(1);
    expect(mutationMethods).toEqual([]);
    await expectNoPageOverflow(page, viewport.width);
    await expect(page.locator("vite-error-overlay")).toHaveCount(0);

    // The real catalog-only server has two unavailable runtime surfaces. Do not
    // suppress errors: match every failed HTTP response and every console record.
    await expect
      .poll(() =>
        [...new Set(failedResponses.map((response) => response.url()))].sort(),
      )
      .toEqual([...AC04_UNAVAILABLE_RUNTIME_URLS].sort());
    for (const response of failedResponses) {
      expect(AC04_UNAVAILABLE_RUNTIME_URLS).toContain(response.url());
      expect(response.request().method()).toBe("GET");
      expect(response.status()).toBe(503);
      expect(response.headers()["content-type"]).toContain(
        "application/problem+json",
      );
      expect(await response.json()).toMatchObject({
        type: "urn:marketing-agents:problem:service_unavailable",
        title: "Service Unavailable",
        status: 503,
        code: "service_unavailable",
        detail: "The service is temporarily unavailable.",
      });
    }
    await expect
      .poll(() => consoleProblems.length)
      .toBe(failedResponses.length);
    expect(pageErrors).toEqual([]);
    expect(
      consoleProblems.toSorted((left, right) =>
        left.url.localeCompare(right.url),
      ),
    ).toEqual(
      failedResponses
        .map((response) => ({
          type: "error",
          text: "Failed to load resource: the server responded with a status of 503 (Service Unavailable)",
          url: response.url(),
        }))
        .sort((left, right) => left.url.localeCompare(right.url)),
    );
    process.stdout.write(
      `${JSON.stringify({
        requirement: "AC-04",
        mode,
        screenshots,
        knownUnavailableRuntimeResponses: failedResponses.length,
      })}\n`,
    );
  });
}

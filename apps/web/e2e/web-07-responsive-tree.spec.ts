// WEB-07 browser evidence exercises the production responsive hierarchy against the real local catalog API.
import { expect, test, type Locator, type Page } from "@playwright/test";

interface HierarchyInstance {
  readonly id: string;
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

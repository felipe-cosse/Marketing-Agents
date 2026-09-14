// WEB-01 browser evidence uses the real Vite proxy and local hierarchy API.
import { expect, test, type Page, type Response } from "./fixtures";

interface HierarchyResponseBody {
  readonly counts: {
    readonly departments: number;
    readonly functions: number;
    readonly templates: number;
    readonly instances: number;
  };
  readonly departments: readonly {
    readonly id: string;
    readonly displayName: string;
    readonly functions: readonly {
      readonly id: string;
      readonly instances: readonly {
        readonly id: string;
        readonly templateId: string;
        readonly sourceOrdinal: number;
      }[];
    }[];
  }[];
}

async function loadHierarchy(page: Page): Promise<HierarchyResponseBody> {
  const responsePromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/catalog/hierarchy" &&
      response.request().method() === "GET",
  );
  await page.goto("/");
  const response: Response = await responsePromise;
  expect(response.status()).toBe(200);
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(43);
  return (await response.json()) as HierarchyResponseBody;
}

async function expectCompleteHierarchyVisible(page: Page): Promise<void> {
  const viewport = page.getByTestId("org-chart-viewport");
  const viewportBox = await viewport.boundingBox();
  expect(viewportBox).not.toBeNull();
  if (viewportBox === null) return;

  const targets = [
    ["root", '[data-node-kind="root"]', 1],
    ["department header", ".department-header", 5],
    ["function header", ".function-header", 12],
    ["function group", '[data-node-kind="function"]', 12],
    ["agent card", '[data-node-kind="instance"]', 43],
  ] as const;
  const tolerance = 1;
  for (const [label, selector, expectedCount] of targets) {
    const locator = page.locator(selector);
    await expect(locator).toHaveCount(expectedCount);
    const boxes = await locator.evaluateAll((nodes) =>
      nodes.map((node) => {
        const rect = node.getBoundingClientRect();
        return {
          left: rect.left,
          top: rect.top,
          right: rect.right,
          bottom: rect.bottom,
          width: rect.width,
          height: rect.height,
        };
      }),
    );
    for (const [index, box] of boxes.entries()) {
      const description = `${label} ${String(index)}`;
      expect(box.width, `${description} has width`).toBeGreaterThan(0);
      expect(box.height, `${description} has height`).toBeGreaterThan(0);
      expect(box.left, `${description} left edge`).toBeGreaterThanOrEqual(
        viewportBox.x - tolerance,
      );
      expect(box.top, `${description} top edge`).toBeGreaterThanOrEqual(
        viewportBox.y - tolerance,
      );
      expect(box.right, `${description} right edge`).toBeLessThanOrEqual(
        viewportBox.x + viewportBox.width + tolerance,
      );
      expect(box.bottom, `${description} bottom edge`).toBeLessThanOrEqual(
        viewportBox.y + viewportBox.height + tolerance,
      );
    }
  }
}

test("ORCH-01 keeps one visible root and excludes the control plane from all 43 source instances", async ({
  page,
}) => {
  const body = await loadHierarchy(page);
  const sourceInstanceIds = body.departments.flatMap((department) =>
    department.functions.flatMap((agentFunction) =>
      agentFunction.instances.map((instance) => instance.id),
    ),
  );
  expect(sourceInstanceIds).toHaveLength(43);
  expect(sourceInstanceIds).not.toContain(
    "control-plane.marketing-orchestrator",
  );

  const desktopRoot = page.locator(
    '[data-node-kind="root"][data-hierarchy-root-id="root"]',
  );
  await expect(desktopRoot).toHaveCount(1);
  await expect(desktopRoot).toContainText("Marketing Agents");
  const desktopControlPlane = desktopRoot.locator(
    '[data-node-kind="control-plane"][data-control-plane-id="control-plane.marketing-orchestrator"]',
  );
  await expect(desktopControlPlane).toHaveCount(1);
  await expect(desktopControlPlane).toContainText("Marketing Orchestrator");
  await expect(desktopControlPlane).toContainText("Control plane");
  await expect(desktopControlPlane).toHaveAttribute(
    "data-counts-as-instance",
    "false",
  );
  expect(await desktopControlPlane.getAttribute("data-instance-id")).toBeNull();
  const desktopRootGeometry = await desktopRoot.evaluate((root) => {
    const icon = root.querySelector<HTMLElement>(".root-node__icon");
    const controlPlane = root.querySelector<HTMLElement>(
      '[data-node-kind="control-plane"]',
    );
    if (icon === null || controlPlane === null) {
      throw new Error("ORCH-01 root geometry markers are missing");
    }
    const rootRect = root.getBoundingClientRect();
    const iconRect = icon.getBoundingClientRect();
    const controlPlaneRect = controlPlane.getBoundingClientRect();
    return {
      root: {
        left: rootRect.left,
        right: rootRect.right,
        top: rootRect.top,
        bottom: rootRect.bottom,
        width: rootRect.width,
        height: rootRect.height,
      },
      icon: { width: iconRect.width, height: iconRect.height },
      controlPlane: {
        left: controlPlaneRect.left,
        right: controlPlaneRect.right,
        top: controlPlaneRect.top,
        bottom: controlPlaneRect.bottom,
      },
    };
  });
  const renderedScale = desktopRootGeometry.root.width / 148;
  expect(desktopRootGeometry.root.height / renderedScale).toBeCloseTo(38, 1);
  expect(desktopRootGeometry.icon.width / renderedScale).toBeCloseTo(25, 1);
  expect(desktopRootGeometry.icon.height / renderedScale).toBeCloseTo(25, 1);
  expect(desktopRootGeometry.controlPlane.left).toBeGreaterThanOrEqual(
    desktopRootGeometry.root.left,
  );
  expect(desktopRootGeometry.controlPlane.right).toBeLessThanOrEqual(
    desktopRootGeometry.root.right,
  );
  expect(desktopRootGeometry.controlPlane.top).toBeGreaterThanOrEqual(
    desktopRootGeometry.root.top,
  );
  expect(desktopRootGeometry.controlPlane.bottom).toBeLessThanOrEqual(
    desktopRootGeometry.root.bottom,
  );

  const desktopInstanceIds = await page
    .locator('[data-node-kind="instance"]')
    .evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("data-instance-id")),
    );
  expect(desktopInstanceIds).toEqual(sourceInstanceIds);

  await page.setViewportSize({ width: 426, height: 923 });
  const tree = page.getByRole("tree", {
    name: "Marketing Agents organization tree",
  });
  await expect(tree).toBeVisible();
  const functionItems = tree.locator(
    '[role="treeitem"][data-node-kind="function"]',
  );
  await expect(functionItems).toHaveCount(12);
  for (let index = 0; index < 12; index += 1) {
    const item = functionItems.nth(index);
    if ((await item.getAttribute("aria-expanded")) !== "true") {
      await item.click();
    }
  }

  await expect(tree.locator('[role="treeitem"]')).toHaveCount(61);
  const narrowRoot = tree.locator(
    '[role="treeitem"][data-node-kind="root"][data-hierarchy-root-id="root"]',
  );
  await expect(narrowRoot).toHaveCount(1);
  await expect(narrowRoot).toContainText("Marketing Agents");
  const narrowControlPlane = narrowRoot.locator(
    '[data-node-kind="control-plane"][data-control-plane-id="control-plane.marketing-orchestrator"]',
  );
  await expect(narrowControlPlane).toHaveCount(1);
  await expect(narrowControlPlane).toContainText("Marketing Orchestrator");
  await expect(narrowControlPlane).toContainText("Control plane");
  await expect(narrowControlPlane).toHaveAttribute(
    "data-counts-as-instance",
    "false",
  );
  expect(await narrowControlPlane.getAttribute("role")).not.toBe("treeitem");
  expect(await narrowControlPlane.getAttribute("data-instance-id")).toBeNull();

  const narrowInstanceIds = await tree
    .locator('[role="treeitem"][data-node-kind="instance"]')
    .evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("data-instance-id")),
    );
  expect(narrowInstanceIds).toEqual(sourceInstanceIds);
});

test("WEB-01 renders the complete ordered API hierarchy with no external requests", async ({
  page,
}, testInfo) => {
  const externalRequests: string[] = [];
  let hierarchyRequestCount = 0;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (!new Set(["127.0.0.1", "localhost"]).has(url.hostname)) {
      externalRequests.push(request.url());
    }
    if (url.pathname === "/api/v1/catalog/hierarchy")
      hierarchyRequestCount += 1;
  });

  const body = await loadHierarchy(page);
  expect(body.counts).toEqual({
    departments: 5,
    functions: 12,
    templates: 36,
    instances: 43,
  });
  await expect(page.locator('[data-node-kind="root"]')).toHaveCount(1);
  await expect(page.locator('[data-node-kind="department"]')).toHaveCount(5);
  await expect(page.locator('[data-node-kind="function"]')).toHaveCount(12);

  const sourceDepartmentIds = body.departments.map(
    (department) => department.id,
  );
  const sourceFunctionIds = body.departments.flatMap((department) =>
    department.functions.map((agentFunction) => agentFunction.id),
  );
  const sourceInstanceIds = body.departments.flatMap((department) =>
    department.functions.flatMap((agentFunction) =>
      agentFunction.instances.map((instance) => instance.id),
    ),
  );
  expect(
    await page
      .locator('[data-node-kind="department"]')
      .evaluateAll((nodes) =>
        nodes.map((node) => node.getAttribute("data-department-id")),
      ),
  ).toEqual(sourceDepartmentIds);
  expect(
    await page
      .locator('[data-node-kind="function"]')
      .evaluateAll((nodes) =>
        nodes.map((node) => node.getAttribute("data-function-id")),
      ),
  ).toEqual(sourceFunctionIds);
  expect(
    await page
      .locator('[data-node-kind="instance"]')
      .evaluateAll((nodes) =>
        nodes.map((node) => node.getAttribute("data-instance-id")),
      ),
  ).toEqual(sourceInstanceIds);
  expect(new Set(sourceInstanceIds).size).toBe(43);

  const departmentCounts = await page
    .locator('[data-node-kind="department"]')
    .evaluateAll((nodes) =>
      nodes.map((node) => Number(node.getAttribute("data-instance-count"))),
    );
  expect(departmentCounts).toEqual([12, 6, 5, 14, 6]);

  const community = body.departments[3];
  expect(community?.displayName).toBe("Community");
  const communityInstances = community?.functions.flatMap(
    (agentFunction) => agentFunction.instances,
  );
  expect(communityInstances).toHaveLength(14);
  expect(
    new Set(communityInstances?.map((instance) => instance.templateId)).size,
  ).toBe(7);
  for (const templateId of new Set(
    communityInstances?.map((instance) => instance.templateId),
  )) {
    expect(
      communityInstances
        ?.filter((instance) => instance.templateId === templateId)
        .map((instance) => instance.sourceOrdinal),
    ).toEqual([1, 2]);
  }

  const newsletterSubscriber = page.locator(
    '[data-template-id="tpl.email.newsletter.newsletter-subscriber"]',
  );
  await expect(newsletterSubscriber).toHaveCount(1);
  await expect(newsletterSubscriber).toContainText(
    "configured newsletter system",
  );
  await expect(newsletterSubscriber).not.toContainText("Loops");
  await expect(newsletterSubscriber).not.toHaveAttribute("title", /Loops/u);
  await expect(newsletterSubscriber).not.toHaveAttribute(
    "aria-label",
    /Loops/u,
  );
  await expect(page.locator("body")).not.toContainText("Loops");

  expect(hierarchyRequestCount).toBe(1);
  expect(externalRequests).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("web-01-desktop-fidelity.png"),
    fullPage: true,
  });
});

test("WEB-01 Vite preview strips API-09 forwarding and identity assertions", async ({
  page,
}) => {
  await page.goto("/");

  const result = await page.evaluate(async () => {
    const response = await fetch("/api/v1/catalog/hierarchy", {
      headers: {
        Forwarded: "for=203.0.113.10;proto=https",
        "X-Forwarded-For": "203.0.113.10",
        "Remote-User": "forged-remote-user",
        "X-Forwarded-User": "forged-forwarded-user",
        "X-Forwarded-Email": "forged@example.com",
        "X-Forwarded-Actor": "forged-forwarded-actor",
        "X-Forwarded-Role": "admin",
        "X-Forwarded-Roles": "admin,owner",
        "X-Forwarded-Scope": "catalog:write",
        "X-Forwarded-Scopes": "catalog:write admin",
        "X-Actor": "forged-actor",
        "X-User": "forged-user",
        "X-Role": "admin",
        "X-Scope": "catalog:write",
        "X-Principal": "forged-principal",
        "X-Auth-Request-User": "forged-auth-request-user",
      },
    });
    const body = (await response.json()) as HierarchyResponseBody;
    return { status: response.status, instances: body.counts.instances };
  });

  expect(result).toEqual({ status: 200, instances: 43 });
});

test("WEB-01 zoom, pan, fit, and duplicate selection remain bounded and local", async ({
  page,
}) => {
  let hierarchyRequestCount = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/catalog/hierarchy") {
      hierarchyRequestCount += 1;
    }
  });
  const body = await loadHierarchy(page);
  const viewport = page.getByTestId("org-chart-viewport");
  const initial = {
    zoom: Number(await viewport.getAttribute("data-viewport-zoom")),
    x: Number(await viewport.getAttribute("data-viewport-x")),
    y: Number(await viewport.getAttribute("data-viewport-y")),
  };
  expect(initial.zoom).toBeGreaterThanOrEqual(0.35);
  expect(initial.zoom).toBeLessThanOrEqual(1);

  await page.getByRole("button", { name: "Zoom in" }).click();
  await expect(viewport).toHaveAttribute(
    "data-viewport-zoom",
    String(Math.round(initial.zoom * 1.2 * 10_000) / 10_000),
  );
  await page.getByRole("button", { name: "Zoom in" }).click();
  const beforePanX = Number(await viewport.getAttribute("data-viewport-x"));
  const beforePanY = Number(await viewport.getAttribute("data-viewport-y"));
  const box = await viewport.boundingBox();
  expect(box).not.toBeNull();
  if (box !== null) {
    const beforeWheelZoom = Number(
      await viewport.getAttribute("data-viewport-zoom"),
    );
    await viewport.dispatchEvent("wheel", {
      bubbles: true,
      cancelable: true,
      clientX: box.x + box.width / 2,
      clientY: box.y + box.height / 2,
      deltaX: 60,
      deltaY: 0,
      deltaMode: 0,
    });
    await expect(viewport).toHaveAttribute(
      "data-viewport-zoom",
      String(beforeWheelZoom),
    );
    expect(Number(await viewport.getAttribute("data-viewport-x"))).not.toBe(
      beforePanX,
    );
    expect(Number(await viewport.getAttribute("data-viewport-y"))).toBe(
      beforePanY,
    );

    const beforeModifierZoom = Number(
      await viewport.getAttribute("data-viewport-zoom"),
    );
    await viewport.dispatchEvent("wheel", {
      bubbles: true,
      cancelable: true,
      clientX: box.x + box.width / 2,
      clientY: box.y + box.height / 2,
      deltaX: 0,
      deltaY: -25,
      deltaMode: 0,
      ctrlKey: true,
    });
    const afterModifierZoom = Number(
      await viewport.getAttribute("data-viewport-zoom"),
    );
    expect(afterModifierZoom).toBeGreaterThan(beforeModifierZoom);
    expect(afterModifierZoom).toBeLessThan(beforeModifierZoom * 1.2);

    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(
      box.x + box.width / 2 - 180,
      box.y + box.height / 2 - 90,
      {
        steps: 6,
      },
    );
    await page.mouse.up();
  }
  expect(Number(await viewport.getAttribute("data-viewport-x"))).not.toBe(
    beforePanX,
  );
  expect(Number(await viewport.getAttribute("data-viewport-y"))).not.toBe(
    beforePanY,
  );

  await page.getByRole("button", { name: "Fit hierarchy" }).click();
  await expect(viewport).toHaveAttribute(
    "data-viewport-zoom",
    String(initial.zoom),
  );
  await expect(viewport).toHaveAttribute("data-viewport-x", String(initial.x));
  await expect(viewport).toHaveAttribute("data-viewport-y", String(initial.y));

  const communityPair = body.departments[3]?.functions[0]?.instances.slice(
    0,
    2,
  );
  expect(communityPair).toHaveLength(2);
  const firstId = communityPair?.[0]?.id;
  const secondId = communityPair?.[1]?.id;
  expect(firstId).toBeDefined();
  expect(secondId).toBeDefined();
  if (firstId !== undefined && secondId !== undefined) {
    const first = page.locator(`[data-instance-id="${firstId}"]`);
    const second = page.locator(`[data-instance-id="${secondId}"]`);
    await first.click();
    await expect(first).toHaveAttribute("aria-pressed", "true");
    await second.click();
    await expect(first).toHaveAttribute("aria-pressed", "false");
    await expect(second).toHaveAttribute("aria-pressed", "true");
    expect(await first.getAttribute("data-template-id")).toBe(
      await second.getAttribute("data-template-id"),
    );
  }

  expect(hierarchyRequestCount).toBe(1);
});

test("WEB-01 refits all 43 cards at a compact desktop viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await loadHierarchy(page);
  const viewport = page.getByTestId("org-chart-viewport");
  const zoom = Number(await viewport.getAttribute("data-viewport-zoom"));
  expect(zoom).toBeGreaterThanOrEqual(0.35);
  expect(zoom).toBeLessThan(1);
  await expectCompleteHierarchyVisible(page);

  await page.getByRole("button", { name: "Zoom in" }).click();
  await page.getByRole("button", { name: "Zoom in" }).click();
  const viewportBox = await viewport.boundingBox();
  expect(viewportBox).not.toBeNull();
  if (viewportBox !== null) {
    await page.mouse.move(
      viewportBox.x + viewportBox.width / 2,
      viewportBox.y + viewportBox.height / 2,
    );
    await page.mouse.down();
    await page.mouse.move(
      viewportBox.x + viewportBox.width / 2 - 160,
      viewportBox.y + viewportBox.height / 2 - 80,
      { steps: 4 },
    );
    await page.mouse.up();
  }
  await page.getByRole("button", { name: "Fit hierarchy" }).click();
  await expect(viewport).toHaveAttribute("data-viewport-intent", "auto-fit");
  await expectCompleteHierarchyVisible(page);
  await expect(page.getByText("14 deployments · 7 templates")).toBeVisible();
});

test("OBJ-02 rejects a count-preserving source ordinal corruption and recovers the real interactive hierarchy", async ({
  page,
}, testInfo) => {
  const pageErrors: string[] = [];
  const browserProblems: { type: string; text: string; url: string }[] = [];
  const failedResponses: Response[] = [];
  // The declared browser runner starts the catalog-only API factory. Its
  // approval and live-status services really return 503; assert those precise
  // responses and honest unavailable UI, rather than fixture success or hide
  // unrelated console failures. Hierarchy and instance details remain real.
  const unavailableRuntimeUrls = [
    "http://127.0.0.1:4173/api/v1/approvals?status=pending&limit=100",
    "http://127.0.0.1:4173/api/v1/agent-instances/status-summary",
  ];
  const mutationMethods: string[] = [];
  const sourceBodies: HierarchyResponseBody[] = [];
  let hierarchyRequestCount = 0;
  page.on("pageerror", (error) => pageErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "warning" || message.type() === "error") {
      browserProblems.push({
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
    if (
      new URL(request.url()).pathname.startsWith("/api/") &&
      !["GET", "HEAD"].includes(request.method())
    ) {
      mutationMethods.push(request.method());
    }
  });

  await page.route("**/api/v1/catalog/hierarchy", async (route) => {
    hierarchyRequestCount += 1;
    if (hierarchyRequestCount !== 1) {
      await route.continue();
      return;
    }
    // Only this first response is a malformed fixture. The original body and
    // every subsequent hierarchy/detail read come from the real local API.
    const response = await route.fetch();
    expect(response.status()).toBe(200);
    const source = (await response.json()) as HierarchyResponseBody;
    sourceBodies.push(source);
    const target = source.departments.find(
      (department) => department.displayName !== "Community",
    )?.functions[0]?.instances[0];
    if (target === undefined) throw new Error("OBJ-02 source instance missing");
    expect(target.sourceOrdinal).toBe(1);
    const malformed = {
      ...source,
      departments: source.departments.map((department) => ({
        ...department,
        functions: department.functions.map((agentFunction) => ({
          ...agentFunction,
          instances: agentFunction.instances.map((instance) =>
            instance.id === target.id
              ? { ...instance, sourceOrdinal: 2 }
              : instance,
          ),
        })),
      })),
    };
    expect(malformed.counts).toEqual(source.counts);
    await route.fulfill({ response, json: malformed });
  });

  await page.goto("/");
  await expect(page).toHaveURL("http://127.0.0.1:4173/");
  await expect(page).toHaveTitle("Organization chart | Marketing Agents");
  await expect(
    page.getByRole("heading", {
      name: "Marketing agent organization",
      exact: true,
    }),
  ).toBeVisible();
  const unavailable = page.getByRole("alert").filter({
    hasText: "The hierarchy is unavailable",
  });
  await expect(unavailable).toBeVisible();
  await expect(page.getByTestId("org-chart-viewport")).toHaveCount(0);
  await expect(page.getByRole("tree")).toHaveCount(0);
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(0);
  expect(hierarchyRequestCount).toBe(1);
  await page.screenshot({
    path: testInfo.outputPath("obj-02-malformed-hierarchy-unavailable.png"),
  });

  const retryResponsePromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/catalog/hierarchy" &&
      response.request().method() === "GET",
  );
  await unavailable.getByRole("button", { name: "Try again" }).click();
  const retryResponse = await retryResponsePromise;
  expect(retryResponse.status()).toBe(200);
  const source = sourceBodies[0];
  if (source === undefined)
    throw new Error("OBJ-02 real hierarchy not observed");
  expect(await retryResponse.json()).toEqual(source);
  expect(source.counts).toEqual({
    departments: 5,
    functions: 12,
    templates: 36,
    instances: 43,
  });
  await expect(unavailable).toHaveCount(0);
  await expectCompleteHierarchyVisible(page);
  await expect(page.locator('[data-node-kind="department"]')).toHaveCount(5);
  await expect.poll(() => failedResponses.length).toBe(2);
  for (const response of failedResponses) {
    expect(unavailableRuntimeUrls).toContain(response.url());
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
  const approvalsLink = page
    .getByRole("navigation", { name: "Primary navigation" })
    .getByRole("link", { name: "Approvals", exact: true });
  await expect(approvalsLink).toBeVisible();
  await expect(approvalsLink.getByRole("status")).toHaveCount(0);
  await page.getByRole("button", { name: "Filters", exact: true }).click();
  const filters = page.getByRole("dialog", { name: "Catalog filters" });
  await expect(
    filters.getByRole("combobox", { name: "Recent run state" }),
  ).toBeDisabled();
  await expect(
    filters.getByText("Recent run status is unavailable.", { exact: true }),
  ).toBeVisible();
  await filters.getByRole("button", { name: "Close", exact: true }).click();
  await expect(filters).toHaveCount(0);
  const sourceInstanceIds = source.departments.flatMap((department) =>
    department.functions.flatMap((agentFunction) =>
      agentFunction.instances.map((instance) => instance.id),
    ),
  );
  expect(
    await page
      .locator('[data-node-kind="instance"]')
      .evaluateAll((nodes) =>
        nodes.map((node) => node.getAttribute("data-instance-id")),
      ),
  ).toEqual(sourceInstanceIds);

  const viewport = page.getByTestId("org-chart-viewport");
  const initialZoom = await viewport.getAttribute("data-viewport-zoom");
  await page.getByRole("button", { name: "Zoom in" }).click();
  await expect(viewport).not.toHaveAttribute(
    "data-viewport-zoom",
    initialZoom ?? "",
  );
  await page.getByRole("button", { name: "Fit hierarchy" }).click();
  await expect(viewport).toHaveAttribute(
    "data-viewport-zoom",
    initialZoom ?? "",
  );
  await expectCompleteHierarchyVisible(page);
  await page.screenshot({
    path: testInfo.outputPath("obj-02-recovered-desktop-hierarchy.png"),
  });

  const pair = source.departments
    .find((department) => department.displayName === "Community")
    ?.functions[0]?.instances.slice(0, 2);
  const first = pair?.[0];
  const second = pair?.[1];
  if (first === undefined || second === undefined) {
    throw new Error("OBJ-02 Community source pair missing");
  }
  expect(first.id).not.toBe(second.id);
  expect(first.templateId).toBe(second.templateId);
  expect([first.sourceOrdinal, second.sourceOrdinal]).toEqual([1, 2]);
  const inspector = page.locator("#agent-inspector");
  for (const instance of [first, second]) {
    const detailResponsePromise = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname ===
          `/api/v1/agent-instances/${instance.id}` &&
        response.request().method() === "GET",
    );
    const card = page.locator(`[data-instance-id="${instance.id}"]`);
    await expect(card).toHaveAccessibleName(
      new RegExp(`Instance ${String(instance.sourceOrdinal)} of 2`, "u"),
    );
    await card.click();
    expect((await detailResponsePromise).status()).toBe(200);
    await expect(card).toHaveAttribute("aria-pressed", "true");
    await expect(inspector.getByRole("heading", { level: 2 })).toContainText(
      `Instance ${String(instance.sourceOrdinal)} of 2`,
    );
    await expect(
      inspector.getByRole("region", { name: "Deployment & configuration" }),
    ).toContainText(instance.id);
    await expect(
      inspector.getByRole("region", { name: "Template", exact: true }),
    ).toContainText(instance.templateId);
    await expect(
      inspector.getByRole("region", { name: "Recent runs", exact: true }),
    ).toContainText("Recent run data is unavailable for this local runtime.");
  }
  await expect(
    page.locator(`[data-instance-id="${first.id}"]`),
  ).toHaveAttribute("aria-pressed", "false");

  await page.setViewportSize({ width: 390, height: 844 });
  const tree = page.getByRole("tree", {
    name: "Marketing Agents organization tree",
  });
  await expect(tree).toBeVisible();
  await expect(page.getByTestId("org-chart-viewport")).toHaveCount(0);
  await expect(tree.locator('[data-node-kind="department"]')).toHaveCount(5);
  await expect(tree.locator('[data-node-kind="function"]')).toHaveCount(12);
  const selected = tree.locator(`[data-instance-id="${second.id}"]`);
  await expect(selected).toHaveAttribute("aria-selected", "true");
  await expect(selected).toHaveAttribute("aria-level", "4");
  await expect(tree.locator('[aria-selected="true"]')).toHaveCount(1);
  await expect(
    tree.locator(`[data-instance-id="${first.id}"]`),
  ).toHaveAttribute("aria-selected", "false");
  await expect(
    page.getByRole("dialog", { name: /Instance 2 of 2/u }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("obj-02-recovered-mobile-selection.png"),
  });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await expect(page).toHaveURL("http://127.0.0.1:4173/");
  await expect(page).toHaveTitle("Organization chart | Marketing Agents");
  await expect(page.locator("vite-error-overlay")).toHaveCount(0);
  expect(hierarchyRequestCount).toBe(2);
  expect(mutationMethods).toEqual([]);
  expect(failedResponses.map((response) => response.url()).sort()).toEqual(
    [...unavailableRuntimeUrls].sort(),
  );
  expect(pageErrors).toEqual([]);
  expect(
    browserProblems.sort((left, right) => left.url.localeCompare(right.url)),
  ).toEqual(
    unavailableRuntimeUrls
      .map((url) => ({
        type: "error",
        text: "Failed to load resource: the server responded with a status of 503 (Service Unavailable)",
        url,
      }))
      .sort((left, right) => left.url.localeCompare(right.url)),
  );
});

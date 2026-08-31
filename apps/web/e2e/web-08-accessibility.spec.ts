// WEB-08 browser evidence exercises accessibility across the production control surface.
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  makeArtifactPage,
  makeArtifactResource,
  makeRunResource,
  makeTimelineEvent,
  makeTimelinePage,
  WEB_06_ARTIFACT_ID,
  WEB_06_RUN_ID,
  WEB_06_STEP_ID,
} from "../src/test/runArtifactFixture";

type JsonObject = Record<string, unknown>;

const LOCAL_HOSTS = new Set(["127.0.0.1", "localhost"]);
// A 1280 x 1024 desktop at 200% zoom exposes a 640 x 512 CSS-pixel reflow viewport.
const REFLOW_VIEWPORT = { width: 640, height: 512 } as const;
const APPROVAL_ID = "approval.web-08.social-publish";
const APPROVAL_ACTION_ID = "action.web-08.social-publish";
const APPROVAL_RUN_ID = "run.web-08.social";
const APPROVAL_STEP_ID = "step.web-08.social";
const APPROVAL_TEMPLATE_ID =
  "tpl.social-media.new-content.linkedin-post-drafter";
const APPROVAL_INSTANCE_ID =
  "inst.social-media.new-content.linkedin-post-drafter.01";
const VISIBLE_TABBABLE_SELECTOR = [
  "a[href]:visible",
  "button:not(:disabled):visible",
  'input:not([type="radio"]):not(:disabled):visible',
  'input[type="radio"]:checked:not(:disabled):visible',
  "select:not(:disabled):visible",
  "summary:visible",
  "textarea:not(:disabled):visible",
  '[tabindex]:not([tabindex="-1"]):visible',
].join(",");

function approvalSummary(): JsonObject {
  return {
    id: APPROVAL_ID,
    status: "pending",
    resource_version: 1,
    generation: 1,
    action_id: APPROVAL_ACTION_ID,
    action_type: "social.publish",
    destination_summary: "Mock social account · accessibility review",
    run_id: APPROVAL_RUN_ID,
    template_id: APPROVAL_TEMPLATE_ID,
    instance_id: APPROVAL_INSTANCE_ID,
    requested_at: "2026-08-31T21:00:00Z",
    expires_at: "2099-08-31T22:00:00Z",
    is_expired: false,
    is_actionable: true,
    approval_url: `/api/v1/approvals/${APPROVAL_ID}`,
    action_url: `/api/v1/external-actions/${APPROVAL_ACTION_ID}`,
    run_url: `/api/v1/runs/${APPROVAL_RUN_ID}`,
  };
}

function approvalDetail(): JsonObject {
  return {
    ...approvalSummary(),
    one_time_use_state: "unused",
    capability_id: "cap.social.publish",
    connector_family: "social",
    binding_id: "mock.social.default",
    redacted_payload: { draft_id: "demo-social-draft-001" },
    payload_hash: "8".repeat(64),
    step_id: APPROVAL_STEP_ID,
    policy_id: "policy.external-write.default",
    required_roles: ["approver"],
    required_scopes: ["scope.external-write"],
    allow_self_approval: true,
    requested_by: "local-accessibility-reviewer",
    updated_at: "2026-08-31T21:00:00Z",
    decision_id: null,
    decision_kind: null,
    decision_actor_id: null,
    decision_reason_code: null,
    decision_reason: null,
    decided_at: null,
    expired_at: null,
    replacement_approval_id: null,
    renewed_at: null,
    superseded_at: null,
    superseded_reason_code: null,
    consumed_at: null,
    step_url: `/api/v1/runs/${APPROVAL_RUN_ID}/steps/${APPROVAL_STEP_ID}`,
    template_url: `/api/v1/agent-templates/${APPROVAL_TEMPLATE_ID}`,
    instance_url: `/api/v1/agent-instances/${APPROVAL_INSTANCE_ID}`,
  };
}

function sessionFixture(): JsonObject {
  return {
    actorId: "local-accessibility-reviewer",
    roles: ["approver", "local_admin", "operator", "viewer"],
    scopes: ["approvals:decide", "approvals:read", "scope.external-write"],
    authMode: "local",
    environment: "local",
    modelMode: "mock",
    connectorMode: "mock",
    networkPermission: false,
    warning: "Local identity — not production authentication",
    csrfToken: "web08csrf".repeat(5),
    csrfHeaderName: "X-CSRF-Token",
  };
}

function wireKey(key: string): string {
  return key.replace(/[A-Z]/gu, (character) => `_${character.toLowerCase()}`);
}

function toWireValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(toWireValue);
  if (typeof value === "string") {
    return value.replace(
      "/api/v1/catalog/templates/",
      "/api/v1/agent-templates/",
    );
  }
  if (typeof value !== "object" || value === null) return value;
  return Object.fromEntries(
    Object.entries(value).map(([key, child]) => [
      wireKey(key),
      toWireValue(child),
    ]),
  );
}

async function installCommonBoundary(page: Page): Promise<string[]> {
  const externalRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (!LOCAL_HOSTS.has(url.hostname)) externalRequests.push(request.url());
  });

  await page.route("**/api/v1/session", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Cache-Control": "no-store" },
      body: JSON.stringify(sessionFixture()),
    });
  });
  await page.route("**/api/v1/approvals**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (request.method() === "GET" && pathname === "/api/v1/approvals") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Cache-Control": "no-store", Vary: "Authorization" },
        body: JSON.stringify({ items: [approvalSummary()], next_cursor: null }),
      });
      return;
    }
    if (
      request.method() === "GET" &&
      pathname === `/api/v1/approvals/${APPROVAL_ID}`
    ) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Cache-Control": "no-store", Vary: "Authorization" },
        body: JSON.stringify(approvalDetail()),
      });
      return;
    }
    throw new Error(
      `Unexpected WEB-08 approval request: ${request.method()} ${pathname}`,
    );
  });
  return externalRequests;
}

async function installRunArtifactBoundary(page: Page): Promise<void> {
  const timeline = makeTimelinePage([
    makeTimelineEvent({ sequence: 1, eventType: "run.received" }),
    makeTimelineEvent({
      sequence: 2,
      eventType: "artifact.created",
      stepId: WEB_06_STEP_ID,
      artifactId: WEB_06_ARTIFACT_ID,
    }),
  ]);
  await page.route("**/api/v1/runs/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (request.method() !== "GET") {
      throw new Error(
        `Unexpected WEB-08 run mutation: ${request.method()} ${pathname}`,
      );
    }
    const payload =
      pathname === `/api/v1/runs/${WEB_06_RUN_ID}`
        ? makeRunResource()
        : pathname === `/api/v1/runs/${WEB_06_RUN_ID}/timeline`
          ? timeline
          : pathname === `/api/v1/runs/${WEB_06_RUN_ID}/artifacts`
            ? makeArtifactPage()
            : null;
    if (payload === null)
      throw new Error(`Unexpected WEB-08 run request: ${pathname}`);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Cache-Control": "no-store", Vary: "Authorization" },
      body: JSON.stringify(toWireValue(payload)),
    });
  });
  await page.route("**/api/v1/artifacts/*", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    expect(request.method()).toBe("GET");
    expect(pathname).toBe(`/api/v1/artifacts/${WEB_06_ARTIFACT_ID}`);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Cache-Control": "no-store", Vary: "Authorization" },
      body: JSON.stringify(toWireValue(makeArtifactResource())),
    });
  });
}

function axeDiagnostics(
  violations: Awaited<ReturnType<AxeBuilder["analyze"]>>["violations"],
): string {
  return violations
    .map(
      ({ id, impact, help, nodes }) =>
        `${impact ?? "unknown"} ${id}: ${help}\n${nodes
          .map(
            ({ target, failureSummary }) =>
              `  ${target.join(" ")} — ${failureSummary ?? "no summary"}`,
          )
          .join("\n")}`,
    )
    .join("\n");
}

async function expectAccessible(page: Page, state: string): Promise<void> {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  const blocking = results.violations.filter(
    ({ impact }) => impact === "serious" || impact === "critical",
  );
  expect(blocking, `${state}\n${axeDiagnostics(blocking)}`).toEqual([]);
}

async function expectVisibleKeyboardFocus(target: Locator): Promise<void> {
  await expect(target).toBeFocused();
  const focusStyle = await target.evaluate((element) => {
    type Rgba = readonly [number, number, number, number];

    const rgba = (value: string): Rgba => {
      const channels = value.match(/[\d.]+/gu)?.map(Number);
      const red = channels?.[0];
      const green = channels?.[1];
      const blue = channels?.[2];
      if (red === undefined || green === undefined || blue === undefined) {
        throw new Error(`Could not resolve computed color ${value}`);
      }
      return [red, green, blue, channels?.[3] ?? 1];
    };
    const composite = (foreground: Rgba, background: Rgba): Rgba => {
      const alpha = foreground[3] + background[3] * (1 - foreground[3]);
      if (alpha === 0) return [0, 0, 0, 0];
      return [
        (foreground[0] * foreground[3] +
          background[0] * background[3] * (1 - foreground[3])) /
          alpha,
        (foreground[1] * foreground[3] +
          background[1] * background[3] * (1 - foreground[3])) /
          alpha,
        (foreground[2] * foreground[3] +
          background[2] * background[3] * (1 - foreground[3])) /
          alpha,
        alpha,
      ];
    };
    const luminance = (color: Rgba): number => {
      const linear = (channel: number): number => {
        const normalized = channel / 255;
        return normalized <= 0.04045
          ? normalized / 12.92
          : ((normalized + 0.055) / 1.055) ** 2.4;
      };
      return (
        linear(color[0]) * 0.2126 +
        linear(color[1]) * 0.7152 +
        linear(color[2]) * 0.0722
      );
    };

    const style = getComputedStyle(element);
    const backgroundLayers: Element[] = [];
    let ancestor = element.parentElement;
    while (ancestor !== null) {
      backgroundLayers.push(ancestor);
      ancestor = ancestor.parentElement;
    }
    const adjacentBackground = backgroundLayers
      .reverse()
      .reduce<Rgba>(
        (background, layer) =>
          composite(rgba(getComputedStyle(layer).backgroundColor), background),
        [255, 255, 255, 1],
      );
    const renderedOutline = composite(
      rgba(style.outlineColor),
      adjacentBackground,
    );
    const outlineLuminance = luminance(renderedOutline);
    const backgroundLuminance = luminance(adjacentBackground);
    return {
      adjacentBackground,
      contrastRatio:
        (Math.max(outlineLuminance, backgroundLuminance) + 0.05) /
        (Math.min(outlineLuminance, backgroundLuminance) + 0.05),
      outlineColor: style.outlineColor,
      outlineStyle: style.outlineStyle,
      outlineWidth: Number.parseFloat(style.outlineWidth),
    };
  });
  expect(focusStyle.outlineStyle).toBe("solid");
  expect(focusStyle.outlineWidth).toBeGreaterThanOrEqual(2);
  expect(
    focusStyle.contrastRatio,
    `outline ${focusStyle.outlineColor} against adjacent background ${focusStyle.adjacentBackground.join(
      ", ",
    )}`,
  ).toBeGreaterThanOrEqual(3);
}

async function expectReducedMotion(page: Page): Promise<void> {
  expect(
    await page.evaluate(
      () => matchMedia("(prefers-reduced-motion: reduce)").matches,
    ),
  ).toBe(true);
  const animated = await page.evaluate(() => {
    const milliseconds = (value: string): number => {
      const trimmed = value.trim();
      return trimmed.endsWith("ms")
        ? Number.parseFloat(trimmed)
        : Number.parseFloat(trimmed) * 1_000;
    };
    return [...document.querySelectorAll<HTMLElement>("body *")]
      .map((element) => {
        const style = getComputedStyle(element);
        return {
          tag: element.tagName.toLowerCase(),
          className: element.className,
          animation: style.animationDuration.split(",").map(milliseconds),
          transition: style.transitionDuration.split(",").map(milliseconds),
          scrollBehavior: style.scrollBehavior,
        };
      })
      .filter(
        ({ animation, transition, scrollBehavior }) =>
          animation.some((duration) => duration > 0.1) ||
          transition.some((duration) => duration > 0.1) ||
          scrollBehavior === "smooth",
      )
      .slice(0, 20);
  });
  expect(animated).toEqual([]);
}

async function expectTwoHundredPercentReflow(page: Page): Promise<void> {
  await page.setViewportSize(REFLOW_VIEWPORT);
  await expect
    .poll(() =>
      page.evaluate(() => ({
        clientWidth: document.documentElement.clientWidth,
        documentScrollWidth: document.documentElement.scrollWidth,
        bodyScrollWidth: document.body.scrollWidth,
      })),
    )
    .toEqual({
      clientWidth: REFLOW_VIEWPORT.width,
      documentScrollWidth: REFLOW_VIEWPORT.width,
      bodyScrollWidth: REFLOW_VIEWPORT.width,
    });
}

test("WEB-08 chart, tree, detail, and dry-run states pass accessibility acceptance", async ({
  page,
}) => {
  const externalRequests = await installCommonBoundary(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");
  await page.keyboard.press("Tab");
  const skipLink = page.getByRole("link", { name: "Skip to main content" });
  await expectVisibleKeyboardFocus(skipLink);
  await page.keyboard.press("Enter");
  await expectVisibleKeyboardFocus(page.locator("#main-content"));
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(43);
  await expect(page.getByTestId("org-chart-viewport")).toBeVisible();
  await expectAccessible(page, "Desktop organization chart");
  await expectReducedMotion(page);

  const treeToggle = page.getByRole("button", { name: "Tree view" });
  await treeToggle.focus();
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("tree", { name: "Marketing Agents organization tree" }),
  ).toBeVisible();
  await expectVisibleKeyboardFocus(treeToggle);
  await expectAccessible(page, "Semantic organization tree");
  await expectTwoHundredPercentReflow(page);

  const filterTrigger = page.getByRole("button", {
    name: "Filters",
    exact: true,
  });
  await filterTrigger.focus();
  await page.keyboard.press("Enter");
  const filterDialog = page.getByRole("dialog", { name: "Catalog filters" });
  await expect(filterDialog).toHaveAttribute("aria-modal", "true");
  await expect(
    filterDialog.getByRole("combobox", { name: "Department" }),
  ).toBeFocused();
  await filterDialog.getByRole("button", { name: "Close" }).focus();
  await page.keyboard.press("Shift+Tab");
  await expect(
    filterDialog.getByRole("combobox", { name: "Capability" }),
  ).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(filterDialog).toHaveCount(0);
  await expectVisibleKeyboardFocus(filterTrigger);

  const root = page.locator('[role="treeitem"][data-node-id="root"]');
  await root.focus();
  await page.keyboard.press("ArrowDown");
  const departmentItem = page.locator('[role="treeitem"]:focus');
  await expect(departmentItem).toHaveAttribute("aria-level", "2");
  await page.keyboard.press("ArrowDown");
  const functionItem = page.locator('[role="treeitem"]:focus');
  await expect(functionItem).toHaveAttribute("aria-level", "3");
  await page.keyboard.press("ArrowRight");
  await expect(functionItem).toHaveAttribute("aria-expanded", "true");
  await page.keyboard.press("ArrowRight");
  const instanceItem = page.locator('[role="treeitem"]:focus');
  await expect(instanceItem).toHaveAttribute("aria-level", "4");
  const selectedNodeId = await instanceItem.getAttribute("data-node-id");
  expect(selectedNodeId).not.toBeNull();
  await page.keyboard.press("Enter");

  const inspector = page.locator("#agent-inspector");
  const form = inspector.getByRole("form", { name: "Manual dry-run input" });
  await expect(inspector).toBeVisible();
  await expectVisibleKeyboardFocus(
    inspector.getByRole("button", { name: /Close details for/u }),
  );
  await expect(form).toBeVisible();
  const formControl = form
    .locator(
      "input:not(:disabled), select:not(:disabled), textarea:not(:disabled)",
    )
    .first();
  await formControl.focus();
  await expectVisibleKeyboardFocus(formControl);

  const inspectorTabbables = inspector.locator(VISIBLE_TABBABLE_SELECTOR);
  const firstInspectorTabbable = inspectorTabbables.first();
  const lastInspectorTabbable = inspectorTabbables.last();
  const inspectorTabbableCount = await inspectorTabbables.count();
  expect(inspectorTabbableCount).toBeGreaterThan(1);
  await firstInspectorTabbable.focus();
  for (let index = 0; index < inspectorTabbableCount; index += 1) {
    await expect(inspectorTabbables.nth(index)).toBeFocused();
    expect(
      await inspector.evaluate((element) =>
        element.contains(document.activeElement),
      ),
    ).toBe(true);
    await page.keyboard.press("Tab");
  }
  await expect(firstInspectorTabbable).toBeFocused();
  expect(
    await inspector.evaluate((element) =>
      element.contains(document.activeElement),
    ),
  ).toBe(true);
  await page.keyboard.press("Shift+Tab");
  await expect(lastInspectorTabbable).toBeFocused();
  expect(
    await inspector.evaluate((element) =>
      element.contains(document.activeElement),
    ),
  ).toBe(true);
  await expectAccessible(page, "Agent detail and schema-driven dry-run form");
  await expectReducedMotion(page);

  await form.getByRole("button", { name: "Create dry run" }).focus();
  await page.keyboard.press("Escape");
  await expect(inspector).toHaveCount(0);
  if (selectedNodeId !== null) {
    await expectVisibleKeyboardFocus(
      page.locator(`[role="treeitem"][data-node-id="${selectedNodeId}"]`),
    );
  }
  expect(externalRequests).toEqual([]);
});

test("WEB-08 approval review and decision dialog preserve keyboard focus", async ({
  page,
}) => {
  const externalRequests = await installCommonBoundary(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/approvals");
  await expect(
    page.getByRole("heading", { level: 1, name: "Approval queue" }),
  ).toBeVisible();
  await expect(
    page.locator(`article[data-approval-id="${APPROVAL_ID}"]`),
  ).toBeVisible();
  await expectTwoHundredPercentReflow(page);
  await expectAccessible(page, "Approval queue at 200 percent reflow");

  const review = page.getByRole("button", {
    name: `Review approval ${APPROVAL_ID}`,
  });
  await review.focus();
  await page.keyboard.press("Enter");
  const reviewPanel = page.locator("#approval-review-panel");
  await expect(reviewPanel).toBeVisible();
  await expectVisibleKeyboardFocus(reviewPanel);
  await page.keyboard.press("Shift+Tab");
  await expectVisibleKeyboardFocus(
    reviewPanel.getByRole("button", { name: "Approve" }),
  );
  await expectAccessible(page, "Exact approval review");

  const approve = reviewPanel.getByRole("button", { name: "Approve" });
  await approve.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Approve exact action?" });
  const cancel = dialog.getByRole("button", { name: "Cancel" });
  const confirm = dialog.getByRole("button", { name: "Approve exact action" });
  const firstFocusable = dialog
    .locator(
      'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
    )
    .first();
  await expect(dialog).toBeVisible();
  await expectVisibleKeyboardFocus(cancel);
  await page.keyboard.press("Tab");
  await expectVisibleKeyboardFocus(confirm);
  await page.keyboard.press("Tab");
  await expectVisibleKeyboardFocus(firstFocusable);
  await page.keyboard.press("Shift+Tab");
  await expectVisibleKeyboardFocus(confirm);
  await expectAccessible(page, "Approval decision modal");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expectVisibleKeyboardFocus(approve);
  await page.keyboard.press("Escape");
  await expect(reviewPanel).toHaveCount(0);
  await expectVisibleKeyboardFocus(review);
  await expectReducedMotion(page);
  expect(externalRequests).toEqual([]);
});

test("WEB-08 run timeline and artifact viewer pass reflow and keyboard acceptance", async ({
  page,
}) => {
  const externalRequests = await installCommonBoundary(page);
  await installRunArtifactBoundary(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize(REFLOW_VIEWPORT);
  await page.goto(`/runs/${WEB_06_RUN_ID}`);
  await expect(
    page.getByRole("heading", { level: 1, name: "Run timeline" }),
  ).toBeVisible();
  await expect(
    page.getByRole("list", { name: "Run timeline in sequence order" }),
  ).toBeVisible();
  await expectTwoHundredPercentReflow(page);
  await expectAccessible(page, "Run timeline at 200 percent reflow");

  const artifactLink = page.getByRole("link", {
    name: `Artifact ${WEB_06_ARTIFACT_ID}`,
  });
  await artifactLink.focus();
  await expectVisibleKeyboardFocus(artifactLink);
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(
    new RegExp(`/artifacts/${WEB_06_ARTIFACT_ID.replaceAll(".", "\\.")}$`, "u"),
  );
  await expect(
    page.getByRole("heading", { level: 1, name: "Artifact viewer" }),
  ).toBeVisible();
  await expectVisibleKeyboardFocus(page.locator("#main-content"));
  await expectTwoHundredPercentReflow(page);
  await expectAccessible(page, "Artifact viewer at 200 percent reflow");
  await expectReducedMotion(page);
  expect(externalRequests).toEqual([]);
});

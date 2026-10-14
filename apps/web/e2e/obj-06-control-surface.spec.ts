// Real fresh SQLite/API/worker journeys. All mutations use rendered controls;
// native same-origin GETs only observe the authoritative persisted outcome.
import type { components } from "../src/api/generated/schema";
import { expect, test, type Page, type TestInfo } from "./obj-06-fixtures";

const WRITE_INSTANCE = "inst.email.newsletter.newsletter-subscriber.01";
const READ_INSTANCE = "inst.social-media.new-content.linkedin-post-drafter.01";

interface RunSnapshot {
  readonly id: string;
  readonly instance_id: string;
  readonly state: string;
  readonly configuration_revision: number;
  readonly transitions: components["schemas"]["RunResourceView"]["transitions"];
  readonly execution_control: {
    readonly model_calls: number;
    readonly tool_calls: number;
  };
  readonly pending_approvals: readonly { readonly id: string }[];
  readonly artifact_summaries: readonly { readonly id: string }[];
  readonly external_actions: readonly {
    readonly id: string;
    readonly state: string;
    readonly receipt_id: string | null;
    readonly delivery_attempt_count: number;
  }[];
}

type TimelinePage = components["schemas"]["RunTimelineResponse"];
type AuditPage = components["schemas"]["AuditEventListResponse"];

async function readJson<T>(page: Page, path: string): Promise<T> {
  return page.evaluate(async (resource) => {
    if (!resource.startsWith("/api/v1/"))
      throw new Error("Not a local API read");
    const response = await fetch(resource, { cache: "no-store" });
    if (!response.ok)
      throw new Error(`Local read failed: ${String(response.status)}`);
    return response.json() as Promise<T>;
  }, path);
}

function observeBrowser(page: Page) {
  const errors: string[] = [];
  const mutations: { method: string; path: string }[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error" || message.type() === "warning")
      errors.push(message.text());
  });
  page.on("request", (request) => {
    if (["POST", "PATCH", "PUT", "DELETE"].includes(request.method()))
      mutations.push({
        method: request.method(),
        path: new URL(request.url()).pathname,
      });
  });
  return { errors, mutations };
}

async function checkPage(page: Page, title: RegExp, heading: string) {
  await expect(page).toHaveTitle(title);
  await expect(
    page.getByRole("heading", { name: heading, exact: true }),
  ).toBeVisible();
  await expect(
    page.locator(
      "vite-error-overlay, nextjs-portal, #webpack-dev-server-client-overlay",
    ),
  ).toHaveCount(0);
  expect(
    await page.evaluate(() => document.body.innerText.trim().length),
  ).toBeGreaterThan(100);
}

async function capture(page: Page, info: TestInfo, name: string) {
  await page.screenshot({
    path: info.outputPath(`${name}.png`),
    fullPage: true,
  });
}

async function selectAgent(page: Page, instanceId: string) {
  await page.getByRole("searchbox", { name: "Search agents" }).fill(instanceId);
  const node = page.locator(`[data-instance-id="${instanceId}"]`);
  await expect(node).toBeVisible();
  await node.click();
  await expect(page.locator("#agent-inspector")).toBeVisible();
  await expect(
    page
      .locator("#agent-inspector")
      .getByRole("heading", { name: "Manual dry run", exact: true }),
  ).toBeVisible();
}

async function openConfiguration(page: Page) {
  await page
    .locator("#agent-inspector")
    .getByRole("button", { name: "Edit", exact: true })
    .click();
  const editor = page.getByRole("form", {
    name: "Deployment configuration editor",
  });
  await expect(editor).toBeVisible();
  return editor;
}

async function waitForRun(page: Page, runId: string, state: string) {
  await expect
    .poll(
      async () =>
        (
          await readJson<RunSnapshot>(
            page,
            `/api/v1/runs/${encodeURIComponent(runId)}`,
          )
        ).state,
      { timeout: 30_000, intervals: [200, 500, 1000] },
    )
    .toBe(state);
  return readJson<RunSnapshot>(
    page,
    `/api/v1/runs/${encodeURIComponent(runId)}`,
  );
}

async function acceptedRunId(page: Page) {
  await expect(page).toHaveURL(/\/runs\/[^/?#]+$/u);
  const id = decodeURIComponent(
    new URL(page.url()).pathname.slice("/runs/".length),
  );
  expect(id).toMatch(/^run\./u);
  return id;
}

// EXEC-05: actual public pagination, never route fixtures or a client-derived
// event count. These tiny journeys must finish within 20 bounded server pages.
async function readPages<T extends TimelinePage | AuditPage>(
  page: Page,
  path: string,
): Promise<readonly T[]> {
  const resource = new URL(path, "http://127.0.0.1:4173");
  resource.searchParams.set("limit", "7");
  const pages: T[] = [];
  const cursors = new Set<string>();
  for (let index = 0; index < 20; index += 1) {
    const result = await readJson<T>(page, resource.pathname + resource.search);
    expect(result.items.length).toBeLessThanOrEqual(7);
    pages.push(result);
    if (result.next_cursor === null) {
      expect(pages.length).toBeGreaterThan(1);
      return pages;
    }
    if (
      typeof result.next_cursor !== "string" ||
      result.next_cursor.length === 0 ||
      result.next_cursor.length > 1_024
    )
      throw new Error("EXEC-05 public history has an invalid continuation");
    expect(cursors.has(result.next_cursor)).toBe(false);
    cursors.add(result.next_cursor);
    resource.searchParams.set("cursor", result.next_cursor);
  }
  throw new Error("EXEC-05 public history exceeded its bounded page inventory");
}

async function openPersistedRunFromIndex(
  page: Page,
  run: RunSnapshot,
  info: TestInfo,
  viewport: "desktop" | "mobile",
) {
  // Discover through the rendered index, not the Run-ID lookup or direct goto.
  await page.getByRole("link", { name: "Runs & audit", exact: true }).click();
  await expect(page).toHaveURL("http://127.0.0.1:4173/runs");
  await checkPage(
    page,
    /^Runs and audit \| Marketing Agents$/u,
    "Runs & audit",
  );
  const row = page.locator(".runs-list > li").filter({
    has: page.getByRole("link", { name: run.id, exact: true }),
  });
  const link = row.getByRole("link", { name: run.id, exact: true });
  await expect(link).toHaveCount(1);
  await expect(link).toHaveAttribute(
    "href",
    `/runs/${encodeURIComponent(run.id)}`,
  );
  await expect(row).toContainText(run.instance_id);
  await expect(row.locator(".run-status")).toHaveText("Completed");
  // EXEC-05: exact persisted IDs and status must remain readable at mobile width.
  // Visible DOM alone does not catch a flex item's intrinsic-width overflow.
  const bounds = await page.locator(".runs-page").evaluate((element) => {
    const viewport = element.getBoundingClientRect();
    return {
      overflow: element.scrollWidth - element.clientWidth,
      clipped: Array.from(
        element.querySelectorAll(".runs-list li, .run-status"),
      ).some((item) => {
        const rectangle = item.getBoundingClientRect();
        return (
          rectangle.left < viewport.left || rectangle.right > viewport.right
        );
      }),
    };
  });
  expect(bounds).toEqual({ overflow: 0, clipped: false });
  await capture(page, info, `${viewport}-runs-index`);
  await link.click();
  expect(await acceptedRunId(page)).toBe(run.id);
  await checkPage(page, /^Run timeline \| Marketing Agents$/u, "Run timeline");
  await page.getByRole("button", { name: "Refresh now", exact: true }).click();
  await expect(
    page.getByText("Terminal snapshot — polling stopped", { exact: true }),
  ).toBeVisible();
}

async function assertCompletePublicTimeline(
  page: Page,
  run: RunSnapshot,
  approvalId?: string,
) {
  const timelinePages = await readPages<TimelinePage>(
    page,
    `/api/v1/runs/${encodeURIComponent(run.id)}/timeline`,
  );
  expect(timelinePages.every((item) => item.run_id === run.id)).toBe(true);
  const timeline = timelinePages.flatMap((item) => item.items);
  const sequence = timeline.map((item) => item.sequence);
  expect(sequence.length).toBeGreaterThan(3);
  expect(sequence).toEqual(sequence.map((_, index) => index + 1));
  expect(new Set(timeline.map((item) => item.id)).size).toBe(timeline.length);

  const feedPages = await readPages<AuditPage>(
    page,
    `/api/v1/audit-events?run_id=${encodeURIComponent(run.id)}`,
  );
  expect(new Set(feedPages.map((item) => item.high_watermark)).size).toBe(1);
  const feed = feedPages.flatMap((item) => item.items);
  const feedSequence = feed.map((item) => item.sequence);
  expect(feedSequence).toEqual(
    [...new Set(feedSequence)].sort((left, right) => right - left),
  );
  expect(feed.map((item) => item.id)).toEqual(
    timeline.map((item) => item.id).reverse(),
  );
  for (const event of timeline) {
    const { sequence: runSequence, ...projection } = event;
    const witness = feed.find((item) => item.id === event.id);
    expect(witness).toMatchObject({
      ...projection,
      run_sequence: runSequence,
      run_id: run.id,
      run_url: `/api/v1/runs/${run.id}`,
    });
  }

  // Expected cardinality and authority come from the separate persisted Run
  // projection. AC-14 owns independent database-fact completeness; this checks
  // public projection consistency and that the rendered history loses no item.
  const transitions = feed.filter(
    (item) =>
      item.aggregate_type === "run" && item.transition_sequence !== null,
  );
  expect(transitions).toHaveLength(run.transitions.length);
  for (const transition of run.transitions) {
    expect(
      transitions.filter(
        (item) => item.transition_sequence === transition.sequence,
      ),
    ).toEqual([
      expect.objectContaining({
        aggregate_id: run.id,
        mutation_version: transition.resulting_version,
        previous_state: transition.previous_state,
        new_state: transition.new_state,
        reason_code: transition.reason_code,
        occurred_at: transition.occurred_at,
        outcome: "accepted",
      }),
    ]);
  }
  expect(
    feed
      .filter((item) => item.event_type === "artifact.persisted")
      .map((item) => item.artifact_id)
      .sort(),
  ).toEqual(run.artifact_summaries.map((item) => item.id).sort());
  const succeededActions = feed.filter(
    (item) => item.event_type === "action.succeeded",
  );
  expect(succeededActions).toHaveLength(run.external_actions.length);
  for (const action of run.external_actions) {
    expect(
      succeededActions.filter((item) => item.action_id === action.id),
    ).toEqual([
      expect.objectContaining({
        receipt_id: action.receipt_id,
        action_attempt_number: action.delivery_attempt_count,
        new_state: action.state,
        outcome: "accepted",
      }),
    ]);
  }
  const approvalEvents = feed.filter(
    (item) => item.aggregate_type === "approval_request",
  );
  expect(approvalEvents.map((item) => item.event_type).sort()).toEqual(
    approvalId === undefined
      ? []
      : ["approval.approved", "approval.consumed", "approval.requested"],
  );
  expect(
    approvalEvents.every((item) => item.approval_request_id === approvalId),
  ).toBe(true);

  const rendered = page.locator(".run-timeline > .run-event");
  await expect(rendered).toHaveCount(Math.min(100, timeline.length));
  for (let loaded = 100; loaded < timeline.length; loaded += 100) {
    await page
      .getByRole("button", { name: "Load more events", exact: true })
      .click();
    await expect(rendered).toHaveCount(Math.min(loaded + 100, timeline.length));
  }
  await expect(
    page.getByRole("button", { name: "Load more events", exact: true }),
  ).toHaveCount(0);
  expect(
    await rendered.evaluateAll((items) =>
      items.map((item) => Number(item.getAttribute("data-sequence"))),
    ),
  ).toEqual(sequence);
  for (const event of timeline) {
    const card = page.locator(
      `.run-event[data-sequence="${String(event.sequence)}"]`,
    );
    await expect(card.locator(".run-event__facts code")).toHaveText(
      event.event_type,
    );
    if (event.artifact_id !== null) {
      await expect(
        card.getByRole("link", {
          name: `Artifact ${event.artifact_id}`,
          exact: true,
        }),
      ).toHaveAttribute(
        "href",
        `/artifacts/${encodeURIComponent(event.artifact_id)}`,
      );
    }
  }
}

test("desktop chart controls persist configuration and authorize exactly one mock write", async ({
  page,
  obj06Installation,
}, info) => {
  expect(obj06Installation.stateDirectory).toContain("obj06");
  const observed = observeBrowser(page);
  await page.goto("/");
  await checkPage(page, /^Organization chart/u, "Marketing agent organization");
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(43);
  await capture(page, info, "desktop-organization");
  await selectAgent(page, WRITE_INSTANCE);
  let editor = await openConfiguration(page);
  await editor.getByLabel("Deployment enabled", { exact: true }).uncheck();
  await editor.getByRole("button", { name: "Save configuration" }).click();
  await expect(
    page.getByText("Configuration saved.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Dry runs are unavailable while this deployment is disabled.",
      { exact: true },
    ),
  ).toBeVisible();
  await page
    .locator("#agent-inspector")
    .getByRole("button", { name: /^Close details for/u })
    .click();
  await selectAgent(page, WRITE_INSTANCE);
  editor = await openConfiguration(page);
  await expect(
    editor.getByLabel("Deployment enabled", { exact: true }),
  ).not.toBeChecked();
  await editor.getByLabel("Deployment enabled", { exact: true }).check();
  await editor.getByRole("button", { name: "Save configuration" }).click();
  await expect(
    page.getByText("Configuration saved.", { exact: true }),
  ).toBeVisible();
  const dryRun = page.getByRole("form", { name: "Manual dry-run input" });
  await expect(dryRun).toBeVisible();
  await dryRun.getByLabel(/^Request id/u).fill("request.obj06.write");
  await dryRun.getByLabel(/^Source content/u).fill(
    JSON.stringify({
      version: 1,
      command: { contact_ref: "contact.local", list_ref: "list.local" },
    }),
  );
  await dryRun.getByRole("radio", { name: /^Mock execution /u }).check();
  await dryRun
    .getByRole("button", { name: "Run with mocks", exact: true })
    .click();
  const runId = await acceptedRunId(page);
  await checkPage(page, /^Run timeline/u, "Run timeline");
  const pending = await waitForRun(page, runId, "awaiting_approval");
  expect(pending.configuration_revision).toBe(3);
  expect(pending.pending_approvals).toHaveLength(1);
  expect(pending.external_actions).toHaveLength(1);
  expect(pending.execution_control.tool_calls).toBe(0);
  expect(pending.external_actions[0]?.receipt_id).toBeNull();
  await page.getByRole("button", { name: "Refresh now", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Review approval", exact: true }),
  ).toHaveAttribute("href", `/approvals?run_id=${encodeURIComponent(runId)}`);
  await capture(page, info, "desktop-awaiting-approval");
  await page
    .getByRole("link", { name: "Review approval", exact: true })
    .click();
  await expect(page).toHaveURL(
    new RegExp(`/approvals\\?run_id=${encodeURIComponent(runId)}$`, "u"),
  );
  await checkPage(page, /^Approvals/u, "Approval queue");
  const approvalId = pending.pending_approvals[0]?.id;
  if (approvalId === undefined) throw new Error("Missing approval identity");
  await expect(
    page.getByRole("button", { name: /^Review approval /u }),
  ).toHaveCount(1);
  await page
    .getByRole("button", { name: `Review approval ${approvalId}`, exact: true })
    .click();
  const review = page.locator("#approval-review-panel");
  await review.getByRole("button", { name: "Approve", exact: true }).click();
  const confirmation = page.getByRole("dialog", {
    name: "Approve exact action?",
    exact: true,
  });
  await expect(confirmation).toBeVisible();
  await expect(confirmation).toContainText("newsletter.subscribe");
  await page.keyboard.press("Escape");
  await expect(confirmation).not.toBeVisible();
  const unchanged = await readJson<RunSnapshot>(
    page,
    `/api/v1/runs/${encodeURIComponent(runId)}`,
  );
  expect(unchanged.state).toBe("awaiting_approval");
  expect(unchanged.execution_control.tool_calls).toBe(0);
  expect(
    observed.mutations.filter(({ path }) =>
      path.startsWith("/api/v1/approvals/"),
    ),
  ).toHaveLength(0);
  await review.getByRole("button", { name: "Approve", exact: true }).click();
  await confirmation
    .getByRole("button", { name: "Approve exact action", exact: true })
    .click();
  await expect(confirmation).not.toBeVisible();
  await review
    .getByRole("link", { name: "Open sequence-ordered timeline" })
    .click();
  const completed = await waitForRun(page, runId, "completed");
  await page.getByRole("button", { name: "Refresh now", exact: true }).click();
  expect(completed.execution_control.tool_calls).toBe(1);
  expect(completed.external_actions[0]?.state).toBe("succeeded");
  expect(completed.external_actions[0]?.receipt_id).toBeTruthy();
  expect(completed.external_actions[0]?.delivery_attempt_count).toBe(1);
  expect(completed.artifact_summaries).toHaveLength(0);
  expect(completed.id).toBe(runId);
  await openPersistedRunFromIndex(page, completed, info, "desktop");
  await assertCompletePublicTimeline(page, completed, approvalId);
  await capture(page, info, "desktop-complete-timeline");
  await expect(
    page
      .getByText("No real external delivery occurred", { exact: true })
      .first(),
  ).toBeVisible();
  await expect(page.locator(".run-snapshot__action-list")).toContainText(
    completed.external_actions[0]?.receipt_id ?? "missing-receipt",
  );
  await page.locator(".run-snapshot__action-list").scrollIntoViewIfNeeded();
  await capture(page, info, "desktop-completed-receipt");
  await page.getByRole("link", { name: "Org chart", exact: true }).click();
  await selectAgent(page, WRITE_INSTANCE);
  await expect(
    page.locator(
      `[data-instance-id="${WRITE_INSTANCE}"] [data-runtime-status]`,
    ),
  ).toHaveAttribute("data-runtime-status", "completed");
  await capture(page, info, "desktop-chart-completed");
  expect(
    observed.mutations.filter(({ method }) => method === "PATCH"),
  ).toHaveLength(2);
  expect(
    observed.mutations.filter(({ path }) => path.endsWith("/dry-runs")),
  ).toHaveLength(1);
  expect(
    observed.mutations.filter(({ path }) =>
      path.startsWith("/api/v1/approvals/"),
    ),
  ).toHaveLength(1);
  expect(observed.errors).toEqual([]);
});

test("mobile chart preserves dirty configuration while a real read run produces a browsable artifact", async ({
  page,
  obj06Installation,
}, info) => {
  expect(obj06Installation.evidenceDirectory).toContain("obj06");
  await page.setViewportSize({ width: 390, height: 844 });
  const observed = observeBrowser(page);
  await page.goto("/");
  await checkPage(page, /^Organization chart/u, "Marketing agent organization");
  await expect(
    page.locator('.chart-workspace[data-hierarchy-view="tree"]'),
  ).toBeVisible();
  await selectAgent(page, READ_INSTANCE);
  const editor = await openConfiguration(page);
  await editor
    .getByLabel("Variant label", { exact: true })
    .fill("Unsaved operator note");
  const dryRun = page.getByRole("form", { name: "Manual dry-run input" });
  await dryRun.getByLabel(/^Request id/u).fill("request.obj06.read");
  await dryRun
    .getByLabel(/^Source content/u)
    .fill("OBJ06 private source for a local-only draft");
  await dryRun
    .getByRole("button", { name: "Create dry run", exact: true })
    .click();
  const discard = page.getByRole("alertdialog", {
    name: "Discard configuration changes?",
    exact: true,
  });
  await expect(discard).toBeVisible();
  await discard
    .getByRole("button", { name: "Keep editing", exact: true })
    .click();
  await expect(editor.getByLabel("Variant label", { exact: true })).toHaveValue(
    "Unsaved operator note",
  );
  await expect(dryRun.getByLabel(/^Source content/u)).toHaveValue("");
  const accepted = page.getByRole("link", {
    name: "Open accepted run resource",
    exact: true,
  });
  const runHref = await accepted.getAttribute("href");
  expect(runHref).toMatch(/^\/runs\/run\./u);
  await capture(page, info, "mobile-accepted-dirty-configuration");
  await accepted.click();
  await expect(discard).toBeVisible();
  await discard
    .getByRole("button", { name: "Discard changes", exact: true })
    .click();
  const runId = await acceptedRunId(page);
  expect(new URL(page.url()).pathname).toBe(runHref);
  const completed = await waitForRun(page, runId, "completed");
  await page.getByRole("button", { name: "Refresh now", exact: true }).click();
  expect(completed.configuration_revision).toBe(1);
  expect(completed.execution_control.model_calls).toBe(1);
  expect(completed.execution_control.tool_calls).toBe(0);
  expect(completed.external_actions).toHaveLength(0);
  expect(completed.pending_approvals).toHaveLength(0);
  expect(completed.artifact_summaries).toHaveLength(1);
  expect(completed.id).toBe(runId);
  await openPersistedRunFromIndex(page, completed, info, "mobile");
  await assertCompletePublicTimeline(page, completed);
  await capture(page, info, "mobile-complete-timeline");
  const artifactId = completed.artifact_summaries[0]?.id;
  if (artifactId === undefined) throw new Error("Missing artifact identity");
  await page
    .locator(
      `.run-artifacts__list a[href="/artifacts/${encodeURIComponent(artifactId)}"]`,
    )
    .click();
  await checkPage(page, /^Artifact viewer/u, "Artifact viewer");
  expect(new URL(page.url()).pathname).toBe(`/artifacts/${artifactId}`);
  await expect(
    page.getByRole("heading", {
      name: "Schema, digest & producer",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Providers", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "Authorized payload (bounded view)",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Providers", exact: true }),
  ).toContainText("mock v1");
  await expect(
    page.getByRole("region", {
      name: "Authorized redacted artifact payload",
      exact: true,
    }),
  ).toContainText(
    "LinkedIn post draft; offline draft/report, not an external action.",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBe(true);
  const persistedBrowserState = await page.evaluate(() =>
    JSON.stringify({
      local: Object.fromEntries(
        Object.keys(localStorage).map((key) => [
          key,
          localStorage.getItem(key),
        ]),
      ),
      session: Object.fromEntries(
        Object.keys(sessionStorage).map((key) => [
          key,
          sessionStorage.getItem(key),
        ]),
      ),
    }),
  );
  expect(persistedBrowserState).not.toMatch(
    /private source|Unsaved operator note|csrfToken/u,
  );
  await capture(page, info, "mobile-artifact");
  await page
    .getByRole("region", {
      name: "Authorized redacted artifact payload",
      exact: true,
    })
    .scrollIntoViewIfNeeded();
  await capture(page, info, "mobile-artifact-content");
  expect(
    observed.mutations.filter(({ method }) => method === "PATCH"),
  ).toHaveLength(0);
  expect(
    observed.mutations.filter(({ path }) => path.endsWith("/dry-runs")),
  ).toHaveLength(1);
  expect(observed.errors).toEqual([]);
});

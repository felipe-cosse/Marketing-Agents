// Real fresh SQLite/API/worker journeys. All mutations use rendered controls;
// native same-origin GETs only observe the authoritative persisted outcome.
import { expect, test, type Page, type TestInfo } from "./obj-06-fixtures";

const WRITE_INSTANCE = "inst.email.newsletter.newsletter-subscriber.01";
const READ_INSTANCE = "inst.social-media.new-content.linkedin-post-drafter.01";

interface RunSnapshot {
  readonly id: string;
  readonly state: string;
  readonly configuration_revision: number;
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

async function assertSequenceOrderedTimeline(page: Page, runId: string) {
  const timeline = await readJson<{
    items: readonly { sequence: number }[];
    next_cursor: string | null;
  }>(page, `/api/v1/runs/${encodeURIComponent(runId)}/timeline?limit=100`);
  expect(timeline.next_cursor).toBeNull();
  const sequence = timeline.items.map((item) => item.sequence);
  expect(sequence.length).toBeGreaterThan(3);
  expect(sequence).toEqual([...new Set(sequence)].sort((a, b) => a - b));
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
  await expect(
    page
      .getByText("No real external delivery occurred", { exact: true })
      .first(),
  ).toBeVisible();
  await expect(page.locator(".run-snapshot__action-list")).toContainText(
    completed.external_actions[0]?.receipt_id ?? "missing-receipt",
  );
  await assertSequenceOrderedTimeline(page, runId);
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
  await assertSequenceOrderedTimeline(page, runId);
  await page
    .locator('.run-artifacts__list a[href^="/artifacts/"]')
    .first()
    .click();
  await checkPage(page, /^Artifact viewer/u, "Artifact viewer");
  const artifactId = completed.artifact_summaries[0]?.id;
  if (artifactId === undefined) throw new Error("Missing artifact identity");
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

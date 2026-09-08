// DEL-05 verifies the existing production stack; it starts no fixture API/server.
/* global window, document */
import "./require-pinned-node.mjs";

import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const args = process.argv.slice(2);
assert.equal(
  args.length,
  2,
  "Usage: run-del-05-e2e.mjs --origin <loopback-origin>",
);
assert.equal(args[0], "--origin");
const origin = new URL(args[1]);
assert.equal(origin.protocol, "http:");
assert.ok(["127.0.0.1", "localhost"].includes(origin.hostname));
assert.equal(
  origin.username + origin.password + origin.search + origin.hash,
  "",
);
assert.equal(origin.pathname, "/");
process.env.PLAYWRIGHT_BROWSERS_PATH ??= "0";
const { chromium, expect } = await import("@playwright/test");
const evidence = await mkdtemp(
  join(tmpdir(), "marketing-agents-del05-browser-"),
);
const browser = await chromium.launch();
const failures = [];
const warnings = [];
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  serviceWorkers: "block",
});

try {
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== origin.origin) {
      failures.push("nonlocal_request_blocked");
      await route.abort("blockedbyclient");
      return;
    }
    await route.continue();
  });
  await context.routeWebSocket(/.*/, (socket) => {
    failures.push("unexpected_websocket_blocked");
    socket.close();
  });
  const page = await context.newPage();
  page.on("pageerror", () => failures.push("uncaught_page_error"));
  page.on("console", (message) => {
    if (message.type() === "error") failures.push("browser_console_error");
    if (message.type() === "warning") warnings.push("browser_console_warning");
  });
  const response = await page.goto(origin.origin, { waitUntil: "networkidle" });
  assert.equal(response?.status(), 200);
  assert.equal(new URL(page.url()).origin, origin.origin);
  await expect(page).toHaveTitle("Organization chart | Marketing Agents");
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(43);
  await expect(page.locator('[data-node-kind="root"]')).toHaveCount(1);
  await expect(page.locator('[data-node-kind="control-plane"]')).toContainText(
    "Marketing Orchestrator",
  );
  await expect(page.locator("vite-error-overlay")).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText("Internal Server Error");
  await page.screenshot({ path: join(evidence, "desktop.png") });

  const viewport = page.getByTestId("org-chart-viewport");
  const initialZoom = await viewport.getAttribute("data-viewport-zoom");
  await page.getByRole("button", { name: "Zoom in", exact: true }).click();
  await expect(viewport).not.toHaveAttribute("data-viewport-zoom", initialZoom);
  await page
    .getByRole("button", { name: "Fit hierarchy", exact: true })
    .click();
  const search = page.getByRole("searchbox", { name: "Search agents" });
  await search.fill("Newsletter Subscriber");
  await expect
    .poll(() => page.locator('[data-node-kind="instance"]').count())
    .toBe(1);
  await search.fill("");
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(43);

  await page.setViewportSize({ width: 426, height: 923 });
  const tree = page.getByRole("tree", {
    name: "Marketing Agents organization tree",
  });
  await expect(tree).toBeVisible();
  const functions = tree.locator(
    '[role="treeitem"][data-node-kind="function"]',
  );
  await expect(functions).toHaveCount(12);
  for (const item of await functions.all()) {
    if ((await item.getAttribute("aria-expanded")) !== "true")
      await item.click();
  }
  await expect(tree.locator('[data-node-kind="instance"]')).toHaveCount(43);
  const firstFunction = functions.first();
  await firstFunction.click();
  await expect(firstFunction).toHaveAttribute("aria-expanded", "false");
  await firstFunction.click();
  await expect(firstFunction).toHaveAttribute("aria-expanded", "true");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: join(evidence, "mobile.png") });
  const horizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth + 1,
  );
  assert.equal(
    horizontalOverflow,
    false,
    "mobile document must not overflow horizontally",
  );
  assert.deepEqual(
    failures,
    [],
    "production browser must remain healthy and same-origin",
  );
  console.log(
    JSON.stringify({
      ok: true,
      checks: [
        "identity",
        "nonblank",
        "no-overlay",
        "console",
        "same-origin",
        "zoom",
        "search",
        "mobile-tree",
      ],
      warnings: warnings.length,
      screenshots: evidence,
    }),
  );
} finally {
  await context.close();
  await browser.close();
}

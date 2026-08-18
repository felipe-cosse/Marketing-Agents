// Requirement SAFE-11: Playwright-style routing aborts non-loopback requests and exposes a zero-attempt assertion.
import assert from "node:assert/strict";
import test from "node:test";

import { installPlaywrightNetworkGuard, isAllowedBrowserUrl } from "../../scripts/browser-network-policy.mjs";

function fakePage() {
  return {
    handler: undefined,
    async route(pattern, handler) {
      assert.equal(pattern, "**/*");
      this.handler = handler;
    },
  };
}

function fakeRoute(url) {
  return {
    outcome: undefined,
    request() {
      return { url: () => url };
    },
    async abort() {
      this.outcome = "aborted";
    },
    async continue() {
      this.outcome = "continued";
    },
  };
}

test("SAFE-11 browser guard permits loopback and aborts external URLs", async () => {
  const page = fakePage();
  const ledger = await installPlaywrightNetworkGuard(page);
  const local = fakeRoute("http://127.0.0.1:4173/assets/app.js");
  const external = fakeRoute("https://example.invalid/tracker.js");
  await page.handler(local);
  await page.handler(external);
  assert.equal(local.outcome, "continued");
  assert.equal(external.outcome, "aborted");
  assert.deepEqual(ledger.blocked, ["https://example.invalid/tracker.js"]);
  assert.throws(() => ledger.assertNoExternalAttempts(), /1 external request/);
});

test("SAFE-11 browser URL policy rejects non-network custom schemes", () => {
  assert.equal(isAllowedBrowserUrl("data:text/plain,safe"), true);
  assert.equal(isAllowedBrowserUrl("http://localhost:5173/"), true);
  assert.equal(isAllowedBrowserUrl("ftp://127.0.0.1/file"), false);
  assert.equal(isAllowedBrowserUrl("https://192.0.2.1/"), false);
});

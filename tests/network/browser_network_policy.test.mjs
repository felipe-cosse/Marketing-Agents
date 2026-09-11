// SAFE-11 / DEL-07: fake transports make negative URL controls non-networking.
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  installPlaywrightNetworkGuard,
  isAllowedBrowserUrl,
} from "../../scripts/browser-network-policy.mjs";

function fakeSurface() {
  return {
    handler: undefined,
    async route(pattern, handler) {
      assert.equal(pattern, "**/*");
      this.handler = handler;
    },
    async routeWebSocket(pattern, handler) {
      assert.equal(pattern, "**/*");
      this.websocketHandler = handler;
    },
  };
}

function fakeContext() {
  const listeners = new Map();
  return {
    ...fakeSurface(),
    pages: () => [],
    on(event, handler) {
      listeners.set(event, handler);
    },
    emit(event, value) {
      listeners.get(event)?.(value);
    },
  };
}

function fakeRoute(url, status = 200) {
  const request = { url: () => url };
  return {
    outcome: undefined,
    request() {
      return request;
    },
    async abort() {
      this.outcome = "aborted";
    },
    async fallback() {
      this.outcome = "fallback";
    },
    async fetch(options) {
      this.fetchOptions = options;
      return { status: () => status };
    },
    async fulfill() {
      this.outcome = "fulfilled";
    },
  };
}

test("DEL-07 exact-origin routing aborts before delegates and deduplicates request audit", async () => {
  const context = fakeContext();
  const ledger = await installPlaywrightNetworkGuard(context);
  const local = fakeRoute("http://127.0.0.1:4173/assets/app.js");
  const external = fakeRoute("https://example.invalid/tracker.js");
  context.emit("request", external.request());
  await context.handler(local);
  await context.handler(external);
  assert.equal(local.outcome, "fulfilled");
  assert.equal(local.fetchOptions.maxRedirects, 0);
  assert.equal(external.outcome, "aborted");
  assert.deepEqual(ledger.blocked, ["http"]);
  assert.throws(
    () => ledger.assertNoExternalAttempts(),
    /1 unapproved network request/,
  );
  assert.equal(
    await installPlaywrightNetworkGuard({ context: () => context }),
    ledger,
  );
  await assert.rejects(
    () => installPlaywrightNetworkGuard(context, "http://127.0.0.1:9999"),
    /cannot change/,
  );
});

test("DEL-07 delegated URL overrides are rejected before the transport", async () => {
  for (const method of ["fetch", "continue", "fallback"]) {
    const context = fakeContext();
    const ledger = await installPlaywrightNetworkGuard(context);
    const page = fakeSurface();
    context.emit("page", page);
    await page.route("**/*", async (route) => {
      try {
        await route[method]({ url: "http://127.0.0.1:9999/unapproved" });
      } catch {
        // Expected denial remains visible through the independent ledger.
      }
    });
    const route = fakeRoute("http://127.0.0.1:4173/approved");
    await page.handler(route);
    assert.equal(route.outcome, "aborted", method);
    assert.equal(route.fetchOptions, undefined, method);
    assert.throws(() => ledger.assertNoExternalAttempts(), /1 unapproved/);
  }
});

test("DEL-07 pass-through never follows redirects and preserves 304 responses", async () => {
  for (const status of [301, 302, 303, 307, 308]) {
    const context = fakeContext();
    const ledger = await installPlaywrightNetworkGuard(context);
    const route = fakeRoute("http://127.0.0.1:4173/approved", status);
    await context.handler(route);
    assert.equal(route.fetchOptions.maxRedirects, 0);
    assert.equal(route.outcome, "aborted");
    assert.throws(() => ledger.assertNoExternalAttempts(), /1 unapproved/);
  }
  const context = fakeContext();
  const ledger = await installPlaywrightNetworkGuard(context);
  const cached = fakeRoute("http://127.0.0.1:4173/cached", 304);
  await context.handler(cached);
  assert.equal(cached.outcome, "fulfilled");
  ledger.assertNoExternalAttempts();
});

test("DEL-07 mocked redirect responses cannot escape the browser guard", async () => {
  const context = fakeContext();
  const ledger = await installPlaywrightNetworkGuard(context);
  const page = fakeSurface();
  context.emit("page", page);
  await page.route("**/*", (route) =>
    route.fulfill({
      status: 302,
      headers: { location: "http://127.0.0.1:9999/" },
    }),
  );
  const route = fakeRoute("http://127.0.0.1:4173/approved");
  await page.handler(route);
  assert.equal(route.outcome, "aborted");
  assert.throws(() => ledger.assertNoExternalAttempts(), /1 unapproved/);
});

test("SAFE-11 browser URL policy rejects non-network custom schemes", () => {
  assert.equal(isAllowedBrowserUrl("data:text/plain,safe"), true);
  assert.equal(isAllowedBrowserUrl("http://localhost:5173/"), false);
  assert.equal(isAllowedBrowserUrl("ftp://127.0.0.1/file"), false);
  assert.equal(isAllowedBrowserUrl("https://192.0.2.1/"), false);
});

test("DEL-07 rejects aliases, credentialed URLs, other ports, and fake loopback hostnames", () => {
  for (const url of [
    "http://127.0.0.1:4173/path?q=local",
    "ws://127.0.0.1:4173/socket",
    "about:blank",
    "data:text/plain,in-memory",
    "blob:http://127.0.0.1:4173/fixture",
  ])
    assert.equal(isAllowedBrowserUrl(url), true, url);
  for (const url of [
    "http://127.evil:4173/",
    "http://127.evil.invalid:4173/",
    "http://localhost:4173/",
    "http://127.0.0.1:4174/",
    "https://127.0.0.1:4173/",
    "ws://127.0.0.1:4174/",
    "wss://127.0.0.1:4173/",
    "http://user:secret@127.0.0.1:4173/",
    "blob:https://example.invalid/fixture",
    "file:///tmp/fixture",
    "not a url",
  ])
    assert.equal(isAllowedBrowserUrl(url), false, url);
  assert.equal(
    isAllowedBrowserUrl("http://[::1]:4173/", "http://[::1]:4173"),
    true,
  );
  assert.equal(
    isAllowedBrowserUrl("http://127.evil:4173/", "http://127.evil:4173"),
    false,
  );
});

test("DEL-07 future pages cannot bypass context policy with their own route handlers", async () => {
  const context = fakeContext();
  const ledger = await installPlaywrightNetworkGuard(context);
  const page = fakeSurface();
  context.emit("page", page);
  let mockCalls = 0;
  await page.route("**/*", async () => {
    mockCalls += 1;
  });
  const request = fakeRoute(
    "https://user:secret@example.invalid/?private=value",
  );
  context.emit("request", request.request());
  await page.handler(request);
  assert.equal(request.outcome, "aborted");
  assert.equal(mockCalls, 0);
  assert.deepEqual(ledger.blocked, ["http"]);
  // Even a request already fulfilled elsewhere is independently observable.
  context.emit("request", { url: () => "http://127.0.0.1:9999/" });
  assert.throws(() => ledger.assertNoExternalAttempts(), /2 unapproved/);
});

test("DEL-07 WebSocket policy never connects an unapproved origin", async () => {
  const context = fakeContext();
  const ledger = await installPlaywrightNetworkGuard(context);
  const socket = (url) => ({
    url: () => url,
    closed: false,
    connected: false,
    close() {
      this.closed = true;
    },
    connectToServer() {
      this.connected = true;
    },
  });
  const local = socket("ws://127.0.0.1:4173/socket");
  const external = socket("ws://127.0.0.1:9999/socket");
  context.websocketHandler(local);
  context.websocketHandler(external);
  assert.equal(local.connected, true);
  assert.equal(external.connected, false);
  assert.equal(external.closed, true);
  assert.deepEqual(ledger.blocked, ["websocket"]);
  assert.throws(() => ledger.assertNoExternalAttempts(), /1 unapproved/);
});

test("DEL-07 every committed browser journey imports the automatic guarded fixture", () => {
  const root = fileURLToPath(new URL("../../apps/web/e2e", import.meta.url));
  const specs = readdirSync(root).filter((name) => name.endsWith(".spec.ts"));
  assert.equal(specs.length, 15);
  const guardedImport =
    /import\s*\{[^}]*\btest\b[^}]*\}\s*from\s*["']\.\/fixtures["']/;
  for (const spec of specs) {
    const source = readFileSync(join(root, spec), "utf8");
    assert.match(source, guardedImport, spec);
    assert.doesNotMatch(source, /from\s*["']@playwright\/test["']/, spec);
    assert.equal(
      guardedImport.test(
        source.replaceAll('"./fixtures"', '"@playwright/test"'),
      ),
      false,
    );
  }
  const configuration = readFileSync(
    new URL("../../apps/web/playwright.config.ts", import.meta.url),
    "utf8",
  );
  assert.match(configuration, /serviceWorkers:\s*["']block["']/);
});

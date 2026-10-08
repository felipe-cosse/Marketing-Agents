// AC-15: all negative transports below are in-memory delegates, never sockets.
import assert from "node:assert/strict";
import test from "node:test";

import {
  installPlaywrightIsolation,
  installPlaywrightRequestGuard,
} from "../../scripts/playwright-network-isolation.mjs";

const origin = "http://127.0.0.1:4173";

function requestContext(status = 200) {
  const calls = [];
  const response = { status: () => status, async dispose() {} };
  const context = {
    calls,
    async fetch(url, options) {
      calls.push({ url, options });
      return response;
    },
  };
  // These mirror the pinned public Playwright convenience methods, all of
  // which delegate dynamically through fetch (actual browser canaries below).
  for (const method of ["get", "head", "post", "put", "patch", "delete"])
    context[method] = function (url, options) {
      return this.fetch(url, { ...options, method: method.toUpperCase() });
    };
  return context;
}

function setup() {
  const contexts = [];
  const factoryContexts = [];
  const guarded = [];
  const browser = {
    pageCalls: 0,
    async newContext(options) {
      const child = {
        options,
        request: requestContext(),
        async close() {},
      };
      contexts.push(child);
      return child;
    },
    async newPage() {
      this.pageCalls += 1;
    },
  };
  const requestFactory = {
    async newContext(options) {
      const child = requestContext();
      child.options = options;
      factoryContexts.push(child);
      return child;
    },
  };
  const context = { request: requestContext() };
  const request = requestContext();
  const install = () =>
    installPlaywrightIsolation({
      browser,
      context,
      request,
      requestFactory,
      allowedOrigin: origin,
      async installContextGuard(value, configuredOrigin) {
        assert.equal(configuredOrigin, origin);
        guarded.push(value);
        return { blocked: [], assertNoExternalAttempts() {} };
      },
    });
  return {
    browser,
    requestFactory,
    context,
    request,
    contexts,
    factoryContexts,
    guarded,
    install,
  };
}

test("AC-15 public API methods deny exact-origin violations before all delegates", async () => {
  const context = requestContext();
  const blocked = [];
  installPlaywrightRequestGuard(context, origin, (kind) => blocked.push(kind));
  const urls = [
    "https://example.invalid/?sensitive=redacted",
    "http://127.0.0.1:9999/",
    "http://localhost:4173/",
    "https://127.0.0.1:4173/",
    "http://127.evil.invalid:4173/",
    "http://user:secret@127.0.0.1:4173/",
    "file:///private/tmp/secret",
    "data:text/plain,bytes",
    "//example.invalid/path",
  ];
  const methods = ["fetch", "get", "head", "post", "put", "patch", "delete"];
  for (const method of methods)
    for (const url of urls)
      await assert.rejects(context[method](url), /blocked by test policy/);
  assert.deepEqual(context.calls, []);
  assert.deepEqual(
    blocked,
    Array(methods.length * urls.length).fill("api-request"),
  );
});

test("AC-15 API relative URLs are pinned and caller redirect options cannot escape", async () => {
  const context = requestContext();
  installPlaywrightRequestGuard(context, origin, () => assert.fail("denied"));
  await context.post("/allowed?local=1", { data: "body", maxRedirects: 20 });
  assert.deepEqual(context.calls, [
    {
      url: `${origin}/allowed?local=1`,
      options: { method: "POST", data: "body", maxRedirects: 0 },
    },
  ]);
  const request = { url: () => `${origin}/real-request` };
  await context.fetch(request);
  assert.equal(context.calls[1].url, request);
  assert.equal(context.calls[1].options.maxRedirects, 0);
});

test("AC-15 hidden API URL overrides and Request URLs fail before delegate", async () => {
  const context = requestContext();
  const blocked = [];
  installPlaywrightRequestGuard(context, origin, (kind) => blocked.push(kind));
  for (const options of [{ url: "http://127.0.0.1:9999/" }, { request: {} }])
    await assert.rejects(context.get("/local", options), /blocked/);
  await assert.rejects(
    context.fetch({ url: () => "http://127.0.0.1:9999/" }),
    /blocked/,
  );
  await assert.rejects(context.fetch({}), /blocked/);
  assert.equal(context.calls.length, 0);
  assert.equal(blocked.length, 4);
});

test("AC-15 every API redirect fails closed while 304 remains valid", async () => {
  for (const status of [301, 302, 303, 307, 308, 304]) {
    const context = requestContext(status);
    const blocked = [];
    installPlaywrightRequestGuard(context, origin, (kind) =>
      blocked.push(kind),
    );
    if (status === 304) {
      assert.equal((await context.get("/cached")).status(), 304);
      assert.deepEqual(blocked, []);
    } else {
      await assert.rejects(context.get("/redirect"), /redirect blocked/);
      assert.deepEqual(blocked, ["api-redirect"]);
    }
    assert.equal(context.calls.length, 1);
    assert.equal(context.calls[0].options.maxRedirects, 0);
  }
});

test("AC-15 API redirect denial is retained when response disposal rejects", async () => {
  const context = {
    async fetch() {
      return {
        status: () => 302,
        async dispose() {
          throw new Error("disposed");
        },
      };
    },
  };
  const blocked = [];
  installPlaywrightRequestGuard(context, origin, (kind) => blocked.push(kind));
  await assert.rejects(context.fetch("/redirect"), /disposed/);
  assert.deepEqual(blocked, ["api-redirect"]);
});

test("AC-15 default and future context/API factory surfaces all retain caught attempts", async () => {
  const fixture = setup();
  const isolation = await fixture.install();
  const child = await fixture.browser.newContext();
  const api = await fixture.requestFactory.newContext();
  assert.deepEqual(fixture.guarded, [fixture.context, child]);
  assert.equal(child.options.serviceWorkers, "block");
  assert.deepEqual(api.options, { baseURL: origin, maxRedirects: 0 });
  for (const request of [
    fixture.context.request,
    fixture.request,
    child.request,
    api,
  ]) {
    await assert.rejects(request.get("http://127.0.0.1:9999/"), /blocked/);
    assert.equal(request.calls.length, 0);
  }
  await assert.rejects(isolation.assertNoExternalAttempts(), /4 unapproved/);
  isolation.restore();
  // Restoring factories cannot turn already created request contexts unsafe.
  await assert.rejects(child.request.get("http://127.0.0.1:9999/"), /blocked/);
  await assert.rejects(isolation.assertNoExternalAttempts(), /5 unapproved/);
});

test("AC-15 direct browser.newPage attempts are denied and counted even when caught", async () => {
  const fixture = setup();
  const originalPage = fixture.browser.newPage;
  const originalContext = fixture.browser.newContext;
  const originalRequest = fixture.requestFactory.newContext;
  const isolation = await fixture.install();
  await assert.rejects(fixture.browser.newPage(), /guarded context/);
  assert.equal(fixture.browser.pageCalls, 0);
  assert.equal(fixture.contexts.length, 0);
  await assert.rejects(isolation.assertNoExternalAttempts(), /1 unapproved/);
  isolation.restore();
  assert.equal(fixture.browser.newPage, originalPage);
  assert.equal(fixture.browser.newContext, originalContext);
  assert.equal(fixture.requestFactory.newContext, originalRequest);
});

test("AC-15 off-origin, proxy and service-worker factory options fail before creation", async () => {
  const fixture = setup();
  const isolation = await fixture.install();
  for (const options of [
    { baseURL: "http://127.0.0.1:9999" },
    { baseURL: "http://user:secret@127.0.0.1:4173" },
    { proxy: { server: "http://127.0.0.1:9999" } },
    { serviceWorkers: "allow" },
  ]) {
    await assert.rejects(
      fixture.browser.newContext(options),
      /options blocked/,
    );
    await assert.rejects(
      fixture.requestFactory.newContext(options),
      /options blocked/,
    );
  }
  assert.equal(fixture.contexts.length, 0);
  assert.equal(fixture.factoryContexts.length, 0);
  await assert.rejects(isolation.assertNoExternalAttempts(), /8 unapproved/);
});

test("AC-15 custom native guard assertions remain mandatory and asynchronous", async () => {
  const fixture = setup();
  let checked = false;
  const isolation = await installPlaywrightIsolation({
    browser: fixture.browser,
    context: fixture.context,
    allowedOrigin: origin,
    async installContextGuard() {
      return {
        blocked: ["redirect"],
        async assertNoUnexpectedAttempts() {
          await Promise.resolve();
          checked = true;
          throw new Error("native guard denied");
        },
      };
    },
  });
  await assert.rejects(
    isolation.assertNoExternalAttempts(),
    /native guard denied/,
  );
  assert.equal(checked, true);
  isolation.restore();
});

test("AC-15 a failed child guard closes its context and leaves durable denial", async () => {
  const fixture = setup();
  let closes = 0;
  const isolation = await installPlaywrightIsolation({
    browser: fixture.browser,
    context: fixture.context,
    allowedOrigin: origin,
    async installContextGuard(value) {
      if (value !== fixture.context) {
        value.close = async () => {
          closes += 1;
        };
        throw new Error("guard setup failed");
      }
      return { blocked: [], assertNoExternalAttempts() {} };
    },
  });
  await assert.rejects(fixture.browser.newContext(), /guard setup failed/);
  assert.equal(closes, 1);
  await assert.rejects(isolation.assertNoExternalAttempts(), /1 unapproved/);
  isolation.restore();
});

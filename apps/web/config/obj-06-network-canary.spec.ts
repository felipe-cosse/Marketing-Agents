// OBJ-06 negative controls target only owned loopback servers or fake delegates.
import { createServer, type Server, type IncomingHttpHeaders } from "node:http";
import {
  test,
  expect,
  type BrowserContext,
  type Route,
  type Request,
} from "@playwright/test";
import {
  Obj06CancellationEvidence,
  installObj06NativeNetworkGuard,
  obj06AllowedUrl,
} from "./obj-06-native-network";

let approved: Server;
let tripwire: Server;
let origin: string;
let otherOrigin: string;
let forbiddenHits = 0;
let headers: IncomingHttpHeaders | undefined;

async function listen(server: Server): Promise<string> {
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (address === null || typeof address === "string")
    throw new Error("OBJ-06 missing owned listener");
  return `http://127.0.0.1:${String(address.port)}`;
}

test.beforeAll(async () => {
  tripwire = createServer((_request, response) => {
    forbiddenHits += 1;
    response.end("local tripwire");
  });
  tripwire.on("upgrade", (_request, socket) => {
    forbiddenHits += 1;
    socket.destroy();
  });
  otherOrigin = await listen(tripwire);
  approved = createServer((request, response) => {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    if (url.pathname === "/redirect") {
      response.writeHead(Number(url.searchParams.get("status") ?? "307"), {
        location:
          url.searchParams.get("same") === "1" ? "/destination" : otherOrigin,
      });
      response.end();
    } else if (url.pathname === "/destination") {
      forbiddenHits += 1;
      response.end("redirect tripwire");
    } else if (url.pathname === "/mutation") {
      headers = request.headers;
      response.setHeader("content-type", "application/json");
      response.end('{"ok":true}');
    } else {
      response.setHeader("content-type", "text/html");
      response.end(
        "<!doctype html><title>OBJ-06 transport canary</title><main>Local canary</main>",
      );
    }
  });
  origin = await listen(approved);
});
test.afterAll(async () => {
  for (const server of [approved, tripwire]) {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
});
test.beforeEach(() => {
  forbiddenHits = 0;
  headers = undefined;
});

test("OBJ-06 native forwarding retains Chromium-originated Fetch Metadata", async ({
  browser,
}) => {
  const context = await browser.newContext({ serviceWorkers: "block" });
  try {
    const guard = await installObj06NativeNetworkGuard(context, origin);
    const page = await context.newPage();
    await expect(page.route("**/*", () => undefined)).rejects.toThrow(
      "additional route handlers",
    );
    await expect(context.route("**/*", () => undefined)).rejects.toThrow(
      "additional route handlers",
    );
    await expect(context.unrouteAll()).rejects.toThrow(
      "additional route handlers",
    );
    await page.goto(origin);
    const status = await page.evaluate(
      async () =>
        (
          await fetch("/mutation", {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: "{}",
          })
        ).status,
    );
    expect(status).toBe(200);
    expect(headers?.origin).toBe(origin);
    expect(headers?.["sec-fetch-site"]).toBe("same-origin");
    expect(headers?.["sec-fetch-mode"]).toBe("cors");
    expect(forbiddenHits).toBe(0);
    await guard.assertNoUnexpectedAttempts();
  } finally {
    await context.close();
  }
});

test("OBJ-06 off-origin fetch, WebSocket and popup never reach local tripwire", async ({
  browser,
}) => {
  const context = await browser.newContext({ serviceWorkers: "block" });
  try {
    const guard = await installObj06NativeNetworkGuard(context, origin);
    const page = await context.newPage();
    await page.goto(origin);
    expect(
      await page.evaluate(async (url) => {
        try {
          await fetch(url);
          return false;
        } catch {
          return true;
        }
      }, otherOrigin),
    ).toBe(true);
    expect(
      await page.evaluate(
        (url) =>
          new Promise<boolean>((resolve) => {
            const socket = new WebSocket(url.replace("http:", "ws:"));
            socket.onopen = () => resolve(false);
            socket.onerror = () => resolve(true);
            socket.onclose = () => resolve(true);
          }),
        otherOrigin,
      ),
    ).toBe(true);
    const popupPromise = context.waitForEvent("page");
    await page.evaluate((url) => {
      window.open(url);
    }, otherOrigin);
    const popup = await popupPromise;
    await expect.poll(() => guard.blocked.length).toBe(3);
    await popup.close();
    expect(forbiddenHits).toBe(0);
    await expect(guard.assertNoUnexpectedAttempts()).rejects.toThrow(
      "blocked 3 unexpected requests",
    );
  } finally {
    await context.close();
  }
});

test("OBJ-06 rejects every redirect before same-origin or off-origin follow", async ({
  browser,
}) => {
  const context = await browser.newContext({ serviceWorkers: "block" });
  try {
    const guard = await installObj06NativeNetworkGuard(context, origin);
    const page = await context.newPage();
    for (const status of [301, 302, 303, 307, 308]) {
      for (const same of [0, 1]) {
        await page
          .goto(
            `${origin}/redirect?status=${String(status)}&same=${String(same)}`,
          )
          .catch(() => undefined);
      }
    }
    expect(guard.blocked).toEqual(Array<string>(10).fill("redirect"));
    expect(forbiddenHits).toBe(0);
    await expect(guard.assertNoUnexpectedAttempts()).rejects.toThrow(
      "blocked 10 unexpected requests",
    );
  } finally {
    await context.close();
  }
});

test("OBJ-06 popup and new-page first-navigation redirects cannot race interception", async ({
  browser,
}) => {
  const context = await browser.newContext({ serviceWorkers: "block" });
  try {
    const guard = await installObj06NativeNetworkGuard(context, origin);
    const page = await context.newPage();
    await page.goto(origin);
    const popupPromise = context.waitForEvent("page");
    await page.evaluate((url) => {
      window.open(url);
    }, `${origin}/redirect`);
    const popup = await popupPromise;
    await expect.poll(() => guard.blocked.length).toBe(1);
    await popup.close();
    const next = await context.newPage();
    await next.goto(`${origin}/redirect`).catch(() => undefined);
    // A popup without a bound frame is denied at the request boundary; an
    // explicit new page reaches the response boundary and denies the redirect.
    expect(guard.blocked).toEqual(["http", "redirect"]);
    expect(forbiddenHits).toBe(0);
    await expect(guard.assertNoUnexpectedAttempts()).rejects.toThrow(
      "blocked 2 unexpected requests",
    );
  } finally {
    await context.close();
  }
});

test("OBJ-06 nonlocal URL controls stop before in-memory delegates, with no OS transport", async () => {
  let handler: ((route: Route, request: Request) => Promise<void>) | undefined;
  let delegated = 0;
  let aborted = 0;
  const fakeContext = {
    newPage() {
      throw new Error("OBJ-06 fake page delegate must not run");
    },
    on() {
      return undefined;
    },
    pages: () => [],
    route(_pattern: string, value: typeof handler) {
      handler = value;
      return Promise.resolve();
    },
    routeWebSocket() {
      return Promise.resolve();
    },
  } as unknown as BrowserContext;
  const guard = await installObj06NativeNetworkGuard(
    fakeContext,
    "http://127.0.0.1:4173",
  );
  for (const url of [
    "https://example.invalid/",
    "http://127.evil.invalid/",
    "http://localhost:4173/",
    "http://127.0.0.1:4174/",
  ]) {
    expect(obj06AllowedUrl(url, "http://127.0.0.1:4173")).toBe(false);
    const request = { url: () => url } as Request;
    const route = {
      abort() {
        aborted += 1;
        return Promise.resolve();
      },
      continue() {
        delegated += 1;
        return Promise.resolve();
      },
    } as unknown as Route;
    if (handler === undefined)
      throw new Error("OBJ-06 route was not installed");
    await handler(route, request);
  }
  expect(delegated).toBe(0);
  expect(aborted).toBe(4);
  await expect(guard.assertNoUnexpectedAttempts()).rejects.toThrow(
    "blocked 4 unexpected requests",
  );
});

test("OBJ-06 only tolerates an invalid response interception with matching browser cancellation", async () => {
  const invalid = new Error(
    "cdpSession.send: Protocol error (Fetch.continueResponse): Invalid InterceptionId.",
  );
  const evidence = new Obj06CancellationEvidence();
  evidence.record({
    requestId: "cancelled",
    canceled: true,
    errorText: "net::ERR_ABORTED",
  });
  expect(await evidence.confirms("cancelled", invalid)).toBe(true);
  // Consumed evidence cannot excuse a later unrelated command failure.
  expect(await evidence.confirms("cancelled", invalid)).toBe(false);
  for (const event of [
    { requestId: "wrong-kind", canceled: false, errorText: "net::ERR_ABORTED" },
    { requestId: "wrong-kind", canceled: true, errorText: "net::ERR_FAILED" },
    {
      requestId: "different-request",
      canceled: true,
      errorText: "net::ERR_ABORTED",
    },
  ])
    evidence.record(event);
  expect(await evidence.confirms("wrong-kind", invalid)).toBe(false);
  expect(await evidence.confirms(undefined, invalid)).toBe(false);
  expect(
    await evidence.confirms("different-request", new Error("Target closed")),
  ).toBe(false);
  expect(
    await evidence.confirms(
      "different-request",
      new Error("Protocol error (Fetch.failRequest): Invalid InterceptionId."),
    ),
  ).toBe(false);
  setTimeout(
    () =>
      evidence.record({
        requestId: "late-event",
        canceled: true,
        errorText: "net::ERR_ABORTED",
      }),
    10,
  );
  expect(await evidence.confirms("late-event", invalid)).toBe(true);
});

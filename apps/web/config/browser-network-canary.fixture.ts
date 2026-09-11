import { createServer, type Server } from "node:http";
import { test, expect } from "../e2e/fixtures";

let server: Server;
let approvedServer: Server;
let tripwireOrigin: string;
let approvedOrigin: string;
let requests = 0;

test.beforeAll(async () => {
  server = createServer((_request, response) => {
    requests += 1;
    response.setHeader("Access-Control-Allow-Origin", "*");
    response.end("unapproved loopback tripwire");
  });
  server.on("upgrade", (_request, socket) => {
    requests += 1;
    socket.destroy();
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (address === null || typeof address === "string")
    throw new Error("loopback tripwire did not bind");
  tripwireOrigin = `http://127.0.0.1:${String(address.port)}`;
  approvedServer = createServer((request, response) => {
    if (request.url === "/redirect") {
      response.writeHead(302, { location: tripwireOrigin });
      response.end();
      return;
    }
    response.setHeader("Content-Type", "text/html");
    response.end("<main>Approved local server</main>");
  });
  await new Promise<void>((resolve) =>
    approvedServer.listen(0, "127.0.0.1", resolve),
  );
  const approvedAddress = approvedServer.address();
  if (approvedAddress === null || typeof approvedAddress === "string")
    throw new Error("approved loopback server did not bind");
  approvedOrigin = `http://127.0.0.1:${String(approvedAddress.port)}`;
});

test.afterAll(async () => {
  await new Promise<void>((resolve, reject) =>
    server.close((error) => (error ? reject(error) : resolve())),
  );
  await new Promise<void>((resolve, reject) =>
    approvedServer.close((error) => (error ? reject(error) : resolve())),
  );
});

test.use({
  baseURL: async ({ browserName }, runFixture) => {
    expect(browserName).toBe("chromium");
    await runFixture(approvedOrigin);
  },
});

test.beforeEach(() => {
  requests = 0;
});

test("DEL-07 default context blocks unapproved loopback before the tripwire", async ({
  page,
}) => {
  const outcome = await page.evaluate(async (origin) => {
    try {
      await fetch(origin);
      return "unexpected network";
    } catch {
      return "blocked";
    }
  }, tripwireOrigin);
  expect(outcome).toBe("blocked");
  expect(requests).toBe(0);
  // The automatic fixture must now fail this otherwise passing test at teardown.
});

test("DEL-07 manual context blocks WebSocket before the tripwire", async ({
  browser,
}) => {
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    const outcome = await page.evaluate(
      (origin) =>
        new Promise<string>((resolve) => {
          const socket = new WebSocket(origin.replace("http:", "ws:"));
          socket.onopen = () => resolve("unexpected network");
          socket.onerror = () => resolve("blocked");
          socket.onclose = () => resolve("blocked");
        }),
      tripwireOrigin,
    );
    expect(outcome).toBe("blocked");
    expect(requests).toBe(0);
  } finally {
    await context.close();
  }
});

test("DEL-07 approved-origin in-memory routes remain usable", async ({
  page,
}) => {
  await page.route(`${approvedOrigin}/**`, (route) =>
    route.fulfill({
      contentType: "text/html",
      body: "<main>In-memory approved fixture</main>",
    }),
  );
  await page.goto(approvedOrigin);
  await expect(page.getByRole("main")).toHaveText("In-memory approved fixture");
  expect(requests).toBe(0);
});

for (const method of ["fetch", "continue", "fallback"] as const) {
  test(`DEL-07 route ${method} rejects URL overrides before the tripwire`, async ({
    page,
  }) => {
    await page.route(`${approvedOrigin}/override`, async (route) => {
      try {
        if (method === "fetch")
          await route.fulfill({
            response: await route.fetch({ url: tripwireOrigin }),
          });
        else await route[method]({ url: tripwireOrigin });
      } catch {
        // The route was aborted; the automatic teardown must still fail.
      }
    });
    await page.goto(`${approvedOrigin}/override`).catch(() => undefined);
    expect(requests).toBe(0);
  });
}

test("DEL-07 ordinary browser redirects never reach the tripwire", async ({
  page,
}) => {
  await page.goto(`${approvedOrigin}/redirect`).catch(() => undefined);
  expect(requests).toBe(0);
});

test("DEL-07 delegated fetch redirects never reach the tripwire", async ({
  page,
}) => {
  await page.route(`${approvedOrigin}/redirect`, async (route) => {
    try {
      await route.fulfill({ response: await route.fetch() });
    } catch {
      // Expected denial remains visible in automatic teardown.
    }
  });
  await page.goto(`${approvedOrigin}/redirect`).catch(() => undefined);
  expect(requests).toBe(0);
});

test("DEL-07 mocked redirects never reach the tripwire", async ({ page }) => {
  await page.route(`${approvedOrigin}/mock-redirect`, (route) =>
    route.fulfill({ status: 302, headers: { location: tripwireOrigin } }),
  );
  await page.goto(`${approvedOrigin}/mock-redirect`).catch(() => undefined);
  expect(requests).toBe(0);
});

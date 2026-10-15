// DEL-01 relay controls use only owned loopback listeners and synthetic bytes.
// These are transport regressions, not the full product delivery qualification.
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { EventEmitter, once } from "node:events";
import { createServer as createHttpServer } from "node:http";
import { createConnection, createServer } from "node:net";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { installPlaywrightIsolation } from "../../../scripts/playwright-network-isolation.mjs";
import { installObj06NativeNetworkGuard } from "../config/obj-06-native-network.ts";
import {
  runWithLoopbackRelay,
  startLoopbackRelay,
  validateRelayOrigin,
} from "./del-01-loopback-relay.mjs";

process.env.PLAYWRIGHT_BROWSERS_PATH ??= "0";
const { chromium, request } = await import("@playwright/test");
const LOOPBACK = "127.0.0.1";
const WRAPPER = fileURLToPath(
  new URL("./del-01-loopback-relay.mjs", import.meta.url),
);
const originFor = (port) => `http://${LOOPBACK}:${String(port)}`;

async function listen(server, port) {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen({ host: LOOPBACK, port, exclusive: true }, resolve);
  });
  assert.equal(server.address().address, LOOPBACK);
  return server.address().port;
}

async function stop(server, sockets = new Set()) {
  for (const socket of sockets) socket.destroy();
  if (server.listening) await new Promise((resolve) => server.close(resolve));
}

async function serverFor(t, port, handler, http = false) {
  const server = http
    ? createHttpServer(handler)
    : createServer({ allowHalfOpen: true }, handler);
  const sockets = new Set();
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.on("error", () => {
      /* Explicit owned fixture cleanup may close a peer. */
    });
    socket.once("close", () => sockets.delete(socket));
  });
  t.after(() => stop(server, sockets));
  const bound = await listen(server, port);
  return { server, sockets, port: bound };
}

async function selectedPort() {
  const probe = createServer();
  const port = await listen(probe, 0);
  await stop(probe);
  assert.notEqual(port, 8080);
  return port;
}

async function relayFor(t, port) {
  const relay = await startLoopbackRelay(originFor(port));
  t.after(() => relay.close());
  return relay;
}

async function connect(port) {
  const socket = createConnection({
    host: LOOPBACK,
    port,
    allowHalfOpen: true,
  });
  socket.on("end", () => socket.end());
  await once(socket, "connect");
  return socket;
}

async function exchange(port, bytes) {
  const socket = await connect(port);
  const chunks = [];
  socket.on("data", (chunk) => chunks.push(chunk));
  const ended = once(socket, "end");
  socket.write(bytes.subarray(0, 13));
  socket.write(bytes.subarray(13, 47));
  socket.end(bytes.subarray(47));
  await ended;
  socket.destroy();
  return Buffer.concat(chunks);
}

for (const port of [1024, 8080, 18080, 65535]) {
  test(`DEL-01 relay accepts canonical unprivileged origin ${String(port)}`, () => {
    assert.equal(validateRelayOrigin(originFor(port)), port);
  });
}

for (const value of [
  "http://127.0.0.1",
  "http://127.0.0.1:80",
  "http://127.0.0.1:0",
  "http://127.0.0.1:1023",
  "http://127.0.0.1:65536",
  "http://127.0.0.1:08080",
  "http://127.0.0.1:8080/",
  "http://127.0.0.1:8080/path",
  "http://127.0.0.1:8080?secret=fixture",
  "http://127.0.0.1:8080#fragment",
  "https://127.0.0.1:8080",
  "http://localhost:8080",
  "http://127.1:8080",
  "http://2130706433:8080",
  "http://0.0.0.0:8080",
  "http://[::1]:8080",
  "http://example.test:8080",
  "http://user:password@127.0.0.1:8080",
  " http://127.0.0.1:8080",
  "http://127.0.0.1:8080\n",
  "not a URL",
  null,
]) {
  test(`DEL-01 relay rejects noncanonical origin fixture ${String(value).length}`, () => {
    assert.throws(() => validateRelayOrigin(value), {
      message: "DEL-01 relay origin_invalid",
    });
  });
}

test(
  "DEL-01 port 8080 bypasses the relay and never collides with its upstream",
  { timeout: 5000 },
  async (t) => {
    await serverFor(t, 8080, (socket) => socket.end());
    const relay = await relayFor(t, 8080);
    assert.equal(relay.skipped, true);
    relay.assertHealthy();
    await relay.close();
    await relay.close();
  },
);

test(
  "DEL-01 relay preserves every HTTP/header and binary byte in both directions",
  { timeout: 5000 },
  async (t) => {
    const received = [];
    const response = Buffer.concat([
      Buffer.from("HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\n"),
      Buffer.from([0, 255, 13, 10, 127, 128, 1, 254]),
    ]);
    await serverFor(t, 8080, (socket) => {
      const chunks = [];
      socket.on("data", (chunk) => chunks.push(chunk));
      socket.on("end", () => {
        received.push(Buffer.concat(chunks));
        socket.end(response);
      });
    });
    const port = await selectedPort();
    const relay = await relayFor(t, port);
    const bytes = Buffer.concat([
      Buffer.from(
        `POST /api/v1/fixture HTTP/1.1\r\nHost: 127.0.0.1:${String(port)}\r\nOrigin: ${originFor(port)}\r\nSec-Fetch-Site: same-origin\r\nX-CSRF-Token: synthetic-relay-fixture\r\nTransfer-Encoding: chunked\r\n\r\n8\r\n`,
      ),
      Buffer.from([0, 255, 13, 10, 127, 128, 1, 254]),
      Buffer.from("\r\n0\r\n\r\n"),
    ]);
    const results = await Promise.all([
      exchange(port, bytes),
      exchange(port, bytes),
    ]);
    assert.equal(received.length, 2);
    assert.ok(received.every((value) => value.equals(bytes)));
    assert.ok(results.every((value) => value.equals(response)));
    relay.assertHealthy();
    await relay.close();
    await serverFor(t, port, (socket) => socket.end());
  },
);

test(
  "DEL-01 relay drains a large binary response to a paused client after request half-close",
  { timeout: 5000 },
  async (t) => {
    const requestBytes = Buffer.alloc(512 * 1024, 0x5a);
    const responseBytes = Buffer.alloc(8 * 1024 * 1024, 0xa5);
    let received;
    await serverFor(t, 8080, (socket) => {
      const chunks = [];
      socket.on("data", (chunk) => chunks.push(chunk));
      socket.on("end", () => {
        received = Buffer.concat(chunks);
        socket.end(responseBytes);
      });
    });
    const port = await selectedPort();
    const relay = await relayFor(t, port);
    const client = await connect(port);
    t.after(() => client.destroy());
    const chunks = [];
    client.on("data", (chunk) => chunks.push(chunk));
    client.pause();
    const ended = once(client, "end");
    client.end(requestBytes);
    const timer = setTimeout(() => client.resume(), 25);
    t.after(() => clearTimeout(timer));
    await ended;
    assert.ok(received.equals(requestBytes));
    assert.ok(Buffer.concat(chunks).equals(responseBytes));
    relay.assertHealthy();
  },
);

test(
  "DEL-01 bind collision fails before launching a journey and leaves the existing listener intact",
  { timeout: 5000 },
  async (t) => {
    const occupied = await serverFor(t, 0, (socket) =>
      socket.end("owned-original"),
    );
    let launched = false;
    await assert.rejects(
      runWithLoopbackRelay(originFor(occupied.port), () => {
        launched = true;
      }),
      { message: "DEL-01 relay bind_failed" },
    );
    assert.equal(launched, false);
    assert.equal(
      (await exchange(occupied.port, Buffer.from("probe"))).toString(),
      "owned-original",
    );
    assert.equal(occupied.server.listening, true);
  },
);

test(
  "DEL-01 unavailable fixed upstream fails without fallback and closes the client",
  { timeout: 5000 },
  async (t) => {
    const upstream = await serverFor(t, 8080, (socket) => socket.end());
    await stop(upstream.server);
    const port = await selectedPort();
    const relay = await relayFor(t, port);
    const client = await connect(port);
    const closed = once(client, "close");
    client.on("error", () => {
      /* The unavailable upstream closes this fixture. */
    });
    client.write("synthetic-probe");
    assert.equal(await relay.failed, "upstream_failed");
    assert.throws(() => relay.assertHealthy(), {
      message: "DEL-01 relay upstream_failed",
    });
    await closed;
    await relay.close();
    await serverFor(t, port, (socket) => socket.end());
  },
);

test(
  "DEL-01 cleanup closes active client/upstream sockets and is repeatable",
  { timeout: 5000 },
  async (t) => {
    const upstream = await serverFor(t, 8080, (socket) =>
      socket.on("end", () => socket.end()),
    );
    const upstreamConnection = once(upstream.server, "connection");
    const port = await selectedPort();
    const relay = await relayFor(t, port);
    const client = await connect(port);
    const [peer] = await upstreamConnection;
    const clientClosed = once(client, "close");
    const peerClosed = once(peer, "close");
    client.on("error", () => {
      /* Explicit owned relay cleanup closes this fixture. */
    });
    await relay.close();
    await Promise.all([clientClosed, peerClosed]);
    await relay.close();
    relay.assertHealthy();
    await serverFor(t, port, (socket) => socket.end());
  },
);

for (const outcome of [0, 1, "throw", "SIGINT", "SIGTERM"]) {
  test(
    `DEL-01 wrapper cleanup and exact argv origin survive ${String(outcome)}`,
    { timeout: 5000 },
    async (t) => {
      const signals = new EventEmitter();
      const port = await selectedPort();
      let stopped = false;
      let seenOrigin;
      const task = (origin) => {
        seenOrigin = origin;
        if (outcome === "throw") throw new Error("synthetic-private-error");
        if (typeof outcome === "string")
          queueMicrotask(() => signals.emit(outcome));
        return {
          completed:
            typeof outcome === "number"
              ? Promise.resolve(outcome)
              : new Promise(() => {
                  /* Only the simulated signal completes this task. */
                }),
          stop: async () => {
            stopped = true;
          },
        };
      };
      const result = runWithLoopbackRelay(originFor(port), task, signals);
      if (outcome === 0) await result;
      else await assert.rejects(result);
      assert.equal(seenOrigin, originFor(port));
      assert.equal(stopped, outcome !== "throw");
      assert.equal(
        signals.listenerCount("SIGINT") + signals.listenerCount("SIGTERM"),
        0,
      );
      await serverFor(t, port, (socket) => socket.end());
    },
  );
}

test(
  "DEL-01 Chromium generates real security headers through the relay and still denies another origin",
  { timeout: 15000 },
  async (t) => {
    const observations = [];
    const body = JSON.stringify({ synthetic: "relay-byte-proof-\u0000-☃" });
    await serverFor(
      t,
      8080,
      (incoming, outgoing) => {
        if (incoming.url !== "/capture") {
          outgoing.writeHead(200, { "content-type": "text/html" });
          outgoing.end(
            "<!doctype html><title>Owned relay control</title><p>Local transport control</p>",
          );
          return;
        }
        const chunks = [];
        incoming.on("data", (chunk) => chunks.push(chunk));
        incoming.on("end", () => {
          observations.push({
            origin: incoming.headers.origin,
            site: incoming.headers["sec-fetch-site"],
            csrf: incoming.headers["x-csrf-token"],
            body: Buffer.concat(chunks).toString(),
          });
          outgoing.writeHead(200, {
            "content-type": "application/json",
            "cache-control": "no-store",
          });
          outgoing.end('{"ok":true}');
        });
      },
      true,
    );
    let tripwireHits = 0;
    const tripwire = await serverFor(t, 0, (socket) => {
      tripwireHits += 1;
      socket.destroy();
    });
    const port = await selectedPort();
    const origin = originFor(port);
    const relay = await relayFor(t, port);
    const browser = await chromium.launch();
    t.after(() => browser.close());
    const context = await browser.newContext({ serviceWorkers: "block" });
    const isolation = await installPlaywrightIsolation({
      browser,
      context,
      requestFactory: request,
      allowedOrigin: origin,
      installContextGuard: (guarded, allowed) =>
        installObj06NativeNetworkGuard(guarded, allowed, {
          denyWebSockets: true,
        }),
    });
    t.after(async () => {
      isolation.restore();
      await context.close();
    });
    const page = await context.newPage();
    await page.goto(origin);
    const result = await page.evaluate(async (syntheticBody) => {
      const response = await fetch("/capture", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": "synthetic-relay-csrf",
        },
        body: syntheticBody,
      });
      return { status: response.status, body: await response.json() };
    }, body);
    assert.deepEqual(result, { status: 200, body: { ok: true } });
    assert.deepEqual(observations, [
      { origin, site: "same-origin", csrf: "synthetic-relay-csrf", body },
    ]);
    await isolation.assertNoExternalAttempts();
    const denied = await page.evaluate(async (otherOrigin) => {
      try {
        await fetch(otherOrigin);
        return false;
      } catch {
        return true;
      }
    }, originFor(tripwire.port));
    assert.equal(denied, true);
    await assert.rejects(isolation.assertNoExternalAttempts());
    assert.equal(tripwireHits, 0);
    relay.assertHealthy();
  },
);

test(
  "DEL-01 interruption stops an owned real Node task and releases its listener",
  { timeout: 5000 },
  async (t) => {
    const port = await selectedPort();
    const signals = new EventEmitter();
    let child;
    let childExited;
    const task = () => {
      child = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
        detached: true,
        stdio: "ignore",
      });
      childExited = once(child, "exit");
      child.once("spawn", () => signals.emit("SIGTERM"));
      return {
        completed: childExited.then(([code]) => code ?? 1),
        stop: async () => {
          process.kill(-child.pid, "SIGTERM");
          await childExited;
        },
      };
    };
    t.after(() => {
      if (child?.exitCode === null && child?.signalCode === null)
        child.kill("SIGTERM");
    });
    await assert.rejects(runWithLoopbackRelay(originFor(port), task, signals), {
      message: "DEL-01 relay interrupted",
    });
    assert.equal(child.signalCode, "SIGTERM");
    await serverFor(t, port, (socket) => socket.end());
  },
);

test(
  "DEL-01 CLI rejects extra arguments with categorical stderr and empty stdout",
  { timeout: 5000 },
  async () => {
    const result = spawnSync(
      process.execPath,
      [
        WRAPPER,
        "--origin",
        "http://127.0.0.1:8080",
        "--target",
        "private-fixture",
      ],
      { encoding: "utf8" },
    );
    assert.equal(result.status, 1);
    assert.equal(result.stdout, "");
    assert.equal(result.stderr, "DEL-01 relay arguments_invalid\n");
  },
);

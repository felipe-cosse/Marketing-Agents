// DEL-01 test-only transport: retain the published browser origin inside the
// owned web network namespace without rewriting a single HTTP/header byte.
import "./require-pinned-node.mjs";
import { spawn } from "node:child_process";
import { createConnection, createServer } from "node:net";
import { fileURLToPath, pathToFileURL } from "node:url";

const LOOPBACK = "127.0.0.1";
const UPSTREAM_PORT = 8080;
const CHILD_GRACE_MS = 1_000;
const CHILD_KILL_MS = 1_000;
const RUNNER = fileURLToPath(new URL("./run-del-05-e2e.mjs", import.meta.url));

class RelayFailure extends Error {
  constructor(code) {
    super(`DEL-01 relay ${code}`);
    this.code = code;
  }
}

export function validateRelayOrigin(value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new RelayFailure("origin_invalid");
  }
  const port = Number(parsed.port);
  if (
    typeof value !== "string" ||
    parsed.protocol !== "http:" ||
    parsed.hostname !== LOOPBACK ||
    parsed.username + parsed.password + parsed.search + parsed.hash !== "" ||
    parsed.pathname !== "/" ||
    !Number.isInteger(port) ||
    port < 1024 ||
    port > 65535 ||
    value !== `http://${LOOPBACK}:${String(port)}`
  )
    throw new RelayFailure("origin_invalid");
  return port;
}

export async function startLoopbackRelay(origin) {
  const port = validateRelayOrigin(origin);
  let failureCode;
  let failPromise;
  const failed = new Promise((resolve) => {
    failPromise = resolve;
  });
  const sockets = new Set();
  let closing = false;
  let closePromise;
  const fail = (code) => {
    if (failureCode === undefined && !closing) {
      failureCode = code;
      failPromise(code);
    }
  };
  const track = (socket) => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
    return socket;
  };
  const server =
    port === UPSTREAM_PORT
      ? undefined
      : createServer({ allowHalfOpen: true }, (client) => {
          if (closing) {
            client.destroy();
            return;
          }
          track(client);
          // There is intentionally no configurable destination, DNS, HTTP parser,
          // request synthesis, fallback or retry. Browser security headers stay intact.
          const upstream = track(
            createConnection({
              host: LOOPBACK,
              port: UPSTREAM_PORT,
              allowHalfOpen: true,
            }),
          );
          client.on("error", () => {
            upstream.destroy();
          });
          upstream.on("error", () => {
            // A browser may cancel its own fetch. Only an already-closed owned
            // downstream or our explicit cleanup suppresses an upstream failure.
            if (!client.destroyed) fail("upstream_failed");
            client.destroy();
          });
          client.once("close", () => upstream.destroy());
          upstream.once("close", () => {
            // Graceful EOF is already piped to client.end(); let queued bytes
            // drain instead of truncating a slow downstream reader.
            if (!upstream.readableEnded) client.destroy();
          });
          client.pipe(upstream);
          upstream.pipe(client);
        });
  const relay = {
    failed,
    skipped: server === undefined,
    assertHealthy() {
      if (failureCode !== undefined) throw new RelayFailure(failureCode);
    },
    close() {
      closePromise ??= (async () => {
        closing = true;
        for (const socket of sockets) socket.destroy();
        if (server?.listening)
          await new Promise((resolve) => server.close(resolve));
      })();
      return closePromise;
    },
  };
  if (server !== undefined) {
    let listening = false;
    server.on("error", () =>
      fail(listening ? "listener_failed" : "bind_failed"),
    );
    try {
      await new Promise((resolve, reject) => {
        server.once("error", () => reject(new RelayFailure("bind_failed")));
        server.listen({ host: LOOPBACK, port, exclusive: true }, () => {
          listening = true;
          resolve();
        });
      });
    } catch {
      await relay.close();
      throw new RelayFailure("bind_failed");
    }
  }
  return relay;
}

function within(promise, milliseconds) {
  let timer;
  return Promise.race([
    promise.then(() => true),
    new Promise((resolve) => {
      timer = setTimeout(() => resolve(false), milliseconds);
    }),
  ]).finally(() => clearTimeout(timer));
}

function ownedBrowserProcess(origin) {
  // Own only this Node process group. Playwright's installed SIGTERM handler
  // gracefully closes Chromium (which has its own process group). A forced
  // fallback is contained by this production Docker PID1 and exact-container
  // cleanup; this is not a generic native descendant-reaping guarantee.
  const child = spawn(process.execPath, [RUNNER, "--origin", origin], {
    detached: true,
    stdio: ["ignore", "inherit", "inherit"],
  });
  let ended = false;
  let complete;
  const completed = new Promise((resolve) => {
    complete = resolve;
  });
  child.once("error", () => {
    ended = true;
    complete(1);
  });
  child.once("exit", (code) => {
    ended = true;
    complete(code ?? 1);
  });
  const signalGroup = (signal) => {
    if (!Number.isInteger(child.pid) || child.pid < 1) return;
    try {
      process.kill(-child.pid, signal);
    } catch (error) {
      if (error?.code !== "ESRCH")
        throw new RelayFailure("child_cleanup_failed");
    }
  };
  return {
    completed,
    async stop() {
      // Signal only the owned runner group, never unrelated browsers/processes.
      signalGroup("SIGTERM");
      if (!ended && !(await within(completed, CHILD_GRACE_MS))) {
        signalGroup("SIGKILL");
        if (!(await within(completed, CHILD_KILL_MS)))
          throw new RelayFailure("child_cleanup_failed");
      }
      signalGroup("SIGKILL");
    },
  };
}

// Task and signal injection are confined to lightweight lifecycle regression
// controls. The CLI has no replacement command, target or environment options.
export async function runWithLoopbackRelay(
  origin,
  task = ownedBrowserProcess,
  signals = process,
) {
  const relay = await startLoopbackRelay(origin);
  let ownedTask;
  let interrupt;
  const interrupted = new Promise((resolve) => {
    interrupt = resolve;
  });
  const onInterrupt = () => interrupt("interrupted");
  signals.on("SIGINT", onInterrupt);
  signals.on("SIGTERM", onInterrupt);
  try {
    ownedTask = task(origin);
    const result = await Promise.race([
      ownedTask.completed,
      relay.failed.then((code) => {
        throw new RelayFailure(code);
      }),
      interrupted.then((code) => {
        throw new RelayFailure(code);
      }),
    ]);
    relay.assertHealthy();
    if (result !== 0) throw new RelayFailure("runner_failed");
  } finally {
    signals.removeListener("SIGINT", onInterrupt);
    signals.removeListener("SIGTERM", onInterrupt);
    try {
      await ownedTask?.stop();
    } finally {
      await relay.close();
    }
  }
}

async function main() {
  const args = process.argv.slice(2);
  try {
    if (args.length !== 2 || args[0] !== "--origin")
      throw new RelayFailure("arguments_invalid");
    await runWithLoopbackRelay(args[1]);
  } catch (error) {
    // Never print child environment, URL arguments, HTTP bytes, headers or errors.
    const code = error instanceof RelayFailure ? error.code : "wrapper_failed";
    process.stderr.write(`DEL-01 relay ${code}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url)
  await main();

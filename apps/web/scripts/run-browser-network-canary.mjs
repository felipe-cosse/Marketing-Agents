// DEL-07 connection witness: real Chromium, only an owned local tripwire server.
import "./require-pinned-node.mjs";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const result = spawnSync(
  resolve(webRoot, "node_modules/.bin/playwright"),
  [
    "test",
    "--config",
    "config/playwright-network-canary.config.ts",
    "--reporter=json",
  ],
  {
    cwd: webRoot,
    env: { ...process.env, PLAYWRIGHT_BROWSERS_PATH: "0" },
    encoding: "utf8",
    timeout: 60_000,
  },
);
assert.equal(result.error, undefined);
assert.equal(result.signal, null);
assert.equal(
  result.status,
  1,
  "expected the fixture's eight intentional teardown denials",
);
const report = JSON.parse(result.stdout);
assert.equal(report.stats.expected, 1);
assert.equal(report.stats.unexpected, 8);
assert.equal(report.stats.skipped, 0);
assert.deepEqual(report.errors, []);
const cases = report.suites
  .flatMap((suite) => suite.specs)
  .flatMap((spec) => spec.tests);
for (const failed of cases.filter((entry) => entry.status === "unexpected")) {
  assert.equal(failed.results.length, 1);
  assert.equal(failed.results[0].errors.length, 1);
  assert.match(
    failed.results[0].errors[0].message,
    /browser attempted 1 unapproved network request/,
  );
}
const reused = spawnSync(
  resolve(webRoot, "node_modules/.bin/playwright"),
  [
    "test",
    "--config",
    "config/playwright-network-canary.config.ts",
    "--reporter=json",
    "--grep",
    "approved-origin",
  ],
  {
    cwd: webRoot,
    env: {
      ...process.env,
      PLAYWRIGHT_BROWSERS_PATH: "0",
      PW_TEST_REUSE_CONTEXT: "1",
    },
    encoding: "utf8",
    timeout: 60_000,
  },
);
assert.equal(reused.error, undefined);
assert.equal(reused.signal, null);
assert.equal(reused.status, 1);
const reuseReport = JSON.parse(reused.stdout);
assert.equal(reuseReport.stats.expected, 0);
assert.equal(reuseReport.stats.unexpected, 1);
assert.equal(reuseReport.stats.skipped, 0);
assert.deepEqual(reuseReport.errors, []);
const reuseCase = reuseReport.suites
  .flatMap((suite) => suite.specs)
  .flatMap((spec) => spec.tests)[0];
assert.equal(reuseCase.results[0].errors.length, 1);
assert.match(
  reuseCase.results[0].errors[0].message,
  /does not support reused contexts/,
);
console.log(
  "DEL-07 browser network canary passed: HTTP, WebSocket, overrides, and redirects denied before the loopback tripwire; in-memory route passed; context reuse rejected.",
);

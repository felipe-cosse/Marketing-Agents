// OBJ-06 actual local composition and browser evidence, with no acquisition.
import "./require-pinned-node.mjs";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const runner = fileURLToPath(import.meta.url);
const webRoot = resolve(dirname(runner), "..");
const root = resolve(webRoot, "../..");
function runLocalTool(name, args, environment) {
  const executable = resolve(webRoot, "node_modules/.bin", name);
  assert.ok(existsSync(executable), "OBJ-06 requires installed local tooling");
  const result = spawnSync(executable, args, {
    cwd: webRoot,
    env: environment,
    stdio: "inherit",
    timeout: 360_000,
  });
  assert.equal(result.error, undefined, "OBJ-06 browser process failed to run");
  assert.equal(
    result.signal,
    null,
    "OBJ-06 browser process did not exit normally",
  );
  if (result.status !== 0) process.exit(result.status ?? 2);
}

if (process.argv.slice(2).length === 0) {
  const result = spawnSync(
    resolve(root, ".venv/bin/python"),
    [
      resolve(root, "scripts/obj_06_browser_harness.py"),
      "--node",
      process.execPath,
    ],
    { cwd: root, env: process.env, stdio: "inherit", timeout: 900_000 },
  );
  assert.equal(result.error, undefined, "OBJ-06 native harness failed to run");
  assert.equal(
    result.signal,
    null,
    "OBJ-06 native harness did not exit normally",
  );
  process.exit(result.status ?? 2);
}
assert.deepEqual(process.argv.slice(2), ["--browser-only"]);
assert.equal(
  process.env.OBJ06_NATIVE_READY,
  "1",
  "OBJ-06 native readiness is required",
);
runLocalTool(
  "playwright",
  [
    "test",
    "--config",
    "config/playwright-obj-06.config.ts",
    "e2e/obj-06-control-surface.spec.ts",
  ],
  {
    ...process.env,
    PLAYWRIGHT_BROWSERS_PATH: "0",
  },
);

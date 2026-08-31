// DEMO-01 browser evidence builds and tests the installed production frontend without Corepack or HOME state.
import "./require-pinned-node.mjs";

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function runLocalTool(name, args, environment = process.env) {
  const executable = resolve(webRoot, "node_modules/.bin", name);
  if (!existsSync(executable)) {
    process.stderr.write(
      `DEMO-01 browser evidence requires installed ${name} from make web-bootstrap.\n`,
    );
    process.exit(2);
  }
  const result = spawnSync(executable, args, {
    cwd: webRoot,
    env: environment,
    stdio: "inherit",
  });
  if (result.error !== undefined) {
    process.stderr.write(
      `DEMO-01 browser evidence could not start ${name}: ${result.error.message}\n`,
    );
    process.exit(2);
  }
  if (result.signal !== null) {
    process.stderr.write(
      `DEMO-01 browser evidence ${name} stopped by signal ${result.signal}.\n`,
    );
    process.exit(2);
  }
  if (result.status !== 0) process.exit(result.status ?? 2);
}

runLocalTool("tsc", ["-b", "--pretty", "false"]);
runLocalTool("vite", ["build"]);
runLocalTool(
  "playwright",
  ["test", "e2e/demo-01-social-draft.spec.ts"],
  Object.freeze({ ...process.env, PLAYWRIGHT_BROWSERS_PATH: "0" }),
);

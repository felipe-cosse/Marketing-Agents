// DEMO-02 unit evidence invokes focused transport and journey tests without Corepack or HOME state.
import "./require-pinned-node.mjs";

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function runLocalTool(name, args) {
  const executable = resolve(webRoot, "node_modules/.bin", name);
  if (!existsSync(executable)) {
    process.stderr.write(
      `DEMO-02 unit evidence requires installed ${name} from make web-bootstrap.\n`,
    );
    process.exit(2);
  }
  const result = spawnSync(executable, args, {
    cwd: webRoot,
    env: process.env,
    stdio: "inherit",
  });
  if (result.error !== undefined) {
    process.stderr.write(
      `DEMO-02 unit evidence could not start ${name}: ${result.error.message}\n`,
    );
    process.exit(2);
  }
  if (result.signal !== null) {
    process.stderr.write(
      `DEMO-02 unit evidence ${name} stopped by signal ${result.signal}.\n`,
    );
    process.exit(2);
  }
  if (result.status !== 0) process.exit(result.status ?? 2);
}

runLocalTool("tsc", ["-b", "--pretty", "false"]);
runLocalTool("vitest", [
  "run",
  "src/api/demoScenarios.test.ts",
  "src/features/demos/DemosPage.test.tsx",
  "src/features/demos/DemosPage.demo02.test.tsx",
]);

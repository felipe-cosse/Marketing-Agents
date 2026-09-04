// ARCH-08 evidence runs the focused frontend dependency-boundary contract.
import "./require-pinned-node.mjs";

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const vitest = resolve(webRoot, "node_modules/.bin/vitest");

if (!existsSync(vitest)) {
  process.stderr.write(
    "ARCH-08 unit evidence requires the frozen frontend dependencies from make web-bootstrap.\n",
  );
  process.exit(2);
}

const result = spawnSync(
  vitest,
  [
    "run",
    "config/arch08FrontendBoundaries.test.ts",
    "--testNamePattern",
    "ARCH-08",
  ],
  {
    cwd: webRoot,
    env: process.env,
    stdio: "inherit",
  },
);

if (result.error !== undefined) {
  process.stderr.write(
    `ARCH-08 unit evidence could not start: ${result.error.message}\n`,
  );
  process.exit(2);
}
if (result.signal !== null) {
  process.stderr.write(
    `ARCH-08 unit evidence stopped by signal ${result.signal}.\n`,
  );
  process.exit(2);
}
process.exit(result.status ?? 2);

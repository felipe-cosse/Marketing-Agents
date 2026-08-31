// Run every committed frontend browser requirement with the exact Node authority.
import "./require-pinned-node.mjs";

import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptsRoot = dirname(fileURLToPath(import.meta.url));
for (const script of [
  "run-web-01-e2e.mjs",
  "run-web-02-e2e.mjs",
  "run-web-03-e2e.mjs",
  "run-web-04-e2e.mjs",
  "run-web-05-e2e.mjs",
  "run-web-06-e2e.mjs",
]) {
  const result = spawnSync(process.execPath, [resolve(scriptsRoot, script)], {
    stdio: "inherit",
    env: process.env,
  });
  if (result.error !== undefined) {
    process.stderr.write(
      `Frontend browser evidence could not start ${script}: ${result.error.message}\n`,
    );
    process.exit(2);
  }
  if (result.signal !== null) {
    process.stderr.write(
      `Frontend browser evidence ${script} stopped by signal ${result.signal}.\n`,
    );
    process.exit(2);
  }
  if (result.status !== 0) process.exit(result.status ?? 2);
}

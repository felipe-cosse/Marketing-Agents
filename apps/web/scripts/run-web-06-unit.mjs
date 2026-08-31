// WEB-06 evidence invokes every installed Vitest case carrying the requirement marker.
import "./require-pinned-node.mjs";

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const vitest = resolve(webRoot, "node_modules/.bin/vitest");

if (!existsSync(vitest)) {
  process.stderr.write(
    "WEB-06 unit evidence requires the frozen frontend dependencies from make web-bootstrap.\n",
  );
  process.exit(2);
}

const result = spawnSync(vitest, ["run", "--testNamePattern", "WEB-06"], {
  cwd: webRoot,
  env: process.env,
  stdio: "inherit",
});

if (result.error !== undefined) {
  process.stderr.write(
    `WEB-06 unit evidence could not start: ${result.error.message}\n`,
  );
  process.exit(2);
}
if (result.signal !== null) {
  process.stderr.write(
    `WEB-06 unit evidence stopped by signal ${result.signal}.\n`,
  );
  process.exit(2);
}
process.exit(result.status ?? 2);

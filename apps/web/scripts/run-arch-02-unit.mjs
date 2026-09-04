// ARCH-02 evidence runs only focused architecture and hierarchy-accessibility tests.
import "./require-pinned-node.mjs";

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const vitest = resolve(webRoot, "node_modules/.bin/vitest");

if (!existsSync(vitest)) {
  process.stderr.write(
    "ARCH-02 unit evidence requires the frozen frontend dependencies from make web-bootstrap.\n",
  );
  process.exit(2);
}

const result = spawnSync(vitest, ["run", "--testNamePattern", "ARCH-02"], {
  cwd: webRoot,
  env: process.env,
  stdio: "inherit",
});

if (result.error !== undefined) {
  process.stderr.write(
    `ARCH-02 unit evidence could not start: ${result.error.message}\n`,
  );
  process.exit(2);
}
if (result.signal !== null) {
  process.stderr.write(
    `ARCH-02 unit evidence stopped by signal ${result.signal}.\n`,
  );
  process.exit(2);
}
process.exit(result.status ?? 2);

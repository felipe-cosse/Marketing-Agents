// Run every committed frontend browser requirement with the exact Node authority.
import "./require-pinned-node.mjs";

import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  BROWSER_RUNNERS,
  readBrowserInventory,
} from "./browser-evidence-inventory.mjs";

const scriptsRoot = dirname(fileURLToPath(import.meta.url));
try {
  const inventory = readBrowserInventory(resolve(scriptsRoot, ".."));
  process.stdout.write(
    `Browser inventory: ${String(inventory.specCount)} specs, ${String(inventory.runnerCount)} unfiltered runners.\n`,
  );
} catch (error) {
  process.stderr.write(`Frontend browser inventory failed: ${error.message}\n`);
  process.exit(2);
}
for (const script of BROWSER_RUNNERS) {
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

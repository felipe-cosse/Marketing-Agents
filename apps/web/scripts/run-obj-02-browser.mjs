// OBJ-02 reuses the full chart/search/responsive/accessibility specs; no test-name filters.
import "./require-pinned-node.mjs";

import { spawnSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const outputRoot = mkdtempSync(
  join(tmpdir(), "marketing-agents-obj02-browser-"),
);
process.stdout.write(`OBJ-02 browser artifacts: ${outputRoot}\n`);

function run(name, args) {
  const result = spawnSync(resolve(webRoot, "node_modules/.bin", name), args, {
    cwd: webRoot,
    env: { ...process.env, PLAYWRIGHT_BROWSERS_PATH: "0" },
    stdio: "inherit",
  });
  if (result.error !== undefined || result.signal !== null) {
    process.stderr.write(
      `OBJ-02 ${name} could not finish: ${result.error?.message ?? result.signal}\n`,
    );
    process.exit(2);
  }
  if (result.status !== 0) process.exit(result.status ?? 2);
}

run("tsc", ["-b", "--pretty", "false"]);
run("vite", ["build"]);
run("playwright", [
  "test",
  "e2e/web-01-org-chart.spec.ts",
  "e2e/web-02-search-filter.spec.ts",
  "e2e/web-07-responsive-tree.spec.ts",
  "e2e/web-08-accessibility.spec.ts",
  "--output",
  outputRoot,
  "--reporter",
  "list",
]);

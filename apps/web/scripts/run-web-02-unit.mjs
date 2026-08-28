// WEB-02 evidence invokes installed, lock-pinned Vitest without Corepack or HOME state.
import "./require-pinned-node.mjs";

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const vitest = resolve(webRoot, "node_modules/.bin/vitest");

if (!existsSync(vitest)) {
  process.stderr.write(
    "WEB-02 unit evidence requires the frozen frontend dependencies from make web-bootstrap.\n",
  );
  process.exit(2);
}

const result = spawnSync(
  vitest,
  [
    "run",
    "src/api/instanceStatusSummary.test.ts",
    "src/features/org-chart/filters.test.ts",
    "src/features/org-chart/filterUrl.test.ts",
    "src/features/org-chart/projectHierarchy.test.ts",
    "src/features/org-chart/layout.test.ts",
    "src/features/org-chart/CatalogToolbar.test.tsx",
    "src/features/org-chart/OrgChartPage.test.tsx",
    "src/features/org-chart/useFilteredFocus.test.tsx",
  ],
  {
    cwd: webRoot,
    env: process.env,
    stdio: "inherit",
  },
);

if (result.error !== undefined) {
  process.stderr.write(
    `WEB-02 unit evidence could not start: ${result.error.message}\n`,
  );
  process.exit(2);
}
if (result.signal !== null) {
  process.stderr.write(
    `WEB-02 unit evidence stopped by signal ${result.signal}.\n`,
  );
  process.exit(2);
}
process.exit(result.status ?? 2);

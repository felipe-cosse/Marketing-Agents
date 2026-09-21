import { defineConfig } from "@playwright/test";
import { isAbsolute, join } from "node:path";

const evidence = process.env.OBJ06_EVIDENCE_DIRECTORY;
if (evidence === undefined || !isAbsolute(evidence))
  throw new Error("OBJ-06 canaries require an external evidence directory");

export default defineConfig({
  testDir: ".",
  testMatch: "obj-06-network-canary.spec.ts",
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 30_000,
  reporter: "list",
  outputDir: join(evidence, "network-canaries"),
  use: {
    browserName: "chromium",
    serviceWorkers: "block",
    trace: "retain-on-failure",
  },
});

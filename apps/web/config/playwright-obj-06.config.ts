import { defineConfig } from "@playwright/test";
import { isAbsolute, join } from "node:path";

const evidence = process.env.OBJ06_EVIDENCE_DIRECTORY;
if (evidence === undefined || !isAbsolute(evidence))
  throw new Error(
    "OBJ-06 requires an external evidence directory from its runner",
  );

export default defineConfig({
  testDir: "../e2e",
  testMatch: "obj-06-control-surface.spec.ts",
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: "list",
  outputDir: join(evidence, "playwright"),
  use: {
    baseURL: "http://127.0.0.1:4173",
    browserName: "chromium",
    actionTimeout: 15_000,
    navigationTimeout: 20_000,
    serviceWorkers: "block",
    colorScheme: "light",
    locale: "en-US",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "off",
    viewport: { width: 1536, height: 1024 },
  },
});

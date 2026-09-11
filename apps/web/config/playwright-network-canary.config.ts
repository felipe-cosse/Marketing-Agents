import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { defineConfig } from "@playwright/test";
import mainConfig from "../playwright.config";

export default defineConfig({
  ...mainConfig,
  testDir: ".",
  testMatch: "browser-network-canary.fixture.ts",
  outputDir: mkdtempSync(join(tmpdir(), "marketing-del07-network-canary-")),
  webServer: [],
  retries: 0,
  use: { ...mainConfig.use, screenshot: "off", trace: "off" },
});

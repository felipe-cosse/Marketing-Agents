import { defineConfig, devices } from "@playwright/test";
import { resolve } from "node:path";

const repositoryRoot = resolve(import.meta.dirname, "../..");

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  outputDir: "test-results",
  use: {
    baseURL: "http://127.0.0.1:4173",
    browserName: "chromium",
    colorScheme: "light",
    locale: "en-US",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "off",
    viewport: { width: 1536, height: 1024 },
  },
  projects: [
    {
      name: "desktop-chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1536, height: 1024 },
      },
    },
  ],
  webServer: [
    {
      command:
        "UV_CACHE_DIR=.cache/uv uv run uvicorn marketing_agents.api:create_app --factory --host 127.0.0.1 --port 8000",
      cwd: repositoryRoot,
      url: "http://127.0.0.1:8000/health/live",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: "node_modules/.bin/vite preview --host 127.0.0.1 --port 4173",
      cwd: import.meta.dirname,
      url: "http://127.0.0.1:4173",
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});

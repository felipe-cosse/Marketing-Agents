import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import playwrightConfig from "../playwright.config";
import mainConfig from "../vitest.config";
import denialConfig from "./vitest-network-denial.config";

describe("AC-15 traceable isolated Vitest execution", () => {
  it.each([
    ["complete suite", mainConfig],
    ["real denial canary", denialConfig],
  ] as const)(
    "keeps %s on supported isolated single-worker threads",
    (_name, config) => {
      expect(config.test?.pool).toBe("threads");
      expect(config.test?.maxWorkers).toBe(1);
      expect(config.test?.isolate).toBe(true);
      expect(config.test?.testNamePattern).toBeUndefined();
      expect(config.test?.passWithNoTests).not.toBe(true);
      expect(config.test?.execArgv).toBeUndefined();
    },
  );

  it("retains the full production test inventory without hard-coded file counts", () => {
    expect(mainConfig.test?.include).toEqual([
      "config/**/*.test.ts",
      "src/**/*.test.{ts,tsx}",
    ]);
    expect(mainConfig.test?.exclude).toBeUndefined();
    expect(mainConfig.test?.setupFiles).toEqual(["./src/test/setup.ts"]);
  });

  it("inherits the real guard setup after safe delegates in the negative canary", () => {
    // This intentionally failing child suite is separate from the complete
    // inventory. networkIsolation.test.ts checks its genuine lifecycle result.
    expect(denialConfig.test?.include).toEqual([
      "config/network-denial.fixture.ts",
    ]);
    expect(denialConfig.test?.setupFiles).toEqual([
      "./config/network-denial.setup.ts",
      "./src/test/setup.ts",
    ]);
    expect(denialConfig.test?.sequence?.setupFiles).toBe("list");
    const setup = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), "../src/test/setup.ts"),
      "utf8",
    );
    expect(setup).toMatch(/^import ["']\.\/networkIsolation["'];/u);
  });

  it("gracefully stops both browser servers without changing startup or readiness", () => {
    const servers = playwrightConfig.webServer;
    expect(Array.isArray(servers)).toBe(true);
    if (!Array.isArray(servers))
      throw new Error("Expected both browser servers");
    expect(
      servers.map(
        ({ command, url, reuseExistingServer, timeout, gracefulShutdown }) => ({
          command,
          url,
          reuseExistingServer,
          timeout,
          gracefulShutdown,
        }),
      ),
    ).toEqual([
      {
        command:
          "UV_CACHE_DIR=.cache/uv uv run uvicorn marketing_agents.api:create_app --factory --host 127.0.0.1 --port 8000",
        url: "http://127.0.0.1:8000/health/live",
        reuseExistingServer: false,
        timeout: 30_000,
        gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
      },
      {
        command: "node_modules/.bin/vite preview --host 127.0.0.1 --port 4173",
        url: "http://127.0.0.1:4173",
        reuseExistingServer: false,
        timeout: 30_000,
        gracefulShutdown: { signal: "SIGTERM", timeout: 10_000 },
      },
    ]);
    expect(playwrightConfig.timeout).toBeUndefined();
    expect(playwrightConfig.testDir).toBe("./e2e");
    expect(playwrightConfig.testIgnore).toBe(
      "**/obj-06-control-surface.spec.ts",
    );
    expect(playwrightConfig.workers).toBe(1);
    expect(playwrightConfig.retries).toBe(process.env.CI ? 1 : 0);
    expect(playwrightConfig.use?.serviceWorkers).toBe("block");
  });
});

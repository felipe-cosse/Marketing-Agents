import { defineConfig } from "vitest/config";

import mainConfig from "../vitest.config";

const configuredSetup = mainConfig.test?.setupFiles ?? [];

export default defineConfig({
  ...mainConfig,
  test: {
    ...mainConfig.test,
    include: ["config/network-denial.fixture.ts"],
    // Exercise the real configuration connection, not a test-only guard import.
    setupFiles: [
      "./config/network-denial.setup.ts",
      ...(typeof configuredSetup === "string"
        ? [configuredSetup]
        : configuredSetup),
    ],
    sequence: { ...mainConfig.test?.sequence, setupFiles: "list" },
    coverage: { enabled: false },
  },
});

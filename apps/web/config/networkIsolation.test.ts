import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, it } from "vitest";

const require = createRequire(import.meta.url);
const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const vitestCli = resolve(
  dirname(require.resolve("vitest/package.json")),
  "vitest.mjs",
);

it("DEL-07 fails swallowed network attempts through real Vitest lifecycle hooks", () => {
  const result = spawnSync(
    process.execPath,
    [
      vitestCli,
      "run",
      "--config",
      "config/vitest-network-denial.config.ts",
      "--reporter=json",
    ],
    { cwd: webRoot, encoding: "utf8", timeout: 20_000 },
  );
  expect(result.error).toBeUndefined();
  expect(result.signal).toBeNull();
  expect(result.status).toBe(1);
  const report = JSON.parse(result.stdout) as {
    numFailedTests: number;
    numPassedTests: number;
    testResults: {
      assertionResults: {
        title: string;
        status: string;
        failureMessages: string[];
      }[];
    }[];
  };
  expect(report.numFailedTests).toBe(2);
  expect(report.numPassedTests).toBe(1);
  const assertions = report.testResults.flatMap(
    (suite) => suite.assertionResults,
  );
  for (const title of [
    "a caught HTTP denial still fails the test lifecycle",
    "a caught fetch denial still fails the test lifecycle",
  ]) {
    const assertion = assertions.find((entry) => entry.title === title);
    expect(assertion?.status).toBe("failed");
    expect(assertion?.failureMessages.join("\n")).toContain(
      "forbidden real network operation(s), even if the transport error was caught",
    );
  }
  expect(
    assertions.find(
      (entry) =>
        entry.title === "an explicit in-memory response remains allowed",
    )?.status,
  ).toBe("passed");
}, 30_000);

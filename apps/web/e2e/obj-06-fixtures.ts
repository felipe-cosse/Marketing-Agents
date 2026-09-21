// OBJ-06 exercises the real native API, database and workers, never API fixtures.
import { test as base } from "@playwright/test";
import { isAbsolute } from "node:path";

import { installObj06NativeNetworkGuard } from "../config/obj-06-native-network";

export { expect } from "@playwright/test";
export type {
  Browser,
  BrowserContext,
  Locator,
  Page,
  Request,
  Response,
  TestInfo,
} from "@playwright/test";

interface Installation {
  readonly stateDirectory: string;
  readonly evidenceDirectory: string;
}

export const test = base.extend<{
  obj06NetworkIsolation: undefined;
  obj06Installation: Installation;
}>({
  obj06Installation: async ({ baseURL }, runFixture) => {
    const stateDirectory = process.env.OBJ06_STATE_DIRECTORY;
    const evidenceDirectory = process.env.OBJ06_EVIDENCE_DIRECTORY;
    if (
      baseURL !== "http://127.0.0.1:4173" ||
      stateDirectory === undefined ||
      !isAbsolute(stateDirectory) ||
      evidenceDirectory === undefined ||
      !isAbsolute(evidenceDirectory)
    )
      throw new Error("OBJ-06 requires its fresh supervised installation");
    await runFixture({ stateDirectory, evidenceDirectory });
  },
  obj06NetworkIsolation: [
    async ({ browser, context, baseURL }, runFixture) => {
      if (
        baseURL !== "http://127.0.0.1:4173" ||
        process.env.PW_TEST_REUSE_CONTEXT
      )
        throw new Error("OBJ-06 requires a fresh exact-origin browser context");
      const guards = [await installObj06NativeNetworkGuard(context, baseURL)];
      const original = browser.newContext.bind(browser);
      const originalNewPage = browser.newPage.bind(browser);
      browser.newPage = () =>
        Promise.reject(
          new Error(
            "OBJ-06 requires pages to be created through a guarded context",
          ),
        );
      browser.newContext = async (options = {}) => {
        if (options.serviceWorkers === "allow")
          throw new Error("OBJ-06 forbids service workers");
        const child = await original({ ...options, serviceWorkers: "block" });
        guards.push(await installObj06NativeNetworkGuard(child, baseURL));
        return child;
      };
      try {
        await runFixture(undefined);
      } finally {
        browser.newContext = original;
        browser.newPage = originalNewPage;
        for (const guard of guards) await guard.assertNoUnexpectedAttempts();
      }
    },
    { auto: true },
  ],
});

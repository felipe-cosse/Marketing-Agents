// OBJ-06 exercises the real native API, database and workers, never API fixtures.
import { test as base } from "@playwright/test";
import { isAbsolute } from "node:path";

import { installPlaywrightIsolation } from "../../../scripts/playwright-network-isolation.mjs";
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
    async ({ browser, context, baseURL, request, playwright }, runFixture) => {
      if (
        baseURL !== "http://127.0.0.1:4173" ||
        process.env.PW_TEST_REUSE_CONTEXT
      )
        throw new Error("OBJ-06 requires a fresh exact-origin browser context");
      const isolation = await installPlaywrightIsolation({
        browser,
        context,
        request,
        requestFactory: playwright.request,
        allowedOrigin: baseURL,
        installContextGuard: installObj06NativeNetworkGuard,
      });
      try {
        await runFixture(undefined);
      } finally {
        isolation.restore();
        await isolation.assertNoExternalAttempts();
      }
    },
    { auto: true },
  ],
});

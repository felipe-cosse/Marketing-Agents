import { test as base, type BrowserContext } from "@playwright/test";

import { installPlaywrightIsolation } from "../../../scripts/playwright-network-isolation.mjs";

export { expect } from "@playwright/test";
export type {
  Browser,
  BrowserContext,
  Locator,
  Page,
  Request,
  Response,
  Route,
  TestInfo,
} from "@playwright/test";

// DEL-07: guard the actual default context before a page is used, plus contexts
// created directly by a journey (WEB-09's mobile context is one example).
const guardedContexts = new WeakSet<BrowserContext>();
export const test = base.extend<{ del07NetworkIsolation: undefined }>({
  del07NetworkIsolation: [
    async ({ browser, baseURL, context, request, playwright }, runFixture) => {
      if (baseURL === undefined)
        throw new Error(
          "DEL-07 browser tests require an exact configured baseURL",
        );
      if (process.env.PW_TEST_REUSE_CONTEXT || guardedContexts.has(context)) {
        throw new Error(
          "DEL-07 browser policy does not support reused contexts",
        );
      }
      guardedContexts.add(context);
      const isolation = await installPlaywrightIsolation({
        browser,
        context,
        request,
        requestFactory: playwright.request,
        allowedOrigin: baseURL,
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

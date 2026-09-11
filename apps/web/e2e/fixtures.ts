import { test as base, type BrowserContext } from "@playwright/test";

import { installPlaywrightNetworkGuard } from "../../../scripts/browser-network-policy.mjs";

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
    async ({ browser, baseURL, context }, runFixture) => {
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
      const original = browser.newContext.bind(browser);
      const guards: Awaited<
        ReturnType<typeof installPlaywrightNetworkGuard>
      >[] = [await installPlaywrightNetworkGuard(context, baseURL)];
      browser.newContext = async (options = {}) => {
        if (options.serviceWorkers === "allow")
          throw new Error("DEL-07 browser tests cannot enable service workers");
        const context = await original({
          ...options,
          serviceWorkers: "block",
        });
        guards.push(await installPlaywrightNetworkGuard(context, baseURL));
        return context;
      };
      try {
        await runFixture(undefined);
      } finally {
        browser.newContext = original;
        for (const guard of guards) guard.assertNoExternalAttempts();
      }
    },
    { auto: true },
  ],
});

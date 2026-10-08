// AC-15: cover Playwright entry points that do not pass through browser routes.
import {
  installPlaywrightNetworkGuard,
  isAllowedBrowserUrl,
} from "./browser-network-policy.mjs";

function exactOrigin(value) {
  const url = new URL(value);
  if (
    !isAllowedBrowserUrl(value, value) ||
    !["http:", "https:"].includes(url.protocol) ||
    url.pathname !== "/" ||
    url.search ||
    url.hash
  )
    throw new Error("Playwright requires one exact loopback HTTP origin");
  return url.origin;
}

export function installPlaywrightRequestGuard(context, allowedOrigin, record) {
  const origin = exactOrigin(allowedOrigin);
  const originalFetch = context.fetch;
  context.fetch = async function (urlOrRequest, options = {}) {
    const deny = (kind) => {
      record(kind);
      throw new Error("Playwright API request blocked by test policy");
    };
    // The public contract accepts strings or Requests. Resolve relative strings
    // ourselves, not against an unknown/inherited APIRequestContext baseURL.
    let url;
    try {
      url = new URL(
        typeof urlOrRequest === "string" ? urlOrRequest : urlOrRequest.url(),
        origin,
      );
    } catch {
      return deny("api-request");
    }
    if (
      !["http:", "https:"].includes(url.protocol) ||
      !isAllowedBrowserUrl(url, origin) ||
      // Runtime JS can supply undocumented _innerFetch overrides. Never allow
      // them to replace the URL that was checked above.
      options.url !== undefined ||
      options.request !== undefined
    )
      return deny("api-request");
    // Preserve a real Request's method/body/headers; strings become absolute.
    const response = await originalFetch.call(
      this,
      typeof urlOrRequest === "string" ? url.href : urlOrRequest,
      { ...options, maxRedirects: 0 },
    );
    if (
      response.status() >= 300 &&
      response.status() < 400 &&
      response.status() !== 304
    ) {
      // Record before disposal so even a caught disposal error remains visible.
      record("api-redirect");
      await response.dispose();
      throw new Error("Playwright API redirect blocked by test policy");
    }
    return response;
  };
}

export async function installPlaywrightIsolation({
  browser,
  context,
  request,
  requestFactory,
  allowedOrigin,
  installContextGuard = installPlaywrightNetworkGuard,
}) {
  const origin = exactOrigin(allowedOrigin);
  const blocked = [];
  const record = (kind) => blocked.push(kind);
  const deny = (kind, message) => {
    record(kind);
    return Promise.reject(new Error(message));
  };
  const requests = new WeakSet();
  const guardRequest = (value) => {
    if (requests.has(value)) return;
    requests.add(value);
    installPlaywrightRequestGuard(value, origin, record);
  };
  const contexts = new WeakMap();
  const guards = [];
  const guardContext = (value) => {
    let pending = contexts.get(value);
    if (pending === undefined) {
      pending = (async () => {
        guardRequest(value.request);
        const guard = await installContextGuard(value, origin);
        guards.push(guard);
      })();
      contexts.set(value, pending);
    }
    return pending;
  };
  await guardContext(context);
  if (request !== undefined) guardRequest(request);
  const originalNewContext = browser.newContext;
  const originalNewPage = browser.newPage;
  const originalRequestContext = requestFactory?.newContext;
  const safeOptions = (options) => {
    if (options.proxy !== undefined || options.serviceWorkers === "allow")
      return false;
    if (options.baseURL === undefined) return true;
    try {
      return (
        new URL(options.baseURL).origin === origin &&
        isAllowedBrowserUrl(options.baseURL, origin)
      );
    } catch {
      return false;
    }
  };
  browser.newPage = () =>
    deny(
      "unguarded-page",
      "Playwright requires pages to be created through a guarded context",
    );
  browser.newContext = async function (options = {}) {
    if (!safeOptions(options))
      return deny("context-options", "Playwright context options blocked");
    const child = await originalNewContext.call(this, {
      ...options,
      serviceWorkers: "block",
    });
    try {
      await guardContext(child);
    } catch (error) {
      record("context-guard");
      await child.close();
      throw error;
    }
    return child;
  };
  if (requestFactory !== undefined) {
    requestFactory.newContext = async function (options = {}) {
      if (!safeOptions(options))
        return deny("request-options", "Playwright request options blocked");
      const child = await originalRequestContext.call(this, {
        ...options,
        baseURL: origin,
        maxRedirects: 0,
      });
      guardRequest(child);
      return child;
    };
  }
  return {
    blocked,
    async assertNoExternalAttempts() {
      if (blocked.length)
        throw new Error(
          `browser attempted ${String(blocked.length)} unapproved network request(s)`,
        );
      for (const guard of guards) {
        if (guard.assertNoExternalAttempts !== undefined)
          await guard.assertNoExternalAttempts();
        else await guard.assertNoUnexpectedAttempts();
      }
    },
    restore() {
      // Context/request guards remain installed for their entire lifetime. Only
      // shared factories are restored between tests; no retained context escapes.
      browser.newContext = originalNewContext;
      browser.newPage = originalNewPage;
      if (requestFactory !== undefined)
        requestFactory.newContext = originalRequestContext;
    },
  };
}

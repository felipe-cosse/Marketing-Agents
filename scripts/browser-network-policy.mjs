// DEL-07: one exact local origin, never an arbitrary loopback host or port.
import { isLoopbackHost } from "./node-network-guard.mjs";

export const DEFAULT_BROWSER_ORIGIN = "http://127.0.0.1:4173";
const installed = new WeakMap();

class BrowserNetworkAccessBlocked extends Error {
  constructor() {
    super("browser delegated transport blocked by test policy");
  }
}

function checkedOrigin(value) {
  const origin = new URL(value);
  if (
    !["http:", "https:"].includes(origin.protocol) ||
    !isLoopbackHost(origin.hostname) ||
    origin.username ||
    origin.password ||
    origin.pathname !== "/" ||
    origin.search ||
    origin.hash
  )
    throw new Error(
      "browser test origin must be one exact loopback HTTP origin",
    );
  return origin.origin;
}

export function isAllowedBrowserUrl(
  value,
  allowedOrigin = DEFAULT_BROWSER_ORIGIN,
) {
  try {
    const origin = checkedOrigin(allowedOrigin);
    const url = new URL(value);
    if (url.username || url.password) return false;
    if (url.protocol === "about:")
      return ["blank", "srcdoc"].includes(url.pathname);
    if (url.protocol === "data:") return true; // In-memory bytes; nested requests are still guarded.
    if (url.protocol === "blob:")
      return new URL(url.pathname).origin === origin;
    if (url.protocol === "ws:") url.protocol = "http:";
    else if (url.protocol === "wss:") url.protocol = "https:";
    return ["http:", "https:"].includes(url.protocol) && url.origin === origin;
  } catch {
    return false;
  }
}

export async function installPlaywrightNetworkGuard(
  target,
  allowedOrigin = DEFAULT_BROWSER_ORIGIN,
) {
  const origin = checkedOrigin(allowedOrigin);
  const context =
    typeof target.context === "function" ? target.context() : target;
  const existing = installed.get(context);
  if (existing) {
    if (existing.origin !== origin)
      throw new Error("browser context cannot change its approved origin");
    return existing.promise;
  }
  const promise = (async () => {
    const blocked = [];
    const observed = new WeakSet();
    const record = (request, kind) => {
      if (observed.has(request)) return;
      observed.add(request);
      // Never retain credentials, query strings, payloads, or arbitrary URLs.
      blocked.push(kind);
    };
    const auditRequest = (request) => {
      if (!isAllowedBrowserUrl(request.url(), origin)) record(request, "http");
    };
    const protectRoute = (route, request) => {
      const deny = async () => {
        record(request, "http");
        await route.abort("blockedbyclient");
        throw new BrowserNetworkAccessBlocked();
      };
      const validateOverride = async (options) => {
        if (
          options?.url !== undefined &&
          !isAllowedBrowserUrl(options.url, origin)
        )
          await deny();
      };
      const isRedirect = (status) =>
        status >= 300 && status < 400 && status !== 304;
      const fetch = async (options = {}) => {
        await validateOverride(options);
        // APIRequestContext redirects bypass browser routing. Never auto-follow.
        const response = await route.fetch({ ...options, maxRedirects: 0 });
        // All redirects are denied, including same-origin. Cache validation is
        // not a redirect; future redirect support needs an explicit safe design.
        if (isRedirect(response.status())) await deny();
        return response;
      };
      const forward = async (options = {}) => {
        try {
          const response = await fetch(options);
          await route.fulfill({ response });
        } catch (error) {
          if (!(error instanceof BrowserNetworkAccessBlocked)) throw error;
        }
      };
      const fulfill = async (options = {}) => {
        const status = options.status ?? options.response?.status() ?? 200;
        if (isRedirect(status)) {
          try {
            await deny();
          } catch (error) {
            if (!(error instanceof BrowserNetworkAccessBlocked)) throw error;
          }
          return;
        }
        await route.fulfill(options);
      };
      return new Proxy(route, {
        get(target, name) {
          if (name === "fetch") return fetch;
          if (name === "continue") return forward;
          if (name === "fulfill") return fulfill;
          if (name === "fallback")
            return async (options = {}) => {
              await validateOverride(options);
              await target.fallback(options);
            };
          const value = Reflect.get(target, name, target);
          return typeof value === "function" ? value.bind(target) : value;
        },
      });
    };
    const httpHandler =
      (handler) =>
      async (route, request = route.request()) => {
        if (!isAllowedBrowserUrl(request.url(), origin)) {
          record(request, "http");
          await route.abort("blockedbyclient");
          return;
        }
        await handler(protectRoute(route, request), request);
      };
    const websocketHandler = (handler) => (socket) => {
      if (!isAllowedBrowserUrl(socket.url(), origin)) {
        record(socket, "websocket");
        socket.close({ code: 1008, reason: "test transport policy" });
        return;
      }
      return handler(socket);
    };
    const protectRouting = (surface) => {
      const originalRoute = surface.route;
      surface.route = function (pattern, handler, options) {
        return originalRoute.call(this, pattern, httpHandler(handler), options);
      };
      const originalWebSocketRoute = surface.routeWebSocket;
      surface.routeWebSocket = function (pattern, handler) {
        return originalWebSocketRoute.call(
          this,
          pattern,
          websocketHandler(handler),
        );
      };
    };
    // Independent auditing catches fulfilled requests too; wrapping later route
    // handlers prevents page.route(... route.continue()) bypassing context.route.
    context.on("request", auditRequest);
    context.on("page", protectRouting);
    for (const page of context.pages()) protectRouting(page);
    protectRouting(context);
    // Native browser forwarding may follow redirects without a second route
    // callback. Fetch without redirects and fulfill the validated response.
    await context.route("**/*", (route) => route.continue());
    await context.routeWebSocket("**/*", (socket) => socket.connectToServer());
    return {
      blocked,
      assertNoExternalAttempts() {
        if (blocked.length)
          throw new Error(
            `browser attempted ${String(blocked.length)} unapproved network request(s)`,
          );
      },
    };
  })();
  installed.set(context, { origin, promise });
  return promise;
}

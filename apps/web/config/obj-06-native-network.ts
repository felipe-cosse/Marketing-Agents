// OBJ-06 keeps Chromium's own Fetch Metadata while denying redirect escape.
import type {
  BrowserContext,
  CDPSession,
  Page,
  Request,
} from "@playwright/test";

interface PausedResponse {
  readonly requestId: string;
  readonly responseStatusCode?: number;
  readonly networkId?: string;
}

interface FailedRequest {
  readonly requestId: string;
  readonly canceled?: boolean;
  readonly errorText: string;
}

// CDP can invalidate an interception after React aborts an in-flight read.
// Never infer cancellation from the command error alone: require the matching
// browser Network.loadingFailed event, with a bounded event-ordering allowance.
export class Obj06CancellationEvidence {
  private readonly cancelled = new Set<string>();
  private readonly waiters = new Map<string, () => void>();

  record(event: FailedRequest): void {
    if (event.canceled !== true || event.errorText !== "net::ERR_ABORTED")
      return;
    this.cancelled.add(event.requestId);
    this.waiters.get(event.requestId)?.();
  }

  async confirms(
    networkId: string | undefined,
    error: unknown,
  ): Promise<boolean> {
    if (
      networkId === undefined ||
      !(error instanceof Error) ||
      !error.message.endsWith(
        "Protocol error (Fetch.continueResponse): Invalid InterceptionId.",
      )
    )
      return false;
    if (!this.cancelled.has(networkId)) {
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, 100);
        this.waiters.set(networkId, () => {
          clearTimeout(timer);
          resolve();
        });
      });
      this.waiters.delete(networkId);
    }
    return this.cancelled.delete(networkId);
  }
}

export interface Obj06NetworkGuard {
  readonly blocked: readonly string[];
  assertNoUnexpectedAttempts(): Promise<void>;
}

export function obj06AllowedUrl(value: string, origin: string): boolean {
  try {
    const url = new URL(value);
    if (url.username || url.password) return false;
    if (url.protocol === "about:") return url.pathname === "blank";
    if (url.protocol === "data:") return true;
    if (url.protocol === "blob:")
      return new URL(url.pathname).origin === origin;
    if (url.protocol === "ws:") url.protocol = "http:";
    return url.protocol === "http:" && url.origin === origin;
  } catch {
    return false;
  }
}

export async function installObj06NativeNetworkGuard(
  context: BrowserContext,
  allowedOrigin: string,
): Promise<Obj06NetworkGuard> {
  const parsed = new URL(allowedOrigin);
  if (
    parsed.protocol !== "http:" ||
    parsed.hostname !== "127.0.0.1" ||
    !parsed.port ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash
  )
    throw new Error("OBJ-06 requires one exact 127.0.0.1 HTTP origin");
  const origin = parsed.origin;
  const blocked: string[] = [];
  const seen = new WeakSet<Request>();
  const sessions = new WeakMap<Page, Promise<CDPSession>>();
  const pending = new Set<Promise<void>>();
  const failures = new Set<string>();
  const record = (request: Request): void => {
    if (!seen.has(request)) {
      seen.add(request);
      blocked.push("http");
    }
  };
  const protectPage = (page: Page): Promise<CDPSession> => {
    let session = sessions.get(page);
    if (session !== undefined) return session;
    session = (async () => {
      const cdp = await context.newCDPSession(page);
      const cancellations = new Obj06CancellationEvidence();
      cdp.on("Network.loadingFailed", (event: FailedRequest) =>
        cancellations.record(event),
      );
      await cdp.send("Network.enable");
      cdp.on("Fetch.requestPaused", (event: PausedResponse) => {
        const task = (async () => {
          const status = event.responseStatusCode;
          if (
            status !== undefined &&
            status >= 300 &&
            status < 400 &&
            status !== 304
          ) {
            blocked.push("redirect");
            await cdp.send("Fetch.failRequest", {
              requestId: event.requestId,
              errorReason: "BlockedByClient",
            });
          } else {
            try {
              await cdp.send("Fetch.continueResponse", {
                requestId: event.requestId,
              });
            } catch (error) {
              if (!(await cancellations.confirms(event.networkId, error)))
                throw error;
            }
          }
        })().catch(() => {
          // Categorical diagnostics never retain URLs, headers or payloads.
          failures.add("response-command");
        });
        pending.add(task);
        void task.finally(() => pending.delete(task));
      });
      await cdp.send("Fetch.enable", {
        patterns: [{ urlPattern: "*", requestStage: "Response" }],
      });
      return cdp;
    })();
    sessions.set(page, session);
    return session;
  };
  // Hold navigation until response interception is ready. Chromium does not
  // expose a frame for some first-popup requests: those must fail closed.
  context.on("request", (request) => {
    if (!obj06AllowedUrl(request.url(), origin)) record(request);
  });
  const originalNewPage = context.newPage.bind(context);
  context.newPage = async () => {
    const page = await originalNewPage();
    await protectPage(page);
    return page;
  };
  for (const page of context.pages()) await protectPage(page);
  await context.route("**/*", async (route, request) => {
    if (!obj06AllowedUrl(request.url(), origin)) {
      record(request);
      await route.abort("blockedbyclient");
      return;
    }
    let page: Page;
    try {
      page = request.frame().page();
    } catch {
      record(request);
      await route.abort("blockedbyclient");
      return;
    }
    try {
      await protectPage(page);
    } catch {
      failures.add("response-attachment");
      await route.abort("blockedbyclient");
      return;
    }
    // Do not supply headers: Chromium must originate Origin/Fetch Metadata.
    await route.continue();
  });
  await context.routeWebSocket("**/*", (socket) => {
    if (!obj06AllowedUrl(socket.url(), origin)) {
      blocked.push("websocket");
      void socket.close({ code: 1008, reason: "OBJ-06 local transport only" });
      return;
    }
    socket.connectToServer();
  });
  // This journey must exercise real APIs. Later fixture registration may not
  // replace this transport boundary or fulfill a synthetic application result.
  const lockRouting = (surface: BrowserContext | Page): void => {
    const refuse = (): Promise<never> =>
      Promise.reject(
        new Error("OBJ-06 does not permit additional route handlers"),
      );
    surface.route = refuse;
    surface.routeWebSocket = refuse;
    surface.routeFromHAR = refuse;
    surface.unroute = refuse;
    surface.unrouteAll = refuse;
  };
  context.on("page", lockRouting);
  for (const page of context.pages()) lockRouting(page);
  lockRouting(context);
  return {
    blocked,
    async assertNoUnexpectedAttempts() {
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        await Promise.race([
          (async () => {
            while (pending.size > 0) await Promise.all([...pending]);
          })(),
          new Promise<never>((_resolve, reject) => {
            timer = setTimeout(
              () =>
                reject(
                  new Error(
                    "OBJ-06 response interception did not quiesce within 2 seconds",
                  ),
                ),
              2_000,
            );
          }),
        ]);
      } finally {
        clearTimeout(timer);
      }
      if (failures.size > 0)
        throw new Error(
          `OBJ-06 response interception failed (${[...failures].join(",")})`,
        );
      if (blocked.length > 0)
        throw new Error(
          `OBJ-06 blocked ${String(blocked.length)} unexpected requests`,
        );
    },
  };
}

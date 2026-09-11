export const DEFAULT_BROWSER_ORIGIN: "http://127.0.0.1:4173";

export interface BrowserNetworkLedger {
  readonly blocked: readonly ("http" | "websocket")[];
  assertNoExternalAttempts(): void;
}

export function isAllowedBrowserUrl(
  value: string | URL,
  allowedOrigin?: string,
): boolean;

export function installPlaywrightNetworkGuard(
  target: unknown,
  allowedOrigin?: string,
): Promise<BrowserNetworkLedger>;

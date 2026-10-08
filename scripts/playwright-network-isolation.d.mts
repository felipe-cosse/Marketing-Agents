interface ContextNetworkLedger {
  readonly blocked: readonly string[];
  assertNoExternalAttempts?(): void | Promise<void>;
  assertNoUnexpectedAttempts?(): Promise<void>;
}

export interface PlaywrightIsolation {
  readonly blocked: readonly string[];
  assertNoExternalAttempts(): Promise<void>;
  restore(): void;
}

export function installPlaywrightRequestGuard(
  context: unknown,
  allowedOrigin: string,
  record: (kind: string) => unknown,
): void;

export function installPlaywrightIsolation<TContext>(options: {
  browser: unknown;
  context: TContext;
  request?: unknown;
  requestFactory?: unknown;
  allowedOrigin: string;
  installContextGuard?: (
    context: TContext,
    origin: string,
  ) => Promise<ContextNetworkLedger>;
}): Promise<PlaywrightIsolation>;

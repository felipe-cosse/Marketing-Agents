import { afterAll, afterEach } from "vitest";

import {
  installNetworkGuard,
  NetworkAccessBlocked,
} from "../../../../scripts/node-network-guard.mjs";

let unexpectedAttempts = 0;
const restore = installNetworkGuard({
  allowLoopback: false,
  onBlocked: () => {
    unexpectedAttempts += 1;
  },
});

export function assertNoUnexpectedNetworkAttempts(): void {
  if (unexpectedAttempts > 0) {
    throw new Error(
      `Test attempted ${String(unexpectedAttempts)} forbidden real network operation(s), even if the transport error was caught. Use an explicit in-memory mock.`,
    );
  }
}

// Used only by network-policy canaries: acknowledgement requires one actual
// guard denial. It cannot turn an arbitrary exception or missing guard green.
export function expectBlockedNetworkAttempt(operation: () => unknown): void {
  const before = unexpectedAttempts;
  try {
    operation();
  } catch (error) {
    if (
      error instanceof NetworkAccessBlocked &&
      unexpectedAttempts === before + 1
    ) {
      unexpectedAttempts -= 1;
      return;
    }
    throw error;
  }
  throw new Error(
    "Expected the installed global network guard to deny the operation",
  );
}

afterEach(() => {
  try {
    assertNoUnexpectedNetworkAttempts();
  } finally {
    unexpectedAttempts = 0;
  }
});
afterAll(() => {
  try {
    assertNoUnexpectedNetworkAttempts();
  } finally {
    restore();
  }
});

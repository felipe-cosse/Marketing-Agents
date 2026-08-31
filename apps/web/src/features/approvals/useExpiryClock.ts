import { useEffect, useMemo, useState } from "react";

const MAX_CLOCK_REFRESH_MS = 60_000;

function parseExpiryKey(expiryKey: string): readonly number[] {
  if (expiryKey.length === 0) return Object.freeze([]);
  return Object.freeze(
    expiryKey
      .split("\u0000")
      .map((value) => Date.parse(value))
      .filter(Number.isFinite)
      .sort((left, right) => left - right),
  );
}

/**
 * Advances at the nearest authoritative expiry boundary while the page stays
 * open. The one-minute ceiling also bounds clock drift and long-lived tabs.
 */
export function useExpiryClock(expiresAt: readonly string[]): number {
  const expiryKey = expiresAt.join("\u0000");
  const expiryTimes = useMemo(() => parseExpiryKey(expiryKey), [expiryKey]);
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    const nextExpiry = expiryTimes.find((value) => value > nowMs);
    if (nextExpiry === undefined) return undefined;
    const delay = Math.min(
      Math.max(nextExpiry - Date.now() + 1, 0),
      MAX_CLOCK_REFRESH_MS,
    );
    const timer = window.setTimeout(() => setNowMs(Date.now()), delay);
    return () => window.clearTimeout(timer);
  }, [expiryTimes, nowMs]);

  return nowMs;
}

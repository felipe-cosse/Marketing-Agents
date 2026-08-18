const PASSIVE_SCHEMES = new Set(["about:", "blob:", "data:"]);

export function isAllowedBrowserUrl(value) {
  const url = value instanceof URL ? value : new URL(value);
  if (PASSIVE_SCHEMES.has(url.protocol)) return true;
  if (!["http:", "https:", "ws:", "wss:"].includes(url.protocol)) return false;
  const host = url.hostname.toLowerCase().replace(/\.$/, "");
  return host === "localhost" || host === "::1" || host.startsWith("127.");
}

export async function installPlaywrightNetworkGuard(page) {
  const blocked = [];
  await page.route("**/*", async (route) => {
    const url = route.request().url();
    if (!isAllowedBrowserUrl(url)) {
      blocked.push(url);
      await route.abort("blockedbyclient");
      return;
    }
    await route.continue();
  });
  return {
    blocked,
    assertNoExternalAttempts() {
      if (blocked.length) throw new Error(`browser attempted ${blocked.length} external request(s)`);
    },
  };
}

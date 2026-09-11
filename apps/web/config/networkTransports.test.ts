import dns from "node:dns";
import dnsPromises from "node:dns/promises";
import http from "node:http";
import https from "node:https";
import net, { connect } from "node:net";
import { afterEach, describe, expect, it, vi } from "vitest";

import { expectBlockedNetworkAttempt } from "../src/test/networkIsolation";

afterEach(() => vi.unstubAllGlobals());

describe("DEL-07 globally installed Vitest network denial", () => {
  it("denies external and real loopback transports without invoking them", () => {
    for (const operation of [
      // Loopback literals keep these global-installation canaries safe even if
      // setup is removed. External destinations are covered with preinstalled
      // in-memory delegates in tests/network/node_network_guard.test.mjs.
      () => net.connect({ host: "127.0.0.1", port: 9 }),
      () => connect({ host: "127.0.0.1", port: 9 }),
      () => new net.Socket().connect(9, "127.0.0.1"),
      () => http.get("http://127.0.0.1:9"),
      () => https.get("https://127.0.0.1:9/"),
      () => dns.lookup("127.0.0.1", () => undefined),
      () => dnsPromises.lookup("127.0.0.1"),
      () => fetch("http://127.0.0.1:9/"),
    ])
      expectBlockedNetworkAttempt(operation);
  });

  it("allows an explicitly installed in-memory fetch response", async () => {
    const mocked = vi.fn().mockResolvedValue(new Response('{"ok":true}'));
    vi.stubGlobal("fetch", mocked);
    const response = await fetch("/api/v1/session");
    await expect(response.json()).resolves.toEqual({ ok: true });
    expect(mocked).toHaveBeenCalledOnce();
  });

  it("still blocks fetch after the preceding test restores its mock", () => {
    expectBlockedNetworkAttempt(() => fetch("http://127.0.0.1:9/"));
  });

  it("cannot acknowledge a no-op or an unrelated error as a guard canary", () => {
    expect(() => expectBlockedNetworkAttempt(() => undefined)).toThrow(
      "Expected the installed",
    );
    expect(() =>
      expectBlockedNetworkAttempt(() => {
        throw new Error("fixture error");
      }),
    ).toThrow("fixture error");
  });
});

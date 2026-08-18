// Requirement SAFE-11: Node net/http/https/dns/fetch canaries must be blocked synchronously.
import assert from "node:assert/strict";
import dns from "node:dns";
import http from "node:http";
import https from "node:https";
import net from "node:net";
import test from "node:test";

import { NetworkAccessBlocked, installNetworkGuard, isLoopbackHost } from "../../scripts/node-network-guard.mjs";

test("SAFE-11 blocks every supported Node egress primitive", () => {
  const restore = installNetworkGuard();
  try {
    assert.throws(() => net.connect({ host: "203.0.113.10", port: 443 }), NetworkAccessBlocked);
    assert.throws(() => net.createConnection(443, "example.invalid"), NetworkAccessBlocked);
    assert.throws(() => http.get("http://example.invalid/path"), NetworkAccessBlocked);
    assert.throws(() => https.request("https://example.invalid/path"), NetworkAccessBlocked);
    assert.throws(() => dns.lookup("example.invalid", () => {}), NetworkAccessBlocked);
    assert.throws(() => dns.resolve4("example.invalid", () => {}), NetworkAccessBlocked);
    if (typeof globalThis.fetch === "function") {
      assert.throws(() => globalThis.fetch("https://example.invalid/path"), NetworkAccessBlocked);
    }
  } finally {
    restore();
  }
});

test("SAFE-11 loopback classifier rejects wildcard and documentation ranges", () => {
  for (const host of ["localhost", "127.0.0.1", "127.8.9.10", "::1"]) assert.equal(isLoopbackHost(host), true);
  for (const host of ["0.0.0.0", "192.0.2.1", "2001:db8::1", "example.com"]) assert.equal(isLoopbackHost(host), false);
});

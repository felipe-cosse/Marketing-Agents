// Requirement SAFE-11: Node net/http/https/dns/fetch canaries must be blocked synchronously.
import assert from "node:assert/strict";
import dgram from "node:dgram";
import dns from "node:dns";
import dnsPromises from "node:dns/promises";
import http from "node:http";
import https from "node:https";
import { syncBuiltinESMExports } from "node:module";
import net, { connect as namedConnect } from "node:net";
import test from "node:test";
import tls from "node:tls";

import {
  NetworkAccessBlocked,
  installNetworkGuard,
  isLoopbackHost,
} from "../../scripts/node-network-guard.mjs";

// Install in-memory delegates BEFORE the guard. A broken guard can only reach
// these tripwires, never a real socket, resolver, or fetch implementation.
function withTransportTripwires(run) {
  const calls = [];
  const restorers = [];
  const sentinel = Symbol("in-memory-transport");
  const replace = (object, name) => {
    const original = object[name];
    if (typeof original !== "function") return;
    object[name] = function (...args) {
      calls.push({ name, args, receiver: this });
      return sentinel;
    };
    restorers.push(() => {
      object[name] = original;
    });
  };
  for (const name of ["connect", "createConnection"]) replace(net, name);
  replace(net.Socket.prototype, "connect");
  replace(tls, "connect");
  for (const module of [http, https]) {
    for (const name of ["request", "get"]) replace(module, name);
  }
  for (const name of ["connect", "send"]) replace(dgram.Socket.prototype, name);
  for (const module of [
    dns,
    dnsPromises,
    dns.Resolver.prototype,
    dnsPromises.Resolver.prototype,
  ]) {
    for (const name of Object.getOwnPropertyNames(module)) {
      if (
        name === "lookup" ||
        name === "lookupService" ||
        name === "reverse" ||
        name.startsWith("resolve")
      )
        replace(module, name);
    }
  }
  replace(globalThis, "fetch");
  syncBuiltinESMExports();
  try {
    run({ calls, sentinel });
  } finally {
    for (const restore of restorers.toReversed()) restore();
    syncBuiltinESMExports();
  }
}

test("SAFE-11 blocks supported Node egress primitives before their delegates", () => {
  withTransportTripwires(({ calls }) => {
    const restore = installNetworkGuard();
    const udp = Object.create(dgram.Socket.prototype);
    const operations = [
      () => net.connect({ host: "203.0.113.10", port: 443 }),
      () => net.createConnection(443, "127.evil.invalid"),
      () => net.connect("443", "203.0.113.10"),
      () => new net.Socket().connect(443, "203.0.113.10"),
      () =>
        net.Socket.prototype.connect.call({}, [
          { host: "203.0.113.10", port: 443 },
        ]),
      () => tls.connect(443, { host: "203.0.113.10" }),
      () => tls.TLSSocket.prototype.connect.call({}, 443, "203.0.113.10"),
      () => http.get("http://example.invalid/path"),
      () => http.request("http://127.0.0.1", { hostname: "example.invalid" }),
      () => http.request({ host: "127.evil.invalid" }),
      () => https.request(new URL("https://example.invalid/path")),
      () => https.get({ hostname: "example.invalid" }),
      () => udp.connect(443, "203.0.113.10"),
      () => udp.send(Buffer.from("canary"), 443, "203.0.113.10"),
      () => udp.send(Buffer.from("canary"), 0, 6, 443, "203.0.113.10"),
      () => udp.send(Buffer.from("canary")),
      () => dns.lookup("example.invalid", () => {}),
      () => dns.lookupService("127.0.0.1", 443, () => {}),
      () => dns.resolve4("localhost", () => {}),
      () => dns.reverse("127.0.0.1", () => {}),
      () => dnsPromises.lookup("example.invalid"),
      () => dnsPromises.resolve4("example.invalid"),
      () => new dns.Resolver().resolve4("example.invalid", () => {}),
      () => new dnsPromises.Resolver().resolve4("example.invalid"),
      () =>
        globalThis.fetch("https://user:secret@example.invalid/?private=value"),
      () => globalThis.fetch(new Request("https://example.invalid")),
    ];
    try {
      for (const operation of operations)
        assert.throws(operation, NetworkAccessBlocked);
      assert.equal(calls.length, 0);
    } finally {
      restore();
    }
  });
});

test("SAFE-11 loopback classifier rejects wildcard and documentation ranges", () => {
  for (const host of [
    "localhost",
    "LOCALHOST.",
    "localhost.localdomain",
    "127.0.0.1",
    "127.8.9.10",
    "::1",
    "[::1]",
    "0:0:0:0:0:0:0:1",
  ])
    assert.equal(isLoopbackHost(host), true, String(host));
  for (const host of [
    undefined,
    null,
    "",
    "0.0.0.0",
    "192.0.2.1",
    "2001:db8::1",
    "example.com",
    "127.evil.invalid",
    "127.1",
    "127.0.0.999",
    "127.00.0.1",
    "localhost.evil.invalid",
    "::1%lo0",
  ])
    assert.equal(isLoopbackHost(host), false, String(host));
});

test("DEL-07 permits explicit local delegates by default and restores named ESM exports", () => {
  withTransportTripwires(({ calls, sentinel }) => {
    const delegate = net.connect;
    const restore = installNetworkGuard();
    const socket = new net.Socket();
    try {
      assert.throws(
        () => namedConnect(443, "203.0.113.10"),
        NetworkAccessBlocked,
      );
      for (const operation of [
        () => namedConnect(9, "127.0.0.1"),
        () => socket.connect({ path: "/tmp/in-memory-canary.sock" }),
        () => http.get("http://[::1]:9"),
        () => dnsPromises.lookup("127.0.0.1"),
        () => fetch("http://127.0.0.1:9"),
      ])
        assert.equal(operation(), sentinel);
      assert.equal(calls.length, 5);
      assert.equal(calls[1].receiver, socket);
    } finally {
      restore();
    }
    assert.equal(net.connect, delegate);
    assert.equal(namedConnect, delegate);
  });
});

test("DEL-07 strict mode denies local/Unix transports and reports caught errors without private arguments", () => {
  withTransportTripwires(({ calls }) => {
    const denied = [];
    const restore = installNetworkGuard({
      allowLoopback: false,
      onBlocked: (error) => denied.push(error),
    });
    try {
      for (const operation of [
        () => net.connect(9, "127.0.0.1"),
        () => new net.Socket().connect("/tmp/in-memory-canary.sock"),
        () => dnsPromises.lookup("127.0.0.1"),
        () => http.request({ socketPath: "/tmp/in-memory-canary.sock" }),
        () => fetch("https://user:secret@example.invalid/?private=value"),
      ])
        assert.throws(operation, NetworkAccessBlocked);
      assert.equal(denied.length, 5);
      assert.equal(calls.length, 0);
      for (const error of denied) {
        assert.equal(error.code, "MARKETING_AGENTS_EXTERNAL_NETWORK_BLOCKED");
        assert.doesNotMatch(error.message, /secret|private|example|canary/);
      }
    } finally {
      restore();
    }
  });
});

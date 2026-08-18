import dns from "node:dns";
import http from "node:http";
import https from "node:https";
import net from "node:net";

export class NetworkAccessBlocked extends Error {
  constructor(host) {
    super(`external network access blocked for host ${JSON.stringify(host)}`);
    this.name = "NetworkAccessBlocked";
    this.code = "MARKETING_AGENTS_EXTERNAL_NETWORK_BLOCKED";
  }
}

export function isLoopbackHost(host) {
  if (host === undefined || host === null || host === "") return true;
  const normalized = String(host).toLowerCase().replace(/\.$/, "").split("%", 1)[0];
  return (
    normalized === "localhost" ||
    normalized === "localhost.localdomain" ||
    normalized === "::1" ||
    normalized === "[::1]" ||
    normalized.startsWith("127.")
  );
}

function hostFromNetArgs(args) {
  const first = args[0];
  if (first && typeof first === "object") return first.host ?? first.hostname ?? (first.path ? undefined : "localhost");
  if (typeof first === "number") return typeof args[1] === "string" ? args[1] : "localhost";
  if (typeof first === "string" && first.startsWith("/")) return undefined;
  return first;
}

function hostFromHttpArgs(args) {
  const first = args[0];
  if (first instanceof URL) return first.hostname;
  if (typeof first === "string") return new URL(first).hostname;
  if (first && typeof first === "object") return first.hostname ?? first.host ?? "localhost";
  return "localhost";
}

function assertLoopback(host) {
  if (!isLoopbackHost(host)) throw new NetworkAccessBlocked(host);
}

export function installNetworkGuard() {
  const originals = {
    netConnect: net.connect,
    netCreateConnection: net.createConnection,
    httpRequest: http.request,
    httpGet: http.get,
    httpsRequest: https.request,
    httpsGet: https.get,
    dnsLookup: dns.lookup,
    dnsResolve: dns.resolve,
    dnsResolve4: dns.resolve4,
    dnsResolve6: dns.resolve6,
    fetch: globalThis.fetch,
  };

  net.connect = function guardedNetConnect(...args) {
    assertLoopback(hostFromNetArgs(args));
    return originals.netConnect.apply(this, args);
  };
  net.createConnection = function guardedCreateConnection(...args) {
    assertLoopback(hostFromNetArgs(args));
    return originals.netCreateConnection.apply(this, args);
  };
  http.request = function guardedHttpRequest(...args) {
    assertLoopback(hostFromHttpArgs(args));
    return originals.httpRequest.apply(this, args);
  };
  http.get = function guardedHttpGet(...args) {
    assertLoopback(hostFromHttpArgs(args));
    return originals.httpGet.apply(this, args);
  };
  https.request = function guardedHttpsRequest(...args) {
    assertLoopback(hostFromHttpArgs(args));
    return originals.httpsRequest.apply(this, args);
  };
  https.get = function guardedHttpsGet(...args) {
    assertLoopback(hostFromHttpArgs(args));
    return originals.httpsGet.apply(this, args);
  };
  for (const [name, original] of [
    ["lookup", originals.dnsLookup],
    ["resolve", originals.dnsResolve],
    ["resolve4", originals.dnsResolve4],
    ["resolve6", originals.dnsResolve6],
  ]) {
    dns[name] = function guardedDns(host, ...args) {
      assertLoopback(host);
      return original.call(this, host, ...args);
    };
  }
  if (typeof originals.fetch === "function") {
    globalThis.fetch = function guardedFetch(input, ...args) {
      const url = input instanceof URL ? input : new URL(typeof input === "string" ? input : input.url);
      assertLoopback(url.hostname);
      return originals.fetch.call(this, input, ...args);
    };
  }

  return function restoreNetworkGuard() {
    net.connect = originals.netConnect;
    net.createConnection = originals.netCreateConnection;
    http.request = originals.httpRequest;
    http.get = originals.httpGet;
    https.request = originals.httpsRequest;
    https.get = originals.httpsGet;
    dns.lookup = originals.dnsLookup;
    dns.resolve = originals.dnsResolve;
    dns.resolve4 = originals.dnsResolve4;
    dns.resolve6 = originals.dnsResolve6;
    if (typeof originals.fetch === "function") globalThis.fetch = originals.fetch;
  };
}

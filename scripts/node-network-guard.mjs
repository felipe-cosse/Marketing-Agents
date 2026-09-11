// Application-level test guard. Kernel network isolation remains a separate gate.
import dgram from "node:dgram";
import dns from "node:dns";
import dnsPromises from "node:dns/promises";
import http from "node:http";
import https from "node:https";
import { syncBuiltinESMExports } from "node:module";
import net from "node:net";
import tls from "node:tls";

export class NetworkAccessBlocked extends Error {
  constructor() {
    // Do not expose URLs, credentials, query strings, or arbitrary arguments.
    super("external network access blocked by the test transport policy");
    this.name = "NetworkAccessBlocked";
    this.code = "MARKETING_AGENTS_EXTERNAL_NETWORK_BLOCKED";
  }
}

export function isLoopbackHost(host) {
  if (typeof host !== "string" || host.length === 0) return false;
  const normalized = host.toLowerCase().replace(/\.$/, "");
  if (normalized === "localhost" || normalized === "localhost.localdomain")
    return true;
  const literal =
    normalized.startsWith("[") && normalized.endsWith("]")
      ? normalized.slice(1, -1)
      : normalized;
  if (net.isIP(literal) === 4) return literal.split(".")[0] === "127";
  if (net.isIP(literal) !== 6 || literal.includes("%")) return false;
  return new URL(`http://[${literal}]/`).hostname === "[::1]";
}

const UNIX_SOCKET = Symbol("unix-socket");

function netDestination(args) {
  const first = args[0];
  // Node's net.createConnection calls Socket.connect with normalized arguments.
  if (Array.isArray(first)) return netDestination(first);
  if (first && typeof first === "object") {
    return first.path !== undefined
      ? UNIX_SOCKET
      : (first.host ?? first.hostname ?? "localhost");
  }
  // Node treats nonnegative numeric strings as TCP ports, not pipe names.
  if (
    typeof first === "number" ||
    (typeof first === "string" && Number(first) >= 0)
  )
    return typeof args[1] === "string" ? args[1] : "localhost";
  if (typeof first === "string") return UNIX_SOCKET;
  return undefined;
}

function httpDestinations(args) {
  const destinations = [];
  for (const argument of args.slice(0, 2)) {
    if (typeof argument === "string") {
      destinations.push(new URL(argument).hostname);
    } else if (argument && typeof argument === "object") {
      // Includes URL objects from either Node or jsdom and URL+options overloads.
      if (argument.hostname !== undefined) destinations.push(argument.hostname);
      else if (argument.host !== undefined) destinations.push(argument.host);
      else if (argument.socketPath !== undefined)
        destinations.push(UNIX_SOCKET);
    }
  }
  return destinations.length ? destinations : ["localhost"];
}

/**
 * Default: permit literal loopback/Unix transport, with no real DNS queries.
 * Vitest uses allowLoopback:false: real outbound networking is denied; fetch mocks
 * remain in-memory seams. Captured native function values or malicious monkey
 * patches are not a sandbox; use the separate network-none container gate too.
 */
export function installNetworkGuard({ allowLoopback = true, onBlocked } = {}) {
  const restorers = [];
  const deny = () => {
    const error = new NetworkAccessBlocked();
    onBlocked?.(error);
    throw error;
  };
  const check = (host) => {
    if (!allowLoopback || (host !== UNIX_SOCKET && !isLoopbackHost(host)))
      deny();
  };
  const patch = (object, name, inspect) => {
    const original = object[name];
    if (typeof original !== "function") return;
    object[name] = function guardedTransport(...args) {
      inspect(args);
      return Reflect.apply(original, this, args);
    };
    restorers.push(() => {
      object[name] = original;
    });
  };

  for (const name of ["connect", "createConnection"]) {
    patch(net, name, (args) => check(netDestination(args)));
  }
  patch(net.Socket.prototype, "connect", (args) => check(netDestination(args)));
  patch(tls, "connect", (args) => {
    check(netDestination(args));
    // tls.connect(port, options) may override the destination in argument two.
    if (args[1] && typeof args[1] === "object")
      check(netDestination([args[1]]));
  });
  for (const module of [http, https]) {
    for (const name of ["request", "get"]) {
      patch(module, name, (args) => httpDestinations(args).forEach(check));
    }
  }
  patch(dgram.Socket.prototype, "connect", (args) =>
    check(args[1] ?? "127.0.0.1"),
  );
  patch(dgram.Socket.prototype, "send", (args) => {
    const address =
      typeof args[1] === "number" && typeof args[2] === "number"
        ? args[4]
        : args[2];
    // An omitted address may use an already-connected peer: do not guess it.
    check(typeof address === "string" ? address : undefined);
  });

  for (const module of [
    dns,
    dnsPromises,
    dns.Resolver.prototype,
    dnsPromises.Resolver.prototype,
  ]) {
    for (const name of Object.getOwnPropertyNames(module)) {
      if (name === "lookup") {
        patch(module, name, (args) => {
          check(args[0]);
          // Literal lookup needs no resolver traffic. Hostnames require a mock.
          if (net.isIP(args[0]) === 0) deny();
        });
      } else if (
        name === "lookupService" ||
        name === "reverse" ||
        name.startsWith("resolve")
      ) {
        // Even resolving "localhost" can query a non-loopback DNS server.
        patch(module, name, deny);
      }
    }
  }
  patch(globalThis, "fetch", (args) => {
    if (!allowLoopback) deny();
    const input = args[0];
    const url = new URL(
      typeof input === "string" ? input : (input?.url ?? input),
    );
    if (!["http:", "https:"].includes(url.protocol)) deny();
    check(url.hostname);
  });
  // Builtin named ESM imports are live bindings, unlike captured function values.
  syncBuiltinESMExports();

  return function restoreNetworkGuard() {
    for (const restore of restorers.toReversed()) restore();
    syncBuiltinESMExports();
  };
}

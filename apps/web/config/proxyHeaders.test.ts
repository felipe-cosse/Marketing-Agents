// WEB-01 proves the local web proxy cannot forward caller-controlled API-09 trust assertions.
import { describe, expect, it, vi } from "vitest";

import {
  isUntrustedUpstreamHeader,
  stripUntrustedUpstreamHeaders,
  type MutableProxyRequestHeaders,
} from "./proxyHeaders";

const API_09_UNTRUSTED_HEADERS = [
  "Forwarded",
  "X-Forwarded-For",
  "Remote-User",
  "X-Forwarded-User",
  "X-Forwarded-Email",
  "X-Forwarded-Actor",
  "X-Forwarded-Role",
  "X-Forwarded-Roles",
  "X-Forwarded-Scope",
  "X-Forwarded-Scopes",
  "X-Actor",
  "X-Actor-Id",
  "X-User",
  "X-Username",
  "X-Role",
  "X-Roles",
  "X-Scope",
  "X-Scoped-Grant",
  "X-Principal",
  "X-Principal-Id",
  "X-Auth-Request-User",
] as const;

describe("WEB-01 Vite API proxy header boundary", () => {
  it.each(API_09_UNTRUSTED_HEADERS)(
    "classifies %s as an untrusted upstream assertion",
    (headerName) => {
      expect(isUntrustedUpstreamHeader(headerName)).toBe(true);
    },
  );

  it("strips every API-09 forwarding and identity assertion before the upstream request", () => {
    const retained = new Set<string>([
      ...API_09_UNTRUSTED_HEADERS,
      "Accept",
      "Authorization",
      "X-CSRF-Token",
      "X-Request-Id",
    ]);
    const removeHeader = vi.fn((headerName: string) => {
      retained.delete(headerName);
    });
    const proxyRequest: MutableProxyRequestHeaders = {
      getHeaderNames: () => [...retained],
      removeHeader,
    };

    stripUntrustedUpstreamHeaders(proxyRequest);

    expect(removeHeader).toHaveBeenCalledTimes(API_09_UNTRUSTED_HEADERS.length);
    expect(retained).toEqual(
      new Set(["Accept", "Authorization", "X-CSRF-Token", "X-Request-Id"]),
    );
  });

  it.each(["Accept", "Authorization", "X-CSRF-Token", "X-Request-Id"])(
    "preserves browser-owned API header %s",
    (headerName) => {
      expect(isUntrustedUpstreamHeader(headerName)).toBe(false);
    },
  );
});

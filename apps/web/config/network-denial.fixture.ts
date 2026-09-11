import http from "node:http";
import { expect, it, vi } from "vitest";

// Only the dedicated canary config includes this intentionally failing fixture.
it("a caught HTTP denial still fails the test lifecycle", () => {
  try {
    http.get("http://203.0.113.10/private");
  } catch {
    // A product-level catch must not hide an attempted external request.
  }
  expect(true).toBe(true);
});

it("a caught fetch denial still fails the test lifecycle", () => {
  try {
    void fetch("https://example.invalid/private");
  } catch {
    // The global afterEach hook must surface this otherwise swallowed error.
  }
  expect(true).toBe(true);
});

it("an explicit in-memory response remains allowed", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response("mock response")),
  );
  const response = await fetch("https://example.invalid/in-memory-only");
  expect(await response.text()).toBe("mock response");
});

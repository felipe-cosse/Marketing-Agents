// WEB-01 proves hierarchy reads remain same-origin, uncached, and bounded by AbortSignal.
import { afterEach, describe, expect, it, vi } from "vitest";

import { getJson } from "./client";

afterEach(() => vi.unstubAllGlobals());

describe("WEB-01 centralized API client", () => {
  it("issues only a same-origin no-store GET", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ value: 1 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await expect(
      getJson("/api/v1/catalog/hierarchy", controller.signal),
    ).resolves.toEqual({
      value: 1,
    });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/catalog/hierarchy", {
      method: "GET",
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
  });

  it.each([
    "https://example.com/api/v1/catalog/hierarchy",
    "//example.com/api/v1/catalog",
  ])("rejects non-local path %s before fetch", async (path) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(getJson(path)).rejects.toMatchObject({
      code: "unsafe_api_path",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("maps an unavailable local API to a stable operator message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("secret network detail")),
    );
    await expect(getJson("/api/v1/catalog/hierarchy")).rejects.toMatchObject({
      status: 0,
      code: "api_unreachable",
      message: "The local API is not ready. Start it and try again.",
    });
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiRequestError } from "./client";
import {
  fetchInstanceStatusSummary,
  InstanceStatusContractError,
  normalizeInstanceStatusSummary,
} from "./instanceStatusSummary";

const WATERMARK = `instance-status-sha256-v1:${"a".repeat(64)}`;
const IDS = Object.freeze([
  "inst/status 01",
  ...Array.from(
    { length: 42 },
    (_, index) => `inst.web-02.status.${String(index + 2).padStart(2, "0")}`,
  ),
]);

function neverRunItem(instanceId: string): Record<string, unknown> {
  return {
    instance_id: instanceId,
    status: "never_run",
    latest_run_id: null,
    latest_run_state: null,
    latest_run_created_at: null,
    latest_run_updated_at: null,
    instance_url: `/api/v1/agent-instances/${encodeURIComponent(instanceId)}`,
    latest_run_url: null,
  };
}

function body(): Record<string, unknown> {
  const items = IDS.map(neverRunItem);
  items[1] = {
    instance_id: idAt(1),
    status: "executing",
    latest_run_id: "run/active one",
    latest_run_state: "executing",
    latest_run_created_at: "2026-08-28T18:00:00Z",
    latest_run_updated_at: "2026-08-28T18:01:00.123456Z",
    instance_url: `/api/v1/agent-instances/${encodeURIComponent(idAt(1))}`,
    latest_run_url: `/api/v1/runs/${encodeURIComponent("run/active one")}`,
  };
  return {
    scope: "single-local-installation",
    runtime_watermark: WATERMARK,
    items,
  };
}

function cloneBody(): Record<string, unknown> {
  return structuredClone(body());
}

function itemsOf(value: Record<string, unknown>): Record<string, unknown>[] {
  return value.items as Record<string, unknown>[];
}

function itemAt(
  value: Record<string, unknown>,
  index: number,
): Record<string, unknown> {
  const item = itemsOf(value)[index];
  if (item === undefined) throw new Error("test fixture item is missing");
  return item;
}

function idAt(index: number): string {
  const identifier = IDS[index];
  if (identifier === undefined) throw new Error("test fixture ID is missing");
  return identifier;
}

function responseFor(value = body(), etag = `"${WATERMARK}"`): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json", ETag: etag },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("WEB-02 instance status normalization", () => {
  it("accepts, cross-binds, camel-cases, and freezes the exact ordered 43-item response", () => {
    const normalized = normalizeInstanceStatusSummary(body(), IDS);

    expect(normalized.scope).toBe("single-local-installation");
    expect(normalized.runtimeWatermark).toBe(WATERMARK);
    expect(normalized.items).toHaveLength(43);
    expect(normalized.items[0]).toMatchObject({
      instanceId: IDS[0],
      status: "never_run",
      instanceUrl: "/api/v1/agent-instances/inst%2Fstatus%2001",
    });
    expect(normalized.items[1]).toMatchObject({
      status: "executing",
      latestRunId: "run/active one",
      latestRunUrl: "/api/v1/runs/run%2Factive%20one",
    });
    expect(Object.isFrozen(normalized)).toBe(true);
    expect(Object.isFrozen(normalized.items)).toBe(true);
    expect(Object.isFrozen(normalized.items[0])).toBe(true);
  });

  it.each([
    ["missing", (value: Record<string, unknown>) => itemsOf(value).pop()],
    [
      "extra",
      (value: Record<string, unknown>) =>
        itemsOf(value).push(neverRunItem("inst.web-02.extra")),
    ],
    [
      "reordered",
      (value: Record<string, unknown>) =>
        itemsOf(value).splice(0, 2, itemAt(value, 1), itemAt(value, 0)),
    ],
    [
      "duplicate",
      (value: Record<string, unknown>) => {
        itemsOf(value)[1] = structuredClone(itemAt(value, 0));
      },
    ],
  ])("rejects a %s status item set", (_label, mutate) => {
    const value = cloneBody();
    mutate(value);
    expect(() => normalizeInstanceStatusSummary(value, IDS)).toThrow(
      InstanceStatusContractError,
    );
  });

  it("requires exactly 43 unique expected hierarchy IDs", () => {
    expect(() =>
      normalizeInstanceStatusSummary(body(), IDS.slice(0, 42)),
    ).toThrow(/exact 43 unique instance IDs/u);
    expect(() =>
      normalizeInstanceStatusSummary(body(), [idAt(0), ...IDS.slice(0, 42)]),
    ).toThrow(/exact 43 unique instance IDs/u);
  });

  it.each([
    [
      "summary fields",
      (value: Record<string, unknown>) => (value.extra = true),
    ],
    [
      "item fields",
      (value: Record<string, unknown>) => (itemAt(value, 0).extra = true),
    ],
    ["scope", (value: Record<string, unknown>) => (value.scope = "global")],
    [
      "watermark",
      (value: Record<string, unknown>) => (value.runtime_watermark = "weak"),
    ],
    [
      "state",
      (value: Record<string, unknown>) => (itemAt(value, 1).status = "unknown"),
    ],
    [
      "never-run coherence",
      (value: Record<string, unknown>) =>
        (itemAt(value, 0).latest_run_id = "run.unbound"),
    ],
    [
      "active-state coherence",
      (value: Record<string, unknown>) =>
        (itemAt(value, 1).latest_run_state = "completed"),
    ],
    [
      "created timestamp",
      (value: Record<string, unknown>) =>
        (itemAt(value, 1).latest_run_created_at = "yesterday"),
    ],
    [
      "calendar timestamp",
      (value: Record<string, unknown>) =>
        (itemAt(value, 1).latest_run_created_at = "2026-02-30T18:00:00Z"),
    ],
    [
      "timestamp order",
      (value: Record<string, unknown>) =>
        (itemAt(value, 1).latest_run_updated_at = "2026-08-28T17:59:59Z"),
    ],
    [
      "instance URL",
      (value: Record<string, unknown>) =>
        (itemAt(value, 0).instance_url = "/api/v1/agent-instances/other"),
    ],
    [
      "run URL",
      (value: Record<string, unknown>) =>
        (itemAt(value, 1).latest_run_url = "/api/v1/runs/other"),
    ],
  ])("rejects invalid %s", (_label, mutate) => {
    const value = cloneBody();
    mutate(value);
    expect(() => normalizeInstanceStatusSummary(value, IDS)).toThrow(
      InstanceStatusContractError,
    );
  });
});

describe("WEB-02 conditional instance status transport", () => {
  it("issues a bounded same-origin GET and requires the response ETag to match the body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(responseFor());
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    const result = await fetchInstanceStatusSummary(IDS, {
      signal: controller.signal,
    });

    expect(result.runtimeWatermark).toBe(WATERMARK);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/agent-instances/status-summary",
      {
        method: "GET",
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      },
    );
  });

  it("sends a quoted conditional ETag and returns prior object identity on a matching 304", async () => {
    const previous = normalizeInstanceStatusSummary(body(), IDS);
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(null, {
        status: 304,
        headers: { ETag: `"${WATERMARK}"` },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await fetchInstanceStatusSummary(IDS, { previous });

    expect(result).toBe(previous);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/agent-instances/status-summary",
      expect.objectContaining({
        headers: {
          Accept: "application/json",
          "If-None-Match": `"${WATERMARK}"`,
        },
      }),
    );
  });

  it.each([
    ["missing prior", undefined, `"${WATERMARK}"`],
    [
      "mismatched ETag",
      normalizeInstanceStatusSummary(body(), IDS),
      `"instance-status-sha256-v1:${"b".repeat(64)}"`,
    ],
  ])("rejects a 304 with %s", async (_label, previous, etag) => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(null, { status: 304, headers: { ETag: etag } }),
        ),
    );
    const options = previous === undefined ? {} : { previous };
    await expect(
      fetchInstanceStatusSummary(IDS, options),
    ).rejects.toMatchObject({
      name: "ApiRequestError",
      code: "invalid_status_summary_response",
    });
  });

  it("rejects a successful body whose ETag does not match its watermark", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(responseFor(body(), '"other"')),
    );
    await expect(fetchInstanceStatusSummary(IDS)).rejects.toMatchObject({
      status: 200,
      code: "invalid_status_summary_response",
    });
  });

  it("maps network and non-JSON failures to stable ApiRequestError values", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("private detail")),
    );
    await expect(fetchInstanceStatusSummary(IDS)).rejects.toEqual(
      expect.objectContaining({
        name: "ApiRequestError",
        status: 0,
        code: "api_unreachable",
      }),
    );

    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("not json", {
          status: 200,
          headers: { ETag: `"${WATERMARK}"` },
        }),
      ),
    );
    await expect(fetchInstanceStatusSummary(IDS)).rejects.toMatchObject({
      name: "ApiRequestError",
      status: 200,
      code: "invalid_json_response",
    });
  });

  it("preserves stable API errors without reflecting structured server diagnostics", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: { secret: "do not reflect" } }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    const failure = await fetchInstanceStatusSummary(IDS).catch(
      (error: unknown) => error,
    );
    expect(failure).toBeInstanceOf(ApiRequestError);
    expect(failure).toMatchObject({
      status: 503,
      code: "api_request_failed",
      message: "The local API could not complete this request.",
    });
    expect(String(failure)).not.toContain("do not reflect");
  });
});

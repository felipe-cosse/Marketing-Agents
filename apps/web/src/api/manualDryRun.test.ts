import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchLocalSession } from "./instanceConfiguration";
import { clearLocalSession } from "./localSession";
import {
  createManualDryRun,
  generateManualDryRunIdempotencyKey,
  type CreateManualDryRunInput,
  type ManualDryRunExecutionMode,
} from "./manualDryRun";

const INSTANCE_ID = "inst.email.newsletter.newsletter-subscriber.01";
const SESSION_TOKEN = "a".repeat(43);
const REFRESHED_SESSION_TOKEN = "b".repeat(43);
const IDEMPOTENCY_KEY = "web-manual-dry-run-retry-0001";
const CORRELATION_ID = `correlation.api.${"c".repeat(32)}`;
const EVENT_ID = `manual-event-hmac-sha256-v1:${"d".repeat(64)}`;
const WORK_ID = "work.manual.web04.01";
const RUN_ID = "run.manual.web04.01";

function sessionBody(csrfToken = SESSION_TOKEN): Record<string, unknown> {
  return {
    actorId: "local-operator",
    roles: ["approver", "local_admin", "operator", "viewer"],
    scopes: ["approvals:decide", "approvals:read"],
    authMode: "local",
    environment: "local",
    modelMode: "mock",
    connectorMode: "mock",
    networkPermission: false,
    warning: "Local identity — not production authentication",
    csrfToken,
    csrfHeaderName: "X-CSRF-Token",
  };
}

function receiptBody(
  executionMode: ManualDryRunExecutionMode = "dry_run",
): Record<string, unknown> {
  return {
    status: "accepted",
    disposition: "created",
    eventId: EVENT_ID,
    workId: WORK_ID,
    runId: RUN_ID,
    executionMode,
    instanceUrl: `/api/v1/agent-instances/${INSTANCE_ID}`,
    runUrl: `/api/v1/runs/${RUN_ID}`,
  };
}

function jsonResponse(
  value: Record<string, unknown>,
  options: { readonly status?: number; readonly contentType?: string } = {},
): Response {
  return new Response(JSON.stringify(value), {
    status: options.status ?? 200,
    headers: {
      "Content-Type": options.contentType ?? "application/json; charset=utf-8",
      "X-Correlation-ID": CORRELATION_ID,
    },
  });
}

function problemResponse(
  status: number,
  code: string,
  optional: Record<string, unknown> = {},
  correlationId = CORRELATION_ID,
): Response {
  return new Response(
    JSON.stringify({
      type: `urn:marketing-agents:problem:${code}`,
      title: "Request rejected",
      status,
      detail: "The request could not be completed.",
      instance: `urn:marketing-agents:request:${correlationId}`,
      code,
      correlation_id: correlationId,
      ...optional,
    }),
    {
      status,
      headers: {
        "Content-Type": "application/problem+json; charset=utf-8",
        "X-Correlation-ID": correlationId,
      },
    },
  );
}

afterEach(() => {
  clearLocalSession();
  vi.unstubAllGlobals();
});

describe("WEB-04 manual dry-run transport", () => {
  it("WEB-04 generates a fresh opaque idempotency key", () => {
    const first = generateManualDryRunIdempotencyKey();
    const second = generateManualDryRunIdempotencyKey();

    expect(first).toMatch(/^[\x21-\x7e]{8,240}$/u);
    expect(second).toMatch(/^[\x21-\x7e]{8,240}$/u);
    expect(second).not.toBe(first);
  });

  it("WEB-04 posts the exact private mutation and returns a cross-bound receipt", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(
        jsonResponse(receiptBody("mock_execute"), { status: 202 }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    const body = JSON.stringify({
      input: { source_content: "hello", count: 2 },
      executionMode: "mock_execute",
    });

    const receipt = await createManualDryRun({
      instanceId: INSTANCE_ID,
      input: { source_content: "hello", count: 2 },
      executionMode: "mock_execute",
      idempotencyKey: IDEMPOTENCY_KEY,
      signal: controller.signal,
    });

    expect(receipt).toEqual(receiptBody("mock_execute"));
    expect(Object.isFrozen(receipt)).toBe(true);
    expect(JSON.stringify(receipt)).not.toContain(SESSION_TOKEN);
    expect(JSON.stringify(receipt)).not.toContain(IDEMPOTENCY_KEY);
    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/v1/session", {
      method: "GET",
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `/api/v1/agent-instances/${INSTANCE_ID}/dry-runs`,
      {
        method: "POST",
        cache: "no-store",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "Idempotency-Key": IDEMPOTENCY_KEY,
          "X-CSRF-Token": SESSION_TOKEN,
        },
        body,
        signal: controller.signal,
      },
    );
  });

  it("WEB-04 reuses the one session token cache populated by the public session API", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(jsonResponse(receiptBody(), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);

    await fetchLocalSession();
    await createManualDryRun({
      instanceId: INSTANCE_ID,
      input: {},
      executionMode: "dry_run",
      idempotencyKey: IDEMPOTENCY_KEY,
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      "/api/v1/session",
      `/api/v1/agent-instances/${INSTANCE_ID}/dry-runs`,
    ]);
  });

  it("WEB-04 refreshes one invalid CSRF token and replays the identical body and key", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(problemResponse(403, "csrf_token_invalid"))
      .mockResolvedValueOnce(jsonResponse(sessionBody(REFRESHED_SESSION_TOKEN)))
      .mockResolvedValueOnce(jsonResponse(receiptBody(), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createManualDryRun({
        instanceId: INSTANCE_ID,
        input: { source_content: "retry me" },
        executionMode: "dry_run",
        idempotencyKey: IDEMPOTENCY_KEY,
      }),
    ).resolves.toMatchObject({ status: "accepted" });

    expect(fetchMock).toHaveBeenCalledTimes(4);
    const firstRequest = fetchMock.mock.calls[1]?.[1];
    const retryRequest = fetchMock.mock.calls[3]?.[1];
    expect(retryRequest?.body).toBe(firstRequest?.body);
    expect(retryRequest?.headers).toMatchObject({
      "Idempotency-Key": IDEMPOTENCY_KEY,
      "X-CSRF-Token": REFRESHED_SESSION_TOKEN,
    });
    expect(firstRequest?.headers).toMatchObject({
      "Idempotency-Key": IDEMPOTENCY_KEY,
      "X-CSRF-Token": SESSION_TOKEN,
    });
  });

  it("WEB-04 stops after exactly one failed CSRF refresh retry", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(problemResponse(403, "csrf_token_invalid"))
      .mockResolvedValueOnce(jsonResponse(sessionBody(REFRESHED_SESSION_TOKEN)))
      .mockResolvedValueOnce(problemResponse(403, "csrf_token_invalid"));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createManualDryRun({
        instanceId: INSTANCE_ID,
        input: { source_content: "retry me" },
        executionMode: "dry_run",
        idempotencyKey: IDEMPOTENCY_KEY,
      }),
    ).rejects.toMatchObject({ status: 403, code: "csrf_token_invalid" });
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("WEB-04 exposes only strict Problem Details field errors", async () => {
    const fieldErrors = [
      {
        pointer: "/input/source_content",
        code: "dry_run_input_invalid",
        message: "invalid request field",
      },
    ];
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(
        problemResponse(422, "dry_run_input_invalid", {
          field_errors: fieldErrors,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const request = createManualDryRun({
      instanceId: INSTANCE_ID,
      input: { source_content: "invalid" },
      executionMode: "dry_run",
      idempotencyKey: IDEMPOTENCY_KEY,
    });

    await expect(request).rejects.toMatchObject({
      status: 422,
      code: "dry_run_input_invalid",
      fieldErrors,
    });
    await expect(request).rejects.not.toHaveProperty("correlationId");
  });

  it("WEB-04 rejects malformed Problem Details without trusting its code or retrying", async () => {
    const response = problemResponse(403, "csrf_token_invalid");
    response.headers.set(
      "X-Correlation-ID",
      `correlation.api.${"e".repeat(32)}`,
    );
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(response);
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createManualDryRun({
        instanceId: INSTANCE_ID,
        input: {},
        executionMode: "dry_run",
        idempotencyKey: IDEMPOTENCY_KEY,
      }),
    ).rejects.toMatchObject({ status: 403, code: "api_request_failed" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each([
    [
      "an unknown field",
      (receipt: Record<string, unknown>) => (receipt.extra = true),
    ],
    [
      "a mismatched mode",
      (receipt: Record<string, unknown>) =>
        (receipt.executionMode = "mock_execute"),
    ],
    [
      "a mismatched instance URL",
      (receipt: Record<string, unknown>) =>
        (receipt.instanceUrl =
          "/api/v1/agent-instances/inst.attacker.invalid.invalid.01"),
    ],
    [
      "a mismatched run URL",
      (receipt: Record<string, unknown>) =>
        (receipt.runUrl = "/api/v1/runs/run.other.01"),
    ],
    [
      "an invalid event ID",
      (receipt: Record<string, unknown>) =>
        (receipt.eventId = "manual-event.unbound"),
    ],
  ])("WEB-04 rejects a 202 receipt with %s", async (_label, mutate) => {
    const value = receiptBody();
    mutate(value);
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(jsonResponse(value, { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createManualDryRun({
        instanceId: INSTANCE_ID,
        input: {},
        executionMode: "dry_run",
        idempotencyKey: IDEMPOTENCY_KEY,
      }),
    ).rejects.toMatchObject({
      status: 202,
      code: "invalid_manual_dry_run_response",
    });
  });

  it.each([
    ["the wrong status", jsonResponse(receiptBody(), { status: 200 })],
    [
      "the wrong media type",
      jsonResponse(receiptBody(), {
        status: 202,
        contentType: "application/problem+json",
      }),
    ],
  ])("WEB-04 rejects a success response with %s", async (_label, response) => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(response);
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createManualDryRun({
        instanceId: INSTANCE_ID,
        input: {},
        executionMode: "dry_run",
        idempotencyKey: IDEMPOTENCY_KEY,
      }),
    ).rejects.toMatchObject({ code: "invalid_manual_dry_run_response" });
  });

  it.each([
    ["invalid instance identity", { instanceId: "../attacker" }],
    ["invalid retry key", { idempotencyKey: "short" }],
    ["non-object input", { input: [] }],
    ["non-JSON input", { input: { source_content: undefined } }],
  ])("WEB-04 rejects %s before fetching", async (_label, override) => {
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createManualDryRun({
        instanceId: INSTANCE_ID,
        input: {},
        executionMode: "dry_run",
        idempotencyKey: IDEMPOTENCY_KEY,
        ...override,
      } as unknown as CreateManualDryRunInput),
    ).rejects.toMatchObject({
      status: 0,
      code: "invalid_manual_dry_run_request",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("WEB-04 preserves AbortError without replacing it", async () => {
    const abortError = new DOMException("aborted", "AbortError");
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockRejectedValueOnce(abortError);
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createManualDryRun({
        instanceId: INSTANCE_ID,
        input: {},
        executionMode: "dry_run",
        idempotencyKey: IDEMPOTENCY_KEY,
      }),
    ).rejects.toBe(abortError);
  });

  it("WEB-04 preserves AbortError while reading an accepted receipt body", async () => {
    const abortError = new DOMException("aborted after headers", "AbortError");
    const receiptResponse = jsonResponse(receiptBody(), { status: 202 });
    vi.spyOn(receiptResponse, "json").mockRejectedValue(abortError);
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(receiptResponse);
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createManualDryRun({
        instanceId: INSTANCE_ID,
        input: {},
        executionMode: "dry_run",
        idempotencyKey: IDEMPOTENCY_KEY,
      }),
    ).rejects.toBe(abortError);
  });

  it("WEB-04 preserves AbortError while reading a Problem Details body", async () => {
    const abortError = new DOMException("aborted after headers", "AbortError");
    const rejectedResponse = problemResponse(422, "dry_run_input_invalid");
    vi.spyOn(rejectedResponse, "json").mockRejectedValue(abortError);
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(rejectedResponse);
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createManualDryRun({
        instanceId: INSTANCE_ID,
        input: {},
        executionMode: "dry_run",
        idempotencyKey: IDEMPOTENCY_KEY,
      }),
    ).rejects.toBe(abortError);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("WEB-04 preserves AbortError while reading the session body", async () => {
    const abortError = new DOMException("aborted after headers", "AbortError");
    const sessionResponse = jsonResponse(sessionBody());
    vi.spyOn(sessionResponse, "json").mockRejectedValue(abortError);
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(sessionResponse);
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      createManualDryRun({
        instanceId: INSTANCE_ID,
        input: {},
        executionMode: "dry_run",
        idempotencyKey: IDEMPOTENCY_KEY,
      }),
    ).rejects.toBe(abortError);
    expect(fetchMock).toHaveBeenCalledOnce();
  });
});

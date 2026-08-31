import { afterEach, describe, expect, it, vi } from "vitest";

import { clearLocalSession } from "./localSession";
import {
  approvalDetailQueryKey,
  approvalListQueryKey,
  decideApproval,
  fetchApprovalDetail,
  fetchApprovalPage,
  normalizeApprovalDecisionResult,
  normalizeApprovalDetail,
  normalizeApprovalPage,
} from "./approvals";

const APPROVAL_ID = "approval.web05.01";
const ACTION_ID = "action.web05.01";
const RUN_ID = "run.web05.01";
const STEP_ID = "step.web05.01";
const TEMPLATE_ID = "tpl.email.newsletter-subscriber";
const INSTANCE_ID = "inst.email.newsletter-subscriber.01";
const PAYLOAD_HASH = "a".repeat(64);
const SESSION_TOKEN = "b".repeat(43);
const REFRESHED_SESSION_TOKEN = "c".repeat(43);
const CORRELATION_ID = `correlation.api.${"d".repeat(32)}`;

function summaryBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: APPROVAL_ID,
    status: "pending",
    resource_version: 1,
    generation: 1,
    action_id: ACTION_ID,
    action_type: "newsletter.subscribe",
    destination_summary: "Configured Email newsletter audience",
    run_id: RUN_ID,
    template_id: TEMPLATE_ID,
    instance_id: INSTANCE_ID,
    requested_at: "2026-08-31T18:00:00Z",
    expires_at: "2026-08-31T19:00:00Z",
    is_expired: false,
    is_actionable: true,
    approval_url: `/api/v1/approvals/${APPROVAL_ID}`,
    action_url: `/api/v1/external-actions/${ACTION_ID}`,
    run_url: `/api/v1/runs/${RUN_ID}`,
    ...overrides,
  };
}

function detailBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    ...summaryBody(),
    one_time_use_state: "unused",
    capability_id: "email.newsletter.subscribe",
    connector_family: "newsletter",
    binding_id: "mock.newsletter.default",
    redacted_payload: {
      audience: "configured-audience",
      fields: { email: "[REDACTED]", opted_in: true },
    },
    payload_hash: PAYLOAD_HASH,
    step_id: STEP_ID,
    policy_id: "policy.email.external-write",
    required_roles: ["approver"],
    required_scopes: ["approvals:decide", "scope.external-write"],
    allow_self_approval: true,
    requested_by: "local-operator",
    updated_at: "2026-08-31T18:00:00Z",
    decision_id: null,
    decision_kind: null,
    decision_actor_id: null,
    decision_reason_code: null,
    decision_reason: null,
    decided_at: null,
    expired_at: null,
    replacement_approval_id: null,
    renewed_at: null,
    superseded_at: null,
    superseded_reason_code: null,
    consumed_at: null,
    step_url: `/api/v1/runs/${RUN_ID}/steps/${STEP_ID}`,
    template_url: `/api/v1/agent-templates/${TEMPLATE_ID}`,
    instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
    ...overrides,
  };
}

function decidedDetail(
  decision: "approve" | "reject" = "approve",
): Record<string, unknown> {
  const approved = decision === "approve";
  return detailBody({
    status: approved ? "approved" : "rejected",
    resource_version: 2,
    is_actionable: false,
    decision_id: "decision.web05.01",
    decision_kind: decision,
    decision_actor_id: "local-operator",
    decision_reason_code: approved ? "approval_granted" : "approval_rejected",
    decision_reason: null,
    decided_at: "2026-08-31T18:05:00Z",
    updated_at: "2026-08-31T18:05:00Z",
  });
}

function pageBody(
  items: readonly Record<string, unknown>[] = [summaryBody()],
  nextCursor: string | null = null,
): Record<string, unknown> {
  return { items: [...items], next_cursor: nextCursor };
}

function sessionBody(csrfToken = SESSION_TOKEN): Record<string, unknown> {
  return {
    actorId: "local-operator",
    roles: ["approver", "local_admin", "operator", "viewer"],
    scopes: [
      "approvals:decide",
      "approvals:read",
      "approvals:request",
      "scope.external-write",
    ],
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

function jsonResponse(
  value: Record<string, unknown>,
  options: {
    readonly status?: number;
    readonly location?: string;
    readonly contentType?: string;
  } = {},
): Response {
  const headers = new Headers({
    "Content-Type": options.contentType ?? "application/json; charset=utf-8",
  });
  if (options.location !== undefined) headers.set("Location", options.location);
  return new Response(JSON.stringify(value), {
    status: options.status ?? 200,
    headers,
  });
}

function decisionResponse(
  decision: "approve" | "reject" = "approve",
  embedded = false,
): Response {
  const value: Record<string, unknown> = {
    approval_id: APPROVAL_ID,
    decision_id: "decision.web05.01",
    action_id: ACTION_ID,
    run_id: RUN_ID,
    status: decision === "approve" ? "approved" : "rejected",
  };
  if (embedded) value.approval = decidedDetail(decision);
  return jsonResponse(value, {
    location: `/api/v1/approvals/${APPROVAL_ID}`,
  });
}

function problemResponse(
  status: number,
  code: string,
  optional: Record<string, unknown> = {},
): Response {
  return new Response(
    JSON.stringify({
      type: `urn:marketing-agents:problem:${code}`,
      title: "Approval request rejected",
      status,
      detail: "The approval request could not be completed.",
      instance: `urn:marketing-agents:request:${CORRELATION_ID}`,
      code,
      correlation_id: CORRELATION_ID,
      ...optional,
    }),
    {
      status,
      headers: {
        "Content-Type": "application/problem+json; charset=utf-8",
        "X-Correlation-ID": CORRELATION_ID,
      },
    },
  );
}

function decisionInput(decision: "approve" | "reject" = "approve") {
  return {
    approvalId: APPROVAL_ID,
    decision,
    expectedGeneration: 1,
    expectedPayloadHash: PAYLOAD_HASH,
    expectedActionId: ACTION_ID,
    expectedRunId: RUN_ID,
  } as const;
}

afterEach(() => {
  clearLocalSession();
  vi.unstubAllGlobals();
});

describe("WEB-05 approval API boundary", () => {
  it("WEB-05 normalizes only the bounded snake_case list projection", () => {
    const page = normalizeApprovalPage(pageBody(), {
      status: "pending",
      runId: RUN_ID,
      actionId: ACTION_ID,
      limit: 1,
    });

    expect(page).toMatchObject({
      nextCursor: null,
      items: [
        {
          id: APPROVAL_ID,
          resourceVersion: 1,
          actionId: ACTION_ID,
          actionType: "newsletter.subscribe",
          runId: RUN_ID,
          isActionable: true,
        },
      ],
    });
    expect(Object.isFrozen(page)).toBe(true);
    expect(Object.isFrozen(page.items)).toBe(true);
    expect(Object.isFrozen(page.items[0])).toBe(true);
    expect(JSON.stringify(page)).not.toContain("payload_hash");
    expect(() =>
      normalizeApprovalPage({ ...pageBody(), unexpected: true }, { limit: 1 }),
    ).toThrow(/fields are unsupported/u);
  });

  it("WEB-05 builds stable non-sensitive list and detail query keys", () => {
    const listKey = approvalListQueryKey({
      status: "pending",
      runId: RUN_ID,
      actionId: ACTION_ID,
      cursor: `approval-page-v1.${"x".repeat(24)}`,
      limit: 50,
    });

    expect(listKey).toEqual([
      "approvals",
      "list",
      {
        status: "pending",
        runId: RUN_ID,
        actionId: ACTION_ID,
        cursor: `approval-page-v1.${"x".repeat(24)}`,
        limit: 50,
      },
    ]);
    expect(approvalListQueryKey()).toEqual([
      "approvals",
      "list",
      { status: null, runId: null, actionId: null, cursor: null, limit: 25 },
    ]);
    expect(approvalDetailQueryKey(APPROVAL_ID)).toEqual([
      "approvals",
      "detail",
      APPROVAL_ID,
    ]);
    expect(JSON.stringify(listKey)).not.toMatch(/payload|hash|reason/iu);
    expect(Object.isFrozen(listKey)).toBe(true);
    expect(Object.isFrozen(listKey[2])).toBe(true);
  });

  it("WEB-05 fetches a bounded server-filtered page same-origin and no-store", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(pageBody()));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await expect(
      fetchApprovalPage(
        {
          status: "pending",
          runId: RUN_ID,
          actionId: ACTION_ID,
          cursor: `approval-page-v1.${"z".repeat(24)}`,
          limit: 1,
        },
        controller.signal,
      ),
    ).resolves.toMatchObject({ items: [{ id: APPROVAL_ID }] });

    const expectedPath =
      `/api/v1/approvals?status=pending&run_id=${RUN_ID}` +
      `&action_id=${ACTION_ID}&cursor=approval-page-v1.${"z".repeat(24)}&limit=1`;
    expect(fetchMock).toHaveBeenCalledWith(expectedPath, {
      method: "GET",
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    expect(expectedPath).not.toMatch(/payload|hash|reason/iu);
  });

  it("WEB-05 enforces filters, keyset order, cursor truth, and page bounds", () => {
    const later = summaryBody({
      id: "approval.web05.02",
      approval_url: "/api/v1/approvals/approval.web05.02",
      requested_at: "2026-08-31T18:01:00Z",
    });
    expect(() =>
      normalizeApprovalPage(pageBody([summaryBody(), later]), { limit: 2 }),
    ).toThrow(/descending keyset order/u);
    expect(() =>
      normalizeApprovalPage(pageBody(), { status: "approved", limit: 1 }),
    ).toThrow(/does not match its filters/u);
    expect(() =>
      normalizeApprovalPage(
        pageBody([summaryBody()], `approval-page-v1.${"n".repeat(24)}`),
        { limit: 2 },
      ),
    ).toThrow(/cursor is invalid/u);
    expect(() => approvalListQueryKey({ limit: 101 })).toThrow(
      /bounded integer/u,
    );
    expect(() => approvalListQueryKey({ cursor: "payload-hash" })).toThrow(
      /cursor is invalid/u,
    );
  });

  it("WEB-05 normalizes the exact detail and recursively bounds redacted payload", () => {
    const detail = normalizeApprovalDetail(detailBody());

    expect(detail).toMatchObject({
      id: APPROVAL_ID,
      oneTimeUseState: "unused",
      payloadHash: PAYLOAD_HASH,
      redactedPayload: {
        audience: "configured-audience",
        fields: { email: "[REDACTED]", opted_in: true },
      },
      decisionId: null,
    });
    expect(Object.isFrozen(detail)).toBe(true);
    expect(Object.isFrozen(detail.redactedPayload)).toBe(true);
    expect(Object.isFrozen(detail.redactedPayload.fields as object)).toBe(true);

    const unsafePayload = JSON.parse(
      '{"__proto__":{"polluted":true}}',
    ) as Record<string, unknown>;
    expect(() =>
      normalizeApprovalDetail(detailBody({ redacted_payload: unsafePayload })),
    ).toThrow(/unsafe key/u);
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    expect(() =>
      normalizeApprovalDetail(detailBody({ redacted_payload: cyclic })),
    ).toThrow(/must not be cyclic/u);
    const sparse: unknown[] = [];
    sparse.length = 1;
    expect(() =>
      normalizeApprovalDetail(
        detailBody({ redacted_payload: { items: sparse } }),
      ),
    ).toThrow(/must not be sparse/u);
    expect(() =>
      normalizeApprovalDetail(
        detailBody({ redacted_payload: { value: "x".repeat(262_145) } }),
      ),
    ).toThrow(/payload bounds/u);
  });

  it("WEB-05 rejects detail binding, lifecycle, and decision drift", () => {
    expect(() =>
      normalizeApprovalDetail(
        detailBody({ action_url: "/api/v1/external-actions/wrong" }),
      ),
    ).toThrow(/action_url does not match/u);
    expect(() =>
      normalizeApprovalDetail(detailBody({ is_actionable: false })),
    ).toThrow(/is_actionable is incoherent/u);
    expect(() =>
      normalizeApprovalDetail(detailBody({ one_time_use_state: "consumed" })),
    ).toThrow(/one-time state is incoherent/u);
    expect(() =>
      normalizeApprovalDetail(
        detailBody({
          decision_id: "decision.web05.01",
          decision_kind: "approve",
        }),
      ),
    ).toThrow(/decision fields are incoherent/u);
    expect(() =>
      normalizeApprovalDetail(
        detailBody({
          required_scopes: ["scope.external-write", "approvals:decide"],
        }),
      ),
    ).toThrow(/unique and sorted/u);
  });

  it("WEB-05 enforces the complete approval status lifecycle matrix", () => {
    const expired = detailBody({
      status: "expired",
      resource_version: 2,
      is_expired: true,
      is_actionable: false,
      expired_at: "2026-08-31T19:00:00Z",
      updated_at: "2026-08-31T19:00:00Z",
    });
    const renewed = {
      ...expired,
      resource_version: 3,
      replacement_approval_id: "approval.web05.02",
      renewed_at: "2026-08-31T19:01:00Z",
      updated_at: "2026-08-31T19:01:00Z",
    };
    const approvedExpired = {
      ...decidedDetail("approve"),
      status: "expired",
      resource_version: 3,
      is_expired: true,
      expired_at: "2026-08-31T19:00:00Z",
      updated_at: "2026-08-31T19:00:00Z",
    };
    const consumed = {
      ...decidedDetail("approve"),
      status: "consumed",
      resource_version: 3,
      one_time_use_state: "consumed",
      consumed_at: "2026-08-31T18:06:00Z",
      updated_at: "2026-08-31T18:06:00Z",
    };
    const superseded = detailBody({
      status: "superseded",
      resource_version: 2,
      is_actionable: false,
      superseded_at: "2026-08-31T18:06:00Z",
      superseded_reason_code: "approval_set_superseded",
      updated_at: "2026-08-31T18:06:00Z",
    });
    const approvedSuperseded = {
      ...decidedDetail("approve"),
      status: "superseded",
      resource_version: 3,
      superseded_at: "2026-08-31T18:06:00Z",
      superseded_reason_code: "run_cancelled",
      updated_at: "2026-08-31T18:06:00Z",
    };
    const approvedRenewed = {
      ...approvedExpired,
      resource_version: 4,
      replacement_approval_id: "approval.web05.02",
      renewed_at: "2026-08-31T19:01:00Z",
      updated_at: "2026-08-31T19:01:00Z",
    };
    const subMillisecondDecision = {
      ...decidedDetail("approve"),
      expires_at: "2026-08-31T19:00:00.000000002Z",
      decided_at: "2026-08-31T19:00:00.000000001Z",
      updated_at: "2026-08-31T19:00:00.000000001Z",
    };

    for (const value of [
      detailBody(),
      detailBody({ is_expired: true, is_actionable: false }),
      decidedDetail("approve"),
      { ...decidedDetail("approve"), is_expired: true },
      decidedDetail("reject"),
      expired,
      renewed,
      approvedExpired,
      approvedRenewed,
      consumed,
      superseded,
      approvedSuperseded,
      subMillisecondDecision,
    ]) {
      expect(() => normalizeApprovalDetail(value)).not.toThrow();
    }

    for (const value of [
      detailBody({
        decision_id: "decision.web05.01",
        decision_kind: "reject",
        decision_actor_id: "local-operator",
        decision_reason_code: "approval_rejected",
        decided_at: "2026-08-31T18:05:00Z",
      }),
      detailBody({
        status: "approved",
        resource_version: 2,
        is_actionable: false,
        updated_at: "2026-08-31T18:05:00Z",
      }),
      detailBody({
        status: "expired",
        resource_version: 2,
        is_expired: true,
        is_actionable: false,
      }),
      detailBody({
        status: "superseded",
        resource_version: 2,
        is_actionable: false,
      }),
      detailBody({
        status: "consumed",
        resource_version: 3,
        one_time_use_state: "consumed",
        is_actionable: false,
        consumed_at: "2026-08-31T18:06:00Z",
        updated_at: "2026-08-31T18:06:00Z",
      }),
      {
        ...superseded,
        superseded_reason_code: "proposal_replaced",
      },
      {
        ...decidedDetail("reject"),
        status: "approved",
      },
      {
        ...decidedDetail("approve"),
        resource_version: 3,
      },
      {
        ...decidedDetail("approve"),
        decided_at: "2026-08-31T19:00:00Z",
        updated_at: "2026-08-31T19:00:00Z",
      },
      {
        ...consumed,
        consumed_at: "2026-08-31T18:04:00Z",
        updated_at: "2026-08-31T18:04:00Z",
      },
      {
        ...decidedDetail("reject"),
        is_expired: true,
      },
      {
        ...renewed,
        replacement_approval_id: APPROVAL_ID,
      },
      {
        ...decidedDetail("approve"),
        expires_at: "2026-08-31T19:00:00.000000001Z",
        decided_at: "2026-08-31T19:00:00.000000002Z",
        updated_at: "2026-08-31T19:00:00.000000002Z",
      },
      {
        ...expired,
        expires_at: "2026-08-31T19:00:00.000000002Z",
        expired_at: "2026-08-31T19:00:00.000000001Z",
        updated_at: "2026-08-31T19:00:00.000000001Z",
      },
    ]) {
      expect(() => normalizeApprovalDetail(value)).toThrow(
        /lifecycle|supersession|lifetime|renew itself/u,
      );
    }
  });

  it("WEB-05 fetches detail by immutable ID and cross-binds the response", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(detailBody()));
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchApprovalDetail(APPROVAL_ID)).resolves.toMatchObject({
      id: APPROVAL_ID,
      actionId: ACTION_ID,
    });
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/approvals/${APPROVAL_ID}`, {
      method: "GET",
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });

    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        detailBody({
          id: "approval.web05.other",
          approval_url: "/api/v1/approvals/approval.web05.other",
        }),
      ),
    );
    await expect(fetchApprovalDetail(APPROVAL_ID)).rejects.toThrow(
      /does not match its request/u,
    );
  });

  it.each(["approve", "reject"] as const)(
    "WEB-05 %s sends only the two immutable preconditions and does not mutate optimistically",
    async (decision) => {
      const fetchMock = vi
        .fn<typeof fetch>()
        .mockResolvedValueOnce(jsonResponse(sessionBody()))
        .mockResolvedValueOnce(decisionResponse(decision));
      vi.stubGlobal("fetch", fetchMock);

      const result = await decideApproval(decisionInput(decision));

      expect(result).toMatchObject({
        approvalId: APPROVAL_ID,
        actionId: ACTION_ID,
        runId: RUN_ID,
        status: decision === "approve" ? "approved" : "rejected",
        approval: null,
      });
      const mutation = fetchMock.mock.calls[1];
      expect(mutation?.[0]).toBe(
        `/api/v1/approvals/${APPROVAL_ID}/${decision}`,
      );
      expect(mutation?.[1]).toEqual({
        method: "POST",
        cache: "no-store",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "X-CSRF-Token": SESSION_TOKEN,
        },
        body: JSON.stringify({
          expected_generation: 1,
          expected_payload_hash: PAYLOAD_HASH,
        }),
      });
      const mutationBody = mutation?.[1]?.body;
      if (typeof mutationBody !== "string") {
        throw new TypeError("WEB-05 decision body must be a JSON string");
      }
      expect(Object.keys(JSON.parse(mutationBody) as object)).toEqual([
        "expected_generation",
        "expected_payload_hash",
      ]);
      const mutationPath = mutation?.[0];
      if (typeof mutationPath !== "string") {
        throw new TypeError("WEB-05 decision path must be a local string");
      }
      expect(mutationPath).not.toContain(PAYLOAD_HASH);
      expect(JSON.stringify(result)).not.toContain(SESSION_TOKEN);
    },
  );

  it("WEB-05 accepts only a fully cross-bound authoritative embedded decision", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(decisionResponse("approve", true));
    vi.stubGlobal("fetch", fetchMock);

    await expect(decideApproval(decisionInput())).resolves.toMatchObject({
      status: "approved",
      approval: {
        id: APPROVAL_ID,
        generation: 1,
        payloadHash: PAYLOAD_HASH,
        decisionId: "decision.web05.01",
        decisionKind: "approve",
      },
    });

    expect(() =>
      normalizeApprovalDecisionResult(
        {
          approval_id: APPROVAL_ID,
          decision_id: "decision.web05.01",
          action_id: "action.web05.wrong",
          run_id: RUN_ID,
          status: "approved",
        },
        decisionInput(),
      ),
    ).toThrow(/does not match its immutable request/u);
  });

  it("WEB-05 refreshes stale CSRF once and replays an identical decision body", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(problemResponse(403, "csrf_token_invalid"))
      .mockResolvedValueOnce(jsonResponse(sessionBody(REFRESHED_SESSION_TOKEN)))
      .mockResolvedValueOnce(decisionResponse());
    vi.stubGlobal("fetch", fetchMock);

    await expect(decideApproval(decisionInput())).resolves.toMatchObject({
      status: "approved",
    });
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(fetchMock.mock.calls[3]?.[1]?.body).toBe(
      fetchMock.mock.calls[1]?.[1]?.body,
    );
    expect(fetchMock.mock.calls[1]?.[1]?.headers).toMatchObject({
      "X-CSRF-Token": SESSION_TOKEN,
    });
    expect(fetchMock.mock.calls[3]?.[1]?.headers).toMatchObject({
      "X-CSRF-Token": REFRESHED_SESSION_TOKEN,
    });
  });

  it("WEB-05 fails closed on conflict, Location, media type, and response drift", async () => {
    const conflictFetch = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(
        problemResponse(409, "approval_decision_conflict", {
          current_resource_version: 7,
        }),
      );
    vi.stubGlobal("fetch", conflictFetch);
    await expect(decideApproval(decisionInput())).rejects.toMatchObject({
      status: 409,
      code: "approval_decision_conflict",
      currentResourceVersion: 7,
      message:
        "The approval changed. Refresh its authoritative state before deciding.",
    });
    expect(conflictFetch).toHaveBeenCalledTimes(2);

    clearLocalSession();
    const invalidFetch = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(
        jsonResponse(
          {
            approval_id: APPROVAL_ID,
            decision_id: "decision.web05.01",
            action_id: ACTION_ID,
            run_id: RUN_ID,
            status: "approved",
          },
          { location: "/api/v1/approvals/wrong" },
        ),
      );
    vi.stubGlobal("fetch", invalidFetch);
    await expect(decideApproval(decisionInput())).rejects.toThrow(
      /Location is invalid/u,
    );

    clearLocalSession();
    invalidFetch.mockReset();
    invalidFetch
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(
        jsonResponse(
          {
            approval_id: APPROVAL_ID,
            decision_id: "decision.web05.01",
            action_id: ACTION_ID,
            run_id: RUN_ID,
            status: "approved",
          },
          {
            location: `/api/v1/approvals/${APPROVAL_ID}`,
            contentType: "text/plain",
          },
        ),
      );
    await expect(decideApproval(decisionInput())).rejects.toMatchObject({
      code: "invalid_json_response",
    });
  });
});

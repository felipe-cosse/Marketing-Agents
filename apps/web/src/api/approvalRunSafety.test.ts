import { afterEach, describe, expect, it, vi } from "vitest";

import {
  approvalRunSafetyQueryKey,
  fetchApprovalRunSafety,
  normalizeApprovalRunSafety,
} from "./approvalRunSafety";

const RUN_ID = "run.web05.email";
const APPROVAL_ID = "approval.web05.email";
const ACTION_ID = "action.web05.email";
const STEP_ID = "step.web05.email";
const TEMPLATE_ID = "tpl.email.newsletter-subscriber";
const INSTANCE_ID = "inst.email.newsletter-subscriber.01";

function pendingApprovalBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: APPROVAL_ID,
    action_id: ACTION_ID,
    step_id: STEP_ID,
    status: "pending",
    destination_summary: "Mock newsletter · configured audience",
    requested_at: "2026-08-31T18:01:00Z",
    expires_at: "2026-08-31T19:01:00Z",
    is_expired: false,
    approval_url: `/api/v1/approvals/${APPROVAL_ID}`,
    action_url: `/api/v1/external-actions/${ACTION_ID}`,
    step_url: `/api/v1/runs/${RUN_ID}/steps/${STEP_ID}`,
    ...overrides,
  };
}

function actionBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: ACTION_ID,
    run_id: RUN_ID,
    step_id: STEP_ID,
    step_key: "email/newsletter/subscribe",
    template_id: TEMPLATE_ID,
    instance_id: INSTANCE_ID,
    proposal_revision: 1,
    action_type: "newsletter.subscribe",
    capability_id: "email.newsletter.subscribe",
    connector_family: "newsletter",
    binding_id: "mock.newsletter.default",
    destination_summary: "Mock newsletter · configured audience",
    redacted_payload: {
      audience: "configured-audience",
      email: "[REDACTED]",
    },
    payload_schema_id: "schema.newsletter.subscribe.v1",
    state: "awaiting_approval",
    created_at: "2026-08-31T18:00:00Z",
    updated_at: "2026-08-31T18:01:00Z",
    version: 1,
    delivery_attempt_count: 0,
    delivery_attempt_limit: 3,
    approval_policy_id: "policy.email.external-write",
    approval_required_roles: ["approver"],
    approval_required_scopes: ["scope.external-write"],
    approval_expires_after_seconds: 3_600,
    approval_allow_self_approval: true,
    terminal_reason_code: null,
    superseded_by_action_id: null,
    superseded_at: null,
    receipt_id: null,
    result_status: null,
    result_safe_metadata: null,
    completed_at: null,
    action_url: `/api/v1/external-actions/${ACTION_ID}`,
    run_url: `/api/v1/runs/${RUN_ID}`,
    step_url: `/api/v1/runs/${RUN_ID}/steps/${STEP_ID}`,
    instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
    template_url: `/api/v1/agent-templates/${TEMPLATE_ID}`,
    ...overrides,
  };
}

function runBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: RUN_ID,
    work_item_id: "work.web05.email",
    instance_id: INSTANCE_ID,
    workflow_id: "workflow.email-signup.v1",
    trigger_id: "trigger.web05.manual",
    source: "manual",
    mode: "mock_execution",
    state: "awaiting_approval",
    catalog_hash: "e".repeat(64),
    configuration_revision: 1,
    approval_required: true,
    terminal_reason_code: null,
    created_at: "2026-08-31T18:00:00Z",
    updated_at: "2026-08-31T18:01:00Z",
    version: 4,
    run_url: `/api/v1/runs/${RUN_ID}`,
    timeline_url: `/api/v1/runs/${RUN_ID}/timeline`,
    artifacts_url: `/api/v1/runs/${RUN_ID}/artifacts`,
    instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
    transitions: [{ sequence: 1 }],
    plan: null,
    execution_control: null,
    pending_approvals: [pendingApprovalBody()],
    artifact_summaries: [],
    artifacts_truncated: false,
    external_actions: [actionBody()],
    terminal_error: null,
    ...overrides,
  };
}

function jsonResponse(value: Record<string, unknown>, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("WEB-05 authoritative approval run safety", () => {
  it("WEB-05 projects zero mock connector calls only from API-07 attempt counts", () => {
    const safety = normalizeApprovalRunSafety(runBody(), RUN_ID);

    expect(safety).toEqual({
      runId: RUN_ID,
      mode: "mock_execution",
      state: "awaiting_approval",
      approvalRequired: true,
      pendingApprovals: [
        {
          id: APPROVAL_ID,
          actionId: ACTION_ID,
          stepId: STEP_ID,
          destinationSummary: "Mock newsletter · configured audience",
          requestedAt: "2026-08-31T18:01:00Z",
          expiresAt: "2026-08-31T19:01:00Z",
          isExpired: false,
          approvalUrl: `/api/v1/approvals/${APPROVAL_ID}`,
          actionUrl: `/api/v1/external-actions/${ACTION_ID}`,
          stepUrl: `/api/v1/runs/${RUN_ID}/steps/${STEP_ID}`,
        },
      ],
      externalActions: [
        {
          id: ACTION_ID,
          stepId: STEP_ID,
          actionType: "newsletter.subscribe",
          state: "awaiting_approval",
          deliveryAttemptCount: 0,
          receiptId: null,
          resultStatus: null,
          completedAt: null,
          actionUrl: `/api/v1/external-actions/${ACTION_ID}`,
          stepUrl: `/api/v1/runs/${RUN_ID}/steps/${STEP_ID}`,
        },
      ],
      zeroMockConnectorCallsConfirmed: true,
      runUrl: `/api/v1/runs/${RUN_ID}`,
      timelineUrl: `/api/v1/runs/${RUN_ID}/timeline`,
    });
    expect(Object.isFrozen(safety)).toBe(true);
    expect(Object.isFrozen(safety.pendingApprovals)).toBe(true);
    expect(Object.isFrozen(safety.externalActions)).toBe(true);
    expect(JSON.stringify(safety)).not.toMatch(
      /redacted_payload|payload_hash/iu,
    );
  });

  it("WEB-05 exposes a stable run query key without payload, hash, or reason", () => {
    const key = approvalRunSafetyQueryKey(RUN_ID);

    expect(key).toEqual(["runs", "approval-safety", RUN_ID]);
    expect(Object.isFrozen(key)).toBe(true);
    expect(JSON.stringify(key)).not.toMatch(/payload|hash|reason/iu);
  });

  it("WEB-05 fetches the authoritative run same-origin and no-store", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(runBody()));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await expect(
      fetchApprovalRunSafety(RUN_ID, controller.signal),
    ).resolves.toMatchObject({
      runId: RUN_ID,
      zeroMockConnectorCallsConfirmed: true,
    });
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/runs/${RUN_ID}`, {
      method: "GET",
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
  });

  it("WEB-05 never claims zero mock calls for attempts, receipts, results, completion, or dry-run facts", () => {
    const attempted = normalizeApprovalRunSafety(
      runBody({
        external_actions: [actionBody({ delivery_attempt_count: 1 })],
      }),
    );
    const dryRun = normalizeApprovalRunSafety(runBody({ mode: "dry_run" }));
    const receipted = normalizeApprovalRunSafety(
      runBody({
        external_actions: [
          actionBody({
            state: "succeeded",
            receipt_id: "receipt.web05.email",
            result_status: "succeeded",
            completed_at: "2026-08-31T18:02:00Z",
          }),
        ],
      }),
    );
    const noActions = normalizeApprovalRunSafety(
      runBody({ pending_approvals: [], external_actions: [] }),
    );

    expect(attempted.externalActions[0]?.deliveryAttemptCount).toBe(1);
    expect(attempted.zeroMockConnectorCallsConfirmed).toBe(false);
    expect(dryRun.zeroMockConnectorCallsConfirmed).toBe(false);
    expect(receipted.zeroMockConnectorCallsConfirmed).toBe(false);
    expect(noActions.zeroMockConnectorCallsConfirmed).toBe(false);
  });

  it("WEB-05 rejects attempt-count, response-link, and expected-run drift", () => {
    expect(() =>
      normalizeApprovalRunSafety(
        runBody({
          external_actions: [
            actionBody({
              delivery_attempt_count: 4,
              delivery_attempt_limit: 3,
            }),
          ],
        }),
      ),
    ).toThrow(/exceeds its attempt limit/u);
    expect(() =>
      normalizeApprovalRunSafety(
        runBody({
          external_actions: [
            actionBody({ run_url: "/api/v1/runs/run.web05.wrong" }),
          ],
        }),
      ),
    ).toThrow(/run_url does not match/u);
    expect(() =>
      normalizeApprovalRunSafety(runBody(), "run.web05.wrong"),
    ).toThrow(/does not match its request/u);
    expect(() =>
      normalizeApprovalRunSafety(
        runBody({
          pending_approvals: [
            pendingApprovalBody({
              requested_at: "2026-08-31T19:01:00.000000002Z",
              expires_at: "2026-08-31T19:01:00.000000001Z",
            }),
          ],
        }),
      ),
    ).toThrow(/expiry must follow/u);
    expect(() =>
      normalizeApprovalRunSafety(
        runBody({
          created_at: "2026-08-31T18:00:00.000000002Z",
          updated_at: "2026-08-31T18:00:00.000000001Z",
        }),
      ),
    ).toThrow(/precedes creation/u);
    expect(() =>
      normalizeApprovalRunSafety(
        runBody({
          external_actions: [
            actionBody({
              created_at: "2026-08-31T18:00:00.000000002Z",
              updated_at: "2026-08-31T18:00:00.000000001Z",
            }),
          ],
        }),
      ),
    ).toThrow(/precedes creation/u);
  });

  it("WEB-05 rejects unknown pending bindings, duplicates, and empty transitions", () => {
    expect(() =>
      normalizeApprovalRunSafety(
        runBody({
          pending_approvals: [
            pendingApprovalBody({
              action_id: "action.web05.unknown",
              action_url: "/api/v1/external-actions/action.web05.unknown",
            }),
          ],
        }),
      ),
    ).toThrow(/does not bind an external action/u);
    expect(() =>
      normalizeApprovalRunSafety(
        runBody({
          external_actions: [actionBody(), actionBody()],
        }),
      ),
    ).toThrow(/identities must be unique/u);
    expect(() =>
      normalizeApprovalRunSafety(runBody({ transitions: [] })),
    ).toThrow(/must not be empty/u);
  });

  it("WEB-05 fails closed on extra fields and unsafe projected action data", () => {
    expect(() =>
      normalizeApprovalRunSafety({ ...runBody(), unexpected: true }),
    ).toThrow(/fields are unsupported/u);
    expect(() =>
      normalizeApprovalRunSafety(
        runBody({
          external_actions: [actionBody({ unexpected: true })],
        }),
      ),
    ).toThrow(/fields are unsupported/u);
    expect(() =>
      normalizeApprovalRunSafety(
        runBody({
          external_actions: [
            actionBody({ result_safe_metadata: { provider: "secret" } }),
          ],
        }),
      ),
    ).toThrow(/result_safe_metadata must be null/u);
    const unsafePayload = JSON.parse('{"constructor":{"leak":true}}') as Record<
      string,
      unknown
    >;
    expect(() =>
      normalizeApprovalRunSafety(
        runBody({
          external_actions: [actionBody({ redacted_payload: unsafePayload })],
        }),
      ),
    ).toThrow(/unsafe key/u);
  });

  it("WEB-05 maps run read errors to stable non-sensitive messages", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(
        {
          code: "run_not_found",
          detail: "internal secret should not be reflected",
        },
        404,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchApprovalRunSafety(RUN_ID)).rejects.toMatchObject({
      status: 404,
      code: "run_not_found",
      message: "The approval run was not found.",
    });
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  artifactResourceQueryKey,
  externalActionQueryKey,
  fetchArtifactResource,
  fetchExternalAction,
  fetchRunArtifactsPage,
  fetchRunPage,
  fetchRunResource,
  fetchRunTimelinePage,
  normalizeArtifactResource,
  normalizeExternalAction,
  normalizeRunArtifactsPage,
  normalizeRunPage,
  normalizeRunResource,
  normalizeRunTimelinePage,
  runArtifactsQueryKey,
  runListQueryKey,
  runResourceQueryKey,
  runTimelineQueryKey,
} from "./runArtifacts";

const RUN_ID = "run.web06.one";
const OTHER_RUN_ID = "run.web06.two";
const WORK_ID = "work.web06.one";
const INSTANCE_ID = "inst.web06.email.01";
const WORKFLOW_ID = "workflow.web06.email";
const STEP_ID = "step.web06.compose";
const TEMPLATE_ID = "tpl.web06.email";
const ARTIFACT_ID = "artifact.web06.one";
const OTHER_ARTIFACT_ID = "artifact.web06.two";
const ACTION_ID = "action.web06.one";

function runSummaryBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  const id = typeof overrides.id === "string" ? overrides.id : RUN_ID;
  const instanceId =
    typeof overrides.instance_id === "string"
      ? overrides.instance_id
      : INSTANCE_ID;
  const state =
    typeof overrides.state === "string" ? overrides.state : "received";
  const terminalReasonCode = Object.prototype.hasOwnProperty.call(
    overrides,
    "terminal_reason_code",
  )
    ? overrides.terminal_reason_code
    : ["completed", "failed", "rejected", "cancelled"].includes(state)
      ? "run_completed"
      : null;
  return {
    id,
    work_item_id: WORK_ID,
    instance_id: instanceId,
    workflow_id: WORKFLOW_ID,
    trigger_id: "trigger.web06.manual",
    source: "manual",
    mode: "mock_execution",
    state,
    catalog_hash: "catalog-sha256-v1:" + "a".repeat(64),
    configuration_revision: 1,
    approval_required: null,
    terminal_reason_code: terminalReasonCode,
    created_at: "2026-08-31T18:00:00.000000001Z",
    updated_at: "2026-08-31T18:00:00.000000001Z",
    version: 1,
    run_url: `/api/v1/runs/${id}`,
    timeline_url: `/api/v1/runs/${id}/timeline`,
    artifacts_url: `/api/v1/runs/${id}/artifacts`,
    instance_url: `/api/v1/agent-instances/${instanceId}`,
    ...overrides,
  };
}

function runResourceBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    ...runSummaryBody(overrides),
    transitions: [
      {
        sequence: 1,
        command: "initialize",
        previous_state: null,
        new_state: "received",
        reason_code: "run_received",
        occurred_at: "2026-08-31T18:00:00.000000001Z",
        expected_version: 0,
        resulting_version: 1,
        completed_effect_count: 0,
        outcome_unknown_effect_count: 0,
      },
    ],
    plan: null,
    execution_control: null,
    pending_approvals: [],
    artifact_summaries: [],
    artifacts_truncated: false,
    external_actions: [],
    terminal_error: null,
    ...overrides,
  };
}

function artifactSummaryBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  const id = typeof overrides.id === "string" ? overrides.id : ARTIFACT_ID;
  const runId =
    typeof overrides.run_id === "string" ? overrides.run_id : RUN_ID;
  const stepId =
    typeof overrides.step_id === "string" ? overrides.step_id : STEP_ID;
  const templateId =
    typeof overrides.template_id === "string"
      ? overrides.template_id
      : TEMPLATE_ID;
  const instanceId =
    typeof overrides.instance_id === "string"
      ? overrides.instance_id
      : INSTANCE_ID;
  return {
    id,
    work_item_id: WORK_ID,
    run_id: runId,
    step_id: stepId,
    workflow_id: WORKFLOW_ID,
    workflow_version: "1.0.0",
    template_id: templateId,
    instance_id: instanceId,
    output_schema_id: "schema.web06.email-output.v1",
    output_schema_version: "1",
    classification: "internal",
    created_at: "2026-08-31T18:00:01.000000001Z",
    artifact_url: `/api/v1/artifacts/${id}`,
    run_url: `/api/v1/runs/${runId}`,
    step_url: `/api/v1/runs/${runId}/steps/${stepId}`,
    template_url: `/api/v1/agent-templates/${templateId}`,
    instance_url: `/api/v1/agent-instances/${instanceId}`,
    ...overrides,
  };
}

function artifactResourceBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    ...artifactSummaryBody(),
    catalog_hash: "catalog-sha256-v1:" + "a".repeat(64),
    instance_config_revision: 1,
    sources: [
      {
        kind: "work_input",
        source_id: WORK_ID,
        classification: "internal",
      },
    ],
    parent_artifact_ids: [],
    providers: [
      {
        provider_kind: "planner",
        mode: "local",
        name: "deterministic-planner",
        version: "1.0.0",
      },
    ],
    output_schema_hash: "schema-sha256-v1:" + "b".repeat(64),
    redacted_payload: { subject: "Welcome", email: "[REDACTED]" },
    payload_digest: "artifact-hmac-sha256-v1:" + "c".repeat(64),
    ...overrides,
  };
}

function externalActionBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  const id = typeof overrides.id === "string" ? overrides.id : ACTION_ID;
  const runId =
    typeof overrides.run_id === "string" ? overrides.run_id : RUN_ID;
  const stepId =
    typeof overrides.step_id === "string" ? overrides.step_id : STEP_ID;
  return {
    id,
    run_id: runId,
    step_id: stepId,
    step_key: "email/compose",
    template_id: TEMPLATE_ID,
    instance_id: INSTANCE_ID,
    proposal_revision: 1,
    action_type: "newsletter.subscribe",
    capability_id: "email.newsletter.subscribe",
    connector_family: "newsletter",
    binding_id: "mock.newsletter.default",
    destination_summary: "Mock newsletter audience",
    redacted_payload: { email: "[REDACTED]" },
    payload_schema_id: "schema.newsletter.subscribe.v1",
    state: "awaiting_approval",
    created_at: "2026-08-31T18:00:01.000000001Z",
    updated_at: "2026-08-31T18:00:01.000000002Z",
    version: 1,
    delivery_attempt_count: 0,
    delivery_attempt_limit: 3,
    approval_policy_id: "policy.external-write",
    approval_required_roles: ["approver"],
    approval_required_scopes: ["scope.external-write"],
    approval_expires_after_seconds: 3600,
    approval_allow_self_approval: false,
    terminal_reason_code: null,
    superseded_by_action_id: null,
    superseded_at: null,
    receipt_id: null,
    result_status: null,
    result_safe_metadata: null,
    completed_at: null,
    action_url: `/api/v1/external-actions/${id}`,
    run_url: `/api/v1/runs/${runId}`,
    step_url: `/api/v1/runs/${runId}/steps/${stepId}`,
    instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
    template_url: `/api/v1/agent-templates/${TEMPLATE_ID}`,
    ...overrides,
  };
}

function stepRuntimePolicyBody(): Record<string, unknown> {
  return {
    operation_key: "email/compose",
    attempt_kind: "tool",
    max_attempts: 3,
    backoff: "bounded_exponential",
    step_timeout_seconds: 60,
    template_run_timeout_seconds: 300,
    max_steps: 5,
    max_model_calls: 2,
    max_tool_calls: 3,
    max_input_bytes: 65_536,
    max_input_field_bytes: 32_768,
    max_output_bytes: 262_144,
    max_model_output_tokens: 4_096,
    rate_limit_scope: "connector",
    rate_limit_key: "mock.newsletter.default",
    rate_limit_max_calls: 10,
    rate_limit_window_seconds: 60,
  };
}

function runStepBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: STEP_ID,
    run_id: RUN_ID,
    key: "email/compose",
    kind: "external_action",
    selected_instance_id: INSTANCE_ID,
    template_id: TEMPLATE_ID,
    dependency_keys: [],
    capability_id: "email.newsletter.subscribe",
    effect: "write",
    state: "awaiting_approval",
    ordinal: 1,
    source_order: 1,
    configuration_revision: 1,
    connector_family: "newsletter",
    routing_slot_key: "email",
    binding_id: "mock.newsletter.default",
    binding_configuration_revision: 1,
    request_schema_id: "schema.newsletter.subscribe.v1",
    result_schema_id: "schema.web06.email-output.v1",
    result_schema_hash: "schema-sha256-v1:" + "d".repeat(64),
    data_classification: "internal",
    idempotency_support: "required",
    timeout_seconds: 60,
    runtime_policy: stepRuntimePolicyBody(),
    approval_policy_id: "policy.external-write",
    approval_required_roles: ["approver"],
    approval_required_scopes: ["scope.external-write"],
    approval_expires_after_seconds: 3600,
    approval_allow_self_approval: false,
    terminal_result: false,
    created_at: "2026-08-31T18:00:00.000000001Z",
    updated_at: "2026-08-31T18:00:00.000000002Z",
    version: 1,
    terminal_reason_code: null,
    transitions: [],
    step_url: `/api/v1/runs/${RUN_ID}/steps/${STEP_ID}`,
    run_url: `/api/v1/runs/${RUN_ID}`,
    instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
    template_url: `/api/v1/agent-templates/${TEMPLATE_ID}`,
    ...overrides,
  };
}

function runPlanBody(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    plan_hash: "d".repeat(64),
    workflow_id: WORKFLOW_ID,
    workflow_version: 1,
    workflow_definition_hash: "e".repeat(64),
    catalog_content_hash: "catalog-sha256-v1:" + "f".repeat(64),
    graph_hash: "1".repeat(64),
    routing_hash: "2".repeat(64),
    approval_required: true,
    step_count: 1,
    runtime_policy: {
      max_steps: 5,
      max_model_calls: 2,
      max_tool_calls: 3,
      run_timeout_seconds: 300,
    },
    created_at: "2026-08-31T18:00:00.000000001Z",
    selected_instances: [
      {
        instance_id: INSTANCE_ID,
        template_id: TEMPLATE_ID,
        configuration_revision: 1,
        display_order: 1,
        source_ordinal: 1,
        selection_order: 1,
        target: true,
        instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
        template_url: `/api/v1/agent-templates/${TEMPLATE_ID}`,
      },
    ],
    routing_assignments: [
      {
        slot_key: "email",
        instance_id: INSTANCE_ID,
        template_id: TEMPLATE_ID,
        required_capability_ids: ["email.newsletter.subscribe"],
        assignment_order: 1,
        instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
        template_url: `/api/v1/agent-templates/${TEMPLATE_ID}`,
      },
    ],
    steps: [runStepBody()],
    ...overrides,
  };
}

function timelineEventBody(
  sequence: number,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: `event.web06.${String(sequence)}`,
    sequence,
    schema_version: 1,
    event_type: "run.received",
    aggregate_type: "run",
    aggregate_id: RUN_ID,
    outcome: "accepted",
    actor_id: "actor.web06.local",
    actor_source: "human",
    auth_method: "local_session",
    correlation_id: "correlation.web06.one",
    occurred_at: `2026-08-31T18:00:00.00000000${String(sequence)}Z`,
    step_id: null,
    action_id: null,
    approval_request_id: null,
    artifact_id: null,
    attempted_command: null,
    previous_state: null,
    new_state: "received",
    reason_code: "run_received",
    metadata: { source: "manual" },
    metadata_classification: "internal",
    metadata_expires_at: "2026-09-01T18:00:00Z",
    metadata_expired: false,
    run_url: `/api/v1/runs/${RUN_ID}`,
    step_url: null,
    action_url: null,
    approval_url: null,
    artifact_url: null,
    ...overrides,
  };
}

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("WEB-06 strict run and artifact read boundary", () => {
  it("WEB-06 exports stable payload-free query keys", () => {
    const keys = [
      runListQueryKey({ state: "completed", limit: 10 }),
      runResourceQueryKey(RUN_ID),
      runTimelineQueryKey(RUN_ID, { limit: 10 }),
      runArtifactsQueryKey(RUN_ID, { limit: 10 }),
      artifactResourceQueryKey(ARTIFACT_ID),
      externalActionQueryKey(ACTION_ID),
    ];

    expect(keys).toEqual([
      [
        "runs",
        "list",
        {
          state: "completed",
          instanceId: null,
          workflowId: null,
          createdAtFrom: null,
          createdAtTo: null,
          cursor: null,
          limit: 10,
        },
      ],
      ["runs", "detail", RUN_ID],
      ["runs", "timeline", RUN_ID, { cursor: null, limit: 10 }],
      ["runs", "artifacts", RUN_ID, { cursor: null, limit: 10 }],
      ["artifacts", "detail", ARTIFACT_ID],
      ["external-actions", "detail", ACTION_ID],
    ]);
    expect(keys.every(Object.isFrozen)).toBe(true);
    expect(JSON.stringify(keys)).not.toMatch(
      /redacted_payload|redactedPayload|metadata|payload_digest|payloadDigest/iu,
    );
  });

  it("WEB-06 fetches every read endpoint same-origin and no-store", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        jsonResponse({ items: [runSummaryBody()], next_cursor: null }),
      )
      .mockResolvedValueOnce(jsonResponse(runResourceBody()))
      .mockResolvedValueOnce(
        jsonResponse({
          run_id: RUN_ID,
          items: [timelineEventBody(1)],
          next_cursor: null,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          run_id: RUN_ID,
          items: [artifactSummaryBody()],
          next_cursor: null,
        }),
      )
      .mockResolvedValueOnce(jsonResponse(artifactResourceBody()))
      .mockResolvedValueOnce(jsonResponse(externalActionBody()));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await fetchRunPage({ limit: 1 }, controller.signal);
    await fetchRunResource(RUN_ID, controller.signal);
    await fetchRunTimelinePage(RUN_ID, { limit: 1 }, controller.signal);
    await fetchRunArtifactsPage(RUN_ID, { limit: 1 }, controller.signal);
    await fetchArtifactResource(ARTIFACT_ID, controller.signal);
    await fetchExternalAction(ACTION_ID, controller.signal);

    const request = {
      method: "GET",
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    };
    expect(fetchMock.mock.calls).toEqual([
      ["/api/v1/runs?limit=1", request],
      [`/api/v1/runs/${RUN_ID}`, request],
      [`/api/v1/runs/${RUN_ID}/timeline?limit=1`, request],
      [`/api/v1/runs/${RUN_ID}/artifacts?limit=1`, request],
      [`/api/v1/artifacts/${ARTIFACT_ID}`, request],
      [`/api/v1/external-actions/${ACTION_ID}`, request],
    ]);
  });

  it("WEB-06 sends only validated run-list filters in a deterministic query", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse({ items: [], next_cursor: null }));
    vi.stubGlobal("fetch", fetchMock);

    await fetchRunPage({
      state: "completed",
      instanceId: INSTANCE_ID,
      workflowId: WORKFLOW_ID,
      createdAtFrom: "2026-08-31T18:00:00Z",
      createdAtTo: "2026-08-31T19:00:00Z",
      cursor: "run-page-v1.abc",
      limit: 10,
    });

    const path = fetchMock.mock.calls[0]?.[0];
    expect(path).toBe(
      `/api/v1/runs?state=completed&instance_id=${INSTANCE_ID}&workflow_id=${WORKFLOW_ID}&created_at_from=2026-08-31T18%3A00%3A00Z&created_at_to=2026-08-31T19%3A00%3A00Z&cursor=run-page-v1.abc&limit=10`,
    );
    expect(typeof path === "string" ? path : "").not.toMatch(
      /payload|metadata|digest/iu,
    );
  });

  it("WEB-06 enforces the declared descending run order and every list filter", () => {
    const page = normalizeRunPage(
      {
        items: [
          runSummaryBody({
            id: OTHER_RUN_ID,
            created_at: "2026-08-31T18:00:00.000000002Z",
            updated_at: "2026-08-31T18:00:00.000000002Z",
            state: "completed",
          }),
          runSummaryBody({
            created_at: "2026-08-31T18:00:00.000000001Z",
            updated_at: "2026-08-31T18:00:00.000000001Z",
            state: "completed",
          }),
        ],
        next_cursor: null,
      },
      {
        state: "completed",
        instanceId: INSTANCE_ID,
        workflowId: WORKFLOW_ID,
        createdAtFrom: "2026-08-31T18:00:00.000000001Z",
        createdAtTo: "2026-08-31T18:00:00.000000002Z",
        limit: 2,
      },
    );

    expect(page.items.map(({ id }) => id)).toEqual([OTHER_RUN_ID, RUN_ID]);
    expect(Object.isFrozen(page.items)).toBe(true);
    expect(() =>
      normalizeRunPage(
        {
          items: [
            runSummaryBody({
              created_at: "2026-08-31T18:00:00.000000001Z",
              updated_at: "2026-08-31T18:00:00.000000001Z",
              state: "completed",
            }),
            runSummaryBody({
              id: OTHER_RUN_ID,
              created_at: "2026-08-31T18:00:00.000000002Z",
              updated_at: "2026-08-31T18:00:00.000000002Z",
              state: "completed",
            }),
          ],
          next_cursor: null,
        },
        { state: "completed", limit: 2 },
      ),
    ).toThrow(/descending keyset order/u);
    expect(() =>
      normalizeRunPage(
        {
          items: [runSummaryBody({ state: "received" })],
          next_cursor: null,
        },
        { state: "completed" },
      ),
    ).toThrow(/does not match its filters/u);
  });

  it("WEB-06 enforces ascending unique timeline sequence and exact child links", () => {
    const page = normalizeRunTimelinePage(
      {
        run_id: RUN_ID,
        items: [timelineEventBody(1), timelineEventBody(2)],
        next_cursor: null,
      },
      RUN_ID,
      { limit: 2 },
    );

    expect(page.items.map(({ sequence }) => sequence)).toEqual([1, 2]);
    expect(() =>
      normalizeRunTimelinePage(
        {
          run_id: RUN_ID,
          items: [timelineEventBody(2), timelineEventBody(1)],
          next_cursor: null,
        },
        RUN_ID,
        { limit: 2 },
      ),
    ).toThrow(/ascending sequence order/u);
    expect(() =>
      normalizeRunTimelinePage(
        {
          run_id: OTHER_RUN_ID,
          items: [],
          next_cursor: null,
        },
        RUN_ID,
      ),
    ).toThrow(/does not match its run/u);
    expect(() =>
      normalizeRunTimelinePage(
        {
          run_id: RUN_ID,
          items: [
            timelineEventBody(1, {
              action_id: ACTION_ID,
              action_url: "/api/v1/external-actions/action.web06.wrong",
            }),
          ],
          next_cursor: null,
        },
        RUN_ID,
      ),
    ).toThrow(/action_url does not match/u);
  });

  it("WEB-06 rejects expired timeline metadata that is not empty", () => {
    expect(() =>
      normalizeRunTimelinePage(
        {
          run_id: RUN_ID,
          items: [timelineEventBody(1, { metadata_expired: true })],
          next_cursor: null,
        },
        RUN_ID,
      ),
    ).toThrow(/metadata must be empty after expiry/u);

    expect(
      normalizeRunTimelinePage(
        {
          run_id: RUN_ID,
          items: [
            timelineEventBody(1, { metadata_expired: true, metadata: {} }),
          ],
          next_cursor: null,
        },
        RUN_ID,
      ).items[0]?.metadata,
    ).toEqual({});
  });

  it("WEB-06 enforces ascending artifact order and page Run binding", () => {
    const page = normalizeRunArtifactsPage(
      {
        run_id: RUN_ID,
        items: [
          artifactSummaryBody(),
          artifactSummaryBody({
            id: OTHER_ARTIFACT_ID,
            created_at: "2026-08-31T18:00:01.000000001Z",
          }),
        ],
        next_cursor: null,
      },
      RUN_ID,
      { limit: 2 },
    );

    expect(page.items.map(({ id }) => id)).toEqual([
      ARTIFACT_ID,
      OTHER_ARTIFACT_ID,
    ]);
    expect(() =>
      normalizeRunArtifactsPage(
        {
          run_id: RUN_ID,
          items: [artifactSummaryBody({ run_id: OTHER_RUN_ID })],
          next_cursor: null,
        },
        RUN_ID,
      ),
    ).toThrow(/resource binding is invalid/u);
    expect(() =>
      normalizeRunArtifactsPage(
        {
          run_id: RUN_ID,
          items: [
            artifactSummaryBody({
              id: OTHER_ARTIFACT_ID,
              created_at: "2026-08-31T18:00:01.000000001Z",
            }),
            artifactSummaryBody(),
          ],
          next_cursor: null,
        },
        RUN_ID,
        { limit: 2 },
      ),
    ).toThrow(/ascending keyset order/u);
  });

  it("WEB-06 validates artifact hashes, digest, identity, classification, and links", () => {
    const artifact = normalizeArtifactResource(
      artifactResourceBody(),
      ARTIFACT_ID,
    );

    expect(artifact.id).toBe(ARTIFACT_ID);
    expect(artifact.redactedPayload).toEqual({
      subject: "Welcome",
      email: "[REDACTED]",
    });
    expect(Object.isFrozen(artifact.redactedPayload)).toBe(true);
    const serverProjected = normalizeArtifactResource(
      artifactResourceBody({
        redacted_payload: { visible: "server-projected-safe-value" },
      }),
    );
    expect(serverProjected.redactedPayload.visible).toBe(
      "server-projected-safe-value",
    );
    for (const overrides of [
      { payload_digest: "artifact-hmac-sha256-v1:" + "A".repeat(64) },
      { output_schema_hash: "schema-sha256-v1:" + "g".repeat(64) },
      { classification: "secret" },
      { artifact_url: "/api/v1/artifacts/artifact.web06.wrong" },
    ]) {
      expect(() =>
        normalizeArtifactResource(artifactResourceBody(overrides), ARTIFACT_ID),
      ).toThrow();
    }
    expect(() =>
      normalizeArtifactResource(artifactResourceBody(), OTHER_ARTIFACT_ID),
    ).toThrow(/does not match its request/u);
  });

  it("WEB-06 rejects backend-impossible artifact provenance", () => {
    const coherent = normalizeArtifactResource(
      artifactResourceBody({
        sources: [
          {
            kind: "work_input",
            source_id: WORK_ID,
            classification: "internal",
          },
          {
            kind: "parent_artifact",
            source_id: OTHER_ARTIFACT_ID,
            classification: "internal",
          },
        ],
        parent_artifact_ids: [OTHER_ARTIFACT_ID],
      }),
    );
    expect(coherent.parentArtifactIds).toEqual([OTHER_ARTIFACT_ID]);

    expect(() =>
      normalizeArtifactResource(
        artifactResourceBody({
          sources: [
            {
              kind: "work_input",
              source_id: WORK_ID,
              classification: "internal",
            },
            {
              kind: "parent_artifact",
              source_id: WORK_ID,
              classification: "internal",
            },
          ],
          parent_artifact_ids: [WORK_ID],
        }),
      ),
    ).toThrow(/source IDs must be unique/u);
    expect(() =>
      normalizeArtifactResource(
        artifactResourceBody({
          parent_artifact_ids: [OTHER_ARTIFACT_ID],
        }),
      ),
    ).toThrow(/parent sources do not match/u);
    expect(() =>
      normalizeArtifactResource(
        artifactResourceBody({
          sources: [
            {
              kind: "work_input",
              source_id: WORK_ID,
              classification: "sensitive",
            },
          ],
        }),
      ),
    ).toThrow(/classification is lower than a source/u);
  });

  it("WEB-06 validates external-action enums, attempts, lifecycle, and link rebinding", () => {
    const action = normalizeExternalAction(
      externalActionBody(),
      ACTION_ID,
      RUN_ID,
    );

    expect(action).toMatchObject({
      id: ACTION_ID,
      runId: RUN_ID,
      state: "awaiting_approval",
      deliveryAttemptCount: 0,
    });
    expect(() =>
      normalizeExternalAction(
        externalActionBody({ state: "unknown" }),
        ACTION_ID,
      ),
    ).toThrow(/state is unsupported/u);
    expect(() =>
      normalizeExternalAction(
        externalActionBody({
          delivery_attempt_count: 4,
          delivery_attempt_limit: 3,
        }),
        ACTION_ID,
      ),
    ).toThrow(/attempt limit/u);
    expect(() =>
      normalizeExternalAction(
        externalActionBody({ run_url: `/api/v1/runs/${OTHER_RUN_ID}` }),
        ACTION_ID,
      ),
    ).toThrow(/run_url does not match/u);
  });

  it("WEB-06 validates the full run resource and every nested exact field set", () => {
    const run = normalizeRunResource(runResourceBody(), RUN_ID);

    expect(run).toMatchObject({
      id: RUN_ID,
      state: "received",
      version: 1,
      artifactsTruncated: false,
    });
    expect(Object.isFrozen(run)).toBe(true);
    expect(Object.isFrozen(run.transitions)).toBe(true);
    expect(() =>
      normalizeRunResource(
        runResourceBody({
          transitions: [
            {
              ...(
                runResourceBody().transitions as Record<string, unknown>[]
              )[0],
              unsupported: "never accept transport drift",
            },
          ],
        }),
      ),
    ).toThrow(/fields are unsupported/u);
    expect(() =>
      normalizeRunResource(
        runResourceBody({
          transitions: [
            {
              ...(
                runResourceBody().transitions as Record<string, unknown>[]
              )[0],
              resulting_version: 2,
            },
          ],
        }),
      ),
    ).toThrow(/transitions are incoherent/u);
    expect(() => normalizeRunResource(runResourceBody(), OTHER_RUN_ID)).toThrow(
      /does not match its request/u,
    );
  });

  it("WEB-06 rejects incoherent run terminal authority", () => {
    expect(() =>
      normalizeRunPage({
        items: [
          runSummaryBody({
            state: "completed",
            terminal_reason_code: null,
          }),
        ],
        next_cursor: null,
      }),
    ).toThrow(/terminal state and reason are incoherent/u);
    expect(() =>
      normalizeRunPage({
        items: [
          runSummaryBody({
            state: "executing",
            terminal_reason_code: "run_completed",
          }),
        ],
        next_cursor: null,
      }),
    ).toThrow(/terminal state and reason are incoherent/u);

    const terminalTransition = {
      sequence: 1,
      command: "complete",
      previous_state: "executing",
      new_state: "completed",
      reason_code: "run_completed",
      occurred_at: "2026-08-31T18:00:00.000000002Z",
      expected_version: 1,
      resulting_version: 2,
      completed_effect_count: 1,
      outcome_unknown_effect_count: 0,
    };
    expect(
      normalizeRunResource(
        runResourceBody({
          state: "completed",
          terminal_reason_code: "run_completed",
          updated_at: "2026-08-31T18:00:00.000000002Z",
          version: 2,
          transitions: [terminalTransition],
        }),
      ).terminalReasonCode,
    ).toBe("run_completed");
    expect(() =>
      normalizeRunResource(
        runResourceBody({
          state: "completed",
          terminal_reason_code: "different_terminal_reason",
          updated_at: "2026-08-31T18:00:00.000000002Z",
          version: 2,
          transitions: [terminalTransition],
        }),
      ),
    ).toThrow(/terminal reason differs from its terminal transition/u);
  });

  it("WEB-06 requires complete connector and WRITE approval snapshots", () => {
    const normalizeStep = (step: Record<string, unknown>) =>
      normalizeRunResource(
        runResourceBody({ plan: runPlanBody({ steps: [step] }) }),
      );

    expect(normalizeStep(runStepBody()).plan?.steps[0]).toMatchObject({
      connectorFamily: "newsletter",
      timeoutSeconds: 60,
      approvalRequiredRoles: ["approver"],
    });
    for (const incompleteConnector of [
      runStepBody({
        binding_id: null,
        binding_configuration_revision: null,
      }),
      runStepBody({ timeout_seconds: null }),
      runStepBody({ request_schema_id: null }),
      runStepBody({ result_schema_id: null, result_schema_hash: null }),
    ]) {
      expect(() => normalizeStep(incompleteConnector)).toThrow(
        /external connector contract snapshot is incomplete/u,
      );
    }
    for (const incompleteApproval of [
      runStepBody({ approval_required_roles: [] }),
      runStepBody({ approval_required_scopes: [] }),
      runStepBody({ approval_expires_after_seconds: null }),
      runStepBody({ approval_allow_self_approval: null }),
    ]) {
      expect(() => normalizeStep(incompleteApproval)).toThrow(
        /WRITE approval snapshot is incomplete/u,
      );
    }
  });

  it("WEB-06 binds sealed steps to the exact selected and routing snapshots", () => {
    const normalizePlan = (plan: Record<string, unknown>) =>
      normalizeRunResource(runResourceBody({ plan }));
    const otherTemplateId = "tpl.web06.other";

    for (const incoherentPlan of [
      runPlanBody({
        steps: [
          runStepBody({
            template_id: otherTemplateId,
            template_url: `/api/v1/agent-templates/${otherTemplateId}`,
          }),
        ],
      }),
      runPlanBody({
        steps: [
          runStepBody({
            configuration_revision: 2,
            binding_configuration_revision: 2,
          }),
        ],
      }),
      runPlanBody({
        steps: [runStepBody({ routing_slot_key: "unsealed-slot" })],
      }),
      runPlanBody({
        routing_assignments: [
          {
            ...(
              runPlanBody().routing_assignments as Record<string, unknown>[]
            )[0],
            required_capability_ids: ["email.contact.upsert"],
          },
        ],
      }),
    ]) {
      expect(() => normalizePlan(incoherentPlan)).toThrow(
        /routing snapshot does not bind its sealed steps/u,
      );
    }
  });

  it("WEB-06 binds external actions to required-idempotency sealed WRITE steps", () => {
    const resource = runResourceBody({
      approval_required: true,
      plan: runPlanBody(),
      external_actions: [externalActionBody()],
    });

    expect(normalizeRunResource(resource).plan?.steps[0]).toMatchObject({
      effect: "write",
      idempotencySupport: "required",
    });
    expect(() =>
      normalizeRunResource(
        runResourceBody({
          approval_required: true,
          plan: runPlanBody({
            steps: [runStepBody({ idempotency_support: "supported" })],
          }),
          external_actions: [externalActionBody()],
        }),
      ),
    ).toThrow(/idempotency contract is incoherent/u);
    expect(() =>
      normalizeRunResource(
        runResourceBody({
          approval_required: true,
          plan: runPlanBody(),
          external_actions: [
            externalActionBody({ capability_id: "email.contact.upsert" }),
          ],
        }),
      ),
    ).toThrow(/does not bind its sealed WRITE step/u);
  });

  it("WEB-06 rejects step binding-revision and terminal-reason drift", () => {
    expect(() =>
      normalizeRunResource(
        runResourceBody({
          plan: runPlanBody({
            steps: [
              runStepBody({
                configuration_revision: 2,
                binding_configuration_revision: 1,
              }),
            ],
          }),
        }),
      ),
    ).toThrow(/binding revision differs/u);
    expect(() =>
      normalizeRunResource(
        runResourceBody({
          plan: runPlanBody({
            steps: [
              runStepBody({
                state: "succeeded",
                terminal_result: true,
                terminal_reason_code: null,
              }),
            ],
          }),
        }),
      ),
    ).toThrow(/terminal state and reason are incoherent/u);
    expect(() =>
      normalizeRunResource(
        runResourceBody({
          plan: runPlanBody({
            steps: [
              runStepBody({
                state: "awaiting_approval",
                terminal_reason_code: "step_succeeded",
              }),
            ],
          }),
        }),
      ),
    ).toThrow(/terminal state and reason are incoherent/u);

    const terminalTransition = {
      sequence: 1,
      command: "succeed",
      previous_state: "executing",
      new_state: "succeeded",
      reason_code: "step_succeeded",
      occurred_at: "2026-08-31T18:00:00.000000002Z",
      expected_version: 1,
      resulting_version: 2,
    };
    const terminalStep = runStepBody({
      state: "succeeded",
      terminal_result: true,
      terminal_reason_code: "step_succeeded",
      version: 2,
      transitions: [terminalTransition],
    });
    expect(
      normalizeRunResource(
        runResourceBody({
          plan: runPlanBody({ steps: [terminalStep] }),
        }),
      ).plan?.steps[0]?.terminalReasonCode,
    ).toBe("step_succeeded");
    expect(() =>
      normalizeRunResource(
        runResourceBody({
          plan: runPlanBody({
            steps: [
              {
                ...terminalStep,
                terminal_reason_code: "different_terminal_reason",
              },
            ],
          }),
        }),
      ),
    ).toThrow(/differs from its terminal transition/u);
  });

  it("WEB-06 rejects unknown fields, unsafe prototypes, and unsafe nested keys", () => {
    expect(() =>
      normalizeRunPage({
        items: [runSummaryBody({ unexpected: true })],
        next_cursor: null,
      }),
    ).toThrow(/fields are unsupported/u);

    const nonPlain = Object.create({ inherited: "unsafe" }) as Record<
      string,
      unknown
    >;
    Object.assign(nonPlain, runResourceBody());
    expect(() => normalizeRunResource(nonPlain)).toThrow(/plain object/u);

    const unsafePayload = JSON.parse(
      '{"__proto__":{"private":"must-not-cross"}}',
    ) as Record<string, unknown>;
    expect(() =>
      normalizeArtifactResource(
        artifactResourceBody({ redacted_payload: unsafePayload }),
      ),
    ).toThrow(/unsafe key/u);
  });

  it("WEB-06 bounds recursive JSON depth, nodes, arrays, keys, sparsity, and bytes", () => {
    let deep: Record<string, unknown> = { end: true };
    for (let index = 0; index < 65; index += 1) deep = { nested: deep };
    const sparse = new Array<unknown>(1);
    const tooManyArrayItems = Array.from({ length: 4_097 }, () => null);
    const tooManyNodes = Array.from({ length: 4_096 }, () => ({ value: 1 }));
    const tooManyFields = Object.fromEntries(
      Array.from({ length: 1_025 }, (_, index) => [
        `field${String(index)}`,
        true,
      ]),
    );
    const oversizedBytes = Object.fromEntries(
      ["a", "b", "c", "d", "e"].map((key) => [key, "x".repeat(220_000)]),
    );
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    const malformed = [
      deep,
      { sparse },
      { values: tooManyArrayItems },
      { values: tooManyNodes },
      tooManyFields,
      { ["k".repeat(101)]: true },
      oversizedBytes,
      cyclic,
    ];

    for (const redactedPayload of malformed) {
      expect(() =>
        normalizeArtifactResource(
          artifactResourceBody({ redacted_payload: redactedPayload }),
        ),
      ).toThrow();
    }
  });

  it("WEB-06 rejects invalid limits, cursor families, query fields, and lower/upper drift", () => {
    expect(() => runListQueryKey({ limit: 0 })).toThrow(/bounded integer/u);
    expect(() =>
      runTimelineQueryKey(RUN_ID, { cursor: "artifact-page-v1.abc" }),
    ).toThrow(/cursor is invalid/u);
    expect(() =>
      runArtifactsQueryKey(RUN_ID, { cursor: "run-timeline-v1.abc" }),
    ).toThrow(/cursor is invalid/u);
    expect(() =>
      runListQueryKey({
        createdAtFrom: "2026-09-01T00:00:00Z",
        createdAtTo: "2026-08-31T00:00:00Z",
      }),
    ).toThrow(/lower time bound follows upper bound/u);
    expect(() => runListQueryKey({ unsupported: "secret" } as never)).toThrow(
      /fields are unsupported/u,
    );
    expect(
      normalizeRunTimelinePage(
        {
          run_id: RUN_ID,
          items: [timelineEventBody(1)],
          next_cursor: "run-timeline-v1.abc",
        },
        RUN_ID,
        { limit: 2 },
      ).nextCursor,
    ).toBe("run-timeline-v1.abc");
    expect(() =>
      normalizeRunTimelinePage(
        {
          run_id: RUN_ID,
          items: [],
          next_cursor: "run-timeline-v1.abc",
        },
        RUN_ID,
        { limit: 1 },
      ),
    ).toThrow(/cursor is invalid/u);
  });

  it("WEB-06 returns stable non-reflective request errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse(
          {
            code: "payload_secret",
            detail: "private-payload-value-must-not-appear",
          },
          503,
        ),
      ),
    );

    const error = await fetchArtifactResource(ARTIFACT_ID).catch(
      (caught: unknown) => caught,
    );
    expect(error).toMatchObject({ status: 503 });
    expect(String(error)).not.toMatch(/private-payload-value|payload_secret/iu);
    expect(JSON.stringify(error)).not.toMatch(
      /private-payload-value|payload_secret/iu,
    );
  });
});

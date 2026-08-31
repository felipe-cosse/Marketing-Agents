// WEB-06 browser evidence uses the production Vite preview and same-origin runtime fixtures.
import {
  expect,
  test,
  type Page,
  type Request,
  type TestInfo,
} from "@playwright/test";

type JsonObject = Record<string, unknown>;

interface Web06Observation {
  readonly externalRequests: string[];
  readonly timelineQueries: string[];
  artifactDetailRequests: number;
  artifactListRequests: number;
  runRequests: number;
}

const RUN_ID = "run.web-06.browser";
const WORK_ID = "work.web-06.browser";
const STEP_ID = "step.web-06.browser";
const ACTION_ID = "action.web-06.browser";
const RECEIPT_ID = "receipt.web-06.browser";
const ARTIFACT_ID = "artifact.web-06.browser";
const INSTANCE_ID = "inst.email.lifecycle-marketing.churned-user-monitor.01";
const TEMPLATE_ID = "tpl.email.lifecycle-marketing.churned-user-monitor";
const WORKFLOW_ID = "workflow.email.churn-review.v1";
const OUTPUT_SCHEMA_ID = `urn:marketing-agents:catalog:v1:${TEMPLATE_ID}:output`;
const TIMELINE_CURSOR = "run-timeline-v1.web06-browser-page-2";
const ARTIFACT_DIGEST = `artifact-hmac-sha256-v1:${"e".repeat(64)}`;
const PRIVATE_STORAGE_CANARY = "web-06-private-artifact-canary";
const HOSTILE_MARKDOWN = [
  "# Advisory review",
  `<script>globalThis.__web06E2ePwned = ${JSON.stringify(PRIVATE_STORAGE_CANARY)}</script>`,
  '<img src="https://attacker.invalid/pixel.png" onerror="steal()">',
  "![remote image](https://attacker.invalid/remote.png)",
  "[unsafe](javascript:alert(1))",
  "[safe review](https://example.test/review)",
].join("\n\n");

function artifactSummary(): JsonObject {
  return {
    id: ARTIFACT_ID,
    work_item_id: WORK_ID,
    run_id: RUN_ID,
    step_id: STEP_ID,
    workflow_id: WORKFLOW_ID,
    workflow_version: "1",
    template_id: TEMPLATE_ID,
    instance_id: INSTANCE_ID,
    output_schema_id: OUTPUT_SCHEMA_ID,
    output_schema_version: "1",
    classification: "internal",
    created_at: "2026-08-31T12:00:03.000000000Z",
    artifact_url: `/api/v1/artifacts/${ARTIFACT_ID}`,
    run_url: `/api/v1/runs/${RUN_ID}`,
    step_url: `/api/v1/runs/${RUN_ID}/steps/${STEP_ID}`,
    template_url: `/api/v1/agent-templates/${TEMPLATE_ID}`,
    instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
  };
}

function artifactFixture(): JsonObject {
  return {
    ...artifactSummary(),
    catalog_hash: "c".repeat(64),
    instance_config_revision: 7,
    sources: [
      {
        kind: "work_input",
        source_id: "source.web-06.browser",
        classification: "internal",
      },
      {
        kind: "parent_artifact",
        source_id: "artifact.web-06.parent",
        classification: "internal",
      },
    ],
    parent_artifact_ids: ["artifact.web-06.parent"],
    providers: [
      {
        provider_kind: "connector",
        mode: "mock",
        name: "deterministic-newsletter",
        version: "1.0.0",
      },
    ],
    output_schema_hash: `schema-sha256-v1:${"d".repeat(64)}`,
    redacted_payload: {
      artifact: HOSTILE_MARKDOWN,
      email: "[REDACTED]",
    },
    payload_digest: ARTIFACT_DIGEST,
  };
}

function stepRuntimePolicy(): JsonObject {
  return {
    operation_key: "write-advisory",
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

function stepFixture(): JsonObject {
  return {
    id: STEP_ID,
    run_id: RUN_ID,
    key: "write-advisory",
    kind: "external_action",
    selected_instance_id: INSTANCE_ID,
    template_id: TEMPLATE_ID,
    dependency_keys: [],
    capability_id: "cap.newsletter.subscribe",
    effect: "write",
    state: "succeeded",
    ordinal: 1,
    source_order: 1,
    configuration_revision: 7,
    connector_family: "newsletter",
    routing_slot_key: null,
    binding_id: "mock.newsletter.default",
    binding_configuration_revision: 7,
    request_schema_id: "schema.newsletter.subscribe.request.v1",
    result_schema_id: OUTPUT_SCHEMA_ID,
    result_schema_hash: `schema-sha256-v1:${"d".repeat(64)}`,
    data_classification: "internal",
    idempotency_support: "required",
    timeout_seconds: 60,
    runtime_policy: stepRuntimePolicy(),
    approval_policy_id: "policy.external-write.default",
    approval_required_roles: ["approver"],
    approval_required_scopes: ["scope.external-write"],
    approval_expires_after_seconds: 3_600,
    approval_allow_self_approval: true,
    terminal_result: true,
    created_at: "2026-08-31T12:00:00.000000000Z",
    updated_at: "2026-08-31T12:00:03.000000000Z",
    version: 1,
    terminal_reason_code: "step_succeeded",
    transitions: [],
    step_url: `/api/v1/runs/${RUN_ID}/steps/${STEP_ID}`,
    run_url: `/api/v1/runs/${RUN_ID}`,
    instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
    template_url: `/api/v1/agent-templates/${TEMPLATE_ID}`,
  };
}

function actionFixture(): JsonObject {
  return {
    id: ACTION_ID,
    run_id: RUN_ID,
    step_id: STEP_ID,
    step_key: "write-advisory",
    template_id: TEMPLATE_ID,
    instance_id: INSTANCE_ID,
    proposal_revision: 1,
    action_type: "newsletter.subscribe",
    capability_id: "cap.newsletter.subscribe",
    connector_family: "newsletter",
    binding_id: "mock.newsletter.default",
    destination_summary: "Mock newsletter · browser audience",
    redacted_payload: { email: "[REDACTED]" },
    payload_schema_id: "schema.newsletter.subscribe.request.v1",
    state: "succeeded",
    created_at: "2026-08-31T12:00:01.000000000Z",
    updated_at: "2026-08-31T12:00:03.000000000Z",
    version: 1,
    delivery_attempt_count: 1,
    delivery_attempt_limit: 3,
    approval_policy_id: "policy.external-write.default",
    approval_required_roles: ["approver"],
    approval_required_scopes: ["scope.external-write"],
    approval_expires_after_seconds: 3_600,
    approval_allow_self_approval: true,
    terminal_reason_code: null,
    superseded_by_action_id: null,
    superseded_at: null,
    receipt_id: RECEIPT_ID,
    result_status: "succeeded",
    result_safe_metadata: null,
    completed_at: "2026-08-31T12:00:03.000000000Z",
    action_url: `/api/v1/external-actions/${ACTION_ID}`,
    run_url: `/api/v1/runs/${RUN_ID}`,
    step_url: `/api/v1/runs/${RUN_ID}/steps/${STEP_ID}`,
    instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
    template_url: `/api/v1/agent-templates/${TEMPLATE_ID}`,
  };
}

function runTransition(
  sequence: number,
  previousState: string | null,
  newState: "executing" | "completed",
): JsonObject {
  return {
    sequence,
    command: newState === "completed" ? "complete" : "start",
    previous_state: previousState,
    new_state: newState,
    reason_code: newState === "completed" ? "run_completed" : "run_started",
    occurred_at: `2026-08-31T12:00:0${String(sequence)}.000000000Z`,
    expected_version: sequence - 1,
    resulting_version: sequence,
    completed_effect_count: newState === "completed" ? 1 : 0,
    outcome_unknown_effect_count: 0,
  };
}

function runFixture(terminal: boolean): JsonObject {
  const state = terminal ? "completed" : "executing";
  const version = terminal ? 2 : 1;
  return {
    id: RUN_ID,
    work_item_id: WORK_ID,
    instance_id: INSTANCE_ID,
    workflow_id: WORKFLOW_ID,
    trigger_id: "trigger.web-06.browser",
    source: "manual",
    mode: "mock_execution",
    state,
    catalog_hash: "c".repeat(64),
    configuration_revision: 7,
    approval_required: true,
    terminal_reason_code: terminal ? "run_completed" : null,
    created_at: "2026-08-31T12:00:00.000000000Z",
    updated_at: terminal
      ? "2026-08-31T12:00:04.000000000Z"
      : "2026-08-31T12:00:01.000000000Z",
    version,
    run_url: `/api/v1/runs/${RUN_ID}`,
    timeline_url: `/api/v1/runs/${RUN_ID}/timeline`,
    artifacts_url: `/api/v1/runs/${RUN_ID}/artifacts`,
    instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
    transitions: terminal
      ? [
          runTransition(1, null, "executing"),
          runTransition(2, "executing", "completed"),
        ]
      : [runTransition(1, null, "executing")],
    plan: {
      plan_hash: "1".repeat(64),
      workflow_id: WORKFLOW_ID,
      workflow_version: 1,
      workflow_definition_hash: "2".repeat(64),
      catalog_content_hash: `catalog-sha256-v1:${"3".repeat(64)}`,
      graph_hash: "4".repeat(64),
      routing_hash: "5".repeat(64),
      approval_required: true,
      step_count: 1,
      runtime_policy: {
        max_steps: 5,
        max_model_calls: 2,
        max_tool_calls: 3,
        run_timeout_seconds: 300,
      },
      created_at: "2026-08-31T12:00:00.000000000Z",
      selected_instances: [
        {
          instance_id: INSTANCE_ID,
          template_id: TEMPLATE_ID,
          configuration_revision: 7,
          display_order: 1,
          source_ordinal: 1,
          selection_order: 1,
          target: true,
          instance_url: `/api/v1/agent-instances/${INSTANCE_ID}`,
          template_url: `/api/v1/agent-templates/${TEMPLATE_ID}`,
        },
      ],
      routing_assignments: [],
      steps: [stepFixture()],
    },
    execution_control: null,
    pending_approvals: [],
    artifact_summaries: [artifactSummary()],
    artifacts_truncated: false,
    external_actions: [actionFixture()],
    terminal_error: null,
  };
}

function timelineEvent(
  sequence: number,
  eventType: string,
  options: {
    readonly actionId?: string;
    readonly artifactId?: string;
    readonly stepId?: string;
  } = {},
): JsonObject {
  const stepId = options.stepId ?? null;
  const actionId = options.actionId ?? null;
  const artifactId = options.artifactId ?? null;
  return {
    id: `event.web-06.browser.${String(sequence)}`,
    sequence,
    schema_version: 1,
    event_type: eventType,
    aggregate_type: "run",
    aggregate_id: RUN_ID,
    outcome: "accepted",
    actor_id: `actor-hmac-sha256-v1:${"a".repeat(64)}`,
    actor_source: "local_session",
    auth_method: "local_session",
    correlation_id: `correlation-hmac-sha256-v1:${"b".repeat(64)}`,
    occurred_at: new Date(
      Date.UTC(2026, 7, 31, 12, 0, 0) + sequence * 1_000,
    ).toISOString(),
    step_id: stepId,
    action_id: actionId,
    approval_request_id: null,
    artifact_id: artifactId,
    attempted_command: null,
    previous_state:
      eventType === "run.completed"
        ? "executing"
        : eventType === "run.executing"
          ? "planned"
          : null,
    new_state:
      eventType === "run.completed"
        ? "completed"
        : eventType === "run.executing"
          ? "executing"
          : null,
    reason_code: null,
    metadata: { safe_summary: `event ${String(sequence)}` },
    metadata_classification: "internal",
    metadata_expires_at: "2099-08-31T12:00:00.000000000Z",
    metadata_expired: false,
    run_url: `/api/v1/runs/${RUN_ID}`,
    step_url: stepId === null ? null : `/api/v1/runs/${RUN_ID}/steps/${stepId}`,
    action_url:
      actionId === null ? null : `/api/v1/external-actions/${actionId}`,
    approval_url: null,
    artifact_url:
      artifactId === null ? null : `/api/v1/artifacts/${artifactId}`,
  };
}

function timelineFixture(cursor: string | null, terminal: boolean): JsonObject {
  if (cursor === null) {
    return {
      run_id: RUN_ID,
      items: [
        timelineEvent(1, "run.executing"),
        timelineEvent(2, "action.succeeded", {
          actionId: ACTION_ID,
          stepId: STEP_ID,
        }),
      ],
      next_cursor: TIMELINE_CURSOR,
    };
  }
  expect(cursor).toBe(TIMELINE_CURSOR);
  return {
    run_id: RUN_ID,
    items: [
      timelineEvent(3, "artifact.created", {
        artifactId: ARTIFACT_ID,
        stepId: STEP_ID,
      }),
      ...(terminal ? [timelineEvent(4, "run.completed")] : []),
    ],
    next_cursor: null,
  };
}

async function installRuntimeBoundary(page: Page): Promise<Web06Observation> {
  const observation: Web06Observation = {
    externalRequests: [],
    timelineQueries: [],
    artifactDetailRequests: 0,
    artifactListRequests: 0,
    runRequests: 0,
  };

  page.on("request", (request: Request) => {
    const url = new URL(request.url());
    if (!new Set(["127.0.0.1", "localhost"]).has(url.hostname)) {
      observation.externalRequests.push(request.url());
    }
  });

  await page.route("**/api/v1/approvals**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Cache-Control": "no-store", Vary: "Authorization" },
      body: JSON.stringify({ items: [], next_cursor: null }),
    });
  });

  await page.route("**/api/v1/session", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Cache-Control": "no-store" },
      body: JSON.stringify({
        actorId: "local-operator",
        roles: ["approver", "local_admin", "operator", "viewer"],
        scopes: ["approvals:read"],
        authMode: "local",
        environment: "local",
        modelMode: "mock",
        connectorMode: "mock",
        networkPermission: false,
        warning: "Local identity — not production authentication",
        csrfToken: "web06csrf".repeat(5),
        csrfHeaderName: "X-CSRF-Token",
      }),
    });
  });

  await page.route("**/api/v1/runs/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const { pathname } = url;
    if (request.method() !== "GET") {
      throw new Error(
        `Unexpected WEB-06 mutation: ${request.method()} ${pathname}`,
      );
    }
    if (pathname === `/api/v1/runs/${RUN_ID}`) {
      observation.runRequests += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Cache-Control": "no-store", Vary: "Authorization" },
        body: JSON.stringify(runFixture(observation.runRequests >= 2)),
      });
      return;
    }
    if (pathname === `/api/v1/runs/${RUN_ID}/timeline`) {
      observation.timelineQueries.push(url.search);
      const cursor = url.searchParams.get("cursor");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Cache-Control": "no-store", Vary: "Authorization" },
        body: JSON.stringify(
          timelineFixture(cursor, observation.runRequests >= 2),
        ),
      });
      return;
    }
    if (pathname === `/api/v1/runs/${RUN_ID}/artifacts`) {
      observation.artifactListRequests += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Cache-Control": "no-store", Vary: "Authorization" },
        body: JSON.stringify({
          run_id: RUN_ID,
          items: [artifactSummary()],
          next_cursor: null,
        }),
      });
      return;
    }
    throw new Error(`Unexpected WEB-06 run request: ${pathname}`);
  });

  await page.route("**/api/v1/artifacts/*", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    expect(request.method()).toBe("GET");
    expect(pathname).toBe(`/api/v1/artifacts/${ARTIFACT_ID}`);
    observation.artifactDetailRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Cache-Control": "no-store", Vary: "Authorization" },
      body: JSON.stringify(artifactFixture()),
    });
  });

  return observation;
}

async function capture(
  page: Page,
  testInfo: TestInfo,
  name: string,
): Promise<void> {
  await page.screenshot({ path: testInfo.outputPath(name), fullPage: true });
}

test("WEB-06 run timeline reaches terminal state and opens one inert provenance-bound artifact", async ({
  page,
}, testInfo) => {
  const observation = await installRuntimeBoundary(page);
  await page.goto(`/runs/${RUN_ID}`);

  await expect(
    page.getByRole("heading", { level: 1, name: "Run timeline" }),
  ).toBeVisible();
  await expect(
    page.getByText("Run Executing — live monitoring active", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(RECEIPT_ID, { exact: true })).toBeVisible();
  await expect(
    page.getByText(
      /Required support; the protected key is not exposed by this read API/u,
    ),
  ).toBeVisible();
  await expect(
    page.getByText("No real external delivery occurred", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Idempotency key", { exact: true })).toHaveCount(
    0,
  );
  await expect(page.locator('a[href*="/receipts/"]')).toHaveCount(0);

  const timeline = page.getByRole("list", {
    name: "Run timeline in sequence order",
  });
  await expect(timeline.getByRole("listitem")).toHaveCount(2);
  await expect(timeline.getByText("Sequence 1", { exact: true })).toBeVisible();
  await expect(timeline.getByText("Sequence 2", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Load more events" }).click();
  await expect(timeline.getByText("Sequence 3", { exact: true })).toBeVisible();
  await expect
    .poll(() => observation.runRequests, { timeout: 10_000 })
    .toBeGreaterThanOrEqual(2);
  await expect(
    page.getByText("Terminal snapshot — polling stopped", { exact: true }),
  ).toBeVisible();
  await expect(timeline.getByText("Sequence 4", { exact: true })).toBeVisible();
  await expect(
    timeline.getByText("Run completed", { exact: true }),
  ).toBeVisible();
  await expect
    .poll(() => observation.artifactListRequests, { timeout: 10_000 })
    .toBeGreaterThanOrEqual(2);

  const settledRunRequests = observation.runRequests;
  await page.waitForTimeout(3_000);
  expect(observation.runRequests).toBe(settledRunRequests);

  const artifactLink = timeline.getByRole("link", {
    name: `Artifact ${ARTIFACT_ID}`,
  });
  await artifactLink.focus();
  await expect(artifactLink).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(
    new RegExp(`/artifacts/${ARTIFACT_ID.replaceAll(".", "\\.")}$`, "u"),
  );
  await expect(
    page.getByRole("heading", { level: 1, name: "Artifact viewer" }),
  ).toBeVisible();
  await expect(page.getByText(ARTIFACT_ID, { exact: true })).toBeVisible();
  await expect(page.getByText(OUTPUT_SCHEMA_ID, { exact: true })).toBeVisible();
  await expect(page.getByText(ARTIFACT_DIGEST, { exact: true })).toBeVisible();
  await expect(page.getByText(TEMPLATE_ID, { exact: true })).toBeVisible();
  await expect(page.getByText(INSTANCE_ID, { exact: true })).toBeVisible();
  await expect(
    page.getByText("Advisory — human decision required", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("No real external delivery occurred", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("[Image omitted: remote image]")).toBeVisible();
  await expect(page.getByRole("link", { name: "unsafe" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "safe review" })).toHaveAttribute(
    "href",
    "https://example.test/review",
  );
  await expect(
    page.locator("main script, main iframe, main img, main object, main embed"),
  ).toHaveCount(0);
  await expect(page.getByRole("link", { name: /download/iu })).toHaveCount(0);
  expect(
    await page.evaluate(
      () =>
        (globalThis as typeof globalThis & { __web06E2ePwned?: string })
          .__web06E2ePwned,
    ),
  ).toBeUndefined();

  const privateBrowserState = await page.evaluate(() =>
    JSON.stringify({
      location: location.href,
      localStorage: Object.keys(localStorage).map((key) => [
        key,
        localStorage.getItem(key),
      ]),
      sessionStorage: Object.keys(sessionStorage).map((key) => [
        key,
        sessionStorage.getItem(key),
      ]),
      cookie: document.cookie,
    }),
  );
  for (const privateValue of [
    PRIVATE_STORAGE_CANARY,
    "[REDACTED]",
    ARTIFACT_DIGEST,
  ]) {
    expect(privateBrowserState).not.toContain(privateValue);
  }
  expect(observation.artifactListRequests).toBeGreaterThanOrEqual(2);
  expect(observation.artifactDetailRequests).toBe(1);
  expect(observation.timelineQueries.length).toBeGreaterThanOrEqual(3);
  expect(observation.externalRequests).toEqual([]);
  await capture(page, testInfo, "web-06-run-artifact-1536x1024.png");
});

// WEB-05 browser evidence uses the production Vite preview and real base FastAPI endpoints.
import {
  expect,
  test,
  type Page,
  type Request,
  type Route,
  type TestInfo,
} from "@playwright/test";

type JsonObject = Record<string, unknown>;

interface ApprovalObservation {
  readonly decisions: {
    readonly path: string;
    readonly headers: Record<string, string>;
    readonly body: JsonObject;
  }[];
  readonly externalRequests: string[];
  readonly listQueries: string[];
  detailRequests: number;
  runRequests: number;
  sessionRequests: number;
}

const APPROVAL_A = "approval.web-05.email-newsletter";
const APPROVAL_B = "approval.web-05.email-crm";
const APPROVAL_C = "approval.web-05.social-publish";
const RUN_ID = "run.web-05.email";
const HASH_A = "a".repeat(64);
const HASH_B = "b".repeat(64);
const HASH_C = "c".repeat(64);
const CSRF_TOKEN = "web05csrf".repeat(5);
const CORRELATION_ID = `correlation.api.${"5".repeat(32)}`;
const PRIVATE_STORAGE_CANARY = "web-05-private-reason-canary";

function summary(input: {
  readonly id: string;
  readonly status: "pending" | "approved";
  readonly generation: number;
  readonly actionId: string;
  readonly actionType: string;
  readonly destinationSummary: string;
  readonly runId: string;
  readonly templateId: string;
  readonly instanceId: string;
  readonly requestedAt: string;
}): JsonObject {
  const actionable = input.status === "pending";
  return {
    id: input.id,
    status: input.status,
    resource_version: input.generation,
    generation: input.generation,
    action_id: input.actionId,
    action_type: input.actionType,
    destination_summary: input.destinationSummary,
    run_id: input.runId,
    template_id: input.templateId,
    instance_id: input.instanceId,
    requested_at: input.requestedAt,
    expires_at: "2099-08-31T22:00:00Z",
    is_expired: false,
    is_actionable: actionable,
    approval_url: `/api/v1/approvals/${input.id}`,
    action_url: `/api/v1/external-actions/${input.actionId}`,
    run_url: `/api/v1/runs/${input.runId}`,
  };
}

const LIST_ITEMS = [
  summary({
    id: APPROVAL_A,
    status: "pending",
    generation: 1,
    actionId: "action.web-05.email-newsletter",
    actionType: "newsletter.subscribe",
    destinationSummary: "Mock newsletter · Demo subscribers",
    runId: RUN_ID,
    templateId: "tpl.email.newsletter.newsletter-subscriber",
    instanceId: "inst.email.newsletter.newsletter-subscriber.01",
    requestedAt: "2026-08-31T21:02:00Z",
  }),
  summary({
    id: APPROVAL_B,
    status: "pending",
    generation: 1,
    actionId: "action.web-05.email-crm",
    actionType: "crm.upsert-contact",
    destinationSummary: "Mock CRM · Demo contact",
    runId: RUN_ID,
    templateId: "tpl.email.lifecycle-marketing.customer-onboarder",
    instanceId: "inst.email.lifecycle-marketing.customer-onboarder.01",
    requestedAt: "2026-08-31T21:01:00Z",
  }),
  summary({
    id: APPROVAL_C,
    status: "approved",
    generation: 1,
    actionId: "action.web-05.social-publish",
    actionType: "social.publish",
    destinationSummary: "Mock social account",
    runId: "run.web-05.social",
    templateId: "tpl.social-media.new-content.linkedin-post-drafter",
    instanceId: "inst.social-media.new-content.linkedin-post-drafter.01",
    requestedAt: "2026-08-31T21:00:00Z",
  }),
] as const;

function listItem(id: string): JsonObject {
  const value = LIST_ITEMS.find((item) => item.id === id);
  if (value === undefined)
    throw new Error(`WEB-05 list fixture is missing ${id}`);
  return value;
}

function currentListItems(refreshed: boolean): readonly JsonObject[] {
  if (!refreshed) return LIST_ITEMS;
  return LIST_ITEMS.map((item) =>
    item.id === APPROVAL_A
      ? {
          ...item,
          status: "approved",
          resource_version: 2,
          is_actionable: false,
        }
      : item,
  );
}

function detail(id: string, refreshed = false): JsonObject {
  const source = listItem(id);
  const approvedByAnotherActor = refreshed && id === APPROVAL_A;
  const generation = source.generation;
  const payloadHash =
    id === APPROVAL_A ? HASH_A : id === APPROVAL_B ? HASH_B : HASH_C;
  const stepId =
    id === APPROVAL_A
      ? "step.web-05.newsletter"
      : id === APPROVAL_B
        ? "step.web-05.crm"
        : "step.web-05.social";
  return {
    ...source,
    status: approvedByAnotherActor ? "approved" : source.status,
    resource_version: approvedByAnotherActor ? 2 : source.resource_version,
    generation,
    is_actionable: approvedByAnotherActor ? false : source.is_actionable,
    one_time_use_state: "unused",
    capability_id:
      id === APPROVAL_A
        ? "cap.newsletter.subscribe"
        : id === APPROVAL_B
          ? "cap.crm.upsert-contact"
          : "cap.social.publish",
    connector_family:
      id === APPROVAL_A ? "newsletter" : id === APPROVAL_B ? "crm" : "social",
    binding_id:
      id === APPROVAL_A
        ? "mock.newsletter.default"
        : id === APPROVAL_B
          ? "mock.crm.default"
          : "mock.social.default",
    redacted_payload:
      id === APPROVAL_A
        ? {
            contact_id: "demo-contact-001",
            email: "[REDACTED]",
            list: "demo-subscribers",
          }
        : id === APPROVAL_B
          ? { contact_id: "demo-contact-001", email: "[REDACTED]" }
          : { draft_id: "demo-social-draft-001" },
    payload_hash: payloadHash,
    step_id: stepId,
    policy_id: "policy.external-write.default",
    required_roles: ["approver"],
    required_scopes: ["scope.external-write"],
    allow_self_approval: true,
    requested_by: "local-operator",
    updated_at: approvedByAnotherActor
      ? "2026-08-31T21:03:00Z"
      : source.requested_at,
    decision_id: approvedByAnotherActor ? "decision.web-05.concurrent" : null,
    decision_kind: approvedByAnotherActor ? "approve" : null,
    decision_actor_id: approvedByAnotherActor
      ? "local-concurrent-approver"
      : null,
    decision_reason_code: approvedByAnotherActor ? "approval_granted" : null,
    decision_reason: approvedByAnotherActor ? PRIVATE_STORAGE_CANARY : null,
    decided_at: approvedByAnotherActor ? "2026-08-31T21:03:00Z" : null,
    expired_at: null,
    replacement_approval_id: null,
    renewed_at: null,
    superseded_at: null,
    superseded_reason_code: null,
    consumed_at: null,
    step_url: `/api/v1/runs/${String(source.run_id)}/steps/${stepId}`,
    template_url: `/api/v1/agent-templates/${String(source.template_id)}`,
    instance_url: `/api/v1/agent-instances/${String(source.instance_id)}`,
  };
}

function sessionFixture(): JsonObject {
  return {
    actorId: "local-operator",
    roles: ["approver", "local_admin", "operator", "viewer"],
    scopes: ["approvals:decide", "approvals:read", "scope.external-write"],
    authMode: "local",
    environment: "local",
    modelMode: "mock",
    connectorMode: "mock",
    networkPermission: false,
    warning: "Local identity — not production authentication",
    csrfToken: CSRF_TOKEN,
    csrfHeaderName: "X-CSRF-Token",
  };
}

function conflictProblem(): JsonObject {
  return {
    type: "urn:marketing-agents:problem:approval_decision_conflict",
    title: "Approval changed",
    status: 409,
    detail: "The approval changed before this decision was recorded.",
    instance: `urn:marketing-agents:request:${CORRELATION_ID}`,
    code: "approval_decision_conflict",
    correlation_id: CORRELATION_ID,
    current_resource_version: 2,
  };
}

function pendingRunApproval(input: {
  readonly id: string;
  readonly actionId: string;
  readonly stepId: string;
  readonly destinationSummary: string;
  readonly requestedAt: string;
}): JsonObject {
  return {
    id: input.id,
    action_id: input.actionId,
    step_id: input.stepId,
    status: "pending",
    destination_summary: input.destinationSummary,
    requested_at: input.requestedAt,
    expires_at: "2099-08-31T22:00:00Z",
    is_expired: false,
    approval_url: `/api/v1/approvals/${input.id}`,
    action_url: `/api/v1/external-actions/${input.actionId}`,
    step_url: `/api/v1/runs/${RUN_ID}/steps/${input.stepId}`,
  };
}

function runAction(input: {
  readonly id: string;
  readonly stepId: string;
  readonly stepKey: string;
  readonly templateId: string;
  readonly instanceId: string;
  readonly actionType: string;
  readonly capabilityId: string;
  readonly connectorFamily: string;
  readonly bindingId: string;
  readonly destinationSummary: string;
  readonly payloadSchemaId: string;
}): JsonObject {
  return {
    id: input.id,
    run_id: RUN_ID,
    step_id: input.stepId,
    step_key: input.stepKey,
    template_id: input.templateId,
    instance_id: input.instanceId,
    proposal_revision: 1,
    action_type: input.actionType,
    capability_id: input.capabilityId,
    connector_family: input.connectorFamily,
    binding_id: input.bindingId,
    destination_summary: input.destinationSummary,
    redacted_payload: { contact_id: "demo-contact-001" },
    payload_schema_id: input.payloadSchemaId,
    state: "awaiting_approval",
    created_at: "2026-08-31T21:00:00Z",
    updated_at: "2026-08-31T21:02:00Z",
    version: 1,
    delivery_attempt_count: 0,
    delivery_attempt_limit: 3,
    approval_policy_id: "policy.external-write.default",
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
    action_url: `/api/v1/external-actions/${input.id}`,
    run_url: `/api/v1/runs/${RUN_ID}`,
    step_url: `/api/v1/runs/${RUN_ID}/steps/${input.stepId}`,
    instance_url: `/api/v1/agent-instances/${input.instanceId}`,
    template_url: `/api/v1/agent-templates/${input.templateId}`,
  };
}

function runFixture(refreshed = false): JsonObject {
  const pendingApprovals = [
    pendingRunApproval({
      id: APPROVAL_A,
      actionId: "action.web-05.email-newsletter",
      stepId: "step.web-05.newsletter",
      destinationSummary: "Mock newsletter · Demo subscribers",
      requestedAt: "2026-08-31T21:02:00Z",
    }),
    pendingRunApproval({
      id: APPROVAL_B,
      actionId: "action.web-05.email-crm",
      stepId: "step.web-05.crm",
      destinationSummary: "Mock CRM · Demo contact",
      requestedAt: "2026-08-31T21:01:00Z",
    }),
  ];
  const externalActions = [
    runAction({
      id: "action.web-05.email-newsletter",
      stepId: "step.web-05.newsletter",
      stepKey: "step-key.web-05.newsletter",
      templateId: "tpl.email.newsletter.newsletter-subscriber",
      instanceId: "inst.email.newsletter.newsletter-subscriber.01",
      actionType: "newsletter.subscribe",
      capabilityId: "cap.newsletter.subscribe",
      connectorFamily: "newsletter",
      bindingId: "mock.newsletter.default",
      destinationSummary: "Mock newsletter · Demo subscribers",
      payloadSchemaId: "schema.newsletter.subscribe.v1",
    }),
    runAction({
      id: "action.web-05.email-crm",
      stepId: "step.web-05.crm",
      stepKey: "step-key.web-05.crm",
      templateId: "tpl.email.lifecycle-marketing.customer-onboarder",
      instanceId: "inst.email.lifecycle-marketing.customer-onboarder.01",
      actionType: "crm.upsert-contact",
      capabilityId: "cap.crm.upsert-contact",
      connectorFamily: "crm",
      bindingId: "mock.crm.default",
      destinationSummary: "Mock CRM · Demo contact",
      payloadSchemaId: "schema.crm.upsert-contact.v1",
    }),
  ];
  return {
    id: RUN_ID,
    work_item_id: "work.web-05.email",
    instance_id: "inst.email.newsletter.newsletter-subscriber.01",
    workflow_id: "workflow.email-signup.v1",
    trigger_id: "trigger.web-05.manual",
    source: "manual",
    mode: "mock_execution",
    state: "awaiting_approval",
    catalog_hash: "e".repeat(64),
    configuration_revision: 1,
    approval_required: true,
    terminal_reason_code: null,
    created_at: "2026-08-31T21:00:00Z",
    updated_at: "2026-08-31T21:02:00Z",
    version: 4,
    run_url: `/api/v1/runs/${RUN_ID}`,
    timeline_url: `/api/v1/runs/${RUN_ID}/timeline`,
    artifacts_url: `/api/v1/runs/${RUN_ID}/artifacts`,
    instance_url:
      "/api/v1/agent-instances/inst.email.newsletter.newsletter-subscriber.01",
    transitions: [{}],
    plan: null,
    execution_control: null,
    pending_approvals: refreshed ? pendingApprovals.slice(1) : pendingApprovals,
    artifact_summaries: [],
    artifacts_truncated: false,
    external_actions: refreshed
      ? externalActions.map((action, index) =>
          index === 0
            ? {
                ...action,
                state: "approved",
                updated_at: "2026-08-31T21:03:00Z",
                version: 2,
              }
            : action,
        )
      : externalActions,
    terminal_error: null,
  };
}

function jsonRequestBody(route: Route): JsonObject {
  const value: unknown = route.request().postDataJSON();
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("WEB-05 expected an exact JSON object decision body");
  }
  return value as JsonObject;
}

function decodedPathId(pathname: string): string | null {
  const match = /^\/api\/v1\/approvals\/([^/]+)$/u.exec(pathname);
  return match?.[1] === undefined ? null : decodeURIComponent(match[1]);
}

async function installApprovalBoundary(
  page: Page,
): Promise<ApprovalObservation> {
  const observation: ApprovalObservation = {
    decisions: [],
    externalRequests: [],
    listQueries: [],
    detailRequests: 0,
    runRequests: 0,
    sessionRequests: 0,
  };
  let authoritativeRefresh = false;

  page.on("request", (request: Request) => {
    const url = new URL(request.url());
    if (!new Set(["127.0.0.1", "localhost"]).has(url.hostname)) {
      observation.externalRequests.push(request.url());
    }
  });

  await page.route("**/api/v1/session", async (route) => {
    observation.sessionRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Cache-Control": "no-store" },
      body: JSON.stringify(sessionFixture()),
    });
  });

  await page.route("**/api/v1/runs/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== `/api/v1/runs/${RUN_ID}`) {
      await route.fallback();
      return;
    }
    observation.runRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(runFixture(authoritativeRefresh)),
    });
  });

  await page.route("**/api/v1/approvals**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const { pathname } = url;
    if (request.method() === "GET" && pathname === "/api/v1/approvals") {
      observation.listQueries.push(url.search);
      const status = url.searchParams.get("status");
      const runId = url.searchParams.get("run_id");
      const items = currentListItems(authoritativeRefresh).filter(
        (item) =>
          (status === null || item.status === status) &&
          (runId === null || item.run_id === runId),
      );
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Cache-Control": "no-store", Vary: "Authorization" },
        body: JSON.stringify({ items, next_cursor: null }),
      });
      return;
    }

    const detailId = decodedPathId(pathname);
    if (request.method() === "GET" && detailId !== null) {
      observation.detailRequests += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Cache-Control": "no-store", Vary: "Authorization" },
        body: JSON.stringify(detail(detailId, authoritativeRefresh)),
      });
      return;
    }

    const decisionMatch =
      /^\/api\/v1\/approvals\/([^/]+)\/(approve|reject)$/u.exec(pathname);
    if (request.method() === "POST" && decisionMatch !== null) {
      const approvalId =
        decisionMatch[1] === undefined
          ? ""
          : decodeURIComponent(decisionMatch[1]);
      expect(approvalId).toBe(APPROVAL_A);
      observation.decisions.push({
        path: pathname,
        headers: request.headers(),
        body: jsonRequestBody(route),
      });
      authoritativeRefresh = true;
      await route.fulfill({
        status: 409,
        contentType: "application/problem+json",
        headers: { "X-Correlation-ID": CORRELATION_ID },
        body: JSON.stringify(conflictProblem()),
      });
      return;
    }

    throw new Error(
      `Unexpected WEB-05 approval request: ${request.method()} ${pathname}`,
    );
  });
  return observation;
}

function approvalRow(page: Page, approvalId: string) {
  return page.locator(`article[data-approval-id="${approvalId}"]`);
}

async function capture(
  page: Page,
  testInfo: TestInfo,
  name: string,
): Promise<void> {
  await page.screenshot({ path: testInfo.outputPath(name), fullPage: true });
}

test("WEB-05 approval queue repeats exact immutable actions and refreshes a coherent authoritative conflict", async ({
  page,
}, testInfo) => {
  const observation = await installApprovalBoundary(page);
  await page.goto("/");

  const approvalsNavigation = page.getByRole("link", { name: /Approvals/u });
  await expect(approvalsNavigation).toBeVisible();
  await expect(approvalsNavigation).toContainText("2");
  await approvalsNavigation.click();
  await expect(page).toHaveURL(/\/approvals$/u);
  await expect(
    page.getByRole("heading", { level: 1, name: "Approval queue" }),
  ).toBeVisible();

  await expect(page.locator("article[data-approval-id]")).toHaveCount(2);
  await page.getByLabel("Approval status").selectOption("");
  await expect(page.locator("article[data-approval-id]")).toHaveCount(3);
  await expect(approvalRow(page, APPROVAL_C)).toBeVisible();
  await page.getByLabel("Approval status").selectOption("pending");
  await expect(page.locator("article[data-approval-id]")).toHaveCount(2);
  await page.getByLabel("Department").selectOption("email");
  await expect(page.locator("article[data-approval-id]")).toHaveCount(2);
  const actionTypeFilter = page.getByLabel("Action type");
  await expect(
    actionTypeFilter.getByRole("option", {
      name: "newsletter.subscribe",
      exact: true,
    }),
  ).toBeAttached();
  await expect(
    actionTypeFilter.getByRole("option", {
      name: "crm.upsert-contact",
      exact: true,
    }),
  ).toBeAttached();
  await actionTypeFilter.selectOption("newsletter.subscribe");
  await expect(page.locator("article[data-approval-id]")).toHaveCount(1);
  await expect(approvalRow(page, APPROVAL_A)).toBeVisible();
  await expect(approvalRow(page, APPROVAL_A)).toContainText(
    "newsletter.subscribe",
  );
  await actionTypeFilter.selectOption("crm.upsert-contact");
  await expect(page.locator("article[data-approval-id]")).toHaveCount(1);
  await expect(approvalRow(page, APPROVAL_B)).toBeVisible();
  await expect(approvalRow(page, APPROVAL_B)).toContainText(
    "crm.upsert-contact",
  );
  await actionTypeFilter.selectOption("");

  const emailRows = page.locator(
    `article[data-approval-id="${APPROVAL_A}"], article[data-approval-id="${APPROVAL_B}"]`,
  );
  await expect(emailRows).toHaveCount(2);
  await expect
    .poll(() =>
      observation.listQueries.some((search) => {
        const parameters = new URLSearchParams(search);
        return (
          parameters.get("run_id") === RUN_ID &&
          parameters.get("limit") === "100" &&
          !parameters.has("status")
        );
      }),
    )
    .toBe(true);

  const row = approvalRow(page, APPROVAL_A);
  const review = row.getByRole("button", {
    name: `Review approval ${APPROVAL_A}`,
  });
  await review.click();
  const reviewPanel = page.locator("#approval-review-panel");
  await expect(reviewPanel).toBeVisible();
  await expect(
    reviewPanel
      .locator(".approval-action-details__summary")
      .getByText("newsletter.subscribe", { exact: true }),
  ).toBeVisible();
  await expect(
    reviewPanel.getByText("Mock newsletter · Demo subscribers", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(reviewPanel.getByText(HASH_A, { exact: true })).toBeVisible();
  await expect(
    reviewPanel.getByRole("link", {
      name: "inst.email.newsletter.newsletter-subscriber.01",
    }),
  ).toBeVisible();
  await expect(reviewPanel.getByRole("link", { name: RUN_ID })).toBeVisible();
  await expect(
    reviewPanel.getByRole("link", { name: "Open authoritative timeline" }),
  ).toHaveAttribute("href", `/api/v1/runs/${RUN_ID}/timeline`);
  await expect(
    reviewPanel.getByRole("link", {
      name: "action.web-05.email-newsletter",
    }),
  ).toHaveAttribute(
    "href",
    "/api/v1/external-actions/action.web-05.email-newsletter",
  );
  await expect(
    reviewPanel.getByRole("link", { name: "step.web-05.newsletter" }),
  ).toBeVisible();
  await expect(reviewPanel.getByText("Unused", { exact: true })).toBeVisible();
  await expect(reviewPanel.getByLabel("Redacted payload JSON")).toContainText(
    '"email": "[REDACTED]"',
  );
  const emailSafety = reviewPanel.getByRole("status");
  await expect(
    emailSafety.getByText(
      "0 mock connector calls until both approvals are approved.",
      { exact: true },
    ),
  ).toBeVisible();
  const emailSafetyActions = emailSafety.getByRole("list", {
    name: "Email run approval actions",
  });
  const newsletterSafetyRow = emailSafetyActions.locator(
    `li[data-approval-id="${APPROVAL_A}"]`,
  );
  const crmSafetyRow = emailSafetyActions.locator(
    `li[data-approval-id="${APPROVAL_B}"]`,
  );
  await expect(newsletterSafetyRow).toHaveCount(1);
  await expect(newsletterSafetyRow).toContainText("newsletter.subscribe");
  await expect(crmSafetyRow).toHaveCount(1);
  await expect(crmSafetyRow).toContainText("crm.upsert-contact");

  const approve = reviewPanel.getByRole("button", { name: "Approve" });
  await approve.click();
  const dialog = page.getByRole("dialog", { name: "Approve exact action?" });
  await expect(dialog).toBeVisible();
  await expect(
    dialog.getByText("newsletter.subscribe", { exact: true }),
  ).toBeVisible();
  await expect(dialog.getByText(HASH_A, { exact: true })).toBeVisible();
  await expect(dialog.getByLabel("Redacted payload JSON")).toContainText(
    '"email": "[REDACTED]"',
  );
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(approve).toBeFocused();

  await approve.click();
  await dialog.getByRole("button", { name: "Approve exact action" }).click();
  await expect.poll(() => observation.decisions.length).toBe(1);
  await expect(page.getByRole("alert")).toContainText(/changed|refresh/iu);
  await expect.poll(() => observation.detailRequests).toBeGreaterThanOrEqual(2);
  await expect(reviewPanel.getByText(HASH_A, { exact: true })).toBeVisible();
  await expect(reviewPanel).toContainText("Approved");
  await expect(
    emailSafety.getByText(
      "0 mock connector calls until both approvals are approved.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(newsletterSafetyRow).toContainText("Approved");
  await expect(crmSafetyRow).toContainText("Pending");
  await expect(row).toHaveCount(0);
  await expect(page.getByText(/Approval recorded as/iu)).toHaveCount(0);
  await expect(page.getByText(PRIVATE_STORAGE_CANARY)).toHaveCount(0);
  await expect(
    page.getByText(/Connector action (?:completed|delivered|published|sent)/iu),
  ).toHaveCount(0);

  expect(observation.decisions).toEqual([
    {
      path: `/api/v1/approvals/${APPROVAL_A}/approve`,
      headers: expect.objectContaining({
        accept: "application/json",
        "content-type": "application/json",
        "x-csrf-token": CSRF_TOKEN,
      }),
      body: {
        expected_generation: 1,
        expected_payload_hash: HASH_A,
      },
    },
  ]);

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
  for (const secret of [HASH_A, "[REDACTED]", PRIVATE_STORAGE_CANARY]) {
    expect(privateBrowserState).not.toContain(secret);
  }
  expect(observation.sessionRequests).toBeGreaterThanOrEqual(1);
  expect(observation.runRequests).toBeGreaterThanOrEqual(1);
  expect(observation.listQueries.length).toBeGreaterThanOrEqual(1);
  expect(observation.externalRequests).toEqual([]);
  await capture(page, testInfo, "web-05-approval-queue-1536x1024.png");
});

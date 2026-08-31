// WEB-05 dependency-free witness executes the production approval and run-safety boundaries.
import "./require-pinned-node.mjs";

import assert from "node:assert/strict";
import { registerHooks } from "node:module";

registerHooks({
  resolve(specifier, context, nextResolve) {
    try {
      return nextResolve(specifier, context);
    } catch (error) {
      if (specifier.startsWith(".") && !specifier.endsWith(".ts")) {
        return nextResolve(`${specifier}.ts`, context);
      }
      throw error;
    }
  },
});

const [approvalModule, runSafetyModule, localSessionModule] = await Promise.all(
  [
    import("../src/api/approvals.ts"),
    import("../src/api/approvalRunSafety.ts"),
    import("../src/api/localSession.ts"),
  ],
);
const {
  ApprovalContractError,
  decideApproval,
  normalizeApprovalDetail,
  normalizeApprovalPage,
} = approvalModule;
const { normalizeApprovalRunSafety } = runSafetyModule;
const { clearLocalSession } = localSessionModule;

const APPROVAL_A = "approval.web-05.witness-newsletter";
const APPROVAL_B = "approval.web-05.witness-crm";
const ACTION_A = "action.web-05.witness-newsletter";
const ACTION_B = "action.web-05.witness-crm";
const STEP_A = "step.web-05.witness-newsletter";
const STEP_B = "step.web-05.witness-crm";
const RUN_ID = "run.web-05.witness-email";
const INSTANCE_A = "inst.email.newsletter.newsletter-subscriber.01";
const INSTANCE_B = "inst.email.lifecycle-marketing.customer-onboarder.01";
const TEMPLATE_A = "tpl.email.newsletter.newsletter-subscriber";
const TEMPLATE_B = "tpl.email.lifecycle-marketing.customer-onboarder";
const HASH_A = "a".repeat(64);
const CSRF_TOKEN = "web05witnesscsrf".repeat(3);

function summary(input) {
  return {
    id: input.id,
    status: "pending",
    resource_version: 1,
    generation: 1,
    action_id: input.actionId,
    action_type: input.actionType,
    destination_summary: input.destinationSummary,
    run_id: RUN_ID,
    template_id: input.templateId,
    instance_id: input.instanceId,
    requested_at: input.requestedAt,
    expires_at: "2026-08-31T22:00:00Z",
    is_expired: false,
    is_actionable: true,
    approval_url: `/api/v1/approvals/${input.id}`,
    action_url: `/api/v1/external-actions/${input.actionId}`,
    run_url: `/api/v1/runs/${RUN_ID}`,
  };
}

const SUMMARY_A = summary({
  id: APPROVAL_A,
  actionId: ACTION_A,
  actionType: "newsletter.subscribe",
  destinationSummary: "Mock newsletter · Witness subscribers",
  templateId: TEMPLATE_A,
  instanceId: INSTANCE_A,
  requestedAt: "2026-08-31T21:02:00Z",
});
const SUMMARY_B = summary({
  id: APPROVAL_B,
  actionId: ACTION_B,
  actionType: "crm.upsert-contact",
  destinationSummary: "Mock CRM · Witness contact",
  templateId: TEMPLATE_B,
  instanceId: INSTANCE_B,
  requestedAt: "2026-08-31T21:01:00Z",
});

function detail(summaryValue, input) {
  return {
    ...summaryValue,
    one_time_use_state: "unused",
    capability_id: input.capabilityId,
    connector_family: input.connectorFamily,
    binding_id: input.bindingId,
    redacted_payload: {
      contact_id: "witness-contact-001",
      email: "[REDACTED]",
    },
    payload_hash: input.payloadHash,
    step_id: input.stepId,
    policy_id: "policy.external-write.default",
    required_roles: ["approver"],
    required_scopes: ["scope.external-write"],
    allow_self_approval: true,
    requested_by: "local-operator",
    updated_at: summaryValue.requested_at,
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
    step_url: `/api/v1/runs/${RUN_ID}/steps/${input.stepId}`,
    template_url: `/api/v1/agent-templates/${summaryValue.template_id}`,
    instance_url: `/api/v1/agent-instances/${summaryValue.instance_id}`,
  };
}

const DETAIL_A = detail(SUMMARY_A, {
  capabilityId: "cap.newsletter.subscribe",
  connectorFamily: "newsletter",
  bindingId: "mock.newsletter.default",
  payloadHash: HASH_A,
  stepId: STEP_A,
});

function pendingApproval(input) {
  return {
    id: input.id,
    action_id: input.actionId,
    step_id: input.stepId,
    status: "pending",
    destination_summary: input.destinationSummary,
    requested_at: input.requestedAt,
    expires_at: "2026-08-31T22:00:00Z",
    is_expired: false,
    approval_url: `/api/v1/approvals/${input.id}`,
    action_url: `/api/v1/external-actions/${input.actionId}`,
    step_url: `/api/v1/runs/${RUN_ID}/steps/${input.stepId}`,
  };
}

function action(input) {
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
    redacted_payload: { contact_id: "witness-contact-001" },
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

function runFixture() {
  return {
    id: RUN_ID,
    work_item_id: "work.web-05.witness-email",
    instance_id: INSTANCE_A,
    workflow_id: "workflow.email-signup.v1",
    trigger_id: "trigger.web-05.witness-manual",
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
    instance_url: `/api/v1/agent-instances/${INSTANCE_A}`,
    transitions: [{}],
    plan: null,
    execution_control: null,
    pending_approvals: [
      pendingApproval({
        id: APPROVAL_A,
        actionId: ACTION_A,
        stepId: STEP_A,
        destinationSummary: SUMMARY_A.destination_summary,
        requestedAt: SUMMARY_A.requested_at,
      }),
      pendingApproval({
        id: APPROVAL_B,
        actionId: ACTION_B,
        stepId: STEP_B,
        destinationSummary: SUMMARY_B.destination_summary,
        requestedAt: SUMMARY_B.requested_at,
      }),
    ],
    artifact_summaries: [],
    artifacts_truncated: false,
    external_actions: [
      action({
        id: ACTION_A,
        stepId: STEP_A,
        stepKey: "step-key.web-05.witness-newsletter",
        templateId: TEMPLATE_A,
        instanceId: INSTANCE_A,
        actionType: "newsletter.subscribe",
        capabilityId: "cap.newsletter.subscribe",
        connectorFamily: "newsletter",
        bindingId: "mock.newsletter.default",
        destinationSummary: SUMMARY_A.destination_summary,
        payloadSchemaId: "schema.newsletter.subscribe.v1",
      }),
      action({
        id: ACTION_B,
        stepId: STEP_B,
        stepKey: "step-key.web-05.witness-crm",
        templateId: TEMPLATE_B,
        instanceId: INSTANCE_B,
        actionType: "crm.upsert-contact",
        capabilityId: "cap.crm.upsert-contact",
        connectorFamily: "crm",
        bindingId: "mock.crm.default",
        destinationSummary: SUMMARY_B.destination_summary,
        payloadSchemaId: "schema.crm.upsert-contact.v1",
      }),
    ],
    terminal_error: null,
  };
}

const page = normalizeApprovalPage({
  items: [SUMMARY_A, SUMMARY_B],
  next_cursor: null,
});
assert.equal(page.items.length, 2);
assert.equal(page.items[0]?.id, APPROVAL_A);
assert.equal(page.items[1]?.id, APPROVAL_B);
assert.equal(page.items[0]?.status, "pending");

const normalizedDetail = normalizeApprovalDetail(DETAIL_A);
assert.equal(normalizedDetail.id, APPROVAL_A);
assert.equal(normalizedDetail.actionType, "newsletter.subscribe");
assert.equal(normalizedDetail.payloadHash, HASH_A);
assert.deepEqual(
  { ...normalizedDetail.redactedPayload },
  {
    contact_id: "witness-contact-001",
    email: "[REDACTED]",
  },
);

assert.throws(
  () =>
    normalizeApprovalDetail({
      ...DETAIL_A,
      payload_hash: "not-a-digest",
    }),
  ApprovalContractError,
);
assert.throws(
  () =>
    normalizeApprovalPage({
      items: [{ ...SUMMARY_A, payload_hash: HASH_A }],
      next_cursor: null,
    }),
  ApprovalContractError,
);

const runSafety = normalizeApprovalRunSafety(runFixture(), RUN_ID);
assert.equal(runSafety.mode, "mock_execution");
assert.equal(runSafety.state, "awaiting_approval");
assert.equal(runSafety.pendingApprovals.length, 2);
assert.equal(runSafety.externalActions.length, 2);
assert.equal(runSafety.zeroMockConnectorCallsConfirmed, true);

const attemptedRun = structuredClone(runFixture());
attemptedRun.external_actions[0].delivery_attempt_count = 1;
assert.equal(
  normalizeApprovalRunSafety(attemptedRun, RUN_ID)
    .zeroMockConnectorCallsConfirmed,
  false,
);

const requests = [];
const originalFetch = globalThis.fetch;
globalThis.fetch = async (path, request) => {
  requests.push({ path, request });
  if (requests.length === 1) {
    return new Response(
      JSON.stringify({
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
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }
  if (requests.length === 2) {
    return new Response(
      JSON.stringify({
        approval_id: APPROVAL_A,
        decision_id: "approval-decision.web-05.witness",
        action_id: ACTION_A,
        run_id: RUN_ID,
        status: "approved",
      }),
      {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          Location: `/api/v1/approvals/${APPROVAL_A}`,
        },
      },
    );
  }
  throw new Error("unexpected WEB-05 witness request");
};

clearLocalSession();
try {
  const decision = await decideApproval({
    approvalId: APPROVAL_A,
    decision: "approve",
    expectedGeneration: 1,
    expectedPayloadHash: HASH_A,
    expectedActionId: ACTION_A,
    expectedRunId: RUN_ID,
  });
  assert.equal(decision.approvalId, APPROVAL_A);
  assert.equal(decision.status, "approved");
  assert.equal(decision.approval, null);
} finally {
  globalThis.fetch = originalFetch;
  clearLocalSession();
}

assert.equal(requests.length, 2);
assert.equal(requests[0]?.path, "/api/v1/session");
assert.equal(requests[1]?.path, `/api/v1/approvals/${APPROVAL_A}/approve`);
const mutation = requests[1]?.request;
assert.ok(mutation !== undefined);
const headers = new Headers(mutation.headers);
assert.equal(headers.get("Accept"), "application/json");
assert.equal(headers.get("Content-Type"), "application/json");
assert.equal(headers.get("X-CSRF-Token"), CSRF_TOKEN);
assert.deepEqual(JSON.parse(mutation.body), {
  expected_generation: 1,
  expected_payload_hash: HASH_A,
});
assert.equal(mutation.body.includes("reason"), false);

process.stdout.write("WEB-05 approval witness passed.\n");

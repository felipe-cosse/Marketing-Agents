import type {
  ApprovalDetail,
  ApprovalStatus,
  ApprovalSummary,
} from "../../api/approvals";
import type { ApprovalRunSafety } from "../../api/approvalRunSafety";
import type { NormalizedHierarchy } from "../org-chart/model";

export const APPROVAL_ONE_ID = "approval.web05.email.newsletter";
export const APPROVAL_TWO_ID = "approval.web05.email.crm";
export const RUN_ID = "run.web05.email";

export function makeApprovalSummary(
  options: {
    readonly id?: string;
    readonly status?: ApprovalStatus;
    readonly actionId?: string;
    readonly actionType?: string;
    readonly instanceId?: string;
    readonly requestedAt?: string;
    readonly expiresAt?: string;
    readonly generation?: number;
  } = {},
): ApprovalSummary {
  const id = options.id ?? APPROVAL_ONE_ID;
  const status = options.status ?? "pending";
  const actionId = options.actionId ?? "action.web05.email.newsletter";
  const instanceId =
    options.instanceId ?? "inst.email.newsletter.newsletter-subscriber.01";
  return Object.freeze({
    id,
    status,
    resourceVersion: options.generation ?? 1,
    generation: options.generation ?? 1,
    actionId,
    actionType: options.actionType ?? "newsletter.subscribe",
    destinationSummary: "Mock newsletter · Demo subscribers",
    runId: RUN_ID,
    templateId: "tpl.email.newsletter.newsletter-subscriber",
    instanceId,
    requestedAt: options.requestedAt ?? "2026-08-31T21:02:00Z",
    expiresAt: options.expiresAt ?? "2099-08-31T22:00:00Z",
    isExpired: status === "expired",
    isActionable: status === "pending",
    approvalUrl: `/api/v1/approvals/${id}`,
    actionUrl: `/api/v1/external-actions/${actionId}`,
    runUrl: `/api/v1/runs/${RUN_ID}`,
  });
}

export function makeSecondApprovalSummary(): ApprovalSummary {
  return makeApprovalSummary({
    id: APPROVAL_TWO_ID,
    actionId: "action.web05.email.crm",
    actionType: "crm.upsert-contact",
    instanceId: "inst.email.lifecycle-marketing.customer-onboarder.01",
    requestedAt: "2026-08-31T21:01:00Z",
  });
}

export function makeApprovalDetail(
  options: {
    readonly status?: ApprovalStatus;
    readonly generation?: number;
    readonly payloadHash?: string;
    readonly oneTimeUseState?: "unused" | "consumed";
    readonly isActionable?: boolean;
    readonly isExpired?: boolean;
    readonly expiresAt?: string;
  } = {},
): ApprovalDetail {
  const status = options.status ?? "pending";
  const summary = makeApprovalSummary({
    status,
    ...(options.expiresAt === undefined
      ? {}
      : { expiresAt: options.expiresAt }),
    ...(options.generation === undefined
      ? {}
      : { generation: options.generation }),
  });
  const decided = status === "approved" || status === "rejected";
  const consumed =
    status === "consumed" || options.oneTimeUseState === "consumed";
  return Object.freeze({
    ...summary,
    isActionable: options.isActionable ?? status === "pending",
    isExpired: options.isExpired ?? status === "expired",
    oneTimeUseState: consumed ? "consumed" : "unused",
    capabilityId: "cap.newsletter.subscribe",
    connectorFamily: "newsletter",
    bindingId: "mock.newsletter.default",
    redactedPayload: Object.freeze({
      email: "[REDACTED]",
      unsafe_text: '<img src=x onerror="steal()">',
    }),
    payloadHash: options.payloadHash ?? "a".repeat(64),
    stepId: "step.web05.newsletter",
    policyId: "policy.external-write.default",
    requiredRoles: Object.freeze(["approver"]),
    requiredScopes: Object.freeze(["scope.external-write"]),
    allowSelfApproval: false,
    requestedBy: "principal.local.operator",
    updatedAt: "2026-08-31T21:03:00Z",
    decisionId: decided ? "decision.web05.01" : null,
    decisionKind:
      status === "approved"
        ? "approve"
        : status === "rejected"
          ? "reject"
          : null,
    decisionActorId: decided ? "principal.local.approver" : null,
    decisionReasonCode:
      status === "approved"
        ? "approval_granted"
        : status === "rejected"
          ? "approval_rejected"
          : null,
    decisionReason: null,
    decidedAt: decided ? "2026-08-31T21:04:00Z" : null,
    expiredAt: status === "expired" ? "2026-08-31T22:00:00Z" : null,
    replacementApprovalId:
      status === "superseded" ? "approval.web05.replacement" : null,
    renewedAt: null,
    supersededAt: status === "superseded" ? "2026-08-31T21:05:00Z" : null,
    supersededReasonCode:
      status === "superseded" ? "approval_set_superseded" : null,
    consumedAt: consumed ? "2026-08-31T21:06:00Z" : null,
    stepUrl: `/api/v1/runs/${RUN_ID}/steps/step.web05.newsletter`,
    templateUrl:
      "/api/v1/agent-templates/tpl.email.newsletter.newsletter-subscriber",
    instanceUrl:
      "/api/v1/agent-instances/inst.email.newsletter.newsletter-subscriber.01",
  });
}

export function makeApprovalRunSafety(confirmed = true): ApprovalRunSafety {
  const first = makeApprovalSummary();
  const second = makeSecondApprovalSummary();
  return Object.freeze({
    runId: RUN_ID,
    mode: "mock_execution",
    state: "awaiting_approval",
    approvalRequired: true,
    pendingApprovals: Object.freeze(
      [first, second].map((approval, index) =>
        Object.freeze({
          id: approval.id,
          actionId: approval.actionId,
          stepId: `step.web05.${index === 0 ? "newsletter" : "crm"}`,
          destinationSummary: approval.destinationSummary,
          requestedAt: approval.requestedAt,
          expiresAt: approval.expiresAt,
          isExpired: false,
          approvalUrl: approval.approvalUrl,
          actionUrl: approval.actionUrl,
          stepUrl: `/api/v1/runs/${RUN_ID}/steps/step.web05.${index === 0 ? "newsletter" : "crm"}`,
        }),
      ),
    ),
    externalActions: Object.freeze(
      [first, second].map((approval, index) =>
        Object.freeze({
          id: approval.actionId,
          stepId: `step.web05.${index === 0 ? "newsletter" : "crm"}`,
          actionType: approval.actionType,
          state: "awaiting_approval" as const,
          deliveryAttemptCount: confirmed ? 0 : 1,
          receiptId: null,
          resultStatus: null,
          completedAt: null,
          actionUrl: approval.actionUrl,
          stepUrl: `/api/v1/runs/${RUN_ID}/steps/step.web05.${index === 0 ? "newsletter" : "crm"}`,
        }),
      ),
    ),
    zeroMockConnectorCallsConfirmed: confirmed,
    runUrl: `/api/v1/runs/${RUN_ID}`,
    timelineUrl: `/api/v1/runs/${RUN_ID}/timeline`,
  });
}

export function makePartiallyApprovedRunSafety(): ApprovalRunSafety {
  const safety = makeApprovalRunSafety();
  const firstAction = safety.externalActions[0];
  const secondAction = safety.externalActions[1];
  const pendingApproval = safety.pendingApprovals[1];
  if (
    firstAction === undefined ||
    secondAction === undefined ||
    pendingApproval === undefined
  ) {
    throw new Error("WEB-05 partial approval fixture is incomplete");
  }
  return Object.freeze({
    ...safety,
    pendingApprovals: Object.freeze([pendingApproval]),
    externalActions: Object.freeze([
      Object.freeze({ ...firstAction, state: "approved" as const }),
      secondAction,
    ]),
  });
}

export const APPROVAL_HIERARCHY: NormalizedHierarchy = Object.freeze({
  catalogVersion: "1.0.0",
  catalogHash: `catalog-sha256-v1:${"f".repeat(64)}`,
  counts: Object.freeze({
    departments: 5,
    functions: 12,
    templates: 36,
    instances: 43,
  }),
  departments: Object.freeze([
    Object.freeze({
      id: "dept.email",
      displayName: "Email",
      displayOrder: 30,
      instanceCount: 5,
      templateCount: 5,
      functions: Object.freeze([
        Object.freeze({
          id: "func.email.newsletter",
          displayName: "Newsletter",
          displayOrder: 10,
          instances: Object.freeze([
            Object.freeze({
              id: "inst.email.newsletter.newsletter-subscriber.01",
              templateId: "tpl.email.newsletter.newsletter-subscriber",
              displayName: "Newsletter subscriber",
              purpose: "Tests approval review.",
              displayOrder: 10,
              enabled: true,
              operationClassification: "mutating",
              triggerTypes: Object.freeze(["manual"] as const),
              capabilitySummaries: Object.freeze([]),
              sourceOrdinal: 1,
              deploymentCount: 1,
            }),
          ]),
        }),
        Object.freeze({
          id: "func.email.lifecycle-marketing",
          displayName: "Lifecycle marketing",
          displayOrder: 20,
          instances: Object.freeze([
            Object.freeze({
              id: "inst.email.lifecycle-marketing.customer-onboarder.01",
              templateId: "tpl.email.lifecycle-marketing.customer-onboarder",
              displayName: "Customer onboarder",
              purpose: "Tests the two-action Email barrier.",
              displayOrder: 10,
              enabled: true,
              operationClassification: "mutating",
              triggerTypes: Object.freeze(["manual"] as const),
              capabilitySummaries: Object.freeze([]),
              sourceOrdinal: 1,
              deploymentCount: 1,
            }),
          ]),
        }),
      ]),
    }),
  ]),
  structuralKey: "approval-test-hierarchy",
});

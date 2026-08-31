import type { ApprovalPage, ApprovalSummary } from "../../api/approvals";
import type { ApprovalRunSafety } from "../../api/approvalRunSafety";
import type { EmailRunSafetyState } from "./ApprovalReviewPanel";
import { sortApprovalsPendingFirst } from "./approvalView";

const EMAIL_ACTION_TYPES = Object.freeze([
  "newsletter.subscribe",
  "crm.upsert-contact",
] as const);
const EMAIL_ACTION_TYPE_SET = new Set<string>(EMAIL_ACTION_TYPES);

function uniqueApprovalItems(
  items: readonly ApprovalSummary[],
): readonly ApprovalSummary[] {
  const byId = new Map<string, ApprovalSummary>();
  for (const item of items) byId.set(item.id, item);
  return [...byId.values()];
}

export function verifiedEmailRunState(
  safety: ApprovalRunSafety | undefined,
  approvalPage: ApprovalPage | undefined,
  isPending: boolean,
  nowMs: number,
): EmailRunSafetyState {
  if (isPending) return Object.freeze({ status: "loading" });
  if (
    !safety?.zeroMockConnectorCallsConfirmed ||
    safety.approvalRequired !== true ||
    safety.state !== "awaiting_approval" ||
    approvalPage?.nextCursor !== null
  ) {
    return Object.freeze({ status: "unavailable" });
  }

  const runItems = uniqueApprovalItems(
    approvalPage.items.filter((item) => item.runId === safety.runId),
  );
  const actionsById = new Map(
    safety.externalActions.map((action) => [action.id, action]),
  );
  const pendingByApprovalId = new Map(
    safety.pendingApprovals.map((approval) => [approval.id, approval]),
  );
  const actionTypes = new Set(runItems.map((item) => item.actionType));
  const pendingItems = runItems.filter((item) => item.status === "pending");
  const itemMatchesAuthoritativeRun = (item: ApprovalSummary): boolean => {
    const action = actionsById.get(item.actionId);
    if (
      action?.actionType !== item.actionType ||
      Date.parse(item.expiresAt) <= nowMs ||
      actionTypes.size !== EMAIL_ACTION_TYPE_SET.size ||
      !EMAIL_ACTION_TYPE_SET.has(item.actionType)
    ) {
      return false;
    }
    const pending = pendingByApprovalId.get(item.id);
    if (item.status === "pending") {
      return (
        item.isActionable &&
        !item.isExpired &&
        pending !== undefined &&
        !pending.isExpired &&
        Date.parse(pending.expiresAt) > nowMs &&
        pending.requestedAt === item.requestedAt &&
        pending.expiresAt === item.expiresAt &&
        pending.actionId === item.actionId &&
        pending.stepId === action.stepId &&
        action.state === "awaiting_approval"
      );
    }
    return (
      item.status === "approved" &&
      !item.isActionable &&
      !item.isExpired &&
      pending === undefined &&
      action.state === "approved"
    );
  };
  const pendingApprovalIds = new Set(
    safety.pendingApprovals.map((approval) => approval.id),
  );
  const completeTwoActionSet =
    safety.externalActions.length === 2 &&
    actionsById.size === 2 &&
    safety.externalActions.every(
      (action) =>
        action.deliveryAttemptCount === 0 &&
        action.receiptId === null &&
        action.resultStatus === null &&
        action.completedAt === null,
    ) &&
    runItems.length === 2 &&
    actionTypes.size === 2 &&
    EMAIL_ACTION_TYPES.every((actionType) => actionTypes.has(actionType)) &&
    pendingItems.length >= 1 &&
    pendingItems.length === safety.pendingApprovals.length &&
    pendingApprovalIds.size === safety.pendingApprovals.length &&
    safety.pendingApprovals.every((pending) =>
      runItems.some(
        (item) => item.id === pending.id && item.actionId === pending.actionId,
      ),
    ) &&
    runItems.every(itemMatchesAuthoritativeRun);

  return completeTwoActionSet
    ? Object.freeze({
        status: "confirmed-zero",
        approvals: sortApprovalsPendingFirst(runItems),
      })
    : Object.freeze({ status: "unavailable" });
}

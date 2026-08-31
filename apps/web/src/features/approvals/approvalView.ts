import type {
  ApprovalDetail,
  ApprovalStatus,
  ApprovalSummary,
} from "../../api/approvals";
import type { NormalizedHierarchy } from "../org-chart/model";

export const APPROVAL_STATUS_LABELS: Readonly<Record<ApprovalStatus, string>> =
  Object.freeze({
    pending: "Pending",
    approved: "Approved",
    rejected: "Rejected",
    expired: "Expired",
    consumed: "Consumed",
    superseded: "Superseded",
  });

const STATUS_ORDER: Readonly<Record<ApprovalStatus, number>> = Object.freeze({
  pending: 0,
  approved: 1,
  rejected: 2,
  expired: 3,
  consumed: 4,
  superseded: 5,
});

export interface ApprovalDepartment {
  readonly id: string;
  readonly filterValue: string;
  readonly displayName: string;
}

export function approvalDepartmentLookup(
  hierarchy: NormalizedHierarchy | undefined,
): ReadonlyMap<string, ApprovalDepartment> {
  const result = new Map<string, ApprovalDepartment>();
  for (const department of hierarchy?.departments ?? []) {
    const filterValue = department.id.startsWith("dept.")
      ? department.id.slice("dept.".length)
      : department.id;
    const value = Object.freeze({
      id: department.id,
      filterValue,
      displayName: department.displayName,
    });
    for (const instance of department.functions.flatMap(
      (agentFunction) => agentFunction.instances,
    )) {
      result.set(instance.id, value);
    }
  }
  return result;
}

export function sortApprovalsPendingFirst(
  items: readonly ApprovalSummary[],
): readonly ApprovalSummary[] {
  return [...items].sort((left, right) => {
    const statusOrder = STATUS_ORDER[left.status] - STATUS_ORDER[right.status];
    if (statusOrder !== 0) return statusOrder;
    const requestedOrder = right.requestedAt.localeCompare(left.requestedAt);
    return requestedOrder === 0
      ? left.id.localeCompare(right.id)
      : requestedOrder;
  });
}

export function approvalDecisionDisabledReason(
  approval: ApprovalDetail,
  nowMs: number,
): string | null {
  if (approval.isExpired || approval.status === "expired") {
    return "This approval has expired and can no longer be decided.";
  }
  if (
    approval.status === "consumed" ||
    approval.oneTimeUseState === "consumed"
  ) {
    return "This one-time approval has already been consumed.";
  }
  if (approval.status === "superseded") {
    return "This approval was superseded by a replacement request.";
  }
  if (approval.status === "approved") {
    return "This approval decision was already recorded as approved.";
  }
  if (approval.status === "rejected") {
    return "This approval decision was already recorded as rejected.";
  }
  if (Date.parse(approval.expiresAt) <= nowMs) {
    return "This approval has expired and can no longer be decided.";
  }
  if (!approval.isActionable) {
    return "The server reports that this approval is not actionable.";
  }
  return null;
}

export function formatApprovalTimestamp(value: string): string {
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return value;
  return timestamp.toISOString().replace(".000Z", "Z");
}

export function formatRedactedPayload(
  payload: ApprovalDetail["redactedPayload"],
): string {
  return JSON.stringify(payload, null, 2);
}

export function humanizeApprovalValue(value: string): string {
  return value
    .replaceAll(/[._-]+/gu, " ")
    .replaceAll(/\b\p{L}/gu, (character) => character.toLocaleUpperCase());
}

export function isEmailApproval(
  approval: Pick<ApprovalSummary, "instanceId">,
  departments: ReadonlyMap<string, ApprovalDepartment>,
): boolean {
  return departments.get(approval.instanceId)?.id === "dept.email";
}

export function pendingCountLabel(count: number, truncated: boolean): string {
  if (truncated) return `${String(count)} or more pending approvals`;
  return `${String(count)} pending ${count === 1 ? "approval" : "approvals"}`;
}

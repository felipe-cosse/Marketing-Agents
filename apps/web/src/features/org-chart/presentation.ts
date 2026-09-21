import type { InstanceRuntimeState } from "../../api/instanceStatusSummary";

const SOURCE_CHART_VENDOR_SUFFIX =
  /\s*;\s*the source chart names [^.;\r\n]+\.?\s*$/iu;

export function presentPurpose(purpose: string): string {
  const vendorNeutral = purpose.replace(SOURCE_CHART_VENDOR_SUFFIX, "").trim();
  if (vendorNeutral === purpose.trim() || vendorNeutral.length === 0) {
    return purpose;
  }
  return vendorNeutral.endsWith(".") ? vendorNeutral : `${vendorNeutral}.`;
}

const RUNTIME_STATUS_LABELS: Readonly<Record<InstanceRuntimeState, string>> = {
  never_run: "Never run",
  received: "Received",
  validated: "Validated",
  planned: "Planned",
  awaiting_approval: "Awaiting approval",
  executing: "Executing",
  completed: "Completed",
  failed: "Failed",
  rejected: "Rejected",
  cancelled: "Cancelled",
};

export function presentRuntimeStatus(
  state: InstanceRuntimeState | undefined,
): string {
  return state === undefined ? "Unavailable" : RUNTIME_STATUS_LABELS[state];
}

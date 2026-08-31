import type {
  ApprovalDecisionKind,
  ApprovalDetail,
  ApprovalSummary,
} from "../../api/approvals";
import { ApprovalActionDetails } from "./ApprovalActionDetails";
import {
  APPROVAL_STATUS_LABELS,
  approvalDecisionDisabledReason,
  type ApprovalDepartment,
  humanizeApprovalValue,
} from "./approvalView";
import { useExpiryClock } from "./useExpiryClock";

export type EmailRunSafetyState =
  | Readonly<{ status: "loading" }>
  | Readonly<{
      status: "confirmed-zero";
      approvals: readonly ApprovalSummary[];
    }>
  | Readonly<{ status: "unavailable" }>;

export interface ApprovalReviewPanelProps {
  readonly approval: ApprovalDetail;
  readonly department: ApprovalDepartment | undefined;
  readonly emailRunSafety: EmailRunSafetyState | null;
  readonly onClose: () => void;
  readonly onRequestDecision: (decision: ApprovalDecisionKind) => void;
}

export function ApprovalEmailRunSafety({
  state,
}: {
  readonly state: EmailRunSafetyState;
}): React.JSX.Element {
  if (state.status === "loading") {
    return (
      <section className="approval-email-safety" aria-live="polite">
        <h3>Email run safety</h3>
        <p>Loading authoritative run safety and approval-set evidence…</p>
      </section>
    );
  }
  if (state.status === "unavailable") {
    return (
      <section className="approval-email-safety is-unavailable" role="status">
        <h3>Email run safety unavailable</h3>
        <p>
          The queue cannot confirm connector-call state or a two-action barrier,
          so it does not infer either from partial approval data.
        </p>
      </section>
    );
  }
  return (
    <section className="approval-email-safety is-confirmed" role="status">
      <h3>Email run safety</h3>
      <strong>0 mock connector calls until both approvals are approved.</strong>
      <p>
        This statement comes from the authoritative safe run summary and the
        complete two-action approval set loaded for this run.
      </p>
      <ol aria-label="Email run approval actions">
        {state.approvals.map((approval) => (
          <li key={approval.id} data-approval-id={approval.id}>
            <span>{humanizeApprovalValue(approval.actionType)}</span>
            <code>{approval.actionType}</code>
            <span>{APPROVAL_STATUS_LABELS[approval.status]}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function ApprovalReviewPanel({
  approval,
  department,
  emailRunSafety,
  onClose,
  onRequestDecision,
}: ApprovalReviewPanelProps): React.JSX.Element {
  const nowMs = useExpiryClock([approval.expiresAt]);
  const disabledReason = approvalDecisionDisabledReason(approval, nowMs);
  const reasonId = `approval-decision-disabled-${approval.id}`;
  const requestDecision = (decision: ApprovalDecisionKind): void => {
    if (approvalDecisionDisabledReason(approval, Date.now()) !== null) return;
    onRequestDecision(decision);
  };

  return (
    <aside
      id="approval-review-panel"
      className="approval-review-panel"
      aria-labelledby={`approval-review-title-${approval.id}`}
      tabIndex={-1}
    >
      <header className="approval-review-panel__header">
        <div>
          <p>{department?.displayName ?? "Department unavailable"}</p>
          <h2 id={`approval-review-title-${approval.id}`}>
            Exact action review
          </h2>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close approval review"
        >
          Close
        </button>
      </header>

      <p className="approval-review-panel__boundary">
        Recording an approval is distinct from connector execution. This panel
        never reports an action as completed without authoritative run evidence.
      </p>

      <ApprovalActionDetails approval={approval} />

      {emailRunSafety === null ? null : (
        <ApprovalEmailRunSafety state={emailRunSafety} />
      )}

      {disabledReason === null ? null : (
        <p id={reasonId} className="approval-review-panel__disabled-reason">
          {disabledReason}
        </p>
      )}

      <div className="approval-review-panel__actions">
        <button
          type="button"
          className="is-danger"
          disabled={disabledReason !== null}
          aria-describedby={disabledReason === null ? undefined : reasonId}
          onClick={() => requestDecision("reject")}
        >
          Reject
        </button>
        <button
          type="button"
          className="is-primary"
          disabled={disabledReason !== null}
          aria-describedby={disabledReason === null ? undefined : reasonId}
          onClick={() => requestDecision("approve")}
        >
          Approve
        </button>
      </div>
    </aside>
  );
}

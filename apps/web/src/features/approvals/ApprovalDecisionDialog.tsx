import { useEffect, useRef } from "react";

import type { ApprovalDecisionKind, ApprovalDetail } from "../../api/approvals";
import { ApprovalActionDetails } from "./ApprovalActionDetails";
import { approvalDecisionDisabledReason } from "./approvalView";
import { useExpiryClock } from "./useExpiryClock";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not(:disabled)",
  "input:not(:disabled)",
  "select:not(:disabled)",
  "textarea:not(:disabled)",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

export interface ApprovalDecisionDialogProps {
  readonly approval: ApprovalDetail;
  readonly decision: ApprovalDecisionKind;
  readonly pending: boolean;
  readonly fallbackFocusId?: string;
  readonly onCancel: () => void;
  readonly onConfirm: () => void;
}

export function ApprovalDecisionDialog({
  approval,
  decision,
  pending,
  fallbackFocusId,
  onCancel,
  onConfirm,
}: ApprovalDecisionDialogProps): React.JSX.Element {
  const cancelButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const onCancelRef = useRef(onCancel);
  const pendingRef = useRef(pending);
  const nowMs = useExpiryClock([approval.expiresAt]);
  const disabledReason = approvalDecisionDisabledReason(approval, nowMs);

  useEffect(() => {
    onCancelRef.current = onCancel;
    pendingRef.current = pending;
  }, [onCancel, pending]);

  useEffect(() => {
    const previouslyFocused =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    cancelButtonRef.current?.focus();

    const handleKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        if (!pendingRef.current) {
          event.preventDefault();
          onCancelRef.current();
        }
        return;
      }
      if (event.key !== "Tab") return;

      const dialog = dialogRef.current;
      if (dialog === null) return;
      const focusable = [
        ...dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ];
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable.at(-1);
      const active = document.activeElement;
      if (
        event.shiftKey &&
        (active === first ||
          !(active instanceof Node) ||
          !dialog.contains(active))
      ) {
        event.preventDefault();
        last?.focus();
      } else if (
        !event.shiftKey &&
        (active === last ||
          !(active instanceof Node) ||
          !dialog.contains(active))
      ) {
        event.preventDefault();
        first?.focus();
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      const previousIsDisabled =
        previouslyFocused instanceof HTMLButtonElement ||
        previouslyFocused instanceof HTMLInputElement ||
        previouslyFocused instanceof HTMLSelectElement ||
        previouslyFocused instanceof HTMLTextAreaElement
          ? previouslyFocused.disabled
          : previouslyFocused?.getAttribute("aria-disabled") === "true";
      if (previouslyFocused?.isConnected && !previousIsDisabled) {
        previouslyFocused.focus();
      } else if (fallbackFocusId !== undefined) {
        document.getElementById(fallbackFocusId)?.focus();
      }
    };
  }, [fallbackFocusId]);

  const approving = decision === "approve";
  const verb = approving ? "Approve" : "Reject";
  const titleId = `approval-decision-title-${approval.id}`;
  const descriptionId = `approval-decision-description-${approval.id}`;
  const disabledReasonId = `approval-decision-disabled-${approval.id}`;

  return (
    <div className="approval-dialog-backdrop">
      <section
        ref={dialogRef}
        className="approval-decision-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        tabIndex={-1}
      >
        <header className="approval-decision-dialog__header">
          <div>
            <h2 id={titleId}>{verb} exact action?</h2>
            <p id={descriptionId}>
              This records a decision for exactly the immutable action below. It
              does not execute the connector action or prove delivery.
            </p>
          </div>
          <span className="approval-decision-dialog__decision">
            {approving ? "Approval" : "Rejection"}
          </span>
        </header>

        <div className="approval-decision-dialog__body">
          <ApprovalActionDetails
            approval={approval}
            compact
            idPrefix="approval-decision"
          />
        </div>

        {pending ? (
          <p className="approval-decision-dialog__pending" role="status">
            Recording the decision with the server. Keep this dialog open until
            the authoritative response returns.
          </p>
        ) : disabledReason === null ? null : (
          <p
            id={disabledReasonId}
            className="approval-decision-dialog__pending"
            role="status"
          >
            {disabledReason} Close this dialog and refresh the authoritative
            request before deciding.
          </p>
        )}

        <div className="approval-decision-dialog__actions">
          <button
            ref={cancelButtonRef}
            type="button"
            disabled={pending}
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            type="button"
            className={approving ? "is-primary" : "is-danger"}
            disabled={pending || disabledReason !== null}
            aria-describedby={
              disabledReason === null ? undefined : disabledReasonId
            }
            onClick={() => {
              if (
                approvalDecisionDisabledReason(approval, Date.now()) === null
              ) {
                onConfirm();
              }
            }}
          >
            {pending ? "Recording decision…" : `${verb} exact action`}
          </button>
        </div>
      </section>
    </div>
  );
}

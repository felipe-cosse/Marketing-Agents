import type { ApprovalSummary } from "../../api/approvals";
import {
  APPROVAL_STATUS_LABELS,
  type ApprovalDepartment,
  formatApprovalTimestamp,
  humanizeApprovalValue,
} from "./approvalView";

export interface ApprovalQueueProps {
  readonly items: readonly ApprovalSummary[];
  readonly departments: ReadonlyMap<string, ApprovalDepartment>;
  readonly selectedApprovalId: string | null;
  readonly onReview: (approvalId: string) => void;
}

export function ApprovalQueue({
  items,
  departments,
  selectedApprovalId,
  onReview,
}: ApprovalQueueProps): React.JSX.Element {
  if (items.length === 0) {
    return (
      <section className="approval-queue-empty" aria-live="polite">
        <strong>No loaded approvals match these filters.</strong>
        <p>
          Change a filter or load another page. This message describes only the
          approvals currently loaded in the browser.
        </p>
      </section>
    );
  }

  return (
    <ol className="approval-queue" aria-label="Loaded approval requests">
      {items.map((approval) => {
        const department = departments.get(approval.instanceId);
        const selected = approval.id === selectedApprovalId;
        return (
          <li key={approval.id}>
            <article
              className={`approval-row${selected ? " is-selected" : ""}`}
              data-approval-id={approval.id}
              aria-labelledby={`approval-row-title-${approval.id}`}
            >
              <div className="approval-row__state">
                <span className={`approval-status is-${approval.status}`}>
                  {APPROVAL_STATUS_LABELS[approval.status]}
                </span>
                <span>
                  {department?.displayName ?? "Department unavailable"}
                </span>
              </div>
              <div className="approval-row__body">
                <h2 id={`approval-row-title-${approval.id}`}>
                  <span>{humanizeApprovalValue(approval.actionType)}</span>
                  <code>{approval.actionType}</code>
                </h2>
                <p>{approval.destinationSummary}</p>
                <dl>
                  <div>
                    <dt>Instance</dt>
                    <dd>{approval.instanceId}</dd>
                  </div>
                  <div>
                    <dt>Requested</dt>
                    <dd>
                      <time dateTime={approval.requestedAt}>
                        {formatApprovalTimestamp(approval.requestedAt)}
                      </time>
                    </dd>
                  </div>
                  <div>
                    <dt>Expires</dt>
                    <dd>
                      <time dateTime={approval.expiresAt}>
                        {formatApprovalTimestamp(approval.expiresAt)}
                      </time>
                    </dd>
                  </div>
                </dl>
              </div>
              <button
                id={`approval-review-trigger-${approval.id}`}
                type="button"
                className="approval-row__review"
                aria-controls="approval-review-panel"
                aria-expanded={selected}
                onClick={() => onReview(approval.id)}
              >
                Review approval {approval.id}
              </button>
            </article>
          </li>
        );
      })}
    </ol>
  );
}

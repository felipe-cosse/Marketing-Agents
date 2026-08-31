import type { ApprovalDetail } from "../../api/approvals";
import {
  APPROVAL_STATUS_LABELS,
  formatApprovalTimestamp,
  formatRedactedPayload,
  humanizeApprovalValue,
} from "./approvalView";

interface ApprovalActionDetailsProps {
  readonly approval: ApprovalDetail;
  readonly compact?: boolean;
  readonly idPrefix?: string;
}

function Detail({
  label,
  children,
}: {
  readonly label: string;
  readonly children: React.ReactNode;
}): React.JSX.Element {
  return (
    <div className="approval-detail-field">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

export function ApprovalActionDetails({
  approval,
  compact = false,
  idPrefix = "approval-detail",
}: ApprovalActionDetailsProps): React.JSX.Element {
  const payloadHeadingId = `${idPrefix}-payload-${approval.id}`;
  return (
    <div
      className={`approval-action-details${compact ? " approval-action-details--compact" : ""}`}
    >
      <dl className="approval-action-details__summary">
        <Detail label="Action type">
          <strong>{humanizeApprovalValue(approval.actionType)}</strong>
          <code>{approval.actionType}</code>
        </Detail>
        <Detail label="Destination">{approval.destinationSummary}</Detail>
        <Detail label="Payload hash">
          <code className="approval-hash">{approval.payloadHash}</code>
        </Detail>
        <Detail label="Request status">
          {APPROVAL_STATUS_LABELS[approval.status]}
        </Detail>
        <Detail label="One-time state">
          {humanizeApprovalValue(approval.oneTimeUseState)}
        </Detail>
        <Detail label="Expires">
          <time dateTime={approval.expiresAt}>
            {formatApprovalTimestamp(approval.expiresAt)}
          </time>
        </Detail>
        <Detail label="Requesting instance">
          <a href={approval.instanceUrl}>{approval.instanceId}</a>
        </Detail>
        <Detail label="Run">
          <a href={`/runs/${encodeURIComponent(approval.runId)}`}>
            {approval.runId}
          </a>
        </Detail>
        <Detail label="Run timeline">
          <a
            href={`/runs/${encodeURIComponent(approval.runId)}#timeline-title`}
          >
            Open sequence-ordered timeline
          </a>
        </Detail>
        <Detail label="Step">
          <a
            href={`/runs/${encodeURIComponent(approval.runId)}#step-${encodeURIComponent(approval.stepId)}`}
          >
            {approval.stepId}
          </a>
        </Detail>
        {!compact ? (
          <>
            <Detail label="External action / receipt">
              <a
                href={`/runs/${encodeURIComponent(approval.runId)}#action-${encodeURIComponent(approval.actionId)}`}
              >
                {approval.actionId}
              </a>
            </Detail>
            <Detail label="Capability">
              <code>{approval.capabilityId}</code>
            </Detail>
            <Detail label="Connector binding">
              <code>{approval.bindingId}</code>
            </Detail>
            <Detail label="Approval ID">
              <code>{approval.id}</code>
            </Detail>
          </>
        ) : null}
      </dl>
      <section
        className="approval-action-details__payload"
        aria-labelledby={payloadHeadingId}
      >
        <h3 id={payloadHeadingId}>Redacted payload</h3>
        <p>
          This is the server-provided safe projection. Redacted values are not
          reconstructed in the browser.
        </p>
        <pre aria-label="Redacted payload JSON" tabIndex={0}>
          {formatRedactedPayload(approval.redactedPayload)}
        </pre>
      </section>
    </div>
  );
}

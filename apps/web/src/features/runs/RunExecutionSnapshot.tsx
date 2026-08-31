import { Link } from "react-router-dom";

import type { ExternalAction, RunResource } from "../../api/runArtifacts";
import { ArtifactPayloadView, MockReceiptNotice } from "../artifacts";
import { formatRuntimeTimestamp, humanizeRuntimeValue } from "./timelineModel";

export interface RunExecutionSnapshotProps {
  readonly run: RunResource;
}

function Fact({
  label,
  children,
}: {
  readonly label: string;
  readonly children: React.ReactNode;
}): React.JSX.Element {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function actionIdempotencySupport(
  run: RunResource,
  action: ExternalAction,
): string {
  const step = run.plan?.steps.find(
    (candidate) => candidate.id === action.stepId,
  );
  return step?.idempotencySupport ?? "unavailable";
}

function ExternalActions({ run }: { readonly run: RunResource }) {
  if (run.externalActions.length === 0) {
    return <p>No external action has been proposed for this run.</p>;
  }
  return (
    <ol className="run-snapshot__action-list">
      {run.externalActions.map((action) => {
        const idempotency = actionIdempotencySupport(run, action);
        return (
          <li id={`action-${action.id}`} key={action.id} tabIndex={-1}>
            <header>
              <div>
                <span aria-hidden="true">→</span>
                <strong>{humanizeRuntimeValue(action.actionType)}</strong>
              </div>
              <span className={`run-status is-${action.state}`}>
                {humanizeRuntimeValue(action.state)}
              </span>
            </header>
            <p>{action.destinationSummary}</p>
            <dl className="run-fact-grid is-compact">
              <Fact label="Action ID">
                <code>{action.id}</code>
              </Fact>
              <Fact label="Connector">
                {action.connectorFamily} · {action.bindingId}
              </Fact>
              <Fact label="Idempotency">
                {humanizeRuntimeValue(idempotency)} support; the protected key
                is not exposed by this read API
              </Fact>
              <Fact label="Dispatch attempts">
                {String(action.deliveryAttemptCount)} of{" "}
                {String(action.deliveryAttemptLimit)}
              </Fact>
              <Fact label="Receipt identity">
                {action.receiptId ?? "No receipt recorded"}
              </Fact>
              <Fact label="Result">
                {action.resultStatus === null
                  ? "No connector result recorded"
                  : humanizeRuntimeValue(action.resultStatus)}
              </Fact>
              <Fact label="Completed">
                {action.completedAt === null ? (
                  "Not completed"
                ) : (
                  <time dateTime={action.completedAt}>
                    {formatRuntimeTimestamp(action.completedAt)}
                  </time>
                )}
              </Fact>
            </dl>
            {run.mode === "mock_execution" ? <MockReceiptNotice /> : null}
            <details>
              <summary>Server-redacted proposed payload</summary>
              <ArtifactPayloadView
                label={`Redacted payload for ${action.id}`}
                presentation="json"
                value={action.redactedPayload}
              />
            </details>
          </li>
        );
      })}
    </ol>
  );
}

export function RunExecutionSnapshot({
  run,
}: RunExecutionSnapshotProps): React.JSX.Element {
  const plan = run.plan;
  return (
    <section
      className="run-snapshot"
      aria-labelledby="run-execution-snapshot-title"
    >
      <header>
        <div>
          <span className="run-section-kicker">Sealed runtime facts</span>
          <h2 id="run-execution-snapshot-title">Execution snapshot</h2>
        </div>
        <span className={`run-status is-${run.state}`}>
          {humanizeRuntimeValue(run.state)}
        </span>
      </header>

      <dl className="run-fact-grid">
        <Fact label="Workflow">{run.workflowId}</Fact>
        <Fact label="Run mode">{humanizeRuntimeValue(run.mode)}</Fact>
        <Fact label="Configuration revision">
          {String(run.configurationRevision)}
        </Fact>
        <Fact label="Catalog hash">
          <code>{run.catalogHash}</code>
        </Fact>
        <Fact label="Created">
          <time dateTime={run.createdAt}>
            {formatRuntimeTimestamp(run.createdAt)}
          </time>
        </Fact>
        <Fact label="Updated">
          <time dateTime={run.updatedAt}>
            {formatRuntimeTimestamp(run.updatedAt)}
          </time>
        </Fact>
      </dl>

      {plan === null ? (
        <p className="run-snapshot__notice">
          A sealed execution plan is not available at this run state.
        </p>
      ) : (
        <>
          <section className="run-snapshot__plan" aria-labelledby="plan-title">
            <h3 id="plan-title">Selected plan and policy</h3>
            <dl className="run-fact-grid is-compact">
              <Fact label="Plan hash">
                <code>{plan.planHash}</code>
              </Fact>
              <Fact label="Workflow version">
                {String(plan.workflowVersion)}
              </Fact>
              <Fact label="Graph hash">
                <code>{plan.graphHash}</code>
              </Fact>
              <Fact label="Routing hash">
                <code>{plan.routingHash}</code>
              </Fact>
              <Fact label="Approval policy">
                {plan.approvalRequired
                  ? "Approval required"
                  : "No approval required"}
              </Fact>
              <Fact label="Run limits">
                {String(plan.runtimePolicy.maxSteps)} steps ·{" "}
                {String(plan.runtimePolicy.maxModelCalls)} model calls ·{" "}
                {String(plan.runtimePolicy.maxToolCalls)} tool calls ·{" "}
                {String(plan.runtimePolicy.runTimeoutSeconds)}s
              </Fact>
            </dl>
          </section>

          <section aria-labelledby="selected-agents-title">
            <h3 id="selected-agents-title">Selected agents</h3>
            <ol className="run-snapshot__agents">
              {plan.selectedInstances.map((instance) => (
                <li
                  key={`${String(instance.selectionOrder)}-${instance.instanceId}`}
                >
                  <span aria-hidden="true">◆</span>
                  <div>
                    <strong>{instance.instanceId}</strong>
                    <span>
                      {instance.templateId} · configuration revision{" "}
                      {String(instance.configurationRevision)}
                    </span>
                  </div>
                  {instance.target ? <em>Target</em> : null}
                </li>
              ))}
            </ol>
          </section>

          <section aria-labelledby="run-steps-title">
            <h3 id="run-steps-title">Steps and attempt policy</h3>
            <ol className="run-snapshot__steps">
              {plan.steps.map((step) => (
                <li id={`step-${step.id}`} key={step.id} tabIndex={-1}>
                  <header>
                    <strong>
                      {String(step.ordinal)}. {humanizeRuntimeValue(step.key)}
                    </strong>
                    <span className={`run-status is-${step.state}`}>
                      {humanizeRuntimeValue(step.state)}
                    </span>
                  </header>
                  <p>
                    {humanizeRuntimeValue(step.kind)} ·{" "}
                    {humanizeRuntimeValue(step.effect)} ·{" "}
                    {step.selectedInstanceId}
                  </p>
                  <dl className="run-fact-grid is-compact">
                    <Fact label="Capability">{step.capabilityId}</Fact>
                    <Fact label="Provider attempt">
                      {humanizeRuntimeValue(step.runtimePolicy.attemptKind)}; up
                      to {String(step.runtimePolicy.maxAttempts)} attempts
                    </Fact>
                    <Fact label="Timeout">
                      {String(step.runtimePolicy.stepTimeoutSeconds)} seconds
                    </Fact>
                    <Fact label="Configuration snapshot">
                      revision {String(step.configurationRevision)}
                    </Fact>
                    <Fact label="Approval policy">{step.approvalPolicyId}</Fact>
                    <Fact label="Idempotency support">
                      {humanizeRuntimeValue(step.idempotencySupport)}
                    </Fact>
                  </dl>
                </li>
              ))}
            </ol>
          </section>
        </>
      )}

      <section aria-labelledby="run-approvals-title">
        <h3 id="run-approvals-title">Pending approvals</h3>
        {run.pendingApprovals.length === 0 ? (
          <p>No pending approval is reported by this run snapshot.</p>
        ) : (
          <ul className="run-snapshot__approvals">
            {run.pendingApprovals.map((approval) => (
              <li key={approval.id}>
                <span aria-hidden="true">⌁</span>
                <div>
                  <strong>{approval.destinationSummary}</strong>
                  <span>
                    {approval.isExpired ? "Expired" : "Expires"}{" "}
                    {formatRuntimeTimestamp(approval.expiresAt)}
                  </span>
                </div>
                {approval.isExpired ? (
                  <span className="run-status is-expired">
                    Expired — no decision available
                  </span>
                ) : (
                  <Link to="/approvals">Review approval</Link>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="external-actions-title">
        <h3 id="external-actions-title">External actions and receipts</h3>
        <ExternalActions run={run} />
      </section>

      {run.terminalError === null ? null : (
        <section className="run-snapshot__terminal-error" role="alert">
          <h3>Terminal failure record</h3>
          <p>
            <strong>{humanizeRuntimeValue(run.terminalError.code)}</strong>
            {run.terminalError.causeCode === null
              ? ""
              : ` · ${humanizeRuntimeValue(run.terminalError.causeCode)}`}
          </p>
          <p>
            Source: {humanizeRuntimeValue(run.terminalError.source)} ·
            retryable: no
          </p>
        </section>
      )}
    </section>
  );
}

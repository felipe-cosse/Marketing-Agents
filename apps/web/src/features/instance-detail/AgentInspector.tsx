import type { KeyboardEvent, MouseEvent, ReactNode } from "react";

import type { AgentInstanceDetail } from "../../api/agentInstanceDetail";
import type { AgentInstance } from "../org-chart/model";
import { presentPurpose } from "../org-chart/presentation";
import "./instance-detail.css";

const DATE_TIME_FORMATTER = new Intl.DateTimeFormat("en-US", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "UTC",
});

const DEFAULT_INSPECTOR_ID = "agent-inspector";

export interface AgentInspectorProps {
  readonly summary: AgentInstance;
  readonly departmentName: string;
  readonly functionName: string;
  readonly detail: AgentInstanceDetail | undefined;
  readonly isPending: boolean;
  readonly error: Error | null;
  readonly onRetry: () => void;
  readonly onClose: () => void;
  readonly onOpenRun: (runId: string) => void;
  readonly dryRunControls?: ReactNode;
  readonly configurationControls?: ReactNode;
  readonly id?: string;
}

function AgentGlyph(): React.JSX.Element {
  return (
    <svg aria-hidden="true" fill="none" focusable="false" viewBox="0 0 24 24">
      <circle cx="12" cy="7.5" r="3" stroke="currentColor" strokeWidth="1.7" />
      <path
        d="M6.5 18.5v-2.2a4 4 0 0 1 4-4h3a4 4 0 0 1 4 4v2.2M4.5 9.5h-1m17 0h-1M6 4 4.8 2.8M18 4l1.2-1.2"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.7"
      />
    </svg>
  );
}

function CloseIcon(): React.JSX.Element {
  return (
    <svg aria-hidden="true" fill="none" focusable="false" viewBox="0 0 24 24">
      <path
        d="m7.5 7.5 9 9m0-9-9 9"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.8"
      />
    </svg>
  );
}

function humanize(value: string): string {
  const words = value.replaceAll("_", " ");
  return `${words.slice(0, 1).toUpperCase()}${words.slice(1)}`;
}

function yesNo(value: boolean): string {
  return value ? "Yes" : "No";
}

function formatTimestamp(value: string): string {
  return `${DATE_TIME_FORMATTER.format(new Date(value))} UTC`;
}

function seconds(value: number): string {
  return `${String(value)} ${value === 1 ? "second" : "seconds"}`;
}

function safeJson(value: Readonly<Record<string, unknown>>): string {
  return JSON.stringify(value, null, 2);
}

function Metadata({
  children,
}: {
  readonly children: ReactNode;
}): React.JSX.Element {
  return <dl className="agent-inspector__metadata">{children}</dl>;
}

function Field({
  label,
  children,
}: {
  readonly label: string;
  readonly children: ReactNode;
}): React.JSX.Element {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

function Empty({
  children,
}: {
  readonly children: ReactNode;
}): React.JSX.Element {
  return <p className="agent-inspector__empty">{children}</p>;
}

function DetailContent({
  detail,
  departmentName,
  functionName,
  dryRunControls,
  configurationControls,
  onOpenRun,
}: {
  readonly detail: AgentInstanceDetail;
  readonly departmentName: string;
  readonly functionName: string;
  readonly dryRunControls: ReactNode | undefined;
  readonly configurationControls: ReactNode | undefined;
  readonly onOpenRun: (runId: string) => void;
}): React.JSX.Element {
  const { instance, template } = detail;
  const connectors = Object.values(instance.connectorBindings).toSorted(
    (left, right) => left.connectorFamily.localeCompare(right.connectorFamily),
  );

  return (
    <>
      <p className="agent-inspector__purpose">
        {presentPurpose(template.purpose)}
      </p>

      <section
        className="agent-inspector__section"
        aria-labelledby="agent-overview-title"
      >
        <h3 id="agent-overview-title">Overview</h3>
        <Metadata>
          <Field label="Department">{departmentName}</Field>
          <Field label="Function">{functionName}</Field>
          <Field label="Effect">
            {template.operationClassification === "read_only"
              ? "Read only"
              : "Mutating"}
          </Field>
          <Field label="Deployment">
            {instance.enabled ? "Enabled" : "Disabled"}
          </Field>
          <Field label="Output handling">
            {humanize(template.outputHandling)}
          </Field>
        </Metadata>
      </section>

      {dryRunControls}

      <section
        className="agent-inspector__section"
        aria-labelledby="agent-deployment-title"
      >
        <h3 id="agent-deployment-title">Deployment &amp; configuration</h3>
        <Metadata>
          <Field label="Instance ID">{instance.id}</Field>
          <Field label="Source ordinal">
            {String(instance.sourceOrdinal)} of{" "}
            {String(detail.sharedTemplateDeploymentCount)}
          </Field>
          <Field label="Variant">{instance.variantLabel ?? "None"}</Field>
          <Field label="Revision">
            {String(instance.configurationRevision)}
          </Field>
          <Field label="Configuration ETag">{instance.configurationEtag}</Field>
          <Field label="Configuration schema">
            {detail.configurationSchema}
          </Field>
        </Metadata>

        <h4>Triggers</h4>
        {instance.triggerBindings.length === 0 ? (
          <Empty>No trigger bindings are configured.</Empty>
        ) : (
          <ul className="agent-inspector__list">
            {instance.triggerBindings.map((trigger) => (
              <li
                key={`${trigger.type}-${trigger.eventSource ?? trigger.cron ?? "manual"}`}
              >
                <strong>
                  {humanize(trigger.type)} ·{" "}
                  {trigger.enabled ? "Enabled" : "Disabled"}
                </strong>
                <span>
                  {trigger.eventSource ??
                    (trigger.cron === null
                      ? "No additional parameters"
                      : `${trigger.cron} · ${trigger.timezone ?? "Timezone unavailable"}`)}
                </span>
                {trigger.misfirePolicy === null ? null : (
                  <span>
                    Misfire: {humanize(trigger.misfirePolicy)} · grace{" "}
                    {seconds(trigger.misfireGraceSeconds ?? 0)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}

        <h4>Connector bindings</h4>
        {connectors.length === 0 ? (
          <Empty>No connector bindings are configured.</Empty>
        ) : (
          <ul className="agent-inspector__list">
            {connectors.map((binding) => (
              <li key={`${binding.connectorFamily}-${binding.bindingId}`}>
                <strong>{binding.connectorFamily}</strong>
                <span>
                  {binding.bindingId} ·{" "}
                  {binding.enabled ? "Enabled" : "Disabled"}
                </span>
              </li>
            ))}
          </ul>
        )}

        <h4>Schedule</h4>
        {instance.schedule === null ? (
          <Empty>No schedule is configured.</Empty>
        ) : (
          <Metadata>
            <Field label="Cron">{instance.schedule.cron}</Field>
            <Field label="Timezone">{instance.schedule.timezone}</Field>
            <Field label="Misfire policy">
              {humanize(instance.schedule.misfirePolicy)}
            </Field>
            <Field label="Grace">
              {seconds(instance.schedule.misfireGraceSeconds)}
            </Field>
          </Metadata>
        )}

        {configurationControls === undefined ? null : (
          <div className="agent-inspector__configuration">
            {configurationControls}
          </div>
        )}
      </section>

      <section
        className="agent-inspector__section"
        aria-labelledby="agent-template-title"
      >
        <h3 id="agent-template-title">Template</h3>
        <Metadata>
          <Field label="Template ID">{template.id}</Field>
          <Field label="Deployments">
            {String(detail.sharedTemplateDeploymentCount)}
          </Field>
          <Field label="Source confidence">
            {humanize(template.sourceConfidence)}
          </Field>
          <Field label="Input schema">{template.inputSchemaId}</Field>
          <Field label="Output schema">{template.outputSchemaId}</Field>
        </Metadata>
        <h4>Implementation notes</h4>
        <p className="agent-inspector__empty">
          {detail.templateImplementationNotes}
        </p>
        <h4>Source references</h4>
        <ul className="agent-inspector__bullet-list">
          {detail.templateSourceReferences.map((reference) => (
            <li key={reference}>{reference}</li>
          ))}
        </ul>
      </section>

      <section
        className="agent-inspector__section"
        aria-labelledby="agent-capabilities-title"
      >
        <h3 id="agent-capabilities-title">Capabilities &amp; policies</h3>
        {detail.capabilities.length === 0 ? (
          <Empty>No capabilities are assigned.</Empty>
        ) : (
          <ul className="agent-inspector__list">
            {detail.capabilities.map((capability) => (
              <li key={capability.id}>
                <strong>{capability.displayName}</strong>
                <span>{capability.description}</span>
                <span>
                  {humanize(capability.effect)} · {capability.connectorFamily} ·{" "}
                  {humanize(capability.dataClassification)}
                </span>
                <span>
                  {humanize(capability.idempotencySupport)} · timeout{" "}
                  {seconds(capability.defaultTimeoutSeconds)}
                </span>
              </li>
            ))}
          </ul>
        )}

        <h4>Approval policy</h4>
        <Metadata>
          <Field label="Policy ID">{detail.approvalPolicy.id}</Field>
          <Field label="Policy">{humanize(detail.approvalPolicy.kind)}</Field>
          <Field label="Required roles">
            {detail.approvalPolicy.requiredRoles.length === 0
              ? "None"
              : detail.approvalPolicy.requiredRoles.join(", ")}
          </Field>
          <Field label="Expires after">
            {seconds(detail.approvalPolicy.expirySeconds)}
          </Field>
          <Field label="Self approval">
            {yesNo(detail.approvalPolicy.allowSelfApproval)}
          </Field>
        </Metadata>

        <h4>Execution policies</h4>
        <Metadata>
          <Field label="Retry attempts">
            {String(template.retryPolicy.maxAttempts)}
          </Field>
          <Field label="Retry backoff">
            {humanize(template.retryPolicy.backoff)}
          </Field>
          <Field label="Step timeout">
            {seconds(template.timeoutPolicy.stepSeconds)}
          </Field>
          <Field label="Run timeout">
            {seconds(template.timeoutPolicy.runSeconds)}
          </Field>
          <Field label="Rate limit">
            {String(template.rateLimitPolicy.maxCalls)} calls /{" "}
            {seconds(template.rateLimitPolicy.windowSeconds)}
          </Field>
          <Field label="Max steps">
            {String(template.budgetPolicy.maxSteps)}
          </Field>
          <Field label="Model calls">
            {String(template.budgetPolicy.maxModelCalls)}
          </Field>
          <Field label="Tool calls">
            {String(template.budgetPolicy.maxToolCalls)}
          </Field>
          <Field label="Input bytes">
            {String(template.budgetPolicy.maxInputBytes)}
          </Field>
          <Field label="Field bytes">
            {String(template.budgetPolicy.maxInputFieldBytes)}
          </Field>
          <Field label="Output bytes">
            {String(template.budgetPolicy.maxOutputBytes)}
          </Field>
          <Field label="Output tokens">
            {String(template.budgetPolicy.maxModelOutputTokens)}
          </Field>
        </Metadata>
      </section>

      <section
        className="agent-inspector__section"
        aria-labelledby="agent-schemas-title"
      >
        <h3 id="agent-schemas-title">Schemas</h3>
        <div className="agent-inspector__schemas">
          <details className="agent-inspector__schema">
            <summary>Input schema JSON</summary>
            <pre>{safeJson(detail.inputSchema)}</pre>
          </details>
          <details className="agent-inspector__schema">
            <summary>Output schema JSON</summary>
            <pre>{safeJson(detail.outputSchema)}</pre>
          </details>
        </div>
      </section>

      <section
        className="agent-inspector__section"
        aria-labelledby="agent-runs-title"
      >
        <h3 id="agent-runs-title">Recent runs</h3>
        {!detail.runtimeAvailable ? (
          <p className="agent-inspector__notice">
            Recent run data is unavailable for this local runtime.
          </p>
        ) : (
          <>
            <Metadata>
              <Field label="Current status">
                {humanize(detail.runtimeStatus.status)}
              </Field>
              <Field label="Latest run">
                {detail.runtimeStatus.latestRunId ?? "None"}
              </Field>
            </Metadata>
            <h4>Run history</h4>
            {detail.recentRuns.length === 0 ? (
              <Empty>No runs have been recorded for this agent.</Empty>
            ) : (
              <ul className="agent-inspector__list">
                {detail.recentRuns.map((run) => (
                  <li className="agent-inspector__run" key={run.id}>
                    <strong>{humanize(run.state)}</strong>
                    <time dateTime={run.updatedAt}>
                      {formatTimestamp(run.updatedAt)}
                    </time>
                    <span>
                      <a
                        href={`/runs/${encodeURIComponent(run.id)}`}
                        onClick={(event: MouseEvent<HTMLAnchorElement>) => {
                          if (
                            event.defaultPrevented ||
                            event.button !== 0 ||
                            event.metaKey ||
                            event.ctrlKey ||
                            event.shiftKey ||
                            event.altKey
                          ) {
                            return;
                          }
                          event.preventDefault();
                          onOpenRun(run.id);
                        }}
                      >
                        {run.id}
                      </a>{" "}
                      · {run.workflowId}
                    </span>
                    <span>
                      Created {formatTimestamp(run.createdAt)} · Updated{" "}
                      {formatTimestamp(run.updatedAt)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </section>
    </>
  );
}

export function AgentInspector({
  summary,
  departmentName,
  functionName,
  detail,
  isPending,
  error,
  onRetry,
  onClose,
  onOpenRun,
  dryRunControls,
  configurationControls,
  id = DEFAULT_INSPECTOR_ID,
}: AgentInspectorProps): React.JSX.Element {
  const deploymentCount =
    detail?.sharedTemplateDeploymentCount ?? summary.deploymentCount;
  const sourceOrdinal = detail?.instance.sourceOrdinal ?? summary.sourceOrdinal;
  const title =
    deploymentCount > 1
      ? `${summary.displayName} · Instance ${String(sourceOrdinal)} of ${String(deploymentCount)}`
      : summary.displayName;
  const enabled = detail?.instance.enabled ?? summary.enabled;

  const handleKeyDown = (event: KeyboardEvent<HTMLElement>): void => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    onClose();
  };

  return (
    <aside
      id={id}
      className="agent-inspector"
      aria-busy={detail === undefined && isPending}
      aria-labelledby={`${id}-title`}
      onKeyDown={handleKeyDown}
    >
      <header className="agent-inspector__header">
        <span
          className={`agent-inspector__icon ${
            summary.operationClassification === "mutating" ? "is-mutating" : ""
          }`}
        >
          <AgentGlyph />
        </span>
        <div className="agent-inspector__identity">
          <h2 id={`${id}-title`}>{title}</h2>
          <p>{summary.id}</p>
          <span
            className={`agent-inspector__state ${enabled ? "" : "is-disabled"}`}
          >
            {enabled ? "Enabled" : "Disabled"}
          </span>
        </div>
        <button
          type="button"
          className="agent-inspector__close"
          aria-label={`Close details for ${summary.displayName}`}
          onClick={onClose}
        >
          <CloseIcon />
        </button>
      </header>

      <div className="agent-inspector__body">
        {detail === undefined && isPending ? (
          <div className="agent-inspector__loading" role="status">
            <strong>Loading agent details</strong>
            <p>
              The selected agent remains available while local metadata loads.
            </p>
          </div>
        ) : null}

        {detail === undefined && error !== null ? (
          <div className="agent-inspector__error" role="alert">
            <strong>Agent details are unavailable</strong>
            <p>{error.message}</p>
            <button
              type="button"
              className="agent-inspector__retry"
              onClick={onRetry}
            >
              Try again
            </button>
          </div>
        ) : null}

        {detail !== undefined && error !== null ? (
          <div
            className="agent-inspector__error agent-inspector__error--inline"
            role="alert"
          >
            <strong>Details could not be refreshed</strong>
            <p>Showing the last available local detail response.</p>
            <button
              type="button"
              className="agent-inspector__retry"
              onClick={onRetry}
            >
              Try again
            </button>
          </div>
        ) : null}

        {detail !== undefined ? (
          <DetailContent
            detail={detail}
            departmentName={departmentName}
            functionName={functionName}
            dryRunControls={dryRunControls}
            configurationControls={configurationControls}
            onOpenRun={onOpenRun}
          />
        ) : null}

        {detail === undefined && !isPending && error === null ? (
          <div className="agent-inspector__error" role="status">
            <strong>Agent details are not available</strong>
            <p>Close the inspector and select the agent again.</p>
          </div>
        ) : null}
      </div>
    </aside>
  );
}

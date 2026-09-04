import type { KeyboardEvent } from "react";

import type { AgentInstance } from "./model";
import type { InstanceLayout } from "./layout";
import { ReadIcon, WriteIcon } from "./icons";
import { presentPurpose } from "./presentation";

interface AgentCardProps {
  readonly instance: AgentInstance;
  readonly departmentLabel: string;
  readonly functionLabel: string;
  readonly placement: InstanceLayout;
  readonly selected: boolean;
  readonly onSelect: (instanceId: string) => void;
  readonly tabIndex: 0 | -1;
  readonly onFocus: (instanceId: string) => void;
  readonly onNavigate: (instanceId: string, key: string) => void;
}

export function AgentCard({
  instance,
  departmentLabel,
  functionLabel,
  placement,
  selected,
  onSelect,
  tabIndex,
  onFocus,
  onNavigate,
}: AgentCardProps): React.JSX.Element {
  const duplicated = instance.deploymentCount > 1;
  const purpose = presentPurpose(instance.purpose);
  const hierarchyDescriptionId = `agent-card-hierarchy-${encodeURIComponent(instance.id)}`;
  const accessibleOrdinal = duplicated
    ? `, Instance ${String(instance.sourceOrdinal)} of ${String(instance.deploymentCount)}`
    : "";
  const accessibleName = `${instance.displayName}${accessibleOrdinal}. ${purpose} ${
    instance.enabled ? "Enabled" : "Disabled"
  } deployment.`;

  return (
    <button
      type="button"
      className="agent-card"
      aria-label={accessibleName}
      aria-describedby={hierarchyDescriptionId}
      aria-controls={selected ? "agent-inspector" : undefined}
      aria-expanded={selected}
      aria-pressed={selected}
      data-instance-id={instance.id}
      data-node-id={instance.id}
      data-node-kind="instance"
      data-source-ordinal={instance.sourceOrdinal}
      data-template-id={instance.templateId}
      onClick={() => onSelect(instance.id)}
      onFocus={() => onFocus(instance.id)}
      onKeyDown={(event: KeyboardEvent<HTMLButtonElement>) => {
        if (
          event.key !== "ArrowDown" &&
          event.key !== "ArrowLeft" &&
          event.key !== "ArrowRight" &&
          event.key !== "ArrowUp" &&
          event.key !== "Home" &&
          event.key !== "End"
        ) {
          return;
        }
        event.preventDefault();
        onNavigate(instance.id, event.key);
      }}
      tabIndex={tabIndex}
      style={{
        left: placement.x,
        top: placement.y,
        width: placement.width,
        height: placement.height,
      }}
      title={`${instance.displayName}\n${purpose}\n${instance.id}`}
    >
      <span id={hierarchyDescriptionId} className="sr-only">
        Department: {departmentLabel}. Function: {functionLabel}. Hierarchy
        level 4.
      </span>
      <span className="agent-card__topline">
        <span className="agent-card__icon" aria-hidden="true">
          {instance.operationClassification === "read_only" ? (
            <ReadIcon />
          ) : (
            <WriteIcon />
          )}
        </span>
        <span
          className={`deployment-state ${instance.enabled ? "is-enabled" : "is-disabled"}`}
        >
          {instance.enabled ? "Enabled" : "Disabled"}
        </span>
      </span>
      <span className="agent-card__name">{instance.displayName}</span>
      <span className="agent-card__purpose">{purpose}</span>
      <span className="agent-card__footer">
        <span>
          {instance.operationClassification === "read_only" ? "Read" : "Write"}
        </span>
        {duplicated ? (
          <span className="ordinal-chip">
            {String(instance.sourceOrdinal)} of{" "}
            {String(instance.deploymentCount)}
          </span>
        ) : null}
      </span>
    </button>
  );
}

import type { AgentInstance } from "./model";
import type { InstanceLayout } from "./layout";
import { ReadIcon, WriteIcon } from "./icons";
import { presentPurpose } from "./presentation";

interface AgentCardProps {
  readonly instance: AgentInstance;
  readonly placement: InstanceLayout;
  readonly selected: boolean;
  readonly onSelect: (instanceId: string) => void;
}

export function AgentCard({
  instance,
  placement,
  selected,
  onSelect,
}: AgentCardProps): React.JSX.Element {
  const duplicated = instance.deploymentCount > 1;
  const purpose = presentPurpose(instance.purpose);
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
      aria-controls={selected ? "agent-inspector" : undefined}
      aria-expanded={selected}
      aria-pressed={selected}
      data-instance-id={instance.id}
      data-node-id={instance.id}
      data-node-kind="instance"
      data-source-ordinal={instance.sourceOrdinal}
      data-template-id={instance.templateId}
      onClick={() => onSelect(instance.id)}
      style={{
        left: placement.x,
        top: placement.y,
        width: placement.width,
        height: placement.height,
      }}
      title={`${instance.displayName}\n${purpose}\n${instance.id}`}
    >
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

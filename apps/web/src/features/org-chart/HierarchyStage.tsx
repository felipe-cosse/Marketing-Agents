import { memo, useCallback, useMemo, useState } from "react";

import { AgentCard } from "./AgentCard";
import { AgentsIcon } from "./icons";
import type { HierarchyLayout } from "./layout";
import {
  MARKETING_AGENTS_ROOT,
  MARKETING_ORCHESTRATOR_CONTROL_PLANE,
} from "./model";
import type { ProjectedHierarchy } from "./projectHierarchy";

interface HierarchyStageProps {
  readonly hierarchy: ProjectedHierarchy;
  readonly layout: HierarchyLayout;
  readonly selectedInstanceId: string | null;
  readonly onSelect: (instanceId: string) => void;
}

function HierarchyStageComponent({
  hierarchy,
  layout,
  selectedInstanceId,
  onSelect,
}: HierarchyStageProps): React.JSX.Element {
  const orderedInstanceIds = useMemo(
    () =>
      hierarchy.departments.flatMap((department) =>
        department.functions.flatMap((agentFunction) =>
          agentFunction.instances.map((instance) => instance.id),
        ),
      ),
    [hierarchy],
  );
  const [rovingInstanceId, setRovingInstanceId] = useState<string | null>(
    selectedInstanceId ?? orderedInstanceIds[0] ?? null,
  );

  const resolvedRovingInstanceId =
    rovingInstanceId !== null && orderedInstanceIds.includes(rovingInstanceId)
      ? rovingInstanceId
      : selectedInstanceId !== null &&
          orderedInstanceIds.includes(selectedInstanceId)
        ? selectedInstanceId
        : (orderedInstanceIds[0] ?? null);

  const moveRovingFocus = useCallback(
    (instanceId: string, key: string): void => {
      const currentIndex = orderedInstanceIds.indexOf(instanceId);
      if (currentIndex < 0) return;
      const nextIndex =
        key === "Home"
          ? 0
          : key === "End"
            ? orderedInstanceIds.length - 1
            : key === "ArrowDown" || key === "ArrowRight"
              ? Math.min(currentIndex + 1, orderedInstanceIds.length - 1)
              : Math.max(currentIndex - 1, 0);
      const nextId = orderedInstanceIds[nextIndex];
      if (nextId === undefined) return;
      setRovingInstanceId(nextId);
      const nextCard = [
        ...document.querySelectorAll<HTMLButtonElement>("[data-instance-id]"),
      ].find((candidate) => candidate.dataset.instanceId === nextId);
      nextCard?.focus();
    },
    [orderedInstanceIds],
  );

  return (
    <div
      className="hierarchy-stage"
      data-pan-surface="true"
      style={{ width: layout.bounds.width, height: layout.bounds.height }}
    >
      <svg
        className="hierarchy-connectors"
        aria-hidden="true"
        focusable="false"
        viewBox={`0 0 ${String(layout.bounds.width)} ${String(layout.bounds.height)}`}
      >
        {layout.lines.map((connector) => (
          <line
            key={connector.id}
            data-connector-id={connector.id}
            x1={connector.x1}
            y1={connector.y1}
            x2={connector.x2}
            y2={connector.y2}
            vectorEffect="non-scaling-stroke"
          />
        ))}
      </svg>

      <div
        className="root-node"
        data-node-kind="root"
        data-node-id={MARKETING_AGENTS_ROOT.id}
        data-hierarchy-root-id={MARKETING_AGENTS_ROOT.id}
        tabIndex={-1}
        style={{
          left: layout.root.x,
          top: layout.root.y,
          width: layout.root.width,
          height: layout.root.height,
        }}
      >
        <span className="root-node__icon">
          <AgentsIcon />
        </span>
        <span className="root-node__content">
          <strong>{MARKETING_AGENTS_ROOT.displayName}</strong>
          <small
            role="note"
            aria-label={`${MARKETING_ORCHESTRATOR_CONTROL_PLANE.displayName}, implementation control plane, not included in the deployed-agent inventory`}
            data-node-kind="control-plane"
            data-control-plane-id={MARKETING_ORCHESTRATOR_CONTROL_PLANE.id}
            data-counts-as-instance={String(
              MARKETING_ORCHESTRATOR_CONTROL_PLANE.countsAsInstance,
            )}
          >
            {MARKETING_ORCHESTRATOR_CONTROL_PLANE.displayName}
            {" · "}
            {MARKETING_ORCHESTRATOR_CONTROL_PLANE.badgeLabel}
          </small>
        </span>
      </div>

      {hierarchy.departments.map((department, departmentIndex) => {
        const departmentLayout = layout.departments[departmentIndex];
        if (departmentLayout === undefined) {
          return null;
        }
        const communitySummary =
          department.instanceCount === 14 && department.templateCount === 7;
        return (
          <section
            key={department.id}
            className="department-group"
            data-department-id={department.id}
            data-instance-count={department.instanceCount}
            data-node-kind="department"
            aria-labelledby={`department-${department.id}`}
          >
            <h2
              id={`department-${department.id}`}
              className="department-header"
              data-focus-node-kind="department"
              data-node-id={department.id}
              tabIndex={-1}
              style={{
                left: departmentLayout.header.x,
                top: departmentLayout.header.y,
                width: departmentLayout.header.width,
                height: departmentLayout.header.height,
              }}
            >
              <span>{department.displayName}</span>
              <small>
                {communitySummary
                  ? "14 deployments · 7 templates"
                  : `${String(department.instanceCount)} deployed agents`}
              </small>
            </h2>

            {department.functions.map((agentFunction, functionIndex) => {
              const functionLayout = departmentLayout.functions[functionIndex];
              if (functionLayout === undefined) {
                return null;
              }
              return (
                <section
                  key={agentFunction.id}
                  className="function-group"
                  data-function-id={agentFunction.id}
                  data-node-kind="function"
                  aria-labelledby={`function-${agentFunction.id}`}
                  style={{
                    left: functionLayout.x,
                    top: functionLayout.y,
                    width: functionLayout.width,
                    height: functionLayout.height,
                  }}
                >
                  <h3
                    id={`function-${agentFunction.id}`}
                    className="function-header"
                    data-focus-node-kind="function"
                    data-node-id={agentFunction.id}
                    tabIndex={-1}
                    style={{
                      left: 0,
                      top: functionLayout.header.y - functionLayout.y,
                      width: functionLayout.header.width,
                      height: functionLayout.header.height,
                    }}
                  >
                    <span>{agentFunction.displayName}</span>
                    <small>
                      {String(agentFunction.instances.length)}{" "}
                      {agentFunction.instances.length === 1
                        ? "agent"
                        : "agents"}
                    </small>
                  </h3>
                  {agentFunction.instances.map((instance, instanceIndex) => {
                    const placement = functionLayout.instances[instanceIndex];
                    if (placement === undefined) {
                      return null;
                    }
                    return (
                      <AgentCard
                        key={instance.id}
                        instance={instance}
                        placement={{
                          ...placement,
                          x: placement.x - functionLayout.x,
                          y: placement.y - functionLayout.y,
                        }}
                        selected={selectedInstanceId === instance.id}
                        onSelect={onSelect}
                        tabIndex={
                          resolvedRovingInstanceId === instance.id ? 0 : -1
                        }
                        onFocus={setRovingInstanceId}
                        onNavigate={moveRovingFocus}
                      />
                    );
                  })}
                </section>
              );
            })}
          </section>
        );
      })}
    </div>
  );
}

export const HierarchyStage = memo(HierarchyStageComponent);

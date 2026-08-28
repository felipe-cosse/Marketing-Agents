import { memo } from "react";

import { AgentCard } from "./AgentCard";
import { AgentsIcon } from "./icons";
import type { HierarchyLayout } from "./layout";
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
        data-node-id="root"
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
        <span>
          <strong>Marketing Agents</strong>
          <small>Control plane</small>
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

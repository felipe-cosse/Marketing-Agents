import type { AgentInstance, NormalizedHierarchy } from "../org-chart/model";

export interface SelectedAgentContext {
  readonly instance: AgentInstance;
  readonly departmentName: string;
  readonly functionName: string;
  readonly departmentId: string;
  readonly functionId: string;
}

export function findSelectedAgent(
  hierarchy: NormalizedHierarchy,
  instanceId: string | null,
): SelectedAgentContext | null {
  if (instanceId === null) return null;
  for (const department of hierarchy.departments) {
    for (const agentFunction of department.functions) {
      const instance = agentFunction.instances.find(
        (candidate) => candidate.id === instanceId,
      );
      if (instance !== undefined) {
        return {
          instance,
          departmentName: department.displayName,
          functionName: agentFunction.displayName,
          departmentId: department.id,
          functionId: agentFunction.id,
        };
      }
    }
  }
  return null;
}

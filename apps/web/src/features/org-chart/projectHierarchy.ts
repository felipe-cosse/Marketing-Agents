import type {
  AgentDepartment,
  AgentFunction,
  HierarchyCounts,
  NormalizedHierarchy,
} from "./model";
import {
  hasActiveFilters,
  type OrgChartFilters,
  type RecentRunState,
} from "./filters";
import { normalizeSearchText } from "./filterUrl";
import { presentPurpose } from "./presentation";

export type RuntimeStatusByInstanceId = ReadonlyMap<string, RecentRunState>;

export interface ProjectedCounts {
  readonly departments: number;
  readonly functions: number;
  readonly templates: number;
  readonly instances: number;
}

export interface ProjectedHierarchy {
  readonly catalogVersion: string;
  readonly catalogHash: string;
  readonly counts: HierarchyCounts | ProjectedCounts;
  readonly departments: readonly AgentDepartment[];
  readonly structuralKey: string;
}

export interface HierarchyProjection {
  readonly hierarchy: ProjectedHierarchy;
  readonly matchedInstanceCount: number;
  readonly visibleDepartmentIds: ReadonlySet<string>;
  readonly visibleFunctionIds: ReadonlySet<string>;
  readonly visibleInstanceIds: ReadonlySet<string>;
}

function matchesSearch(
  instance: AgentFunction["instances"][number],
  query: string,
): boolean {
  if (query === "") return true;
  return [
    instance.displayName,
    presentPurpose(instance.purpose),
    instance.id,
    instance.templateId,
    ...instance.capabilitySummaries.map(({ displayName }) => displayName),
  ].some((value) => normalizeSearchText(value).includes(query));
}

function projectionKey(departments: readonly AgentDepartment[]): string {
  return JSON.stringify(
    departments.map((department) => [
      department.id,
      department.functions.map((agentFunction) => [
        agentFunction.id,
        agentFunction.instances.map(({ id }) => id),
      ]),
    ]),
  );
}

export function projectHierarchy(
  hierarchy: NormalizedHierarchy,
  filters: OrgChartFilters,
  statuses: RuntimeStatusByInstanceId = new Map(),
): HierarchyProjection {
  if (!hasActiveFilters(filters)) {
    return {
      hierarchy,
      matchedInstanceCount: hierarchy.counts.instances,
      visibleDepartmentIds: new Set(hierarchy.departments.map(({ id }) => id)),
      visibleFunctionIds: new Set(
        hierarchy.departments.flatMap(({ functions }) =>
          functions.map(({ id }) => id),
        ),
      ),
      visibleInstanceIds: new Set(
        hierarchy.departments.flatMap(({ functions }) =>
          functions.flatMap(({ instances }) => instances.map(({ id }) => id)),
        ),
      ),
    };
  }

  const query = normalizeSearchText(filters.q);
  const departments: AgentDepartment[] = [];
  for (const department of hierarchy.departments) {
    if (filters.departmentId !== null && department.id !== filters.departmentId)
      continue;
    const functions: AgentFunction[] = [];
    for (const agentFunction of department.functions) {
      if (
        filters.functionId !== null &&
        agentFunction.id !== filters.functionId
      )
        continue;
      const instances = agentFunction.instances.filter((instance) => {
        if (!matchesSearch(instance, query)) return false;
        if (
          filters.deployment !== null &&
          instance.enabled !== (filters.deployment === "enabled")
        )
          return false;
        if (
          filters.runState !== null &&
          statuses.get(instance.id) !== filters.runState
        )
          return false;
        return (
          filters.capabilityId === null ||
          instance.capabilitySummaries.some(
            ({ id }) => id === filters.capabilityId,
          )
        );
      });
      if (instances.length > 0) functions.push({ ...agentFunction, instances });
    }
    if (functions.length > 0) {
      const instances = functions.flatMap(({ instances }) => instances);
      departments.push({
        ...department,
        functions,
        instanceCount: instances.length,
        templateCount: new Set(instances.map(({ templateId }) => templateId))
          .size,
      });
    }
  }

  const functions = departments.flatMap(({ functions }) => functions);
  const instances = functions.flatMap(({ instances }) => instances);
  const projected: ProjectedHierarchy = Object.freeze({
    catalogVersion: hierarchy.catalogVersion,
    catalogHash: hierarchy.catalogHash,
    counts: Object.freeze({
      departments: departments.length,
      functions: functions.length,
      templates: new Set(instances.map(({ templateId }) => templateId)).size,
      instances: instances.length,
    }),
    departments: Object.freeze(departments),
    structuralKey: projectionKey(departments),
  });
  return {
    hierarchy: projected,
    matchedInstanceCount: instances.length,
    visibleDepartmentIds: new Set(departments.map(({ id }) => id)),
    visibleFunctionIds: new Set(functions.map(({ id }) => id)),
    visibleInstanceIds: new Set(instances.map(({ id }) => id)),
  };
}

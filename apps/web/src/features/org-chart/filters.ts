import type { NormalizedHierarchy } from "./model";

export const RUN_STATES = [
  "never_run",
  "received",
  "validated",
  "planned",
  "awaiting_approval",
  "executing",
  "completed",
  "failed",
  "rejected",
  "cancelled",
] as const;

export type RecentRunState = (typeof RUN_STATES)[number];
export type DeploymentFilter = "enabled" | "disabled";

export interface OrgChartFilters {
  readonly q: string;
  readonly departmentId: string | null;
  readonly functionId: string | null;
  readonly deployment: DeploymentFilter | null;
  readonly runState: RecentRunState | null;
  readonly capabilityId: string | null;
}

export interface FilterOption {
  readonly id: string;
  readonly displayName: string;
}

export interface OrgChartFilterOptions {
  readonly departments: readonly FilterOption[];
  readonly functionsByDepartment: ReadonlyMap<string, readonly FilterOption[]>;
  readonly capabilities: readonly FilterOption[];
}

export const DEFAULT_ORG_CHART_FILTERS: OrgChartFilters = Object.freeze({
  q: "",
  departmentId: null,
  functionId: null,
  deployment: null,
  runState: null,
  capabilityId: null,
});

export function hasActiveFilters(filters: OrgChartFilters): boolean {
  return (
    filters.q !== "" ||
    filters.departmentId !== null ||
    filters.functionId !== null ||
    filters.deployment !== null ||
    filters.runState !== null ||
    filters.capabilityId !== null
  );
}

export function deriveFilterOptions(
  hierarchy: NormalizedHierarchy,
): OrgChartFilterOptions {
  const capabilities = new Map<string, FilterOption>();
  const functionsByDepartment = new Map<string, readonly FilterOption[]>();

  for (const department of hierarchy.departments) {
    functionsByDepartment.set(
      department.id,
      Object.freeze(
        department.functions.map(({ id, displayName }) => ({
          id,
          displayName,
        })),
      ),
    );
    for (const agentFunction of department.functions) {
      for (const instance of agentFunction.instances) {
        for (const capability of instance.capabilitySummaries) {
          if (!capabilities.has(capability.id)) {
            capabilities.set(capability.id, {
              id: capability.id,
              displayName: capability.displayName,
            });
          }
        }
      }
    }
  }

  return Object.freeze({
    departments: Object.freeze(
      hierarchy.departments.map(({ id, displayName }) => ({ id, displayName })),
    ),
    functionsByDepartment,
    capabilities: Object.freeze([...capabilities.values()]),
  });
}

import {
  DEFAULT_ORG_CHART_FILTERS,
  RUN_STATES,
  type DeploymentFilter,
  type OrgChartFilters,
  type RecentRunState,
} from "./filters";
import type { NormalizedHierarchy } from "./model";

const MAX_QUERY_LENGTH = 2_048;
const MAX_RAW_SEARCH_LENGTH = 256;
type SupportedKey =
  "q" | "department" | "function" | "deployment" | "run" | "capability";
const DEPLOYMENTS = new Set<DeploymentFilter>(["enabled", "disabled"]);
const RUN_STATE_SET = new Set<RecentRunState>(RUN_STATES);
const UNICODE_WHITESPACE = /\p{White_Space}+/gu;

export function cleanSearchQuery(value: string): string {
  return value.normalize("NFKC").trim().replace(UNICODE_WHITESPACE, " ");
}

export function normalizeSearchText(value: string): string {
  return cleanSearchQuery(value).toLocaleLowerCase("en-US");
}

function singleValue(
  params: URLSearchParams,
  key: SupportedKey,
): string | null {
  const values = params.getAll(key);
  return values.length === 1 ? (values[0] ?? null) : null;
}

export function parseOrgChartFilters(
  search: string,
  hierarchy: NormalizedHierarchy,
): OrgChartFilters {
  if (search.length > MAX_QUERY_LENGTH) {
    return DEFAULT_ORG_CHART_FILTERS;
  }
  const params = new URLSearchParams(
    search.startsWith("?") ? search.slice(1) : search,
  );
  const departmentIds = new Set(hierarchy.departments.map(({ id }) => id));
  const functionOwners = new Map<string, string>();
  const capabilityIds = new Set<string>();
  for (const department of hierarchy.departments) {
    for (const agentFunction of department.functions) {
      functionOwners.set(agentFunction.id, department.id);
      for (const instance of agentFunction.instances) {
        for (const capability of instance.capabilitySummaries) {
          capabilityIds.add(capability.id);
        }
      }
    }
  }

  const rawQ = singleValue(params, "q");
  const q =
    rawQ !== null && rawQ.length <= MAX_RAW_SEARCH_LENGTH
      ? cleanSearchQuery(rawQ)
      : "";
  const rawDepartment = singleValue(params, "department");
  const departmentId =
    rawDepartment !== null && departmentIds.has(rawDepartment)
      ? rawDepartment
      : null;
  const rawFunction = singleValue(params, "function");
  const functionId =
    rawFunction !== null &&
    departmentId !== null &&
    functionOwners.get(rawFunction) === departmentId
      ? rawFunction
      : null;
  const rawDeployment = singleValue(params, "deployment");
  const deployment =
    rawDeployment !== null && DEPLOYMENTS.has(rawDeployment as DeploymentFilter)
      ? (rawDeployment as DeploymentFilter)
      : null;
  const rawRun = singleValue(params, "run");
  const runState =
    rawRun !== null && RUN_STATE_SET.has(rawRun as RecentRunState)
      ? (rawRun as RecentRunState)
      : null;
  const rawCapability = singleValue(params, "capability");
  const capabilityId =
    rawCapability !== null && capabilityIds.has(rawCapability)
      ? rawCapability
      : null;

  return { q, departmentId, functionId, deployment, runState, capabilityId };
}

export function serializeOrgChartFilters(filters: OrgChartFilters): string {
  const params = new URLSearchParams();
  const q = cleanSearchQuery(filters.q);
  if (q !== "" && q.length <= MAX_RAW_SEARCH_LENGTH) params.set("q", q);
  if (filters.departmentId !== null)
    params.set("department", filters.departmentId);
  if (filters.functionId !== null) params.set("function", filters.functionId);
  if (filters.deployment !== null) params.set("deployment", filters.deployment);
  if (filters.runState !== null) params.set("run", filters.runState);
  if (filters.capabilityId !== null)
    params.set("capability", filters.capabilityId);
  const value = params.toString();
  return value === "" ? "" : `?${value}`;
}

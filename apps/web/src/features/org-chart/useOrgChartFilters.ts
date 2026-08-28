import { useCallback, useEffect, useMemo } from "react";
import { useLocation, useSearchParams } from "react-router-dom";

import { parseOrgChartFilters, serializeOrgChartFilters } from "./filterUrl";
import type {
  DeploymentFilter,
  OrgChartFilters,
  RecentRunState,
} from "./filters";
import type { NormalizedHierarchy } from "./model";

interface OrgChartFilterState {
  readonly filters: OrgChartFilters;
  readonly setQuery: (query: string) => void;
  readonly setDepartment: (departmentId: string | null) => void;
  readonly setFunction: (functionId: string | null) => void;
  readonly setDeployment: (deployment: DeploymentFilter | null) => void;
  readonly setRunState: (runState: RecentRunState | null) => void;
  readonly setCapability: (capabilityId: string | null) => void;
  readonly clearAll: () => void;
}

function paramsFor(filters: OrgChartFilters): URLSearchParams {
  const serialized = serializeOrgChartFilters(filters);
  return new URLSearchParams(
    serialized.startsWith("?") ? serialized.slice(1) : serialized,
  );
}

export function useOrgChartFilters(
  hierarchy: NormalizedHierarchy,
): OrgChartFilterState {
  const location = useLocation();
  const [, setSearchParams] = useSearchParams();
  const filters = useMemo(
    () => parseOrgChartFilters(location.search, hierarchy),
    [hierarchy, location.search],
  );
  const canonicalSearch = serializeOrgChartFilters(filters);

  useEffect(() => {
    if (location.search !== canonicalSearch) {
      setSearchParams(paramsFor(filters), { replace: true });
    }
  }, [canonicalSearch, filters, location.search, setSearchParams]);

  const write = useCallback(
    (next: OrgChartFilters, replace = false): void => {
      const serialized = serializeOrgChartFilters(next);
      if (serialized !== location.search) {
        setSearchParams(paramsFor(next), { replace });
      }
    },
    [location.search, setSearchParams],
  );

  const setQuery = useCallback(
    (q: string) => write({ ...filters, q }, true),
    [filters, write],
  );
  const setDepartment = useCallback(
    (departmentId: string | null) =>
      write({
        ...filters,
        departmentId,
        functionId:
          departmentId === filters.departmentId ? filters.functionId : null,
      }),
    [filters, write],
  );
  const setFunction = useCallback(
    (functionId: string | null) => write({ ...filters, functionId }),
    [filters, write],
  );
  const setDeployment = useCallback(
    (deployment: DeploymentFilter | null) => write({ ...filters, deployment }),
    [filters, write],
  );
  const setRunState = useCallback(
    (runState: RecentRunState | null) => write({ ...filters, runState }),
    [filters, write],
  );
  const setCapability = useCallback(
    (capabilityId: string | null) => write({ ...filters, capabilityId }),
    [filters, write],
  );
  const clearAll = useCallback(
    () =>
      write({
        q: "",
        departmentId: null,
        functionId: null,
        deployment: null,
        runState: null,
        capabilityId: null,
      }),
    [write],
  );

  return {
    filters,
    setQuery,
    setDepartment,
    setFunction,
    setDeployment,
    setRunState,
    setCapability,
    clearAll,
  };
}

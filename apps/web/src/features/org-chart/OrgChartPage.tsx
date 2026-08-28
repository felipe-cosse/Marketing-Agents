import { useQuery } from "@tanstack/react-query";
import {
  useCallback,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import {
  CATALOG_HIERARCHY_QUERY_KEY,
  fetchCatalogHierarchy,
} from "../../api/catalogHierarchy";
import {
  fetchInstanceStatusSummary,
  type InstanceStatusSummary,
} from "../../api/instanceStatusSummary";
import { AgentDetailPane } from "../instance-detail/AgentDetailPane";
import { UnsavedConfigurationDialog } from "../instance-detail/UnsavedConfigurationDialog";
import { findSelectedAgent } from "../instance-detail/selectedAgent";
import { CatalogToolbar, type CatalogFilterOptions } from "./CatalogToolbar";
import {
  RUN_STATES,
  deriveFilterOptions,
  type DeploymentFilter,
  type RecentRunState,
} from "./filters";
import type { NormalizedHierarchy } from "./model";
import { OrgChartCanvas } from "./OrgChartCanvas";
import { projectHierarchy } from "./projectHierarchy";
import { useFilteredFocus } from "./useFilteredFocus";
import { useOrgChartFilters } from "./useOrgChartFilters";

const INSTANCE_STATUS_QUERY_KEY = [
  "agent-instances",
  "status-summary",
] as const;
const STATUS_REFRESH_INTERVAL_MS = 5_000;
const STATUS_ERROR_REFRESH_INTERVAL_MS = 30_000;
const NO_RUNTIME_STATUSES: ReadonlyMap<string, RecentRunState> = new Map();

const RUN_STATE_LABELS: Readonly<Record<RecentRunState, string>> = {
  never_run: "Never run",
  received: "Received",
  validated: "Validated",
  planned: "Planned",
  awaiting_approval: "Awaiting approval",
  executing: "Executing",
  completed: "Completed",
  failed: "Failed",
  rejected: "Rejected",
  cancelled: "Cancelled",
};

function instanceIds(hierarchy: NormalizedHierarchy): readonly string[] {
  return hierarchy.departments.flatMap((department) =>
    department.functions.flatMap((agentFunction) =>
      agentFunction.instances.map((instance) => instance.id),
    ),
  );
}

function catalogFilterOptions(
  hierarchy: NormalizedHierarchy,
): CatalogFilterOptions {
  const derived = deriveFilterOptions(hierarchy);
  return {
    departments: derived.departments.map((option) => ({
      value: option.id,
      label: option.displayName,
    })),
    functions: hierarchy.departments.flatMap((department) =>
      (derived.functionsByDepartment.get(department.id) ?? []).map(
        (option) => ({
          value: option.id,
          label: option.displayName,
          departmentId: department.id,
        }),
      ),
    ),
    deploymentStates: [
      { value: "enabled", label: "Enabled" },
      { value: "disabled", label: "Disabled" },
    ],
    recentRunStates: RUN_STATES.map((value) => ({
      value,
      label: RUN_STATE_LABELS[value],
    })),
    capabilities: derived.capabilities.map((option) => ({
      value: option.id,
      label: option.displayName,
    })),
  };
}

interface LoadedOrgChartProps {
  readonly hierarchy: NormalizedHierarchy;
}

interface PendingSelection {
  readonly instanceId: string | null;
  readonly restoreFocusId: string | null;
}

function focusInstanceCard(instanceId: string): boolean {
  const card = [
    ...document.querySelectorAll<HTMLButtonElement>("[data-instance-id]"),
  ].find((candidate) => candidate.dataset.instanceId === instanceId);
  card?.focus();
  return card !== undefined;
}

function LoadedOrgChart({ hierarchy }: LoadedOrgChartProps): React.JSX.Element {
  const expectedInstanceIds = useMemo(
    () => instanceIds(hierarchy),
    [hierarchy],
  );
  const priorStatusRef = useRef<InstanceStatusSummary | undefined>(undefined);
  const statusQuery = useQuery({
    queryKey: INSTANCE_STATUS_QUERY_KEY,
    queryFn: async ({ signal }) => {
      const previous = priorStatusRef.current;
      const summary = await fetchInstanceStatusSummary(
        expectedInstanceIds,
        previous === undefined ? { signal } : { previous, signal },
      );
      priorStatusRef.current = summary;
      return summary;
    },
    retry: false,
    refetchInterval: (query) =>
      query.state.error === null
        ? STATUS_REFRESH_INTERVAL_MS
        : STATUS_ERROR_REFRESH_INTERVAL_MS,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
  });
  const filterState = useOrgChartFilters(hierarchy);
  const [selectedInstanceId, setSelectedInstanceId] = useState<string | null>(
    null,
  );
  const [configurationDirty, setConfigurationDirty] = useState(false);
  const [pendingSelection, setPendingSelection] =
    useState<PendingSelection | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const options = useMemo(() => catalogFilterOptions(hierarchy), [hierarchy]);
  const runtimeStatusByInstanceId = useMemo(
    () =>
      new Map(
        statusQuery.data?.items.map((item) => [item.instanceId, item]) ?? [],
      ),
    [statusQuery.data],
  );
  const statusByInstanceId = useMemo(
    () =>
      new Map(
        [...runtimeStatusByInstanceId].map(([instanceId, item]) => [
          instanceId,
          item.status,
        ]),
      ),
    [runtimeStatusByInstanceId],
  );
  const projectionStatuses =
    filterState.filters.runState === null
      ? NO_RUNTIME_STATUSES
      : statusByInstanceId;
  const projection = useMemo(
    () => projectHierarchy(hierarchy, filterState.filters, projectionStatuses),
    [filterState.filters, hierarchy, projectionStatuses],
  );
  const handleSelectionChange = useCallback(
    (instanceId: string | null) => {
      if (instanceId === selectedInstanceId) return;
      if (configurationDirty) {
        setPendingSelection({
          instanceId,
          restoreFocusId: instanceId === null ? selectedInstanceId : null,
        });
        return;
      }
      setSelectedInstanceId(instanceId);
    },
    [configurationDirty, selectedInstanceId],
  );
  useFilteredFocus({
    sourceHierarchy: hierarchy,
    projection,
    selectedInstanceId,
    onSelectionChange: handleSelectionChange,
    searchRef,
  });

  const departmentIds = useMemo(
    () => new Set(options.departments.map((option) => option.value)),
    [options.departments],
  );
  const functionIds = useMemo(
    () =>
      new Set(
        options.functions
          .filter(
            (option) =>
              option.departmentId === filterState.filters.departmentId,
          )
          .map((option) => option.value),
      ),
    [filterState.filters.departmentId, options.functions],
  );
  const capabilityIds = useMemo(
    () => new Set(options.capabilities.map((option) => option.value)),
    [options.capabilities],
  );
  const onDepartmentChange = useCallback(
    (value: string) =>
      filterState.setDepartment(
        value !== "" && departmentIds.has(value) ? value : null,
      ),
    [departmentIds, filterState],
  );
  const onFunctionChange = useCallback(
    (value: string) =>
      filterState.setFunction(
        value !== "" && functionIds.has(value) ? value : null,
      ),
    [filterState, functionIds],
  );
  const onDeploymentChange = useCallback(
    (value: string) => {
      const deployment: DeploymentFilter | null =
        value === "enabled" || value === "disabled" ? value : null;
      filterState.setDeployment(deployment);
    },
    [filterState],
  );
  const onRunStateChange = useCallback(
    (value: string) =>
      filterState.setRunState(
        RUN_STATES.find((candidate) => candidate === value) ?? null,
      ),
    [filterState],
  );
  const onCapabilityChange = useCallback(
    (value: string) =>
      filterState.setCapability(
        value !== "" && capabilityIds.has(value) ? value : null,
      ),
    [capabilityIds, filterState],
  );

  const runStatusUnavailable =
    filterState.filters.runState !== null && statusQuery.data === undefined;
  const emptyMessage = runStatusUnavailable
    ? statusQuery.isError
      ? "Recent run status is unavailable. Clear the recent run filter or try again shortly."
      : "Recent run status is loading. Clear the recent run filter to return to the full hierarchy."
    : "No agents match your search and filters.";
  const emptyTitle = runStatusUnavailable
    ? statusQuery.isError
      ? "Recent run status unavailable"
      : "Loading recent run status"
    : "No agents match";
  const clearAndFocusSearch = useCallback(() => {
    filterState.clearAll();
    requestAnimationFrame(() => searchRef.current?.focus());
  }, [filterState]);
  const selectedAgent = useMemo(
    () => findSelectedAgent(hierarchy, selectedInstanceId),
    [hierarchy, selectedInstanceId],
  );
  const closeInspector = useCallback(() => {
    if (selectedInstanceId === null) return;
    if (configurationDirty) {
      setPendingSelection({
        instanceId: null,
        restoreFocusId: selectedInstanceId,
      });
      return;
    }
    const restoreFocusId = selectedInstanceId;
    setSelectedInstanceId(null);
    requestAnimationFrame(() => {
      if (!focusInstanceCard(restoreFocusId)) searchRef.current?.focus();
    });
  }, [configurationDirty, selectedInstanceId]);
  const keepEditing = useCallback(() => {
    setPendingSelection(null);
    requestAnimationFrame(() => {
      const inspector = document.querySelector("#agent-inspector");
      const editorTarget = inspector?.querySelector<HTMLElement>(
        "form input:not(:disabled), form select:not(:disabled), form button:not(:disabled)",
      );
      const fallback = inspector?.querySelector<HTMLElement>(
        "button:not(:disabled)",
      );
      (editorTarget ?? fallback)?.focus();
    });
  }, []);
  const discardAndContinue = useCallback(() => {
    if (pendingSelection === null) return;
    const { instanceId, restoreFocusId } = pendingSelection;
    setPendingSelection(null);
    setConfigurationDirty(false);
    setSelectedInstanceId(instanceId);
    requestAnimationFrame(() => {
      const focusId = instanceId ?? restoreFocusId;
      if (focusId !== null && focusInstanceCard(focusId)) return;
      searchRef.current?.focus();
    });
  }, [pendingSelection]);
  const handleWorkspaceKeyDown = (
    event: KeyboardEvent<HTMLDivElement>,
  ): void => {
    const eventTarget = event.target instanceof Element ? event.target : null;
    if (
      event.key !== "Escape" ||
      event.defaultPrevented ||
      selectedInstanceId === null ||
      pendingSelection !== null ||
      eventTarget?.closest('[role="dialog"], [role="alertdialog"]') !== null
    ) {
      return;
    }
    event.preventDefault();
    closeInspector();
  };

  return (
    <div
      className={`chart-workspace ${selectedAgent === null ? "" : "chart-workspace--with-inspector"}`}
      onKeyDown={handleWorkspaceKeyDown}
    >
      <OrgChartCanvas
        hierarchy={projection.hierarchy}
        selectedInstanceId={selectedInstanceId}
        onSelectionChange={handleSelectionChange}
        emptyTitle={emptyTitle}
        emptyMessage={emptyMessage}
        onClearFilters={clearAndFocusSearch}
        toolbar={
          <CatalogToolbar
            hierarchy={hierarchy}
            options={options}
            filters={{
              q: filterState.filters.q,
              departmentId: filterState.filters.departmentId ?? "",
              functionId: filterState.filters.functionId ?? "",
              deploymentState: filterState.filters.deployment ?? "",
              recentRunState: filterState.filters.runState ?? "",
              capabilityId: filterState.filters.capabilityId ?? "",
            }}
            resultCount={projection.matchedInstanceCount}
            resultAnnouncement={runStatusUnavailable ? emptyTitle : undefined}
            statusAvailable={statusQuery.data !== undefined}
            statusStale={statusQuery.isRefetchError}
            searchRef={searchRef}
            onQueryChange={filterState.setQuery}
            onDepartmentChange={onDepartmentChange}
            onFunctionChange={onFunctionChange}
            onDeploymentStateChange={onDeploymentChange}
            onRecentRunStateChange={onRunStateChange}
            onCapabilityChange={onCapabilityChange}
            onClear={filterState.clearAll}
          />
        }
      />
      {selectedAgent === null ? null : (
        <AgentDetailPane
          hierarchy={hierarchy}
          selected={selectedAgent}
          runtimeStatus={runtimeStatusByInstanceId.get(
            selectedAgent.instance.id,
          )}
          onClose={closeInspector}
          onConfigurationDirtyChange={setConfigurationDirty}
        />
      )}
      <UnsavedConfigurationDialog
        open={pendingSelection !== null}
        destinationLabel={
          pendingSelection?.instanceId === null
            ? "close this inspector"
            : "open another agent"
        }
        onDiscard={discardAndContinue}
        onKeepEditing={keepEditing}
      />
    </div>
  );
}

export function OrgChartPage(): React.JSX.Element {
  const hierarchyQuery = useQuery({
    queryKey: CATALOG_HIERARCHY_QUERY_KEY,
    queryFn: ({ signal }) => fetchCatalogHierarchy(signal),
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: Number.POSITIVE_INFINITY,
    refetchOnWindowFocus: false,
  });

  return (
    <main className="workspace">
      <header className="workspace-heading">
        <div>
          <p className="eyebrow">Catalog topology</p>
          <h1 id="org-chart-title">Marketing agent organization</h1>
          <p>
            Explore the complete source-authoritative hierarchy. Select an agent
            to keep it highlighted while you move through the canvas.
          </p>
        </div>
        {hierarchyQuery.data === undefined ? null : (
          <div
            className="catalog-version"
            title={hierarchyQuery.data.catalogHash}
          >
            <span>Catalog</span>
            <strong>{hierarchyQuery.data.catalogVersion}</strong>
          </div>
        )}
      </header>

      {hierarchyQuery.isPending ? (
        <section className="chart-state" aria-live="polite">
          <span className="loading-mark" aria-hidden="true" />
          <div>
            <strong>Loading the local catalog</strong>
            <p>Building the exact 5-department hierarchy from the API.</p>
          </div>
        </section>
      ) : null}

      {hierarchyQuery.isError ? (
        <section className="chart-state chart-state--error" role="alert">
          <div>
            <strong>The hierarchy is unavailable</strong>
            <p>{hierarchyQuery.error.message}</p>
          </div>
          <button
            type="button"
            className="retry-button"
            onClick={() => void hierarchyQuery.refetch()}
          >
            Try again
          </button>
        </section>
      ) : null}

      {hierarchyQuery.data === undefined ? null : (
        <LoadedOrgChart hierarchy={hierarchyQuery.data} />
      )}
    </main>
  );
}

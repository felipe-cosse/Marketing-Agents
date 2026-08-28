import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";

import {
  fetchAgentInstanceDetail,
  type AgentInstanceDetail,
  type AgentInstanceDetailIdentity,
} from "../../api/agentInstanceDetail";
import { CATALOG_HIERARCHY_QUERY_KEY } from "../../api/catalogHierarchy";
import type { InstanceRuntimeStatus } from "../../api/instanceStatusSummary";
import type { NormalizedHierarchy } from "../org-chart/model";
import { AgentInspector } from "./AgentInspector";
import { InstanceConfigurationEditor } from "./InstanceConfigurationEditor";
import type { SelectedAgentContext } from "./selectedAgent";

interface AgentDetailPaneProps {
  readonly hierarchy: NormalizedHierarchy;
  readonly selected: SelectedAgentContext;
  readonly runtimeStatus: InstanceRuntimeStatus | undefined;
  readonly onClose: () => void;
  readonly onConfigurationDirtyChange: (dirty: boolean) => void;
}

function detailIdentity(
  hierarchy: NormalizedHierarchy,
  selected: SelectedAgentContext,
): AgentInstanceDetailIdentity {
  return {
    instanceId: selected.instance.id,
    templateId: selected.instance.templateId,
    departmentId: selected.departmentId,
    functionId: selected.functionId,
    sourceOrdinal: selected.instance.sourceOrdinal,
    sharedTemplateDeploymentCount: selected.instance.deploymentCount,
    catalogVersion: hierarchy.catalogVersion,
    catalogHash: hierarchy.catalogHash,
  };
}

function runtimeRevisionKey(
  status: InstanceRuntimeStatus | undefined,
): string | null {
  if (status === undefined) return null;
  return [
    status.status,
    status.latestRunId ?? "",
    status.latestRunState ?? "",
    status.latestRunCreatedAt ?? "",
    status.latestRunUpdatedAt ?? "",
  ].join("|");
}

export function AgentDetailPane({
  hierarchy,
  selected,
  runtimeStatus,
  onClose,
  onConfigurationDirtyChange,
}: AgentDetailPaneProps): React.JSX.Element {
  const queryClient = useQueryClient();
  const identity = useMemo(
    () => detailIdentity(hierarchy, selected),
    [hierarchy, selected],
  );
  const queryKey = useMemo(
    () =>
      [
        "catalog",
        "agent-instance",
        hierarchy.catalogHash,
        selected.instance.id,
      ] as const,
    [hierarchy.catalogHash, selected.instance.id],
  );
  const detailQuery = useQuery({
    queryKey,
    queryFn: async ({ signal }) => {
      const previous = queryClient.getQueryData<AgentInstanceDetail>(queryKey);
      return fetchAgentInstanceDetail(
        identity,
        previous === undefined ? { signal } : { previous, signal },
      );
    },
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: Number.POSITIVE_INFINITY,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
  });
  const observedRuntimeKeys = useRef(new Map<string, string | null>());
  const statusKey = runtimeRevisionKey(runtimeStatus);

  useEffect(() => {
    const instanceId = selected.instance.id;
    const previousKey = observedRuntimeKeys.current.get(instanceId);
    observedRuntimeKeys.current.set(instanceId, statusKey);
    if (previousKey === undefined || previousKey === statusKey) return;
    void queryClient.invalidateQueries({ queryKey, exact: true });
  }, [queryClient, queryKey, selected.instance.id, statusKey]);

  const refreshDetail = async (): Promise<void> => {
    await detailQuery.refetch({ throwOnError: true });
  };

  const refreshAfterConfiguration = async (): Promise<void> => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: CATALOG_HIERARCHY_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey, exact: true }),
    ]);
  };

  return (
    <AgentInspector
      summary={selected.instance}
      departmentName={selected.departmentName}
      functionName={selected.functionName}
      detail={detailQuery.data}
      isPending={detailQuery.isPending}
      error={detailQuery.error}
      onRetry={() => void refreshDetail()}
      onClose={onClose}
      configurationControls={
        detailQuery.data === undefined ? undefined : (
          <InstanceConfigurationEditor
            detail={detailQuery.data}
            onDirtyChange={onConfigurationDirtyChange}
            onSaved={refreshAfterConfiguration}
            onReload={refreshDetail}
          />
        )
      }
    />
  );
}

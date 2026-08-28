import { useQuery } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import {
  CATALOG_HIERARCHY_QUERY_KEY,
  fetchCatalogHierarchy,
} from "../../api/catalogHierarchy";
import { OrgChartCanvas } from "./OrgChartCanvas";

export function OrgChartPage(): React.JSX.Element {
  const hierarchyQuery = useQuery({
    queryKey: CATALOG_HIERARCHY_QUERY_KEY,
    queryFn: () => fetchCatalogHierarchy(),
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: Number.POSITIVE_INFINITY,
    refetchOnWindowFocus: false,
  });
  const [selectedInstanceId, setSelectedInstanceId] = useState<string | null>(
    null,
  );
  const handleSelectionChange = useCallback((instanceId: string | null) => {
    setSelectedInstanceId(instanceId);
  }, []);

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
        <OrgChartCanvas
          hierarchy={hierarchyQuery.data}
          selectedInstanceId={selectedInstanceId}
          onSelectionChange={handleSelectionChange}
        />
      )}
    </main>
  );
}

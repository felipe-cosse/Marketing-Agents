import { useCallback, useMemo, type ReactNode } from "react";

import { CanvasControls } from "./CanvasControls";
import { describeGraphHierarchy } from "./hierarchyAccessibility";
import { HierarchyStage } from "./HierarchyStage";
import { layoutHierarchy } from "./layout";
import type { ProjectedHierarchy } from "./projectHierarchy";
import { useOrgChartViewport } from "./useOrgChartViewport";

interface OrgChartCanvasProps {
  readonly hierarchy: ProjectedHierarchy;
  readonly selectedInstanceId: string | null;
  readonly onSelectionChange: (instanceId: string | null) => void;
  readonly toolbar?: ReactNode;
  readonly emptyTitle?: string;
  readonly emptyMessage?: string;
  readonly onClearFilters?: () => void;
  readonly minimumAutoZoom?: number | undefined;
}

const EMPTY_BOUNDS = Object.freeze({
  x: 0,
  y: 0,
  width: 148,
  height: 314,
});

export function OrgChartCanvas({
  hierarchy,
  selectedInstanceId,
  onSelectionChange,
  toolbar,
  emptyTitle = "No agents match",
  emptyMessage = "No agents match your search and filters.",
  onClearFilters,
  minimumAutoZoom,
}: OrgChartCanvasProps): React.JSX.Element {
  const layout = useMemo(
    () =>
      hierarchy.departments.length === 0 ? null : layoutHierarchy(hierarchy),
    // The key intentionally excludes status and presentation metadata.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [hierarchy.structuralKey],
  );
  const selectedRect =
    selectedInstanceId === null || layout === null
      ? null
      : (layout.instanceById.get(selectedInstanceId) ?? null);
  const clearSelection = useCallback(
    () => onSelectionChange(null),
    [onSelectionChange],
  );
  const { containerRef, transform, fit, zoomIn, zoomOut, pointerHandlers } =
    useOrgChartViewport({
      bounds: layout?.bounds ?? EMPTY_BOUNDS,
      structuralKey: hierarchy.structuralKey,
      selectedRect,
      onClearSelection: clearSelection,
      minimumAutoZoom,
    });

  return (
    <section className="chart-surface" aria-labelledby="org-chart-title">
      <div className="chart-toolbar">
        {toolbar ?? (
          <div className="inventory-summary" aria-label="Catalog inventory">
            <span className="inventory-summary__dot" aria-hidden="true" />
            <strong>Complete catalog</strong>
            <span>5 departments</span>
            <span>12 functions</span>
            <span>43 deployed agents</span>
          </div>
        )}
        <CanvasControls
          zoom={transform.zoom}
          onZoomIn={zoomIn}
          onZoomOut={zoomOut}
          onFit={fit}
        />
      </div>
      <p id="org-chart-structure-summary" className="sr-only">
        {describeGraphHierarchy(hierarchy)}
      </p>
      <p id="chart-keyboard-help" className="sr-only">
        Drag the chart to pan. Use arrow keys to pan, plus or minus to zoom, and
        zero to fit the complete hierarchy.
      </p>
      <div
        ref={containerRef}
        className="org-chart-viewport"
        data-testid="org-chart-viewport"
        data-viewport-intent={transform.intent}
        data-viewport-x={transform.translateX}
        data-viewport-y={transform.translateY}
        data-viewport-zoom={transform.zoom}
        data-hierarchy-semantics="visual"
        aria-describedby="org-chart-structure-summary chart-keyboard-help"
        aria-label="Marketing Agents interactive organization chart"
        role="region"
        tabIndex={0}
        {...pointerHandlers}
      >
        {layout === null ? (
          <div className="catalog-empty-state" role="region" aria-live="polite">
            <strong>{emptyTitle}</strong>
            <p>{emptyMessage}</p>
            {onClearFilters === undefined ? null : (
              <button type="button" onClick={onClearFilters}>
                Clear search and filters
              </button>
            )}
          </div>
        ) : (
          <div
            className="org-chart-transform"
            style={{
              transform: `translate3d(${String(transform.translateX)}px, ${String(
                transform.translateY,
              )}px, 0) scale(${String(transform.zoom)})`,
            }}
          >
            <HierarchyStage
              hierarchy={hierarchy}
              layout={layout}
              selectedInstanceId={selectedInstanceId}
              onSelect={onSelectionChange}
            />
          </div>
        )}
      </div>
    </section>
  );
}

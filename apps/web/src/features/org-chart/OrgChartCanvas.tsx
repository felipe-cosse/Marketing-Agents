import { useCallback, useMemo } from "react";

import { CanvasControls } from "./CanvasControls";
import { HierarchyStage } from "./HierarchyStage";
import { layoutHierarchy } from "./layout";
import type { NormalizedHierarchy } from "./model";
import { useOrgChartViewport } from "./useOrgChartViewport";

interface OrgChartCanvasProps {
  readonly hierarchy: NormalizedHierarchy;
  readonly selectedInstanceId: string | null;
  readonly onSelectionChange: (instanceId: string | null) => void;
}

export function OrgChartCanvas({
  hierarchy,
  selectedInstanceId,
  onSelectionChange,
}: OrgChartCanvasProps): React.JSX.Element {
  const layout = useMemo(
    () => layoutHierarchy(hierarchy),
    // The key intentionally excludes status and presentation metadata.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [hierarchy.structuralKey],
  );
  const selectedRect =
    selectedInstanceId === null
      ? null
      : (layout.instanceById.get(selectedInstanceId) ?? null);
  const clearSelection = useCallback(
    () => onSelectionChange(null),
    [onSelectionChange],
  );
  const { containerRef, transform, fit, zoomIn, zoomOut, pointerHandlers } =
    useOrgChartViewport({
      bounds: layout.bounds,
      structuralKey: hierarchy.structuralKey,
      selectedRect,
      onClearSelection: clearSelection,
    });

  return (
    <section className="chart-surface" aria-labelledby="org-chart-title">
      <div className="chart-toolbar">
        <div className="inventory-summary" aria-label="Catalog inventory">
          <span className="inventory-summary__dot" aria-hidden="true" />
          <strong>Complete catalog</strong>
          <span>5 departments</span>
          <span>12 functions</span>
          <span>43 deployed agents</span>
        </div>
        <CanvasControls
          zoom={transform.zoom}
          onZoomIn={zoomIn}
          onZoomOut={zoomOut}
          onFit={fit}
        />
      </div>
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
        aria-describedby="chart-keyboard-help"
        aria-label="Marketing Agents interactive organization chart"
        role="region"
        tabIndex={0}
        {...pointerHandlers}
      >
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
      </div>
    </section>
  );
}

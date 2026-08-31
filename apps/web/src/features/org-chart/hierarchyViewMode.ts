export const HIERARCHY_VIEW_BREAKPOINT_CSS_PX = 720;
export const HIERARCHY_VIEW_MEDIA_QUERY = `(max-width: ${String(HIERARCHY_VIEW_BREAKPOINT_CSS_PX)}px)`;

export type HierarchyViewMode = "graph" | "tree";
export type HierarchyViewOverride = HierarchyViewMode | null;

export function resolveHierarchyViewMode(
  isNarrow: boolean,
  explicitOverride: HierarchyViewOverride = null,
): HierarchyViewMode {
  return explicitOverride ?? (isNarrow ? "tree" : "graph");
}

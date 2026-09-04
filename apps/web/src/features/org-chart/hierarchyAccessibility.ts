import type { ProjectedHierarchy } from "./projectHierarchy";

type HierarchySummaryProjection = Pick<ProjectedHierarchy, "counts">;

function visibleCount(count: number, singular: string, plural: string): string {
  return `${String(count)} visible ${count === 1 ? singular : plural}`;
}

function describeVisibleHierarchy(
  projection: HierarchySummaryProjection,
): string {
  const { departments, functions, instances } = projection.counts;
  const departmentCount = visibleCount(
    departments,
    "department",
    "departments",
  );
  const functionCount = visibleCount(functions, "function", "functions");
  const instanceCount = visibleCount(
    instances,
    "deployed agent",
    "deployed agents",
  );
  return `${departmentCount}, ${functionCount}, and ${instanceCount}`;
}

export function describeGraphHierarchy(
  projection: HierarchySummaryProjection,
): string {
  const visibleHierarchy = describeVisibleHierarchy(projection);
  if (projection.counts.instances === 0) {
    return `Graph view shows ${visibleHierarchy}. No department headings, function headings, or agent controls are present. For complete level-by-level hierarchy navigation, use Tree view.`;
  }
  return `Graph view shows ${visibleHierarchy}. Department and function headings group the agent controls. For complete level-by-level hierarchy navigation, use Tree view.`;
}

export function describeTreeHierarchy(
  projection: HierarchySummaryProjection,
): string {
  const visibleHierarchy = describeVisibleHierarchy(projection);
  if (projection.counts.instances === 0) {
    return `Tree view has no matching hierarchy nodes: ${visibleHierarchy}. Adjust or clear filters to restore the complete level-by-level hierarchy navigation model.`;
  }
  return `Tree view is the complete level-by-level hierarchy navigation model for ${visibleHierarchy}.`;
}

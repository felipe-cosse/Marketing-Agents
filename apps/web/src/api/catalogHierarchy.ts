import { getJson } from "./client";
import { normalizeHierarchy } from "../features/org-chart/normalizeHierarchy";
import type { NormalizedHierarchy } from "../features/org-chart/model";

export const CATALOG_HIERARCHY_QUERY_KEY = ["catalog", "hierarchy"] as const;

export async function fetchCatalogHierarchy(
  signal?: AbortSignal,
): Promise<NormalizedHierarchy> {
  const response = await getJson("/api/v1/catalog/hierarchy", signal);
  return normalizeHierarchy(response);
}

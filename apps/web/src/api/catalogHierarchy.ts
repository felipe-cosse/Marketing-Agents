import { getJson } from "./client";
import { normalizeHierarchy } from "./normalizeCatalogHierarchy";
import type { NormalizedHierarchy } from "../contracts/catalogHierarchy";

export const CATALOG_HIERARCHY_QUERY_KEY = ["catalog", "hierarchy"] as const;

export async function fetchCatalogHierarchy(
  signal?: AbortSignal,
): Promise<NormalizedHierarchy> {
  const response = await getJson("/api/v1/catalog/hierarchy", signal);
  return normalizeHierarchy(response);
}

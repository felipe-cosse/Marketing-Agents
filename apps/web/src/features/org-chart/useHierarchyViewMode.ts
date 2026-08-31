import { useCallback, useState, useSyncExternalStore } from "react";

import {
  HIERARCHY_VIEW_MEDIA_QUERY,
  resolveHierarchyViewMode,
  type HierarchyViewMode,
  type HierarchyViewOverride,
} from "./hierarchyViewMode";

export interface HierarchyViewModeState {
  readonly mode: HierarchyViewMode;
  readonly isNarrow: boolean;
  readonly hasExplicitOverride: boolean;
  readonly setMode: (mode: HierarchyViewMode) => void;
  readonly resetMode: () => void;
}

function narrowViewportMediaQuery(): MediaQueryList | null {
  if (
    typeof window === "undefined" ||
    typeof window.matchMedia !== "function"
  ) {
    return null;
  }
  return window.matchMedia(HIERARCHY_VIEW_MEDIA_QUERY);
}

function subscribeToNarrowViewport(onStoreChange: () => void): () => void {
  const mediaQuery = narrowViewportMediaQuery();
  if (mediaQuery === null) return () => undefined;

  mediaQuery.addEventListener("change", onStoreChange);
  return () => mediaQuery.removeEventListener("change", onStoreChange);
}

function getNarrowViewportSnapshot(): boolean {
  return narrowViewportMediaQuery()?.matches ?? false;
}

function getServerNarrowViewportSnapshot(): boolean {
  return false;
}

export function useHierarchyViewMode(): HierarchyViewModeState {
  const isNarrow = useSyncExternalStore(
    subscribeToNarrowViewport,
    getNarrowViewportSnapshot,
    getServerNarrowViewportSnapshot,
  );
  const [explicitOverride, setExplicitOverride] =
    useState<HierarchyViewOverride>(null);
  const setMode = useCallback((mode: HierarchyViewMode) => {
    setExplicitOverride(mode);
  }, []);
  const resetMode = useCallback(() => {
    setExplicitOverride(null);
  }, []);

  return {
    mode: resolveHierarchyViewMode(isNarrow, explicitOverride),
    isNarrow,
    hasExplicitOverride: explicitOverride !== null,
    setMode,
    resetMode,
  };
}

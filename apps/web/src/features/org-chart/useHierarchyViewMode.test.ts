import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HIERARCHY_VIEW_MEDIA_QUERY } from "./hierarchyViewMode";
import { useHierarchyViewMode } from "./useHierarchyViewMode";

interface MatchMediaController {
  readonly matchMedia: (query: string) => MediaQueryList;
  readonly setMatches: (matches: boolean) => void;
}

function createMatchMediaController(
  initialMatches: boolean,
): MatchMediaController {
  let matches = initialMatches;
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  const mediaQueryList = {
    get matches() {
      return matches;
    },
    media: HIERARCHY_VIEW_MEDIA_QUERY,
    onchange: null,
    addEventListener: (
      type: string,
      listener: (event: MediaQueryListEvent) => void,
    ) => {
      if (type === "change") listeners.add(listener);
    },
    removeEventListener: (
      type: string,
      listener: (event: MediaQueryListEvent) => void,
    ) => {
      if (type === "change") listeners.delete(listener);
    },
  } as unknown as MediaQueryList;

  return {
    matchMedia: vi.fn(() => mediaQueryList),
    setMatches(nextMatches: boolean) {
      matches = nextMatches;
      const event = {
        matches,
        media: HIERARCHY_VIEW_MEDIA_QUERY,
      } as MediaQueryListEvent;
      for (const listener of listeners) listener(event);
    },
  };
}

function installMatchMedia(controller: MatchMediaController): void {
  vi.stubGlobal("matchMedia", controller.matchMedia);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("hierarchy view mode hook", () => {
  it("WEB-07 defaults safely to graph when matchMedia is unavailable", () => {
    const { result } = renderHook(() => useHierarchyViewMode());

    expect(result.current).toMatchObject({
      mode: "graph",
      isNarrow: false,
      hasExplicitOverride: false,
    });
  });

  it("WEB-07 selects tree automatically for a narrow viewport", () => {
    const controller = createMatchMediaController(true);
    installMatchMedia(controller);

    const { result } = renderHook(() => useHierarchyViewMode());

    expect(controller.matchMedia).toHaveBeenCalledWith(
      HIERARCHY_VIEW_MEDIA_QUERY,
    );
    expect(result.current).toMatchObject({
      mode: "tree",
      isNarrow: true,
      hasExplicitOverride: false,
    });
  });

  it("WEB-07 follows matchMedia changes while automatic mode remains active", () => {
    const controller = createMatchMediaController(false);
    installMatchMedia(controller);
    const { result } = renderHook(() => useHierarchyViewMode());

    act(() => controller.setMatches(true));
    expect(result.current).toMatchObject({ mode: "tree", isNarrow: true });

    act(() => controller.setMatches(false));
    expect(result.current).toMatchObject({ mode: "graph", isNarrow: false });
  });

  it("WEB-07 retains an explicit override in component memory across viewport changes", () => {
    const controller = createMatchMediaController(false);
    installMatchMedia(controller);
    const { result } = renderHook(() => useHierarchyViewMode());

    act(() => result.current.setMode("tree"));
    expect(result.current).toMatchObject({
      mode: "tree",
      hasExplicitOverride: true,
    });

    act(() => controller.setMatches(true));
    act(() => controller.setMatches(false));
    expect(result.current).toMatchObject({ mode: "tree", isNarrow: false });

    act(() => result.current.resetMode());
    expect(result.current).toMatchObject({
      mode: "graph",
      hasExplicitOverride: false,
    });
  });
});

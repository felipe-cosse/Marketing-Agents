import { describe, expect, it } from "vitest";

import {
  HIERARCHY_VIEW_BREAKPOINT_CSS_PX,
  HIERARCHY_VIEW_MEDIA_QUERY,
  resolveHierarchyViewMode,
} from "./hierarchyViewMode";

describe("hierarchy view mode", () => {
  it("WEB-07 resolves automatic and explicit hierarchy view modes without browser state", () => {
    expect(HIERARCHY_VIEW_BREAKPOINT_CSS_PX).toBe(720);
    expect(HIERARCHY_VIEW_MEDIA_QUERY).toBe("(max-width: 720px)");
    expect(resolveHierarchyViewMode(false)).toBe("graph");
    expect(resolveHierarchyViewMode(true)).toBe("tree");
    expect(resolveHierarchyViewMode(false, "tree")).toBe("tree");
    expect(resolveHierarchyViewMode(true, "graph")).toBe("graph");
  });
});

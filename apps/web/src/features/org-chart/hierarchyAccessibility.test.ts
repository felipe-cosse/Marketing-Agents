import { describe, expect, it } from "vitest";

import {
  describeGraphHierarchy,
  describeTreeHierarchy,
} from "./hierarchyAccessibility";

function projectionCounts(
  departments: number,
  functions: number,
  instances: number,
) {
  return {
    counts: {
      departments,
      functions,
      templates: instances,
      instances,
    },
  };
}

describe("ARCH-02 hierarchy accessibility summaries", () => {
  it.each([
    {
      label: "the full projection",
      projection: projectionCounts(5, 12, 43),
      visibleCounts:
        "5 visible departments, 12 visible functions, and 43 visible deployed agents",
    },
    {
      label: "a singular filtered projection",
      projection: projectionCounts(1, 1, 1),
      visibleCounts:
        "1 visible department, 1 visible function, and 1 visible deployed agent",
    },
    {
      label: "an empty projection",
      projection: projectionCounts(0, 0, 0),
      visibleCounts:
        "0 visible departments, 0 visible functions, and 0 visible deployed agents",
    },
  ])(
    "describes $label with projection-aware counts",
    ({ projection, visibleCounts }) => {
      expect(describeGraphHierarchy(projection)).toContain(visibleCounts);
      expect(describeTreeHierarchy(projection)).toContain(visibleCounts);
    },
  );

  it("points from Graph view to Tree view for complete hierarchy navigation", () => {
    const summary = describeGraphHierarchy(projectionCounts(5, 12, 43));

    expect(summary).toContain(
      "For complete level-by-level hierarchy navigation, use Tree view.",
    );
    expect(summary).not.toMatch(/Graph view is the complete/i);
  });

  it("identifies Tree view as the complete hierarchy navigation model", () => {
    expect(describeTreeHierarchy(projectionCounts(5, 12, 43))).toContain(
      "Tree view is the complete level-by-level hierarchy navigation model",
    );
  });

  it("describes the empty graph and Tree states without inventing rendered structure", () => {
    const empty = projectionCounts(0, 0, 0);

    expect(describeGraphHierarchy(empty)).toContain(
      "No department headings, function headings, or agent controls are present.",
    );
    expect(describeTreeHierarchy(empty)).toBe(
      "Tree view has no matching hierarchy nodes: 0 visible departments, 0 visible functions, and 0 visible deployed agents. Adjust or clear filters to restore the complete level-by-level hierarchy navigation model.",
    );
  });
});

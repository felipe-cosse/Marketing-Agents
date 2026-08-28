import { describe, expect, it } from "vitest";

import { makeHierarchyPayload } from "../../test/hierarchyFixture";
import { DEFAULT_ORG_CHART_FILTERS, type OrgChartFilters } from "./filters";
import { normalizeHierarchy } from "./normalizeHierarchy";
import { projectHierarchy } from "./projectHierarchy";

function filters(changes: Partial<OrgChartFilters>): OrgChartFilters {
  return { ...DEFAULT_ORG_CHART_FILTERS, ...changes };
}

function customizedHierarchy() {
  const payload = makeHierarchyPayload();
  const departments = payload.departments as {
    functions: { instances: Record<string, unknown>[] }[];
  }[];
  const first = departments[0]?.functions[0]?.instances[0];
  const second = departments[0]?.functions[0]?.instances[1];
  if (first === undefined || second === undefined)
    throw new Error("fixture missing");
  first.displayName = "Café Planner";
  first.purpose = "Neutral configured system; the source chart names VendorX.";
  first.enabled = false;
  second.capabilitySummaries = [
    {
      id: "cap.unique.publish",
      displayName: "Publish Brief",
      connectorFamily: "local",
      effect: "write",
    },
  ];
  return normalizeHierarchy(payload);
}

describe("WEB-02 hierarchy projection", () => {
  const hierarchy = customizedHierarchy();
  const firstId = "inst.social-media.new-content.agent-1.01";
  const secondId = "inst.social-media.new-content.agent-2.01";

  it.each([
    ["display name", "cafe\u0301 planner", firstId],
    ["presented purpose", "neutral configured system", firstId],
    ["instance ID", firstId, firstId],
    ["template ID", "tpl.social-media.new-content.agent-2", secondId],
    ["capability label", "publish brief", secondId],
  ])("searches %s with Unicode normalization", (_label, q, expectedId) => {
    const result = projectHierarchy(hierarchy, filters({ q }));
    expect([...result.visibleInstanceIds]).toEqual([expectedId]);
  });

  it("never searches raw source-vendor evidence removed by presentation", () => {
    expect(
      projectHierarchy(hierarchy, filters({ q: "VendorX" }))
        .matchedInstanceCount,
    ).toBe(0);
  });

  it("applies department, function, deployment, run state, and capability with AND semantics", () => {
    const statuses = new Map([[secondId, "completed" as const]]);
    const result = projectHierarchy(
      hierarchy,
      filters({
        departmentId: "dept.social-media",
        functionId: "func.social-media.new-content",
        deployment: "enabled",
        runState: "completed",
        capabilityId: "cap.unique.publish",
      }),
      statuses,
    );
    expect([...result.visibleInstanceIds]).toEqual([secondId]);
    expect(result.hierarchy.counts).toEqual({
      departments: 1,
      functions: 1,
      templates: 1,
      instances: 1,
    });
  });

  it("retains only matching ancestors and exact source order", () => {
    const result = projectHierarchy(
      hierarchy,
      filters({ capabilityId: "cap.community.read" }),
    );
    expect(result.hierarchy.departments.map(({ id }) => id)).toEqual([
      "dept.community",
    ]);
    expect(
      result.hierarchy.departments[0]?.functions.map(({ id }) => id),
    ).toEqual([
      "func.community.events",
      "func.community.education",
      "func.community.discussion",
    ]);
    expect(result.matchedInstanceCount).toBe(14);
  });

  it("does not treat a missing runtime status as never run and supports empty results", () => {
    const result = projectHierarchy(
      hierarchy,
      filters({ runState: "never_run" }),
      new Map(),
    );
    expect(result.matchedInstanceCount).toBe(0);
    expect(result.hierarchy.departments).toEqual([]);
  });

  it("preserves full hierarchy identity by default", () => {
    expect(
      projectHierarchy(hierarchy, DEFAULT_ORG_CHART_FILTERS).hierarchy,
    ).toBe(hierarchy);
  });

  it("changes structural key only when status affects active-filter membership", () => {
    const noRunFilterA = projectHierarchy(
      hierarchy,
      filters({ q: "Agent" }),
      new Map([[secondId, "received"]]),
    );
    const noRunFilterB = projectHierarchy(
      hierarchy,
      filters({ q: "Agent" }),
      new Map([[secondId, "completed"]]),
    );
    expect(noRunFilterA.hierarchy.structuralKey).toBe(
      noRunFilterB.hierarchy.structuralKey,
    );

    const activeA = projectHierarchy(
      hierarchy,
      filters({ runState: "completed" }),
      new Map([[secondId, "received"]]),
    );
    const activeB = projectHierarchy(
      hierarchy,
      filters({ runState: "completed" }),
      new Map([[secondId, "completed"]]),
    );
    expect(activeA.hierarchy.structuralKey).not.toBe(
      activeB.hierarchy.structuralKey,
    );
  });
});

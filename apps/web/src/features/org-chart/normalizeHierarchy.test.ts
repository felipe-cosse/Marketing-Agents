// WEB-01 proves that API data normalizes to one exact source-authoritative hierarchy.
import { describe, expect, it } from "vitest";

import { makeHierarchyPayload } from "../../test/hierarchyFixture";
import {
  HierarchyContractError,
  normalizeHierarchy,
} from "./normalizeHierarchy";

describe("WEB-01 hierarchy normalization", () => {
  it("accepts and freezes the exact 5/12/36/43 contract", () => {
    const hierarchy = normalizeHierarchy(makeHierarchyPayload());

    expect(hierarchy.counts).toEqual({
      departments: 5,
      functions: 12,
      templates: 36,
      instances: 43,
    });
    expect(
      hierarchy.departments.map((department) => department.displayName),
    ).toEqual([
      "Social media",
      "Blog & SEO",
      "Email",
      "Community",
      "Partnerships",
    ]);
    expect(
      hierarchy.departments.map((department) => department.instanceCount),
    ).toEqual([12, 6, 5, 14, 6]);
    expect(
      hierarchy.departments.map((department) => department.functions.length),
    ).toEqual([3, 2, 2, 3, 2]);
    expect(Object.isFrozen(hierarchy)).toBe(true);
  });

  it("keeps seven independently addressable Community template pairs", () => {
    const hierarchy = normalizeHierarchy(makeHierarchyPayload());
    const community = hierarchy.departments[3];
    expect(community).toBeDefined();
    const instances = community?.functions.flatMap(
      (agentFunction) => agentFunction.instances,
    );
    expect(instances).toHaveLength(14);
    expect(
      new Set(instances?.map((instance) => instance.templateId)),
    ).toHaveLength(7);

    const pairs = new Map<string, NonNullable<typeof instances>>();
    for (const instance of instances ?? []) {
      const pair = pairs.get(instance.templateId) ?? [];
      pair.push(instance);
      pairs.set(instance.templateId, pair);
    }
    for (const pair of pairs.values()) {
      expect(pair.map((instance) => instance.sourceOrdinal)).toEqual([1, 2]);
      expect(pair.map((instance) => instance.id)).toHaveLength(2);
      expect(new Set(pair.map((instance) => instance.id)).size).toBe(2);
    }
  });

  it("restores display-order authority when transport arrays are shuffled", () => {
    const payload = makeHierarchyPayload();
    const departments = payload.departments as {
      functions: { instances: unknown[] }[];
    }[];
    departments.reverse();
    for (const department of departments) {
      department.functions.reverse();
      for (const agentFunction of department.functions)
        agentFunction.instances.reverse();
    }

    const hierarchy = normalizeHierarchy(payload);
    expect(hierarchy.departments[0]?.displayName).toBe("Social media");
    expect(hierarchy.departments[3]?.displayName).toBe("Community");
    expect(
      hierarchy.departments[3]?.functions[0]?.instances[0]?.sourceOrdinal,
    ).toBe(1);
  });

  it("keeps layout identity independent from presentation metadata", () => {
    const original = makeHierarchyPayload();
    const changed = structuredClone(original);
    const first = (
      changed.departments as {
        displayName: string;
        functions: {
          instances: { purpose: string; enabled: boolean }[];
        }[];
      }[]
    )[0];
    expect(first).toBeDefined();
    if (first !== undefined) {
      first.displayName = "Renamed label";
      const instance = first.functions[0]?.instances[0];
      if (instance !== undefined) {
        instance.purpose = "Changed status-only presentation";
        instance.enabled = false;
      }
    }

    expect(normalizeHierarchy(changed).structuralKey).toBe(
      normalizeHierarchy(original).structuralKey,
    );
  });

  it.each([
    [
      "count drift",
      (payload: Record<string, unknown>) => {
        (payload.counts as { instances: number }).instances = 42;
      },
    ],
    [
      "duplicate IDs",
      (payload: Record<string, unknown>) => {
        const departments = payload.departments as {
          functions: { instances: { id: string }[] }[];
        }[];
        const first = departments[0]?.functions[0]?.instances[0];
        const second = departments[0]?.functions[0]?.instances[1];
        if (first !== undefined && second !== undefined) second.id = first.id;
      },
    ],
    [
      "duplicate display order",
      (payload: Record<string, unknown>) => {
        const departments = payload.departments as {
          displayOrder: number;
        }[];
        const first = departments[0];
        const second = departments[1];
        if (first !== undefined && second !== undefined)
          second.displayOrder = first.displayOrder;
      },
    ],
    [
      "broken Community ordinal",
      (payload: Record<string, unknown>) => {
        const departments = payload.departments as {
          functions: { instances: { sourceOrdinal: number }[] }[];
        }[];
        const second = departments[3]?.functions[0]?.instances[1];
        if (second !== undefined) second.sourceOrdinal = 3;
      },
    ],
  ])("rejects %s", (_label, mutate) => {
    const payload = makeHierarchyPayload();
    mutate(payload);
    expect(() => normalizeHierarchy(payload)).toThrow(HierarchyContractError);
  });
});

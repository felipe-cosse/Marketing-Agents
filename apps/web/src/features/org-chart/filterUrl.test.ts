import { describe, expect, it } from "vitest";

import { makeHierarchyPayload } from "../../test/hierarchyFixture";
import { normalizeHierarchy } from "./normalizeHierarchy";
import { parseOrgChartFilters, serializeOrgChartFilters } from "./filterUrl";

describe("WEB-02 canonical filter URL", () => {
  const hierarchy = normalizeHierarchy(makeHierarchyPayload());

  it("cleans Unicode whitespace and serializes supported fields in fixed order", () => {
    const filters = parseOrgChartFilters(
      "?capability=cap.email.read&run=completed&deployment=enabled&function=func.email.newsletter&department=dept.email&q=%EF%BC%A1%C2%A0%20agent",
      hierarchy,
    );
    expect(filters.q).toBe("A agent");
    expect(serializeOrgChartFilters(filters)).toBe(
      "?q=A+agent&department=dept.email&function=func.email.newsletter&deployment=enabled&run=completed&capability=cap.email.read",
    );
  });

  it("rejects repeated scalars, invalid IDs, and a function outside its department", () => {
    expect(
      parseOrgChartFilters(
        "?q=one&q=two&department=dept.email&function=func.community.events&deployment=maybe&run=unknown&capability=cap.missing",
        hierarchy,
      ),
    ).toEqual({
      q: "",
      departmentId: "dept.email",
      functionId: null,
      deployment: null,
      runState: null,
      capabilityId: null,
    });
  });

  it("fails closed for oversized search and total query inputs", () => {
    expect(
      parseOrgChartFilters(
        `?q=${"x".repeat(257)}&department=dept.email`,
        hierarchy,
      ).q,
    ).toBe("");
    expect(parseOrgChartFilters(`?q=${"x".repeat(2049)}`, hierarchy)).toEqual({
      q: "",
      departmentId: null,
      functionId: null,
      deployment: null,
      runState: null,
      capabilityId: null,
    });
  });
});

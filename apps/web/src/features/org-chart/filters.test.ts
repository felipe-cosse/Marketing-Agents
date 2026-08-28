import { describe, expect, it } from "vitest";

import { makeHierarchyPayload } from "../../test/hierarchyFixture";
import { normalizeHierarchy } from "./normalizeHierarchy";
import { deriveFilterOptions } from "./filters";

describe("WEB-02 filter options", () => {
  it("derives departments, constrained functions, and first-seen capabilities in source order", () => {
    const options = deriveFilterOptions(
      normalizeHierarchy(makeHierarchyPayload()),
    );
    expect(options.departments.map(({ id }) => id)).toEqual([
      "dept.social-media",
      "dept.blog-seo",
      "dept.email",
      "dept.community",
      "dept.partnerships",
    ]);
    expect(
      options.functionsByDepartment.get("dept.email")?.map(({ id }) => id),
    ).toEqual(["func.email.newsletter", "func.email.lifecycle-marketing"]);
    expect(options.capabilities.map(({ id }) => id)).toEqual([
      "cap.social-media.read",
      "cap.blog-seo.read",
      "cap.email.read",
      "cap.community.read",
      "cap.partnerships.read",
    ]);
  });
});

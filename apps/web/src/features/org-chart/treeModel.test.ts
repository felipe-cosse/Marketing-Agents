import { describe, expect, it } from "vitest";

import { makeHierarchyPayload } from "../../test/hierarchyFixture";
import { DEFAULT_ORG_CHART_FILTERS } from "./filters";
import { normalizeHierarchy } from "./normalizeHierarchy";
import { projectHierarchy } from "./projectHierarchy";
import {
  buildOrgTreeModel,
  findOrgTreeTypeaheadTarget,
  getOrgTreeNavigationTarget,
  getVisibleOrgTreeNodes,
  ORG_TREE_ROOT_ID,
} from "./treeModel";

function fullHierarchy() {
  return normalizeHierarchy(makeHierarchyPayload());
}

describe("WEB-07 semantic organization tree model", () => {
  it("WEB-07 creates the exact 1/5/12/43 source-ordered tree with stable ARIA metadata", () => {
    const hierarchy = fullHierarchy();
    const model = buildOrgTreeModel(hierarchy);

    expect(model.nodes).toHaveLength(61);
    expect(
      model.nodes.reduce<Record<string, number>>((counts, node) => {
        counts[node.kind] = (counts[node.kind] ?? 0) + 1;
        return counts;
      }, {}),
    ).toEqual({ root: 1, department: 5, function: 12, instance: 43 });
    expect(model.nodes.slice(0, 5).map(({ id }) => id)).toEqual([
      ORG_TREE_ROOT_ID,
      "dept.social-media",
      "func.social-media.new-content",
      "inst.social-media.new-content.agent-1.01",
      "inst.social-media.new-content.agent-2.01",
    ]);

    const firstDepartment = model.nodeById.get("dept.social-media");
    const firstFunction = model.nodeById.get("func.social-media.new-content");
    const lastInstance = model.nodeById.get(
      "inst.partnerships.integration-partners.agent-1.01",
    );
    expect(firstDepartment).toMatchObject({
      parentId: ORG_TREE_ROOT_ID,
      level: 2,
      posInSet: 1,
      setSize: 5,
      expandable: true,
    });
    expect(firstFunction).toMatchObject({
      parentId: "dept.social-media",
      level: 3,
      posInSet: 1,
      setSize: 3,
      expandable: true,
    });
    expect(lastInstance).toMatchObject({
      parentId: "func.partnerships.integration-partners",
      level: 4,
      posInSet: 1,
      setSize: 1,
      expandable: false,
    });

    const rebuilt = buildOrgTreeModel(hierarchy);
    expect(rebuilt.nodes.map(({ id }) => id)).toEqual(
      model.nodes.map(({ id }) => id),
    );
  });

  it("WEB-07 defaults to root and department expansion and fully expands to 61 visible nodes", () => {
    const model = buildOrgTreeModel(fullHierarchy());
    const initiallyVisible = getVisibleOrgTreeNodes(
      model,
      model.defaultExpandedIds,
    );

    expect([...model.defaultExpandedIds]).toEqual([
      ORG_TREE_ROOT_ID,
      "dept.social-media",
      "dept.blog-seo",
      "dept.email",
      "dept.community",
      "dept.partnerships",
    ]);
    expect(initiallyVisible).toHaveLength(18);
    expect(initiallyVisible.at(-1)).toMatchObject({
      id: "func.partnerships.integration-partners",
      level: 3,
    });

    const everyBranchExpanded = new Set(
      model.nodes.filter(({ expandable }) => expandable).map(({ id }) => id),
    );
    expect(getVisibleOrgTreeNodes(model, everyBranchExpanded)).toHaveLength(61);
  });

  it("WEB-07 rebuilds filtered projections with only their retained ancestor chain", () => {
    const hierarchy = fullHierarchy();
    const projection = projectHierarchy(hierarchy, {
      ...DEFAULT_ORG_CHART_FILTERS,
      q: "Agent 3",
      departmentId: "dept.community",
      functionId: "func.community.education",
    });
    const model = buildOrgTreeModel(projection.hierarchy);
    const expanded = new Set(
      model.nodes.filter(({ expandable }) => expandable).map(({ id }) => id),
    );

    expect(getVisibleOrgTreeNodes(model, expanded).map(({ id }) => id)).toEqual(
      [
        ORG_TREE_ROOT_ID,
        "dept.community",
        "func.community.education",
        "inst.community.education.agent-3.01",
        "inst.community.education.agent-3.02",
      ],
    );
    expect(model.nodeById.get("dept.community")).toMatchObject({
      posInSet: 1,
      setSize: 1,
    });
    expect(model.nodeById.get("func.community.education")).toMatchObject({
      posInSet: 1,
      setSize: 1,
    });
  });

  it("WEB-07 resolves previous next parent child Home and End navigation targets", () => {
    const model = buildOrgTreeModel(fullHierarchy());
    const expanded = new Set(
      model.nodes.filter(({ expandable }) => expandable).map(({ id }) => id),
    );
    const visible = getVisibleOrgTreeNodes(model, expanded);
    const firstDepartment = "dept.social-media";
    const firstFunction = "func.social-media.new-content";
    const firstInstance = "inst.social-media.new-content.agent-1.01";

    expect(
      getOrgTreeNavigationTarget(visible, ORG_TREE_ROOT_ID, "previous"),
    ).toBeNull();
    expect(getOrgTreeNavigationTarget(visible, ORG_TREE_ROOT_ID, "next")).toBe(
      firstDepartment,
    );
    expect(
      getOrgTreeNavigationTarget(visible, ORG_TREE_ROOT_ID, "first-child"),
    ).toBe(firstDepartment);
    expect(
      getOrgTreeNavigationTarget(visible, firstDepartment, "first-child"),
    ).toBe(firstFunction);
    expect(getOrgTreeNavigationTarget(visible, firstInstance, "parent")).toBe(
      firstFunction,
    );
    expect(getOrgTreeNavigationTarget(visible, firstInstance, "previous")).toBe(
      firstFunction,
    );
    expect(getOrgTreeNavigationTarget(visible, firstInstance, "home")).toBe(
      ORG_TREE_ROOT_ID,
    );
    expect(getOrgTreeNavigationTarget(visible, firstInstance, "end")).toBe(
      "inst.partnerships.integration-partners.agent-1.01",
    );
  });

  it("WEB-07 wraps normalized printable typeahead through visible source order", () => {
    const payload = makeHierarchyPayload();
    const departments = payload.departments as { displayName: string }[];
    const firstDepartment = departments[0];
    if (firstDepartment === undefined) throw new Error("fixture missing");
    firstDepartment.displayName = "Caf\u00e9 social media";
    const model = buildOrgTreeModel(normalizeHierarchy(payload));
    const visible = getVisibleOrgTreeNodes(model, model.defaultExpandedIds);
    const lastVisibleId = visible.at(-1)?.id ?? ORG_TREE_ROOT_ID;

    expect(
      findOrgTreeTypeaheadTarget(visible, lastVisibleId, "cafe\u0301"),
    ).toBe("dept.social-media");
    expect(
      findOrgTreeTypeaheadTarget(visible, "dept.social-media", "community"),
    ).toBe("dept.community");
    expect(
      findOrgTreeTypeaheadTarget(visible, "dept.community", "marketing"),
    ).toBe(ORG_TREE_ROOT_ID);
    expect(
      findOrgTreeTypeaheadTarget(visible, ORG_TREE_ROOT_ID, "missing"),
    ).toBeNull();
  });
});

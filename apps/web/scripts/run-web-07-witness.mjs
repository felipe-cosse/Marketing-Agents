// WEB-07 dependency-free witness executes the production tree and responsive-mode boundaries.
import "./require-pinned-node.mjs";

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { registerHooks } from "node:module";

registerHooks({
  resolve(specifier, context, nextResolve) {
    try {
      return nextResolve(specifier, context);
    } catch (error) {
      if (specifier.startsWith(".") && !specifier.endsWith(".ts")) {
        return nextResolve(`${specifier}.ts`, context);
      }
      throw error;
    }
  },
});

const [treeModule, viewModeModule] = await Promise.all([
  import("../src/features/org-chart/treeModel.ts"),
  import("../src/features/org-chart/hierarchyViewMode.ts"),
]);
const {
  ORG_TREE_ROOT_ID,
  buildOrgTreeModel,
  findOrgTreeTypeaheadTarget,
  getOrgTreeNavigationTarget,
  getVisibleOrgTreeNodes,
} = treeModule;
const {
  HIERARCHY_VIEW_BREAKPOINT_CSS_PX,
  HIERARCHY_VIEW_MEDIA_QUERY,
  resolveHierarchyViewMode,
} = viewModeModule;

const functionInstanceCounts = [6, 2, 4, 3, 3, 2, 3, 6, 6, 2, 5, 1];
const departmentFunctionCounts = [3, 2, 2, 3, 2];
const departmentNames = [
  "Social media",
  "Blog & SEO",
  "Email",
  "Community",
  "Partnerships",
];
let functionIndex = 0;
let instanceIndex = 0;
const departments = departmentFunctionCounts.map(
  (departmentFunctionCount, departmentIndex) => {
    const departmentId = `dept.web-07-${String(departmentIndex + 1)}`;
    const functions = Array.from(
      { length: departmentFunctionCount },
      (_, departmentFunctionIndex) => {
        const instanceCount = functionInstanceCounts[functionIndex];
        assert.notEqual(instanceCount, undefined);
        functionIndex += 1;
        const functionId = `${departmentId}.function-${String(departmentFunctionIndex + 1)}`;
        const instances = Array.from(
          { length: instanceCount },
          (_, ordinal) => {
            instanceIndex += 1;
            return {
              id: `${functionId}.instance-${String(ordinal + 1)}`,
              templateId: `tpl.web-07-${String(instanceIndex)}`,
              displayName: `Agent ${String(instanceIndex)}`,
              purpose: `Witness purpose ${String(instanceIndex)}`,
              displayOrder: ordinal + 1,
              enabled: true,
              operationClassification: "read_only",
              triggerTypes: ["manual"],
              capabilitySummaries: [],
              sourceOrdinal: 1,
              deploymentCount: 1,
            };
          },
        );
        return {
          id: functionId,
          displayName: `Function ${String(functionIndex)}`,
          displayOrder: departmentFunctionIndex + 1,
          instances,
        };
      },
    );
    return {
      id: departmentId,
      displayName: departmentNames[departmentIndex],
      displayOrder: departmentIndex + 1,
      instanceCount: functions.reduce(
        (total, agentFunction) => total + agentFunction.instances.length,
        0,
      ),
      templateCount: functions.reduce(
        (total, agentFunction) => total + agentFunction.instances.length,
        0,
      ),
      functions,
    };
  },
);
const hierarchy = {
  catalogVersion: "web-07-witness",
  catalogHash: "7".repeat(64),
  counts: { departments: 5, functions: 12, templates: 36, instances: 43 },
  departments,
  structuralKey: "web-07-witness-structure",
};

assert.equal(functionIndex, 12);
assert.equal(instanceIndex, 43);

const tree = buildOrgTreeModel(hierarchy);
assert.equal(tree.nodes.length, 61);
assert.deepEqual(
  tree.nodes.reduce((counts, node) => {
    counts[node.kind] = (counts[node.kind] ?? 0) + 1;
    return counts;
  }, {}),
  { root: 1, department: 5, function: 12, instance: 43 },
);
assert.deepEqual(
  [...tree.defaultExpandedIds],
  [ORG_TREE_ROOT_ID, ...departments.map(({ id }) => id)],
);
assert.equal(getVisibleOrgTreeNodes(tree, tree.defaultExpandedIds).length, 18);

const everyBranchExpanded = new Set(
  tree.nodes.filter(({ expandable }) => expandable).map(({ id }) => id),
);
const visible = getVisibleOrgTreeNodes(tree, everyBranchExpanded);
assert.equal(visible.length, 61);
const firstDepartmentId = departments[0].id;
const firstFunctionId = departments[0].functions[0].id;
const firstInstanceId = departments[0].functions[0].instances[0].id;
assert.deepEqual(tree.nodeById.get(firstDepartmentId), {
  ...tree.nodeById.get(firstDepartmentId),
  parentId: ORG_TREE_ROOT_ID,
  level: 2,
  posInSet: 1,
  setSize: 5,
});
assert.equal(
  getOrgTreeNavigationTarget(visible, ORG_TREE_ROOT_ID, "first-child"),
  firstDepartmentId,
);
assert.equal(
  getOrgTreeNavigationTarget(visible, firstDepartmentId, "first-child"),
  firstFunctionId,
);
assert.equal(
  getOrgTreeNavigationTarget(visible, firstInstanceId, "parent"),
  firstFunctionId,
);
assert.equal(
  getOrgTreeNavigationTarget(visible, firstInstanceId, "home"),
  ORG_TREE_ROOT_ID,
);
assert.equal(
  getOrgTreeNavigationTarget(visible, firstInstanceId, "end"),
  visible.at(-1).id,
);
assert.equal(
  findOrgTreeTypeaheadTarget(visible, ORG_TREE_ROOT_ID, "email"),
  departments[2].id,
);

assert.equal(HIERARCHY_VIEW_BREAKPOINT_CSS_PX, 720);
assert.equal(HIERARCHY_VIEW_MEDIA_QUERY, "(max-width: 720px)");
assert.equal(resolveHierarchyViewMode(false), "graph");
assert.equal(resolveHierarchyViewMode(true), "tree");
assert.equal(resolveHierarchyViewMode(false, "tree"), "tree");
assert.equal(resolveHierarchyViewMode(true, "graph"), "graph");

const [treeComponentSource, chartPageSource, viewHookSource] =
  await Promise.all([
    readFile(
      new URL("../src/features/org-chart/OrgTreeFallback.tsx", import.meta.url),
      "utf8",
    ),
    readFile(
      new URL("../src/features/org-chart/OrgChartPage.tsx", import.meta.url),
      "utf8",
    ),
    readFile(
      new URL(
        "../src/features/org-chart/useHierarchyViewMode.ts",
        import.meta.url,
      ),
      "utf8",
    ),
  ]);
for (const productionBoundary of [
  "buildOrgTreeModel",
  "getVisibleOrgTreeNodes",
  "getOrgTreeNavigationTarget",
  "findOrgTreeTypeaheadTarget",
  'role="tree"',
  'role="treeitem"',
  "aria-level",
  "aria-posinset",
  "aria-setsize",
]) {
  assert.ok(
    treeComponentSource.includes(productionBoundary),
    `OrgTreeFallback must retain ${productionBoundary}`,
  );
}
for (const productionBoundary of [
  "useHierarchyViewMode",
  "OrgTreeFallback",
  'hierarchyView.mode === "graph"',
  "HierarchyViewToggle",
]) {
  assert.ok(
    chartPageSource.includes(productionBoundary),
    `OrgChartPage must retain ${productionBoundary}`,
  );
}
assert.ok(viewHookSource.includes("resolveHierarchyViewMode"));
assert.ok(viewHookSource.includes("useSyncExternalStore"));

process.stdout.write(
  `WEB-07 dependency-free witness passed: Node ${process.versions.node}, exact 1/5/12/43 semantic tree, navigation, typeahead, responsive mode, and production integration.\n`,
);

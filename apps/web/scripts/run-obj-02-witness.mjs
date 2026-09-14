// OBJ-02 executes source-identity validation and both hierarchy projections without dependencies.
import "./require-pinned-node.mjs";

import assert from "node:assert/strict";
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

const { normalizeHierarchy, HierarchyContractError } =
  await import("../src/api/normalizeCatalogHierarchy.ts");
const { makeHierarchyPayload } =
  await import("../src/test/hierarchyFixture.ts");
const { layoutHierarchy } = await import("../src/features/org-chart/layout.ts");
const { buildOrgTreeModel } =
  await import("../src/features/org-chart/treeModel.ts");

const payload = makeHierarchyPayload();
const hierarchy = normalizeHierarchy(payload);
assert.deepEqual(hierarchy.counts, {
  departments: 5,
  functions: 12,
  templates: 36,
  instances: 43,
});
assert.equal(layoutHierarchy(hierarchy).instanceById.size, 43);
const tree = buildOrgTreeModel(hierarchy);
assert.equal(tree.nodes.length, 61);
assert.deepEqual(
  tree.nodes.filter((node) => node.kind === "instance").map((node) => node.id),
  hierarchy.departments.flatMap((department) =>
    department.functions.flatMap((agentFunction) =>
      agentFunction.instances.map(({ id }) => id),
    ),
  ),
);

for (const [label, mutate] of [
  [
    "unsupported singleton ordinal",
    (body) => {
      const instance = body.departments[0].functions[0].instances[0];
      instance.sourceOrdinal = 2;
      instance.id = instance.id.replace(/\.01$/u, ".02");
    },
  ],
  [
    "mismatched instance identity",
    (body) => {
      body.departments[0].functions[0].instances[0].id += "-invented";
    },
  ],
  [
    "cross-function role",
    (body) => {
      const instance = body.departments[0].functions[0].instances[0];
      instance.templateId = "tpl.social-media.research.misplaced";
      instance.id = "inst.social-media.research.misplaced.01";
    },
  ],
  [
    "unknown source function",
    (body) => {
      body.departments[0].functions[0].id = "func.social-media.invented";
    },
  ],
  [
    "unknown source department",
    (body) => {
      body.departments[0].id = "dept.invented";
      body.departmentCounts[0].departmentId = "dept.invented";
    },
  ],
]) {
  const invalid = structuredClone(payload);
  mutate(invalid);
  assert.deepEqual(invalid.counts, payload.counts);
  assert.throws(
    () => normalizeHierarchy(invalid),
    HierarchyContractError,
    label,
  );
}

process.stdout.write(
  "OBJ-02 passed: exact graph/tree identity projection and count-preserving identity rejection.\n",
);

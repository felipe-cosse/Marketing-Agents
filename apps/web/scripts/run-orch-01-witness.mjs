// ORCH-01 dependency-free witness executes the production identity and hierarchy boundaries.
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

const [modelModule, normalizationModule, treeModule, fixtureModule] =
  await Promise.all([
    import("../src/features/org-chart/model.ts"),
    import("../src/features/org-chart/normalizeHierarchy.ts"),
    import("../src/features/org-chart/treeModel.ts"),
    import("../src/test/hierarchyFixture.ts"),
  ]);

const { MARKETING_AGENTS_ROOT, MARKETING_ORCHESTRATOR_CONTROL_PLANE } =
  modelModule;
const { HierarchyContractError, normalizeHierarchy } = normalizationModule;
const { buildOrgTreeModel } = treeModule;
const { makeHierarchyPayload } = fixtureModule;

assert.equal(Object.isFrozen(MARKETING_AGENTS_ROOT), true);
assert.deepEqual(MARKETING_AGENTS_ROOT, {
  id: "root",
  displayName: "Marketing Agents",
});
assert.equal(Object.isFrozen(MARKETING_ORCHESTRATOR_CONTROL_PLANE), true);
assert.deepEqual(MARKETING_ORCHESTRATOR_CONTROL_PLANE, {
  id: "control-plane.marketing-orchestrator",
  displayName: "Marketing Orchestrator",
  badgeLabel: "Control plane",
  countsAsInstance: false,
});

const hierarchy = normalizeHierarchy(makeHierarchyPayload());
const instanceIds = hierarchy.departments.flatMap((department) =>
  department.functions.flatMap((agentFunction) =>
    agentFunction.instances.map((instance) => instance.id),
  ),
);
assert.equal(instanceIds.length, 43);
assert.equal(
  instanceIds.includes(MARKETING_ORCHESTRATOR_CONTROL_PLANE.id),
  false,
);

const tree = buildOrgTreeModel(hierarchy);
assert.equal(tree.nodes.length, 61);
assert.deepEqual(
  tree.nodes.reduce((counts, node) => {
    counts[node.kind] = (counts[node.kind] ?? 0) + 1;
    return counts;
  }, {}),
  { root: 1, department: 5, function: 12, instance: 43 },
);
assert.equal(tree.rootId, MARKETING_AGENTS_ROOT.id);
assert.equal(
  tree.nodeById.get(tree.rootId)?.label,
  MARKETING_AGENTS_ROOT.displayName,
);
assert.equal(tree.nodeById.has(MARKETING_ORCHESTRATOR_CONTROL_PLANE.id), false);

for (const reservedId of [
  MARKETING_AGENTS_ROOT.id,
  MARKETING_ORCHESTRATOR_CONTROL_PLANE.id,
]) {
  for (const nodeKind of ["department", "function", "instance"]) {
    const rejectedPayload = makeHierarchyPayload();
    const department = rejectedPayload.departments[0];
    const agentFunction = department?.functions[0];
    const instance = agentFunction?.instances[0];
    assert.notEqual(department, undefined);
    assert.notEqual(agentFunction, undefined);
    assert.notEqual(instance, undefined);
    if (nodeKind === "department") department.id = reservedId;
    else if (nodeKind === "function") agentFunction.id = reservedId;
    else instance.id = reservedId;
    assert.throws(
      () => normalizeHierarchy(rejectedPayload),
      (error) =>
        error instanceof HierarchyContractError &&
        error.message.includes(`reserved UI identity "${reservedId}"`),
    );
  }
}

const [graphSource, treeSource, styleSource] = await Promise.all([
  readFile(
    new URL("../src/features/org-chart/HierarchyStage.tsx", import.meta.url),
    "utf8",
  ),
  readFile(
    new URL("../src/features/org-chart/OrgTreeFallback.tsx", import.meta.url),
    "utf8",
  ),
  readFile(new URL("../src/styles/org-chart.css", import.meta.url), "utf8"),
]);
for (const [label, source] of [
  ["graph", graphSource],
  ["tree", treeSource],
]) {
  for (const boundary of [
    "MARKETING_AGENTS_ROOT",
    "MARKETING_ORCHESTRATOR_CONTROL_PLANE",
    'data-node-kind="control-plane"',
    "data-control-plane-id",
    "data-counts-as-instance",
  ]) {
    assert.ok(source.includes(boundary), `${label} must retain ${boundary}`);
  }
}
for (const boundary of [
  "flex: 0 0 25px",
  ".root-node__content",
  "overflow-wrap: anywhere",
]) {
  assert.ok(
    styleSource.includes(boundary),
    `root style must retain ${boundary}`,
  );
}

process.stdout.write(
  `ORCH-01 dependency-free witness passed: Node ${process.versions.node}, one Marketing Agents root, one non-instance Marketing Orchestrator control plane, and exact 1/5/12/43 hierarchy.\n`,
);

// ARCH-02 dependency-free witness executes the production summary and source-binds its consumers.
import "./require-pinned-node.mjs";

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { registerHooks } from "node:module";
import { fileURLToPath } from "node:url";

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

const { describeGraphHierarchy, describeTreeHierarchy } =
  await import("../src/features/org-chart/hierarchyAccessibility.ts");

const projection = (departments, functions, instances) => ({
  counts: { departments, functions, templates: instances, instances },
});
assert.equal(
  describeGraphHierarchy(projection(5, 12, 43)),
  "Graph view shows 5 visible departments, 12 visible functions, and 43 visible deployed agents. Department and function headings group the agent controls. For complete level-by-level hierarchy navigation, use Tree view.",
);
assert.equal(
  describeGraphHierarchy(projection(1, 1, 1)),
  "Graph view shows 1 visible department, 1 visible function, and 1 visible deployed agent. Department and function headings group the agent controls. For complete level-by-level hierarchy navigation, use Tree view.",
);
assert.equal(
  describeGraphHierarchy(projection(0, 0, 0)),
  "Graph view shows 0 visible departments, 0 visible functions, and 0 visible deployed agents. No department headings, function headings, or agent controls are present. For complete level-by-level hierarchy navigation, use Tree view.",
);
assert.equal(
  describeTreeHierarchy(projection(0, 0, 0)),
  "Tree view has no matching hierarchy nodes: 0 visible departments, 0 visible functions, and 0 visible deployed agents. Adjust or clear filters to restore the complete level-by-level hierarchy navigation model.",
);

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(webRoot, "../..");
const readWeb = (relativePath) =>
  readFile(resolve(webRoot, relativePath), "utf8");

const [
  manifestText,
  lock,
  vite,
  typescript,
  entry,
  canvas,
  tree,
  card,
  stage,
  page,
  aggregateBrowserRunner,
] = await Promise.all([
  readWeb("package.json"),
  readFile(resolve(repositoryRoot, "pnpm-lock.yaml"), "utf8"),
  readWeb("vite.config.ts"),
  readWeb("tsconfig.app.json"),
  readWeb("src/main.tsx"),
  readWeb("src/features/org-chart/OrgChartCanvas.tsx"),
  readWeb("src/features/org-chart/OrgTreeFallback.tsx"),
  readWeb("src/features/org-chart/AgentCard.tsx"),
  readWeb("src/features/org-chart/HierarchyStage.tsx"),
  readWeb("src/features/org-chart/OrgChartPage.tsx"),
  readWeb("scripts/run-web-e2e.mjs"),
]);

const manifest = JSON.parse(manifestText);
assert.deepEqual(
  {
    react: manifest.dependencies.react,
    reactDom: manifest.dependencies["react-dom"],
    pluginReact: manifest.devDependencies["@vitejs/plugin-react"],
    typescript: manifest.devDependencies.typescript,
    vite: manifest.devDependencies.vite,
    build: manifest.scripts.build,
  },
  {
    react: "19.2.8",
    reactDom: "19.2.8",
    pluginReact: "6.1.1",
    typescript: "6.0.3",
    vite: "8.2.2",
    build: "tsc -b && vite build",
  },
);
for (const boundary of [
  "react:\n        specifier: 19.2.8\n        version: 19.2.8",
  "react-dom:\n        specifier: 19.2.8\n        version: 19.2.8(react@19.2.8)",
  '"@vitejs/plugin-react":\n        specifier: 6.1.1\n        version: 6.1.1(vite@8.2.2',
  "typescript:\n        specifier: 6.0.3\n        version: 6.0.3",
  "vite:\n        specifier: 8.2.2\n        version: 8.2.2",
]) {
  assert.ok(lock.includes(boundary), `frozen lock must retain ${boundary}`);
}
assert.match(vite, /import react from "@vitejs\/plugin-react"/u);
assert.match(vite, /plugins:\s*\[react\(\)\]/u);
const compilerOptions = JSON.parse(typescript).compilerOptions;
for (const [option, expected] of Object.entries({
  strict: true,
  noUncheckedIndexedAccess: true,
  exactOptionalPropertyTypes: true,
  isolatedModules: true,
  noEmit: true,
  jsx: "react-jsx",
})) {
  assert.equal(
    compilerOptions[option],
    expected,
    `TypeScript must retain ${option}`,
  );
}
assert.ok(entry.includes('import { StrictMode } from "react";'));
assert.ok(entry.includes('import { createRoot } from "react-dom/client";'));
assert.match(entry, /createRoot\(root\)\.render\(\s*<StrictMode>/u);

for (const boundary of [
  'import { describeGraphHierarchy } from "./hierarchyAccessibility"',
  "describeGraphHierarchy(hierarchy)",
  'id="org-chart-structure-summary"',
  'aria-describedby="org-chart-structure-summary chart-keyboard-help"',
  'data-hierarchy-semantics="visual"',
]) {
  assert.ok(canvas.includes(boundary), `graph must retain ${boundary}`);
}
for (const boundary of [
  'import { describeTreeHierarchy } from "./hierarchyAccessibility"',
  "describeTreeHierarchy(hierarchy)",
  'id="org-tree-structure-summary"',
  'aria-describedby="org-tree-structure-summary org-tree-keyboard-help"',
  'data-hierarchy-semantics="tree-authority"',
  'aria-label="Marketing Agents organization tree empty state"',
  'aria-describedby="org-tree-structure-summary"',
  'data-hierarchy-semantics="tree-empty"',
]) {
  assert.ok(tree.includes(boundary), `tree must retain ${boundary}`);
}
for (const boundary of [
  "departmentLabel: string",
  "functionLabel: string",
  "hierarchyDescriptionId",
  "aria-describedby={hierarchyDescriptionId}",
  "Hierarchy",
  "level 4.",
]) {
  assert.ok(card.includes(boundary), `agent card must retain ${boundary}`);
}
for (const boundary of [
  "departmentLabel={department.displayName}",
  "functionLabel={agentFunction.displayName}",
]) {
  assert.ok(
    stage.includes(boundary),
    `hierarchy stage must retain ${boundary}`,
  );
}
assert.ok(page.includes('hierarchyView.mode === "graph" ? ('));
assert.equal(page.match(/<OrgChartCanvas/gu)?.length, 1);
assert.equal(page.match(/<OrgTreeFallback/gu)?.length, 1);
assert.ok(aggregateBrowserRunner.includes('"run-arch-02-e2e.mjs"'));

process.stdout.write(
  `ARCH-02 dependency-free witness passed: Node ${process.versions.node}, locked React/TypeScript/Vite wiring, projection summaries, card lineage, and exclusive graph/tree sources.\n`,
);

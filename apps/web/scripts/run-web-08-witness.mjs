// WEB-08 dependency-free witness source-binds production accessibility boundaries.
import "./require-pinned-node.mjs";

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const readProductionSource = async (relativePath) =>
  readFile(new URL(relativePath, import.meta.url), "utf8");

const [
  globalStyles,
  tokens,
  orgChartStyles,
  approvalStyles,
  appShell,
  orgChartPage,
  catalogToolbar,
  agentInspector,
  approvalPage,
  approvalDialog,
  unsavedDialog,
  runTimelinePage,
  artifactPage,
] = await Promise.all([
  readProductionSource("../src/styles/global.css"),
  readProductionSource("../src/styles/tokens.css"),
  readProductionSource("../src/styles/org-chart.css"),
  readProductionSource("../src/features/approvals/approval-queue.css"),
  readProductionSource("../src/app/App.tsx"),
  readProductionSource("../src/features/org-chart/OrgChartPage.tsx"),
  readProductionSource("../src/features/org-chart/CatalogToolbar.tsx"),
  readProductionSource("../src/features/instance-detail/AgentInspector.tsx"),
  readProductionSource("../src/features/approvals/ApprovalQueuePage.tsx"),
  readProductionSource("../src/features/approvals/ApprovalDecisionDialog.tsx"),
  readProductionSource(
    "../src/features/instance-detail/UnsavedConfigurationDialog.tsx",
  ),
  readProductionSource("../src/features/runs/RunTimelinePage.tsx"),
  readProductionSource("../src/features/runs/ArtifactViewerPage.tsx"),
]);

const relativeLuminance = (hex) => {
  const channels = hex
    .slice(1)
    .match(/.{2}/gu)
    ?.map((channel) => Number.parseInt(channel, 16) / 255);
  assert.equal(channels?.length, 3, `unsupported color token ${hex}`);
  const [red, green, blue] = channels.map((channel) =>
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
};

const contrastRatio = (first, second) => {
  const firstLuminance = relativeLuminance(first);
  const secondLuminance = relativeLuminance(second);
  const lighter = Math.max(firstLuminance, secondLuminance);
  const darker = Math.min(firstLuminance, secondLuminance);
  return (lighter + 0.05) / (darker + 0.05);
};

for (const selector of [
  "button:focus-visible",
  "a:focus-visible",
  "input:focus-visible",
  "select:focus-visible",
  "textarea:focus-visible",
  "[tabindex]:focus-visible",
]) {
  assert.ok(
    globalStyles.includes(selector),
    `global focus treatment must include ${selector}`,
  );
}
for (const focusDeclaration of [
  "outline:",
  "outline-offset:",
  "box-shadow: var(--focus-ring)",
]) {
  assert.ok(
    globalStyles.includes(focusDeclaration),
    `global focus treatment must retain ${focusDeclaration}`,
  );
}

assert.ok(globalStyles.includes("@media (prefers-reduced-motion: reduce)"));
for (const reducedMotionDeclaration of [
  "scroll-behavior: auto !important",
  "animation-duration: 0.01ms !important",
  "animation-iteration-count: 1 !important",
  "transition-duration: 0.01ms !important",
]) {
  assert.ok(
    globalStyles.includes(reducedMotionDeclaration),
    `global reduced-motion policy must retain ${reducedMotionDeclaration}`,
  );
}
assert.ok(orgChartStyles.includes("@media (prefers-reduced-motion: reduce)"));
assert.ok(orgChartStyles.includes(".org-tree-item__disclosure"));
assert.ok(orgChartStyles.includes("transition: none"));

const strongBorder = tokens.match(
  /--color-border-strong:\s*(#[0-9a-f]{6})/iu,
)?.[1];
assert.notEqual(strongBorder, undefined);
assert.ok(contrastRatio(strongBorder, "#ffffff") >= 3);
assert.ok(
  orgChartStyles.includes("border: 1px solid var(--color-border-strong)"),
);
assert.ok(
  approvalStyles.includes("border: 1px solid var(--color-border-strong)"),
);
assert.ok(orgChartStyles.includes("@media (max-width: 720px)"));
assert.ok(orgChartStyles.includes("min-height: 44px"));
assert.ok(approvalStyles.includes("min-height: 44px"));

for (const landmarkBoundary of [
  "<header",
  "<nav",
  'aria-label="Primary navigation"',
  "<aside",
  'aria-label="Safe local execution mode"',
]) {
  assert.ok(
    appShell.includes(landmarkBoundary),
    `application shell must retain ${landmarkBoundary}`,
  );
}
for (const skipBoundary of [
  'className="skip-link"',
  'href="#main-content"',
  'id="main-content"',
  "tabIndex={-1}",
]) {
  assert.ok(
    appShell.includes(skipBoundary),
    `application skip route must retain ${skipBoundary}`,
  );
}
for (const mainSource of [
  orgChartPage,
  approvalPage,
  runTimelinePage,
  artifactPage,
]) {
  assert.ok(
    mainSource.includes("<main"),
    "routed page must retain a main landmark",
  );
}

for (const liveRegionBoundary of [
  'role="status"',
  'aria-live="polite"',
  'aria-atomic="true"',
]) {
  assert.ok(
    catalogToolbar.includes(liveRegionBoundary),
    `catalog result announcement must retain ${liveRegionBoundary}`,
  );
}
assert.ok(approvalPage.includes('aria-live="polite"'));
assert.ok(runTimelinePage.includes('aria-live="polite"'));
assert.ok(artifactPage.includes('aria-live="polite"'));

assert.equal(
  orgChartPage.match(/modal=\{hierarchyView\.isNarrow\}/gu)?.length,
  3,
);
for (const responsiveDialogBoundary of [
  'event.key !== "Tab"',
  'event.key === "Escape"',
  'aria-modal={modal ? "true" : undefined}',
  'panelRef.current?.querySelector<HTMLSelectElement>("select")?.focus()',
]) {
  assert.ok(
    catalogToolbar.includes(responsiveDialogBoundary),
    `responsive filter dialog must retain ${responsiveDialogBoundary}`,
  );
}
for (const responsiveInspectorBoundary of [
  'event.key !== "Tab"',
  'event.key !== "Escape"',
  'role={modal ? "dialog" : undefined}',
  'aria-modal={modal ? "true" : undefined}',
  "closeButtonRef.current?.focus()",
]) {
  assert.ok(
    agentInspector.includes(responsiveInspectorBoundary),
    `responsive inspector must retain ${responsiveInspectorBoundary}`,
  );
}

for (const [name, source, role] of [
  ["approval decision", approvalDialog, "dialog"],
  ["unsaved-change", unsavedDialog, "alertdialog"],
]) {
  for (const dialogBoundary of [
    `role="${role}"`,
    'aria-modal="true"',
    "aria-labelledby=",
    "aria-describedby=",
    'event.key === "Escape"',
    'event.key !== "Tab"',
    "previouslyFocused",
    ".focus()",
  ]) {
    assert.ok(
      source.includes(dialogBoundary),
      `${name} dialog must retain ${dialogBoundary}`,
    );
  }
}

process.stdout.write(
  `WEB-08 dependency-free witness passed: Node ${process.versions.node}, production focus, reduced-motion, landmark, live-region, and modal source boundaries.\n`,
);

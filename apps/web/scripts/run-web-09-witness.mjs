// WEB-09 dependency-free witness source-binds the neutral visual and local-asset contract.
import "./require-pinned-node.mjs";

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readdir, readFile } from "node:fs/promises";

const readSource = async (relativePath) =>
  readFile(new URL(relativePath, import.meta.url), "utf8");
const readBinary = async (relativePath) =>
  readFile(new URL(relativePath, import.meta.url));

const [
  tokens,
  globalStyles,
  orgChartStyles,
  inspectorStyles,
  appShell,
  icons,
  hierarchyStage,
  agentCard,
  orgTree,
  purposePresentation,
  agentInspector,
  restrictedMarkdown,
  indexHtml,
  viteConfig,
  packageManifestText,
  sourceBoundary,
  designSpecification,
  verificationLedger,
] = await Promise.all([
  readSource("../src/styles/tokens.css"),
  readSource("../src/styles/global.css"),
  readSource("../src/styles/org-chart.css"),
  readSource("../src/features/instance-detail/instance-detail.css"),
  readSource("../src/app/App.tsx"),
  readSource("../src/features/org-chart/icons.tsx"),
  readSource("../src/features/org-chart/HierarchyStage.tsx"),
  readSource("../src/features/org-chart/AgentCard.tsx"),
  readSource("../src/features/org-chart/OrgTreeFallback.tsx"),
  readSource("../src/features/org-chart/presentation.ts"),
  readSource("../src/features/instance-detail/AgentInspector.tsx"),
  readSource("../src/features/artifacts/restrictedMarkdown.tsx"),
  readSource("../index.html"),
  readSource("../vite.config.ts"),
  readSource("../package.json"),
  readSource(
    "../../../docs/implementation-plan/00-source-evidence-scope-and-assumptions.md",
  ),
  readSource("../../../docs/design/README.md"),
  readSource("../../../docs/verification/requirements/WEB-09.md"),
]);

const tokenValue = (name) => {
  const match = new RegExp(`--${name}:\\s*([^;]+);`, "u").exec(tokens);
  assert.notEqual(match, null, `missing visual token --${name}`);
  return match[1].trim().replaceAll(/\s+/gu, " ");
};

assert.match(tokens, /font-family:\s*system-ui,/u);
assert.doesNotMatch(tokens, /\bInter\b/u);
for (const [name, expected] of Object.entries({
  "color-canvas": "#f2f7fb",
  "color-surface": "#ffffff",
  "color-text": "#172033",
  "color-muted": "#667085",
  "color-border": "#cfd8e3",
  "color-accent": "#2463eb",
  "color-awaiting": "#c65f00",
  "color-safe": "#11854b",
  "radius-control": "8px",
  "radius-card": "10px",
  "shadow-panel": "0 12px 36px rgb(30 50 80 / 12%)",
})) {
  assert.equal(tokenValue(name), expected, `unexpected --${name}`);
}
assert.ok(
  inspectorStyles.includes("box-shadow: var(--shadow-panel)"),
  "the inspector must consume the accepted panel shadow",
);
assert.ok(
  orgChartStyles.includes("box-shadow: var(--shadow-panel)"),
  "a narrow hierarchy sheet must consume the accepted panel shadow",
);

assert.match(icons, /<svg[\s\S]*aria-hidden="true"/u);
assert.match(icons, /fill="none"/u);
assert.match(icons, /focusable="false"/u);
assert.match(icons, /stroke="currentColor"/u);
assert.doesNotMatch(icons, /https?:\/\//iu);
for (const [label, source, importPath] of [
  ["application shell", appShell, "../features/org-chart/icons"],
  ["hierarchy stage", hierarchyStage, "./icons"],
  ["agent card", agentCard, "./icons"],
  ["semantic tree", orgTree, "./icons"],
]) {
  assert.ok(
    source.includes(`from "${importPath}"`),
    `${label} must use the bundled neutral icon module`,
  );
}

assert.match(
  agentInspector,
  /Capability badges are implementation metadata,\s*not copied vendor\s*affiliations\./u,
);
assert.match(purposePresentation, /source chart names/u);
assert.match(restrictedMarkdown, /\[Image omitted:/u);
assert.doesNotMatch(restrictedMarkdown, /<img\b|<image\b/iu);

const remotelyLoadedAsset =
  /(?:src|href)\s*=\s*["']https?:\/\/|url\(\s*["']?https?:\/\/|@import\s+(?:url\()?\s*["']?https?:\/\//iu;
const visualSources = [
  tokens,
  globalStyles,
  orgChartStyles,
  inspectorStyles,
  appShell,
  icons,
  hierarchyStage,
  agentCard,
  orgTree,
  agentInspector,
  indexHtml,
];
for (const source of visualSources) {
  assert.doesNotMatch(source, remotelyLoadedAsset);
  assert.doesNotMatch(source, /@font-face/iu);
}
assert.match(
  indexHtml,
  /<script type="module" src="\/src\/main\.tsx"><\/script>/u,
);
for (const match of viteConfig.matchAll(/target:\s*["']([^"']+)["']/gu)) {
  const target = new URL(match[1]);
  assert.ok(
    target.hostname === "127.0.0.1" || target.hostname === "localhost",
    `Vite target must remain loopback-only: ${target.href}`,
  );
}

const packageManifest = JSON.parse(packageManifestText);
assert.deepEqual(Object.keys(packageManifest.dependencies ?? {}).sort(), [
  "@tanstack/react-query",
  "react",
  "react-dom",
  "react-router-dom",
]);

const mediaExtensions = new Set([
  ".gif",
  ".jpeg",
  ".jpg",
  ".otf",
  ".png",
  ".svg",
  ".ttf",
  ".webp",
  ".woff",
  ".woff2",
]);
const extensionOf = (name) => {
  const position = name.lastIndexOf(".");
  return position === -1 ? "" : name.slice(position).toLowerCase();
};
const listFiles = async (relativeDirectory) => {
  const directory = new URL(relativeDirectory, import.meta.url);
  let entries;
  try {
    entries = await readdir(directory, { withFileTypes: true });
  } catch (error) {
    if (error?.code === "ENOENT") return [];
    throw error;
  }
  const nested = await Promise.all(
    entries.map(async (entry) => {
      const relativePath = `${relativeDirectory.replace(/\/$/u, "")}/${entry.name}`;
      return entry.isDirectory() ? listFiles(relativePath) : [relativePath];
    }),
  );
  return nested.flat();
};
const bundledMedia = (
  await Promise.all([listFiles("../src"), listFiles("../public")])
)
  .flat()
  .filter((path) => mediaExtensions.has(extensionOf(path)));
assert.deepEqual(
  bundledMedia,
  [],
  `visual identity must remain code-native; found static media: ${bundledMedia.join(", ")}`,
);

for (const requiredSourceBoundary of [
  "One visible `Marketing Agents` root.",
  "Third-party product logos.",
  "The source-frame watermark.",
  "Brand colors or trade dress that imply an official integration.",
  "Use neutral internal capability icons",
]) {
  assert.ok(
    sourceBoundary.includes(requiredSourceBoundary),
    `source boundary must retain ${requiredSourceBoundary}`,
  );
}
for (const requiredDesignBoundary of [
  /Use neutral internal icons/u,
  /Do not load remote\s+fonts/u,
  /Final visual verification compares native-size browser screenshots/u,
]) {
  assert.match(designSpecification, requiredDesignBoundary);
}
for (const ledgerHeading of [
  "### App chrome and visible copy",
  "### Hierarchy and default viewport",
  "### Typography and visual tokens",
  "### Inspector and mobile sheet",
  "### Desktop and mobile interaction",
  "### Responsive and accessibility continuity",
]) {
  assert.ok(
    verificationLedger.includes(ledgerHeading),
    `WEB-09 fidelity ledger must retain ${ledgerHeading}`,
  );
}

const conceptAuthorities = [
  {
    path: "../../../docs/design/concepts/marketing-agents-desktop.png",
    width: 1536,
    height: 1024,
    sha256: "b67f1007008f5680f31da22c247568e5265d67ed64f29b58ff278cd0262a795c",
  },
  {
    path: "../../../docs/design/concepts/marketing-agents-mobile.png",
    width: 852,
    height: 1846,
    sha256: "f21b53c8817c9064a0eee2b870e22ed92dbf4b74024a1c8a2491d79fe76b2593",
  },
];
for (const authority of conceptAuthorities) {
  const image = await readBinary(authority.path);
  assert.equal(image.toString("ascii", 1, 4), "PNG");
  assert.equal(image.readUInt32BE(16), authority.width);
  assert.equal(image.readUInt32BE(20), authority.height);
  assert.equal(
    createHash("sha256").update(image).digest("hex"),
    authority.sha256,
  );
}

process.stdout.write(
  `WEB-09 dependency-free witness passed: Node ${process.versions.node}, accepted concepts, system tokens, neutral code-native icons, local assets, and fidelity ledger.\n`,
);

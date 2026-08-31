import { readFile } from "node:fs/promises";

import { beforeAll, describe, expect, it } from "vitest";

interface ProductionSources {
  readonly app: string;
  readonly globalStyles: string;
  readonly tokens: string;
  readonly orgChartStyles: string;
  readonly approvalStyles: string;
  readonly orgChartPage: string;
  readonly catalogToolbar: string;
  readonly agentInspector: string;
}

const readSource = (relativePath: string): Promise<string> =>
  readFile(new URL(relativePath, import.meta.url), "utf8");

function relativeLuminance(hex: string): number {
  const channels = hex
    .slice(1)
    .match(/.{2}/gu)
    ?.map((channel) => Number.parseInt(channel, 16) / 255);
  if (channels?.length !== 3) {
    throw new Error(`Unsupported color token ${hex}`);
  }
  const converted = channels.map((channel) =>
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
  );
  const red = converted[0];
  const green = converted[1];
  const blue = converted[2];
  if (red === undefined || green === undefined || blue === undefined) {
    throw new Error(`Unsupported color token ${hex}`);
  }
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function contrastRatio(first: string, second: string): number {
  const firstLuminance = relativeLuminance(first);
  const secondLuminance = relativeLuminance(second);
  const lighter = Math.max(firstLuminance, secondLuminance);
  const darker = Math.min(firstLuminance, secondLuminance);
  return (lighter + 0.05) / (darker + 0.05);
}

describe("WEB-08 production accessibility source contracts", () => {
  let sources: ProductionSources;

  beforeAll(async () => {
    const [
      app,
      globalStyles,
      tokens,
      orgChartStyles,
      approvalStyles,
      orgChartPage,
      catalogToolbar,
      agentInspector,
    ] = await Promise.all([
      readSource("./app/App.tsx"),
      readSource("./styles/global.css"),
      readSource("./styles/tokens.css"),
      readSource("./styles/org-chart.css"),
      readSource("./features/approvals/approval-queue.css"),
      readSource("./features/org-chart/OrgChartPage.tsx"),
      readSource("./features/org-chart/CatalogToolbar.tsx"),
      readSource("./features/instance-detail/AgentInspector.tsx"),
    ]);
    sources = {
      app,
      globalStyles,
      tokens,
      orgChartStyles,
      approvalStyles,
      orgChartPage,
      catalogToolbar,
      agentInspector,
    };
  });

  it("WEB-08 retains the skip target, route boundary, and visible focus selectors", () => {
    expect(sources.app).toContain('className="skip-link"');
    expect(sources.app).toContain('href="#main-content"');
    expect(sources.app).toContain('id="main-content"');
    expect(sources.app).toContain("tabIndex={-1}");
    for (const selector of [
      "button:focus-visible",
      "a:focus-visible",
      "input:focus-visible",
      "select:focus-visible",
      "textarea:focus-visible",
      "[tabindex]:focus-visible",
    ]) {
      expect(sources.globalStyles).toContain(selector);
    }
    expect(sources.globalStyles).toContain("outline: 2px solid");
    expect(sources.globalStyles).toContain("box-shadow: var(--focus-ring)");
    expect(sources.globalStyles).toContain(".skip-link:focus-visible");
  });

  it("WEB-08 source-binds narrow filter and inspector modal focus behavior", () => {
    expect(
      sources.orgChartPage.match(/modal=\{hierarchyView\.isNarrow\}/gu),
    ).toHaveLength(3);
    for (const boundary of [
      'event.key !== "Tab"',
      'event.key === "Escape"',
      'aria-modal={modal ? "true" : undefined}',
      'panelRef.current?.querySelector<HTMLSelectElement>("select")?.focus()',
    ]) {
      expect(sources.catalogToolbar).toContain(boundary);
    }
    for (const boundary of [
      'event.key !== "Tab"',
      'event.key !== "Escape"',
      'role={modal ? "dialog" : undefined}',
      'aria-modal={modal ? "true" : undefined}',
      "closeButtonRef.current?.focus()",
    ]) {
      expect(sources.agentInspector).toContain(boundary);
    }
  });

  it("WEB-08 retains non-text contrast, touch-target, and reduced-motion boundaries", () => {
    const strongBorder = /--color-border-strong:\s*(#[0-9a-f]{6})/iu.exec(
      sources.tokens,
    )?.[1];
    expect(strongBorder).toBeDefined();
    expect(
      contrastRatio(strongBorder ?? "#ffffff", "#ffffff"),
    ).toBeGreaterThanOrEqual(3);
    expect(sources.orgChartStyles).toContain(
      "border: 1px solid var(--color-border-strong)",
    );
    expect(sources.approvalStyles).toContain(
      "border: 1px solid var(--color-border-strong)",
    );
    expect(sources.orgChartStyles).toContain("@media (max-width: 720px)");
    expect(sources.orgChartStyles).toContain("min-height: 44px");
    expect(sources.approvalStyles).toContain("min-height: 44px");
    for (const reducedMotionBoundary of [
      "@media (prefers-reduced-motion: reduce)",
      "scroll-behavior: auto !important",
      "animation-duration: 0.01ms !important",
      "transition-duration: 0.01ms !important",
    ]) {
      expect(sources.globalStyles).toContain(reducedMotionBoundary);
    }
    expect(sources.orgChartStyles).toContain(
      "@media (prefers-reduced-motion: reduce)",
    );
    expect(sources.approvalStyles).toContain(
      "@media (prefers-reduced-motion: reduce)",
    );
  });
});

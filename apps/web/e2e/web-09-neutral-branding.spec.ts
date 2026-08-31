// WEB-09 browser evidence compares the production hierarchy at native desktop and DPR2 mobile sizes.
import {
  expect,
  test,
  type Browser,
  type BrowserContext,
  type Locator,
  type Page,
  type TestInfo,
} from "@playwright/test";

interface HierarchyInstance {
  readonly id: string;
  readonly templateId: string;
  readonly sourceOrdinal: number;
}

interface HierarchyFunction {
  readonly id: string;
  readonly instances: readonly HierarchyInstance[];
}

interface HierarchyDepartment {
  readonly id: string;
  readonly displayName: string;
  readonly functions: readonly HierarchyFunction[];
}

interface HierarchyBody {
  readonly counts: {
    readonly departments: number;
    readonly functions: number;
    readonly templates: number;
    readonly instances: number;
  };
  readonly departments: readonly HierarchyDepartment[];
}

interface ResourceAudit {
  readonly cssAssetReferences: readonly string[];
  readonly documentFontFaces: number;
  readonly embeddedAssetElements: number;
  readonly externalResources: readonly string[];
  readonly inlineSvgCount: number;
  readonly remoteAssetElements: readonly string[];
  readonly svgExternalReferences: number;
  readonly vendorMarkerElements: number;
  readonly watermarkMarkerElements: number;
}

const DESKTOP_VIEWPORT = { width: 1_536, height: 1_024 } as const;
const MOBILE_VIEWPORT = { width: 426, height: 923 } as const;
const MOBILE_DEVICE_SCALE_FACTOR = 2;
const LOCAL_HOSTS = new Set(["127.0.0.1", "localhost"]);
const EXPECTED_DEPARTMENTS = [
  "Social media",
  "Blog & SEO",
  "Email",
  "Community",
  "Partnerships",
] as const;
const NEWSLETTER_TEMPLATE_ID = "tpl.email.newsletter.newsletter-subscriber";
const NON_AFFILIATION_NOTE =
  "Capability badges are implementation metadata, not copied vendor affiliations.";
const EXPECTED_TOKENS = Object.freeze({
  "--color-canvas": "rgb(242, 247, 251)",
  "--color-surface": "rgb(255, 255, 255)",
  "--color-text": "rgb(23, 32, 51)",
  "--color-muted": "rgb(102, 112, 133)",
  "--color-border": "rgb(207, 216, 227)",
  "--color-accent": "rgb(36, 99, 235)",
  "--color-awaiting": "rgb(198, 95, 0)",
  "--color-safe": "rgb(17, 133, 75)",
  "--radius-control": "8px",
  "--radius-card": "10px",
  "--shadow-panel": "rgba(30, 50, 80, 0.12) 0px 12px 36px 0px",
});

function orderedIds(hierarchy: HierarchyBody): {
  readonly departments: readonly string[];
  readonly functions: readonly string[];
  readonly instances: readonly string[];
} {
  return {
    departments: hierarchy.departments.map(({ id }) => id),
    functions: hierarchy.departments.flatMap(({ functions }) =>
      functions.map(({ id }) => id),
    ),
    instances: hierarchy.departments.flatMap(({ functions }) =>
      functions.flatMap(({ instances }) => instances.map(({ id }) => id)),
    ),
  };
}

function initialTreeIds(hierarchy: HierarchyBody): readonly string[] {
  return [
    "root",
    ...hierarchy.departments.flatMap((department) => [
      department.id,
      ...department.functions.map(({ id }) => id),
    ]),
  ];
}

function installExternalRequestAudit(page: Page): string[] {
  const externalRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (!LOCAL_HOSTS.has(url.hostname)) externalRequests.push(request.url());
  });
  return externalRequests;
}

async function loadHierarchy(page: Page): Promise<HierarchyBody> {
  const responsePromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/catalog/hierarchy" &&
      response.request().method() === "GET",
  );
  await page.goto("/", { waitUntil: "networkidle" });
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  return (await response.json()) as HierarchyBody;
}

async function expectExactHierarchy(
  page: Page,
  hierarchy: HierarchyBody,
): Promise<void> {
  expect(hierarchy.counts).toEqual({
    departments: 5,
    functions: 12,
    templates: 36,
    instances: 43,
  });
  expect(hierarchy.departments.map(({ displayName }) => displayName)).toEqual(
    EXPECTED_DEPARTMENTS,
  );

  const ids = orderedIds(hierarchy);
  expect(new Set(ids.instances).size).toBe(43);
  expect(
    await page
      .locator('[data-node-kind="department"]')
      .evaluateAll((nodes) =>
        nodes.map(
          (node) =>
            node.getAttribute("data-node-id") ??
            node.getAttribute("data-department-id"),
        ),
      ),
  ).toEqual(ids.departments);
  expect(
    await page
      .locator('[data-node-kind="function"]')
      .evaluateAll((nodes) =>
        nodes.map(
          (node) =>
            node.getAttribute("data-node-id") ??
            node.getAttribute("data-function-id"),
        ),
      ),
  ).toEqual(ids.functions);
  expect(
    await page
      .locator('[data-node-kind="instance"]')
      .evaluateAll((nodes) =>
        nodes.map((node) => node.getAttribute("data-node-id")),
      ),
  ).toEqual(ids.instances);

  const community = hierarchy.departments[3];
  expect(community?.displayName).toBe("Community");
  const communityInstances =
    community?.functions.flatMap(({ instances }) => instances) ?? [];
  expect(communityInstances).toHaveLength(14);
  const communityTemplateIds = [
    ...new Set(communityInstances.map(({ templateId }) => templateId)),
  ];
  expect(communityTemplateIds).toHaveLength(7);
  for (const templateId of communityTemplateIds) {
    expect(
      communityInstances
        .filter((instance) => instance.templateId === templateId)
        .map(({ sourceOrdinal }) => sourceOrdinal),
    ).toEqual([1, 2]);
  }
}

async function expectInternalVisualSystem(page: Page): Promise<void> {
  const visual = await page.evaluate((expectedTokenNames) => {
    const rootStyle = getComputedStyle(document.documentElement);
    const probe = document.createElement("span");
    probe.style.position = "fixed";
    probe.style.visibility = "hidden";
    document.body.append(probe);
    const resolvedToken = (name: string): string => {
      if (name.startsWith("--color-")) {
        probe.style.color = `var(${name})`;
        return getComputedStyle(probe).color;
      }
      if (name === "--shadow-panel") {
        probe.style.boxShadow = `var(${name})`;
        return getComputedStyle(probe).boxShadow;
      }
      return rootStyle.getPropertyValue(name).trim();
    };
    const viewport = document.querySelector<HTMLElement>(
      ".org-chart-viewport, .org-tree-scroll",
    );
    const surface = document.querySelector<HTMLElement>(".chart-surface");
    const hierarchyNode = document.querySelector<HTMLElement>(
      ".agent-card, .org-tree-item",
    );
    if (viewport === null || surface === null || hierarchyNode === null) {
      throw new Error("WEB-09 visual-system target is missing");
    }
    const result = {
      tokens: Object.fromEntries(
        expectedTokenNames.map((name) => [name, resolvedToken(name)]),
      ),
      missingTokens: expectedTokenNames.filter(
        (name) => rootStyle.getPropertyValue(name).trim().length === 0,
      ),
      fontFamily: rootStyle.fontFamily,
      surfaceBackground: getComputedStyle(surface).backgroundColor,
      viewportBackground: getComputedStyle(viewport).backgroundColor,
      nodeBackground: getComputedStyle(hierarchyNode).backgroundColor,
      nodeBorderRadius: getComputedStyle(hierarchyNode).borderRadius,
    };
    probe.remove();
    return result;
  }, Object.keys(EXPECTED_TOKENS));

  expect(visual.missingTokens).toEqual([]);
  expect(visual.tokens).toEqual(EXPECTED_TOKENS);
  expect(visual.fontFamily.split(",")[0]?.trim()).toBe("system-ui");
  expect(visual.surfaceBackground).toBe("rgb(255, 255, 255)");
  expect(visual.viewportBackground).toBe("rgb(242, 247, 251)");
  expect(visual.nodeBackground).toBe("rgb(255, 255, 255)");
  expect(visual.nodeBorderRadius).toMatch(/^(9|10|11)px$/u);
}

async function resourceAudit(page: Page): Promise<ResourceAudit> {
  return page.evaluate(() => {
    const localHosts = new Set(["127.0.0.1", "localhost"]);
    const externalResources = performance
      .getEntriesByType("resource")
      .map(({ name }) => name)
      .filter((name) => !localHosts.has(new URL(name, location.href).hostname));

    const cssAssetReferences: string[] = [];
    const inspectRules = (rules: CSSRuleList): void => {
      for (const rule of [...rules]) {
        const cssText = rule.cssText;
        if (/\b(?:url\s*\(|@import\b|@font-face\b)/iu.test(cssText)) {
          cssAssetReferences.push(cssText);
        }
        if ("cssRules" in rule) {
          inspectRules((rule as CSSGroupingRule).cssRules);
        }
      }
    };
    for (const sheet of document.styleSheets) {
      inspectRules(sheet.cssRules);
    }

    const assetSelectors = [
      "img[src]",
      "picture source[srcset]",
      "video[src]",
      "video[poster]",
      "audio[src]",
      "iframe[src]",
      "embed[src]",
      "object[data]",
      'link[rel="stylesheet"][href]',
      'link[rel="preload"][href]',
      "script[src]",
    ];
    const remoteAssetElements = [
      ...document.querySelectorAll<HTMLElement>(assetSelectors.join(",")),
    ]
      .map(
        (element) =>
          element.getAttribute("src") ??
          element.getAttribute("srcset") ??
          element.getAttribute("poster") ??
          element.getAttribute("data") ??
          element.getAttribute("href") ??
          "",
      )
      .filter(
        (reference) =>
          reference.length > 0 &&
          !localHosts.has(new URL(reference, location.href).hostname),
      );

    return {
      cssAssetReferences,
      documentFontFaces: [...document.fonts].length,
      embeddedAssetElements: document.querySelectorAll(
        "img, picture, video, audio, iframe, embed, object",
      ).length,
      externalResources,
      inlineSvgCount: document.querySelectorAll("svg").length,
      remoteAssetElements,
      svgExternalReferences: document.querySelectorAll(
        "svg image, svg use, svg [href], svg [xlink\\:href]",
      ).length,
      vendorMarkerElements: document.querySelectorAll(
        "[data-vendor], [data-brand], .vendor-badge, .vendor-logo, .third-party-brand",
      ).length,
      watermarkMarkerElements: document.querySelectorAll(
        '[class*="watermark" i], [id*="watermark" i], [data-watermark]',
      ).length,
    };
  });
}

async function expectNeutralLocalAssets(
  page: Page,
  externalRequests: readonly string[],
): Promise<void> {
  await expect.poll(() => externalRequests).toEqual([]);
  const audit = await resourceAudit(page);
  expect(audit.inlineSvgCount).toBeGreaterThan(0);
  expect(audit).toEqual({
    cssAssetReferences: [],
    documentFontFaces: 0,
    embeddedAssetElements: 0,
    externalResources: [],
    inlineSvgCount: audit.inlineSvgCount,
    remoteAssetElements: [],
    svgExternalReferences: 0,
    vendorMarkerElements: 0,
    watermarkMarkerElements: 0,
  });
}

async function expectNeutralAgentCardIcons(page: Page): Promise<void> {
  const iconAudit = await page.locator(".agent-card").evaluateAll((cards) => {
    const icons = cards.map((card) => {
      const icon = card.querySelector<HTMLElement>(".agent-card__icon");
      const svg = icon?.querySelector("svg");
      if (icon === null || svg === null || svg === undefined) {
        throw new Error("Agent card has no inline icon");
      }
      const style = getComputedStyle(icon);
      return {
        background: style.backgroundColor,
        color: style.color,
        markup: svg.innerHTML,
      };
    });
    return {
      backgrounds: [...new Set(icons.map(({ background }) => background))],
      colors: [...new Set(icons.map(({ color }) => color))],
      markups: [...new Set(icons.map(({ markup }) => markup))],
    };
  });
  expect(iconAudit.backgrounds).toHaveLength(1);
  expect(iconAudit.colors).toHaveLength(1);
  expect(iconAudit.markups).toHaveLength(2);
}

async function expectNoDocumentOverflow(
  page: Page,
  expectedWidth: number,
): Promise<void> {
  await expect
    .poll(() =>
      page.evaluate(() => ({
        documentClientWidth: document.documentElement.clientWidth,
        documentScrollWidth: document.documentElement.scrollWidth,
        bodyClientWidth: document.body.clientWidth,
        bodyScrollWidth: document.body.scrollWidth,
      })),
    )
    .toEqual({
      documentClientWidth: expectedWidth,
      documentScrollWidth: expectedWidth,
      bodyClientWidth: expectedWidth,
      bodyScrollWidth: expectedWidth,
    });
}

async function expectPanelShadow(panel: Locator): Promise<void> {
  const shadow = await panel.evaluate(
    (element) => getComputedStyle(element).boxShadow,
  );
  expect(shadow).toContain("rgba(30, 50, 80, 0.12)");
  expect(shadow).toContain("0px 12px 36px 0px");
}

async function saveNativeScreenshot(
  page: Page,
  testInfo: TestInfo,
  filename: string,
  expectedPixelSize: { readonly width: number; readonly height: number },
): Promise<void> {
  const image = await page.screenshot({
    path: testInfo.outputPath(filename),
    fullPage: true,
    scale: "device",
  });
  expect(image.subarray(1, 4).toString("ascii")).toBe("PNG");
  expect({
    width: image.readUInt32BE(16),
    height: image.readUInt32BE(20),
  }).toEqual(expectedPixelSize);
}

async function newMobilePage(browser: Browser): Promise<{
  readonly context: BrowserContext;
  readonly page: Page;
}> {
  const context = await browser.newContext({
    baseURL: "http://127.0.0.1:4173",
    colorScheme: "light",
    deviceScaleFactor: MOBILE_DEVICE_SCALE_FACTOR,
    locale: "en-US",
    viewport: MOBILE_VIEWPORT,
  });
  return { context, page: await context.newPage() };
}

test("WEB-09 desktop preserves the neutral source hierarchy with local-only assets", async ({
  page,
}, testInfo) => {
  expect(page.viewportSize()).toEqual(DESKTOP_VIEWPORT);
  expect(await page.evaluate(() => devicePixelRatio)).toBe(1);
  const externalRequests = installExternalRequestAudit(page);
  const hierarchy = await loadHierarchy(page);

  await expect(page.getByTestId("org-chart-viewport")).toBeVisible();
  await expectExactHierarchy(page, hierarchy);
  await expectInternalVisualSystem(page);
  await expectNeutralAgentCardIcons(page);
  await expectNeutralLocalAssets(page, externalRequests);
  await expectNoDocumentOverflow(page, DESKTOP_VIEWPORT.width);
  await saveNativeScreenshot(
    page,
    testInfo,
    "web-09-desktop-hierarchy.png",
    DESKTOP_VIEWPORT,
  );

  await page.locator(`[data-template-id="${NEWSLETTER_TEMPLATE_ID}"]`).click();
  const inspector = page.locator("#agent-inspector");
  const affiliationNote = inspector.getByText(NON_AFFILIATION_NOTE, {
    exact: true,
  });
  await expect(inspector).toBeVisible();
  await expect(affiliationNote).toBeVisible();
  await expectPanelShadow(inspector);
  await affiliationNote.scrollIntoViewIfNeeded();
  await expectNeutralLocalAssets(page, externalRequests);
  await expectNoDocumentOverflow(page, DESKTOP_VIEWPORT.width);
  await saveNativeScreenshot(
    page,
    testInfo,
    "web-09-desktop-neutral-inspector.png",
    DESKTOP_VIEWPORT,
  );
});

test("WEB-09 mobile preserves the neutral semantic tree and detail sheet with local-only assets", async ({
  browser,
}, testInfo) => {
  const { context, page } = await newMobilePage(browser);
  try {
    expect(page.viewportSize()).toEqual(MOBILE_VIEWPORT);
    expect(await page.evaluate(() => devicePixelRatio)).toBe(
      MOBILE_DEVICE_SCALE_FACTOR,
    );
    const externalRequests = installExternalRequestAudit(page);
    const hierarchy = await loadHierarchy(page);
    const tree = page.getByRole("tree", {
      name: "Marketing Agents organization tree",
    });
    await expect(tree).toBeVisible();
    await expect(page.getByTestId("org-chart-viewport")).toHaveCount(0);
    expect(
      await tree
        .locator(':scope > [role="treeitem"]')
        .evaluateAll((nodes) =>
          nodes.map((node) => node.getAttribute("data-node-id")),
        ),
    ).toEqual(initialTreeIds(hierarchy));
    await expectInternalVisualSystem(page);
    await expectNeutralLocalAssets(page, externalRequests);
    await expectNoDocumentOverflow(page, MOBILE_VIEWPORT.width);
    await saveNativeScreenshot(page, testInfo, "web-09-mobile-tree-dpr2.png", {
      width: MOBILE_VIEWPORT.width * MOBILE_DEVICE_SCALE_FACTOR,
      height: MOBILE_VIEWPORT.height * MOBILE_DEVICE_SCALE_FACTOR,
    });

    await page
      .getByRole("searchbox", { name: "Search agents" })
      .fill("Newsletter Subscriber");
    const newsletter = tree.locator(
      `[data-template-id="${NEWSLETTER_TEMPLATE_ID}"]`,
    );
    await expect(newsletter).toHaveCount(1);
    await newsletter.click();

    const inspector = page.getByRole("dialog", {
      name: /Newsletter Subscriber/u,
    });
    const affiliationNote = inspector.getByText(NON_AFFILIATION_NOTE, {
      exact: true,
    });
    await expect(inspector).toBeVisible();
    await expect(affiliationNote).toBeVisible();
    await expectPanelShadow(inspector);
    expect(await inspector.boundingBox()).toMatchObject({
      x: 0,
      y: 0,
      width: MOBILE_VIEWPORT.width,
      height: MOBILE_VIEWPORT.height,
    });
    await affiliationNote.scrollIntoViewIfNeeded();
    await expectNeutralLocalAssets(page, externalRequests);
    await expectNoDocumentOverflow(page, MOBILE_VIEWPORT.width);
    await saveNativeScreenshot(
      page,
      testInfo,
      "web-09-mobile-neutral-sheet-dpr2.png",
      {
        width: MOBILE_VIEWPORT.width * MOBILE_DEVICE_SCALE_FACTOR,
        height: MOBILE_VIEWPORT.height * MOBILE_DEVICE_SCALE_FACTOR,
      },
    );
  } finally {
    await context.close();
  }
});

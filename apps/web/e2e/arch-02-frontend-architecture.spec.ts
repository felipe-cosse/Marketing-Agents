// ARCH-02 browser evidence proves one accessible graph or semantic tree in the production frontend.
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Response } from "@playwright/test";

interface HierarchyBody {
  readonly counts: {
    readonly departments: number;
    readonly functions: number;
    readonly instances: number;
  };
  readonly departments: readonly {
    readonly displayName: string;
    readonly functions: readonly {
      readonly displayName: string;
      readonly instances: readonly {
        readonly id: string;
        readonly templateId: string;
        readonly sourceOrdinal: number;
      }[];
    }[];
  }[];
}

async function loadHierarchy(page: Page): Promise<HierarchyBody> {
  const responsePromise = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/v1/catalog/hierarchy" &&
      response.request().method() === "GET",
  );
  await page.goto("/");
  const response: Response = await responsePromise;
  expect(response.status()).toBe(200);
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(43);
  return (await response.json()) as HierarchyBody;
}

async function expectNoBlockingAxeViolations(
  page: Page,
  state: string,
): Promise<void> {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  const blocking = results.violations.filter(
    ({ impact }) => impact === "serious" || impact === "critical",
  );
  expect(
    blocking.map(({ id, impact, help }) => ({ id, impact, help })),
    state,
  ).toEqual([]);
}

test("ARCH-02 mounts one accessible graph or tree in the production frontend", async ({
  page,
}) => {
  const body = await loadHierarchy(page);
  expect(body.counts).toMatchObject({
    departments: 5,
    functions: 12,
    instances: 43,
  });

  const graph = page.getByRole("region", {
    name: "Marketing Agents interactive organization chart",
  });
  await expect(graph).toHaveCount(1);
  await expect(graph).toHaveAttribute("data-hierarchy-semantics", "visual");
  await expect(graph).toHaveAccessibleDescription(
    /Graph view shows 5 visible departments, 12 visible functions, and 43 visible deployed agents.*For complete level-by-level hierarchy navigation, use Tree view.*Use arrow keys to pan/u,
  );
  await expect(page.getByRole("tree")).toHaveCount(0);

  const expectedCards = body.departments.flatMap((department) =>
    department.functions.flatMap((agentFunction) =>
      agentFunction.instances.map((instance) => ({
        ...instance,
        departmentLabel: department.displayName,
        functionLabel: agentFunction.displayName,
      })),
    ),
  );
  expect(expectedCards).toHaveLength(43);
  for (const expected of expectedCards) {
    const card = page.locator(
      `[data-node-kind="instance"][data-instance-id="${expected.id}"]`,
    );
    await expect(card).toHaveCount(1);
    await expect(card).toHaveAccessibleDescription(
      `Department: ${expected.departmentLabel}. Function: ${expected.functionLabel}. Hierarchy level 4.`,
    );
  }

  const communityByTemplate = new Map<
    string,
    (typeof expectedCards)[number][]
  >();
  for (const card of expectedCards) {
    if (card.departmentLabel !== "Community") continue;
    const duplicates = communityByTemplate.get(card.templateId) ?? [];
    duplicates.push(card);
    communityByTemplate.set(card.templateId, duplicates);
  }
  expect(communityByTemplate.size).toBe(7);
  for (const duplicateCards of communityByTemplate.values()) {
    expect(duplicateCards.map(({ sourceOrdinal }) => sourceOrdinal)).toEqual([
      1, 2,
    ]);
    for (const duplicate of duplicateCards) {
      await expect(
        page.locator(`[data-instance-id="${duplicate.id}"]`),
      ).toHaveAccessibleName(
        new RegExp(`Instance ${String(duplicate.sourceOrdinal)} of 2`, "u"),
      );
    }
  }

  const firstExpected = expectedCards[0];
  const secondExpected = expectedCards[1];
  if (firstExpected === undefined || secondExpected === undefined) {
    throw new Error("ARCH-02 expected at least two source agent cards");
  }
  const first = page.locator(`[data-instance-id="${firstExpected.id}"]`);
  const second = page.locator(`[data-instance-id="${secondExpected.id}"]`);
  await first.focus();
  await page.keyboard.press("ArrowDown");
  await expect(second).toBeFocused();
  await expect(second).toHaveAccessibleDescription(
    `Department: ${secondExpected.departmentLabel}. Function: ${secondExpected.functionLabel}. Hierarchy level 4.`,
  );

  const search = page.getByRole("searchbox", { name: "Search agents" });
  await search.fill(firstExpected.id);
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(1);
  await expect(graph).toHaveAccessibleDescription(
    /Graph view shows 1 visible department, 1 visible function, and 1 visible deployed agent/u,
  );
  await search.fill("");
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(43);
  await expectNoBlockingAxeViolations(page, "ARCH-02 graph view");

  await page.getByRole("button", { name: "Tree view" }).click();
  await expect(graph).toHaveCount(0);
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(0);
  const tree = page.getByRole("tree", {
    name: "Marketing Agents organization tree",
  });
  await expect(tree).toHaveCount(1);
  await expect(tree).toHaveAttribute(
    "data-hierarchy-semantics",
    "tree-authority",
  );
  await expect(tree).toHaveAccessibleDescription(
    /Tree view is the complete level-by-level hierarchy navigation model for 5 visible departments, 12 visible functions, and 43 visible deployed agents.*Use Up and Down to move/u,
  );
  await expect(tree.locator('[role="treeitem"]')).toHaveCount(18);
  await expectNoBlockingAxeViolations(page, "ARCH-02 Tree view");
});

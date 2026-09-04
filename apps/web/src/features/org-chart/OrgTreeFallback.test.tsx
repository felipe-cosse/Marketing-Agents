import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { makeHierarchyPayload } from "../../test/hierarchyFixture";
import { DEFAULT_ORG_CHART_FILTERS } from "./filters";
import {
  MARKETING_AGENTS_ROOT,
  MARKETING_ORCHESTRATOR_CONTROL_PLANE,
} from "./model";
import { normalizeHierarchy } from "./normalizeHierarchy";
import { OrgTreeFallback } from "./OrgTreeFallback";
import { projectHierarchy } from "./projectHierarchy";

const hierarchy = normalizeHierarchy(makeHierarchyPayload());

function renderTree(
  options: {
    readonly selectedInstanceId?: string | null;
    readonly autoExpandMatches?: boolean;
    readonly projectedQuery?: string;
  } = {},
) {
  const onSelectionChange = vi.fn();
  const onFocusSearch = vi.fn();
  const projection = projectHierarchy(hierarchy, {
    ...DEFAULT_ORG_CHART_FILTERS,
    q: options.projectedQuery ?? "",
  });
  render(
    <OrgTreeFallback
      hierarchy={projection.hierarchy}
      selectedInstanceId={options.selectedInstanceId ?? null}
      onSelectionChange={onSelectionChange}
      autoExpandMatches={options.autoExpandMatches}
      onFocusSearch={onFocusSearch}
      toolbar={<span>Shared catalog toolbar</span>}
    />,
  );
  return { onFocusSearch, onSelectionChange };
}

function item(nodeId: string): HTMLButtonElement {
  const target = document.querySelector<HTMLButtonElement>(
    `[role="treeitem"][data-node-id="${nodeId}"]`,
  );
  if (target === null) throw new Error(`Missing tree item ${nodeId}`);
  return target;
}

describe("WEB-07 semantic organization tree", () => {
  it("ORCH-01 marks the control plane on the source root without adding a tree item or card", () => {
    renderTree({ autoExpandMatches: true });

    const root = item(MARKETING_AGENTS_ROOT.id);
    expect(root).toHaveTextContent(MARKETING_AGENTS_ROOT.displayName);
    expect(root).toHaveAttribute(
      "data-hierarchy-root-id",
      MARKETING_AGENTS_ROOT.id,
    );
    const controlPlane = root.querySelector(
      `[data-node-kind="control-plane"][data-control-plane-id="${MARKETING_ORCHESTRATOR_CONTROL_PLANE.id}"]`,
    );
    expect(controlPlane).toHaveTextContent(
      MARKETING_ORCHESTRATOR_CONTROL_PLANE.displayName,
    );
    expect(controlPlane).toHaveTextContent(
      MARKETING_ORCHESTRATOR_CONTROL_PLANE.badgeLabel,
    );
    expect(controlPlane).toHaveAttribute("data-counts-as-instance", "false");
    expect(root).toHaveAccessibleName(
      /Marketing Agents.*Marketing Orchestrator.*not included in the deployed-agent inventory/u,
    );
    expect(screen.getAllByRole("treeitem")).toHaveLength(61);
    expect(
      document.querySelectorAll('[data-node-kind="instance"]'),
    ).toHaveLength(43);
    expect(
      document.querySelector(
        `[role="treeitem"][data-node-id="${MARKETING_ORCHESTRATOR_CONTROL_PLANE.id}"]`,
      ),
    ).toBeNull();
    expect(
      document.querySelector(
        `[data-node-kind="instance"][data-instance-id="${MARKETING_ORCHESTRATOR_CONTROL_PLANE.id}"]`,
      ),
    ).toBeNull();
  });

  it("WEB-07 exposes exact source-ordered ARIA metadata with one roving tab stop", () => {
    renderTree();

    expect(
      screen.getByRole("tree", { name: "Marketing Agents organization tree" }),
    ).toBeVisible();
    const visibleItems = screen.getAllByRole("treeitem");
    expect(visibleItems).toHaveLength(18);
    expect(
      visibleItems.map((node) => node.getAttribute("data-node-id")),
    ).toEqual([
      "root",
      "dept.social-media",
      "func.social-media.new-content",
      "func.social-media.research",
      "func.social-media.tracking-analysis",
      "dept.blog-seo",
      "func.blog-seo.new-content",
      "func.blog-seo.tracking-analysis",
      "dept.email",
      "func.email.newsletter",
      "func.email.lifecycle-marketing",
      "dept.community",
      "func.community.events",
      "func.community.education",
      "func.community.discussion",
      "dept.partnerships",
      "func.partnerships.implementation-partners",
      "func.partnerships.integration-partners",
    ]);
    expect(item("root")).toHaveAttribute("aria-level", "1");
    expect(item("dept.community")).toHaveAttribute("aria-posinset", "4");
    expect(item("dept.community")).toHaveAttribute("aria-setsize", "5");
    expect(item("func.community.education")).toHaveAttribute("aria-level", "3");
    expect(
      visibleItems.filter((node) => node.getAttribute("tabindex") === "0"),
    ).toHaveLength(1);
    expect(item("root")).toHaveAttribute("tabindex", "0");
  });

  it("ARCH-02 identifies Tree view as the sole complete hierarchy navigation model", () => {
    renderTree();
    const tree = screen.getByRole("tree", {
      name: "Marketing Agents organization tree",
    });

    expect(tree).toHaveAttribute("data-hierarchy-semantics", "tree-authority");
    expect(tree).toHaveAttribute(
      "aria-describedby",
      "org-tree-structure-summary org-tree-keyboard-help",
    );
    expect(tree).toHaveAccessibleDescription(
      /Tree view is the complete level-by-level hierarchy navigation model for 5 visible departments, 12 visible functions, and 43 visible deployed agents.*Use Up and Down to move/u,
    );
    expect(screen.queryByTestId("org-chart-viewport")).not.toBeInTheDocument();
    expect(screen.getAllByRole("treeitem")).toHaveLength(18);
  });

  it("ARCH-02 exposes a described empty Tree representation without an empty ARIA tree", () => {
    renderTree({ projectedQuery: "no matching source agent" });

    expect(screen.queryByRole("tree")).not.toBeInTheDocument();
    const empty = screen.getByRole("region", {
      name: "Marketing Agents organization tree empty state",
    });
    expect(empty).toHaveAttribute("data-hierarchy-semantics", "tree-empty");
    expect(empty).toHaveAccessibleDescription(
      /Tree view has no matching hierarchy nodes: 0 visible departments, 0 visible functions, and 0 visible deployed agents.*Adjust or clear filters/u,
    );
  });

  it("WEB-07 expands branches, preserves duplicate ordinals, and selects each instance independently", async () => {
    const user = userEvent.setup();
    const { onSelectionChange } = renderTree();
    const education = item("func.community.education");

    expect(education).toHaveAttribute("aria-expanded", "false");
    await user.click(education);
    expect(education).toHaveAttribute("aria-expanded", "true");

    const first = item("inst.community.education.agent-1.01");
    const second = item("inst.community.education.agent-1.02");
    expect(first).toHaveTextContent("Instance 1 of 2");
    expect(second).toHaveTextContent("Instance 2 of 2");
    expect(first).toHaveAttribute("aria-level", "4");
    expect(first).toHaveAttribute("aria-posinset", "1");
    expect(first).toHaveAttribute("aria-setsize", "6");

    await user.click(first);
    await user.click(second);
    expect(onSelectionChange.mock.calls).toEqual([
      ["inst.community.education.agent-1.01"],
      ["inst.community.education.agent-1.02"],
    ]);
  });

  it("WEB-07 implements arrows Home End activation typeahead and slash search focus", async () => {
    const { onFocusSearch, onSelectionChange } = renderTree();
    const root = item("root");
    root.focus();

    fireEvent.keyDown(root, { key: "ArrowDown" });
    await waitFor(() => expect(item("dept.social-media")).toHaveFocus());
    fireEvent.keyDown(item("dept.social-media"), { key: "ArrowRight" });
    await waitFor(() =>
      expect(item("func.social-media.new-content")).toHaveFocus(),
    );
    fireEvent.keyDown(item("func.social-media.new-content"), {
      key: "ArrowLeft",
    });
    await waitFor(() => expect(item("dept.social-media")).toHaveFocus());
    fireEvent.keyDown(item("dept.social-media"), { key: "End" });
    await waitFor(() =>
      expect(item("func.partnerships.integration-partners")).toHaveFocus(),
    );
    fireEvent.keyDown(item("func.partnerships.integration-partners"), {
      key: "Home",
    });
    await waitFor(() => expect(root).toHaveFocus());
    fireEvent.keyDown(root, { key: "c" });
    await waitFor(() => expect(item("dept.community")).toHaveFocus());

    const community = item("dept.community");
    fireEvent.keyDown(community, { key: "ArrowRight" });
    await waitFor(() => expect(item("func.community.events")).toHaveFocus());
    const events = item("func.community.events");
    fireEvent.keyDown(events, { key: "ArrowRight" });
    expect(events).toHaveAttribute("aria-expanded", "true");
    fireEvent.keyDown(events, { key: "ArrowRight" });
    await waitFor(() =>
      expect(item("inst.community.events.agent-1.01")).toHaveFocus(),
    );
    fireEvent.keyDown(item("inst.community.events.agent-1.01"), {
      key: "Enter",
    });
    expect(onSelectionChange).toHaveBeenCalledWith(
      "inst.community.events.agent-1.01",
    );

    fireEvent.keyDown(item("inst.community.events.agent-1.01"), { key: "/" });
    expect(onFocusSearch).toHaveBeenCalledOnce();
  });

  it("WEB-07 auto-expands retained ancestors so filtered instances stay exposed", async () => {
    renderTree({
      projectedQuery: "inst.community.education.agent-3.02",
      autoExpandMatches: true,
    });

    await waitFor(() =>
      expect(item("inst.community.education.agent-3.02")).toBeInTheDocument(),
    );
    expect(screen.getAllByRole("treeitem")).toHaveLength(4);
    expect(item("dept.community")).toHaveAttribute("aria-setsize", "1");
    expect(item("func.community.education")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("WEB-07 lets explicit branch choices override filtered auto-expansion", async () => {
    const user = userEvent.setup();
    renderTree({
      projectedQuery: "inst.community.education.agent-3.02",
      autoExpandMatches: true,
    });
    const education = item("func.community.education");
    const instanceId = "inst.community.education.agent-3.02";

    expect(education).toHaveAttribute("aria-expanded", "true");
    expect(item(instanceId)).toBeInTheDocument();

    await user.click(education);
    expect(education).toHaveAttribute("aria-expanded", "false");
    expect(
      document.querySelector(`[role="treeitem"][data-node-id="${instanceId}"]`),
    ).toBeNull();

    await user.click(education);
    expect(education).toHaveAttribute("aria-expanded", "true");
    expect(item(instanceId)).toBeInTheDocument();
  });
});

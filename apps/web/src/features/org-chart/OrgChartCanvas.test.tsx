// WEB-01 component evidence covers exact node counts, duplicate selection, and controls.
import { fireEvent, render, screen } from "@testing-library/react";
import { useCallback, useState } from "react";
import { describe, expect, it } from "vitest";

import { makeHierarchyPayload } from "../../test/hierarchyFixture";
import { DEFAULT_ORG_CHART_FILTERS } from "./filters";
import {
  MARKETING_AGENTS_ROOT,
  MARKETING_ORCHESTRATOR_CONTROL_PLANE,
} from "./model";
import { normalizeHierarchy } from "./normalizeHierarchy";
import { OrgChartCanvas } from "./OrgChartCanvas";
import { projectHierarchy } from "./projectHierarchy";

const hierarchy = normalizeHierarchy(makeHierarchyPayload());

function CanvasHarness(): React.JSX.Element {
  const [selection, setSelection] = useState<string | null>(null);
  const changeSelection = useCallback((instanceId: string | null) => {
    setSelection(instanceId);
  }, []);
  return (
    <OrgChartCanvas
      hierarchy={hierarchy}
      selectedInstanceId={selection}
      onSelectionChange={changeSelection}
    />
  );
}

describe("WEB-01 interactive org chart canvas", () => {
  it("renders exactly one root, five departments, twelve functions, and 43 instances", () => {
    const { container } = render(<CanvasHarness />);
    expect(container.querySelectorAll('[data-node-kind="root"]')).toHaveLength(
      1,
    );
    expect(
      container.querySelectorAll('[data-node-kind="department"]'),
    ).toHaveLength(5);
    expect(
      container.querySelectorAll('[data-node-kind="function"]'),
    ).toHaveLength(12);
    expect(
      container.querySelectorAll('[data-node-kind="instance"]'),
    ).toHaveLength(43);
    expect(
      [...container.querySelectorAll<HTMLElement>("[data-instance-count]")].map(
        (node) => node.dataset.instanceCount,
      ),
    ).toEqual(["12", "6", "5", "14", "6"]);
    expect(
      container.querySelectorAll(".hierarchy-connectors line"),
    ).toHaveLength(41);
  });

  it("keeps each Community deployment independently selectable and accessibly named", () => {
    const { container } = render(<CanvasHarness />);
    const pair = container.querySelectorAll<HTMLButtonElement>(
      '[data-template-id="tpl.community.events.agent-1"]',
    );
    expect(pair).toHaveLength(2);
    expect(pair[0]).toHaveAccessibleName(/Instance 1 of 2/u);
    expect(pair[1]).toHaveAccessibleName(/Instance 2 of 2/u);

    if (pair[0] !== undefined && pair[1] !== undefined) {
      fireEvent.click(pair[0]);
      expect(pair[0]).toHaveAttribute("aria-pressed", "true");
      fireEvent.click(pair[1]);
      expect(pair[0]).toHaveAttribute("aria-pressed", "false");
      expect(pair[1]).toHaveAttribute("aria-pressed", "true");
      expect(pair[0].dataset.instanceId).not.toBe(pair[1].dataset.instanceId);
    }
  });

  it("offers working zoom, fit, and keyboard pan controls", () => {
    render(<CanvasHarness />);
    const viewport = screen.getByTestId("org-chart-viewport");
    expect(viewport).toHaveAttribute("data-viewport-zoom", "1");

    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(viewport).toHaveAttribute("data-viewport-zoom", "1.2");
    expect(screen.getByTestId("zoom-readout")).toHaveTextContent("120%");

    viewport.focus();
    const beforePanX = Number(viewport.getAttribute("data-viewport-x"));
    const beforePanY = Number(viewport.getAttribute("data-viewport-y"));
    fireEvent.keyDown(viewport, { key: "ArrowRight" });
    expect(Number(viewport.getAttribute("data-viewport-x"))).toBe(
      beforePanX - 48,
    );
    expect(Number(viewport.getAttribute("data-viewport-y"))).toBe(beforePanY);
    expect(viewport).toHaveAttribute("data-viewport-intent", "manual");

    for (let index = 0; index < 10; index += 1) {
      fireEvent.keyDown(viewport, { key: "ArrowRight" });
    }
    expect(viewport).toHaveAttribute("data-viewport-x", "-304");
    for (let index = 0; index < 10; index += 1) {
      fireEvent.keyDown(viewport, { key: "ArrowLeft" });
    }
    expect(viewport).toHaveAttribute("data-viewport-x", "64");

    fireEvent.click(screen.getByRole("button", { name: "Fit hierarchy" }));
    expect(viewport).toHaveAttribute("data-viewport-intent", "auto-fit");
    expect(viewport).toHaveAttribute("data-viewport-x", "28");
    expect(viewport).toHaveAttribute("data-viewport-y", "51");
  });

  it("uses one roving graph-card tab stop and arrow-key navigation", () => {
    const { container } = render(<CanvasHarness />);
    const cards = [
      ...container.querySelectorAll<HTMLButtonElement>("[data-instance-id]"),
    ];
    expect(cards).toHaveLength(43);
    expect(cards.filter((card) => card.tabIndex === 0)).toHaveLength(1);
    expect(cards[0]).toHaveAttribute("tabindex", "0");

    const firstCard = cards[0];
    if (firstCard === undefined)
      throw new Error("Expected the first graph card");
    firstCard.focus();
    fireEvent.keyDown(firstCard, { key: "ArrowDown" });
    expect(cards[1]).toHaveFocus();
    expect(cards[1]).toHaveAttribute("tabindex", "0");
    expect(cards[0]).toHaveAttribute("tabindex", "-1");
  });

  it("ARCH-02 exposes a projection-aware graph region without claiming tree semantics", () => {
    const { container } = render(<CanvasHarness />);
    const viewport = screen.getByRole("region", {
      name: "Marketing Agents interactive organization chart",
    });

    expect(viewport).toHaveAttribute("data-hierarchy-semantics", "visual");
    expect(viewport).toHaveAttribute(
      "aria-describedby",
      "org-chart-structure-summary chart-keyboard-help",
    );
    expect(viewport).toHaveAccessibleDescription(
      /Graph view shows 5 visible departments, 12 visible functions, and 43 visible deployed agents.*For complete level-by-level hierarchy navigation, use Tree view.*Drag the chart to pan/u,
    );
    expect(screen.queryByRole("tree")).not.toBeInTheDocument();
    expect(container.querySelectorAll('[role="treeitem"]')).toHaveLength(0);

    const firstCard = container.querySelector<HTMLButtonElement>(
      '[data-instance-id="inst.social-media.new-content.agent-1.01"]',
    );
    expect(firstCard).toHaveAccessibleDescription(
      "Department: Social media. Function: New content. Hierarchy level 4.",
    );
  });

  it("ARCH-02 describes an empty graph without inventing headings or agent controls", () => {
    const projection = projectHierarchy(hierarchy, {
      ...DEFAULT_ORG_CHART_FILTERS,
      q: "no matching source agent",
    });
    const { container } = render(
      <OrgChartCanvas
        hierarchy={projection.hierarchy}
        selectedInstanceId={null}
        onSelectionChange={() => undefined}
      />,
    );
    const viewport = screen.getByRole("region", {
      name: "Marketing Agents interactive organization chart",
    });

    expect(viewport).toHaveAccessibleDescription(
      /Graph view shows 0 visible departments, 0 visible functions, and 0 visible deployed agents.*No department headings, function headings, or agent controls are present/u,
    );
    expect(container.querySelector('[data-node-kind="department"]')).toBeNull();
    expect(container.querySelector('[data-node-kind="function"]')).toBeNull();
    expect(container.querySelector('[data-node-kind="instance"]')).toBeNull();
  });

  it("ORCH-01 keeps the named control plane outside the 43 source agent cards", () => {
    const { container } = render(<CanvasHarness />);
    const root = container.querySelector(
      `[data-node-kind="root"][data-hierarchy-root-id="${MARKETING_AGENTS_ROOT.id}"]`,
    );
    expect(root).toHaveTextContent(MARKETING_AGENTS_ROOT.displayName);
    const controlPlane = root?.querySelector(
      `[data-node-kind="control-plane"][data-control-plane-id="${MARKETING_ORCHESTRATOR_CONTROL_PLANE.id}"]`,
    );
    expect(controlPlane).toHaveTextContent(
      MARKETING_ORCHESTRATOR_CONTROL_PLANE.displayName,
    );
    expect(controlPlane).toHaveTextContent(
      MARKETING_ORCHESTRATOR_CONTROL_PLANE.badgeLabel,
    );
    expect(controlPlane).toHaveAttribute("data-counts-as-instance", "false");
    expect(controlPlane).toHaveAccessibleName(
      /Marketing Orchestrator, implementation control plane, not included in the deployed-agent inventory/u,
    );
    expect(
      container.querySelectorAll('[data-node-kind="instance"]'),
    ).toHaveLength(43);
    expect(
      container.querySelector(
        `[data-node-kind="instance"][data-instance-id="${MARKETING_ORCHESTRATOR_CONTROL_PLANE.id}"]`,
      ),
    ).toBeNull();
    expect(screen.getAllByRole("button")).toHaveLength(46);
  });
});

import { fireEvent, render, waitFor } from "@testing-library/react";
import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";

import { makeHierarchyPayload } from "../../test/hierarchyFixture";
import { DEFAULT_ORG_CHART_FILTERS } from "./filters";
import { normalizeHierarchy } from "./normalizeHierarchy";
import { projectHierarchy, type HierarchyProjection } from "./projectHierarchy";
import { useFilteredFocus } from "./useFilteredFocus";

const hierarchy = normalizeHierarchy(makeHierarchyPayload());
const selectedId = "inst.social-media.new-content.agent-1.01";

function FocusHarness({
  projection,
  onSelectionChange,
}: {
  readonly projection: HierarchyProjection;
  readonly onSelectionChange: (value: string | null) => void;
}): React.JSX.Element {
  const searchRef = createRef<HTMLInputElement>();
  useFilteredFocus({
    sourceHierarchy: hierarchy,
    projection,
    selectedInstanceId: selectedId,
    onSelectionChange,
    searchRef,
  });
  const firstDepartment = projection.hierarchy.departments[0];
  const firstFunction = firstDepartment?.functions[0];
  return (
    <div>
      <input ref={searchRef} aria-label="Search agents" />
      {projection.matchedInstanceCount === 0 ? null : (
        <div data-node-kind="root" data-node-id="root" tabIndex={-1} />
      )}
      {firstDepartment === undefined ? null : (
        <div
          data-focus-node-kind="department"
          data-node-id={firstDepartment.id}
          tabIndex={-1}
        />
      )}
      {firstFunction === undefined ? null : (
        <div
          data-focus-node-kind="function"
          data-node-id={firstFunction.id}
          tabIndex={-1}
        />
      )}
      {firstFunction?.instances.map((instance) => (
        <button
          key={instance.id}
          type="button"
          data-node-kind="instance"
          data-node-id={instance.id}
        >
          {instance.displayName}
        </button>
      ))}
    </div>
  );
}

describe("WEB-02 filtered focus repair", () => {
  it("moves a removed instance to its retained function and clears selection", async () => {
    const onSelectionChange = vi.fn();
    const full = projectHierarchy(hierarchy, DEFAULT_ORG_CHART_FILTERS);
    const retainedSibling = projectHierarchy(hierarchy, {
      ...DEFAULT_ORG_CHART_FILTERS,
      q: "inst.social-media.new-content.agent-2.01",
    });
    const view = render(
      <FocusHarness projection={full} onSelectionChange={onSelectionChange} />,
    );
    const selected = view.container.querySelector<HTMLElement>(
      `[data-node-id="${selectedId}"]`,
    );
    if (selected === null) throw new Error("selected fixture node is missing");
    fireEvent.focus(selected);

    view.rerender(
      <FocusHarness
        projection={retainedSibling}
        onSelectionChange={onSelectionChange}
      />,
    );

    await waitFor(() =>
      expect(
        view.container.querySelector(
          '[data-node-id="func.social-media.new-content"]',
        ),
      ).toHaveFocus(),
    );
    expect(onSelectionChange).toHaveBeenCalledWith(null);
  });

  it("moves focus to search when filtering removes every ancestor", async () => {
    const full = projectHierarchy(hierarchy, DEFAULT_ORG_CHART_FILTERS);
    const empty = projectHierarchy(hierarchy, {
      ...DEFAULT_ORG_CHART_FILTERS,
      q: "no-agent-can-match-this",
    });
    const view = render(
      <FocusHarness projection={full} onSelectionChange={vi.fn()} />,
    );
    const selected = view.container.querySelector<HTMLElement>(
      `[data-node-id="${selectedId}"]`,
    );
    if (selected === null) throw new Error("selected fixture node is missing");
    fireEvent.focus(selected);
    view.rerender(
      <FocusHarness projection={empty} onSelectionChange={vi.fn()} />,
    );
    await waitFor(() =>
      expect(
        view.getByRole("textbox", { name: "Search agents" }),
      ).toHaveFocus(),
    );
  });
});

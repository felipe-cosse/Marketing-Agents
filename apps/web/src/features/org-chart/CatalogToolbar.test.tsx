import { createRef } from "react";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { makeHierarchyPayload } from "../../test/hierarchyFixture";
import {
  CatalogToolbar,
  type CatalogFilters,
  type CatalogToolbarProps,
} from "./CatalogToolbar";
import { normalizeHierarchy } from "./normalizeHierarchy";

const hierarchy = normalizeHierarchy(makeHierarchyPayload());
const searchRef = createRef<HTMLInputElement>();

const options: CatalogToolbarProps["options"] = {
  departments: [
    { value: "dept.social-media", label: "Social media" },
    { value: "dept.blog-seo", label: "Blog & SEO" },
    { value: "dept.email", label: "Email" },
    { value: "dept.community", label: "Community" },
    { value: "dept.partnerships", label: "Partnerships" },
  ],
  functions: [
    {
      value: "func.social-media.new-content",
      label: "New content",
      departmentId: "dept.social-media",
    },
    {
      value: "func.social-media.research",
      label: "Research",
      departmentId: "dept.social-media",
    },
    {
      value: "func.blog-seo.new-content",
      label: "New content",
      departmentId: "dept.blog-seo",
    },
  ],
  deploymentStates: [
    { value: "enabled", label: "Enabled" },
    { value: "disabled", label: "Disabled" },
  ],
  recentRunStates: [
    { value: "completed", label: "Completed" },
    { value: "failed", label: "Failed" },
  ],
  capabilities: [
    { value: "cap.catalog.read", label: "Read catalog" },
    { value: "cap.newsletter.write", label: "Write newsletter" },
  ],
};

const emptyFilters: CatalogFilters = {
  q: "",
  departmentId: "",
  functionId: "",
  deploymentState: "",
  recentRunState: "",
  capabilityId: "",
};

function makeProps(
  overrides: Partial<CatalogToolbarProps> = {},
): CatalogToolbarProps {
  return {
    hierarchy,
    options,
    filters: emptyFilters,
    resultCount: 43,
    statusAvailable: true,
    statusStale: false,
    searchRef,
    onQueryChange: vi.fn(),
    onDepartmentChange: vi.fn(),
    onFunctionChange: vi.fn(),
    onDeploymentStateChange: vi.fn(),
    onRecentRunStateChange: vi.fn(),
    onCapabilityChange: vi.fn(),
    onClear: vi.fn(),
    ...overrides,
  };
}

describe("WEB-02 catalog toolbar presentation", () => {
  it("keeps options in source order and constrains Function by Department", () => {
    const props = makeProps();
    const { rerender } = render(<CatalogToolbar {...props} />);

    fireEvent.click(screen.getByRole("button", { name: "Filters" }));
    const department = screen.getByRole("combobox", { name: "Department" });
    const agentFunction = screen.getByRole("combobox", { name: "Function" });
    expect(
      within(department)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual([
      "All departments",
      "Social media",
      "Blog & SEO",
      "Email",
      "Community",
      "Partnerships",
    ]);
    expect(agentFunction).toBeDisabled();
    expect(within(agentFunction).getAllByRole("option")).toHaveLength(1);

    rerender(
      <CatalogToolbar
        {...props}
        filters={{ ...emptyFilters, departmentId: "dept.social-media" }}
      />,
    );
    expect(agentFunction).toBeEnabled();
    expect(
      within(agentFunction)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["All functions", "New content", "Research"]);
  });

  it("reports query and independent filter changes and closes with Escape", () => {
    const onQueryChange = vi.fn();
    const onDepartmentChange = vi.fn();
    const onDeploymentStateChange = vi.fn();
    const onRecentRunStateChange = vi.fn();
    render(
      <CatalogToolbar
        {...makeProps({
          onQueryChange,
          onDepartmentChange,
          onDeploymentStateChange,
          onRecentRunStateChange,
        })}
      />,
    );

    fireEvent.change(screen.getByRole("searchbox", { name: "Search agents" }), {
      target: { value: "newsletter" },
    });
    expect(onQueryChange).toHaveBeenCalledWith("newsletter");

    const trigger = screen.getByRole("button", { name: "Filters" });
    fireEvent.click(trigger);
    fireEvent.change(screen.getByRole("combobox", { name: "Department" }), {
      target: { value: "dept.email" },
    });
    fireEvent.change(
      screen.getByRole("combobox", { name: "Deployment state" }),
      { target: { value: "enabled" } },
    );
    fireEvent.change(
      screen.getByRole("combobox", { name: "Recent run state" }),
      { target: { value: "failed" } },
    );
    expect(onDepartmentChange).toHaveBeenCalledWith("dept.email");
    expect(onDeploymentStateChange).toHaveBeenCalledWith("enabled");
    expect(onRecentRunStateChange).toHaveBeenCalledWith("failed");

    fireEvent.keyDown(document, { key: "Escape" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveFocus();
    expect(
      screen.queryByRole("dialog", { name: "Catalog filters" }),
    ).toBeNull();
  });

  it("focuses search with slash unless the user is editing another control", () => {
    render(
      <>
        <CatalogToolbar {...makeProps()} />
        <textarea aria-label="Other editor" />
      </>,
    );
    const search = screen.getByRole("searchbox", { name: "Search agents" });
    const editor = screen.getByRole("textbox", { name: "Other editor" });

    fireEvent.keyDown(document, { key: "/" });
    expect(search).toHaveFocus();

    editor.focus();
    fireEvent.keyDown(editor, { key: "/" });
    expect(editor).toHaveFocus();
  });

  it("WEB-08 does not move focus out of another active modal with slash", () => {
    render(
      <>
        <CatalogToolbar {...makeProps()} />
        <section role="dialog" aria-modal="true" aria-label="Other modal">
          <button type="button">Modal action</button>
        </section>
      </>,
    );
    const search = screen.getByRole("searchbox", { name: "Search agents" });
    const modalAction = screen.getByRole("button", { name: "Modal action" });

    modalAction.focus();
    fireEvent.keyDown(modalAction, { key: "/" });

    expect(modalAction).toHaveFocus();
    expect(search).not.toHaveFocus();
  });

  it("WEB-08 closes the narrow modal backdrop without activating background UI", () => {
    const backgroundPointerDown = vi.fn();
    render(
      <>
        <CatalogToolbar {...makeProps({ modal: true })} />
        <button type="button" onPointerDown={backgroundPointerDown}>
          Background action
        </button>
      </>,
    );
    const trigger = screen.getByRole("button", { name: "Filters" });
    fireEvent.click(trigger);
    const backdrop = document.querySelector<HTMLElement>(
      ".catalog-filter-backdrop",
    );
    if (backdrop === null) throw new Error("Expected modal filter backdrop");

    fireEvent.pointerDown(backdrop);

    expect(backgroundPointerDown).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("dialog", { name: "Catalog filters" }),
    ).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it("renders removable active filters, compact summary, and Clear all", () => {
    const onDepartmentChange = vi.fn();
    const onDeploymentStateChange = vi.fn();
    const onClear = vi.fn();
    render(
      <CatalogToolbar
        {...makeProps({
          filters: {
            ...emptyFilters,
            q: "email",
            departmentId: "dept.email",
            deploymentState: "enabled",
          },
          resultCount: 4,
          onDepartmentChange,
          onDeploymentStateChange,
          onClear,
        })}
      />,
    );

    expect(screen.getByText("2 active")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", {
        name: "Remove Department: Email filter",
      }),
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: "Remove Deployment: Enabled filter",
      }),
    );
    expect(onDepartmentChange).toHaveBeenCalledWith("");
    expect(onDeploymentStateChange).toHaveBeenCalledWith("");

    fireEvent.click(screen.getByRole("button", { name: "Clear all" }));
    expect(onClear).toHaveBeenCalledOnce();
    expect(screen.getByRole("status")).toHaveTextContent("4 of 43 agents");
  });

  it("keeps recent run status distinct and exposes unavailable and stale states", () => {
    const props = makeProps({ statusAvailable: false, statusStale: false });
    const { rerender } = render(<CatalogToolbar {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Filters" }));
    const recentRun = screen.getByRole("combobox", {
      name: "Recent run state",
    });
    expect(recentRun).toBeDisabled();
    expect(screen.getByText("Recent run status is unavailable.")).toBeVisible();

    rerender(
      <CatalogToolbar {...props} statusAvailable={true} statusStale={true} />,
    );
    expect(recentRun).toBeEnabled();
    expect(screen.getByText("Recent run status may be stale.")).toBeVisible();
  });

  it("announces singular, plural, and empty result treatment", () => {
    const props = makeProps({ resultCount: 1 });
    const { rerender } = render(<CatalogToolbar {...props} />);
    expect(screen.getByRole("status")).toHaveTextContent("1 of 43 agents");

    rerender(<CatalogToolbar {...props} resultCount={43} />);
    expect(screen.getByRole("status")).toHaveTextContent("43 agents");

    rerender(<CatalogToolbar {...props} resultCount={0} />);
    expect(screen.getByRole("status")).toHaveTextContent("No agents match");

    rerender(
      <CatalogToolbar
        {...props}
        resultCount={0}
        resultAnnouncement="Recent run status unavailable"
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Recent run status unavailable",
    );
  });

  it("restores focus when a removable filter or Clear all disappears", async () => {
    render(
      <CatalogToolbar
        {...makeProps({
          filters: {
            ...emptyFilters,
            departmentId: "dept.email",
          },
          resultCount: 5,
        })}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: "Remove Department: Email filter",
      }),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Filters" })).toHaveFocus(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Clear all" }));
    await waitFor(() =>
      expect(
        screen.getByRole("searchbox", { name: "Search agents" }),
      ).toHaveFocus(),
    );
  });
});

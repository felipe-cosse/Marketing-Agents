import type { HierarchyViewMode } from "./hierarchyViewMode";

interface HierarchyViewToggleProps {
  readonly mode: HierarchyViewMode;
  readonly automatic: boolean;
  readonly restoreFocus?: boolean | undefined;
  readonly onChange: (mode: HierarchyViewMode) => void;
}

function GraphIcon(): React.JSX.Element {
  return (
    <svg aria-hidden="true" fill="none" focusable="false" viewBox="0 0 24 24">
      <rect
        x="9"
        y="3"
        width="6"
        height="5"
        rx="1.5"
        stroke="currentColor"
        strokeWidth="1.7"
      />
      <rect
        x="3"
        y="16"
        width="6"
        height="5"
        rx="1.5"
        stroke="currentColor"
        strokeWidth="1.7"
      />
      <rect
        x="15"
        y="16"
        width="6"
        height="5"
        rx="1.5"
        stroke="currentColor"
        strokeWidth="1.7"
      />
      <path
        d="M12 8v4m0 0H6v4m6-4h6v4"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.7"
      />
    </svg>
  );
}

function TreeIcon(): React.JSX.Element {
  return (
    <svg aria-hidden="true" fill="none" focusable="false" viewBox="0 0 24 24">
      <path
        d="M5 5v14m0-10h4m-4 7h4"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.7"
      />
      <rect
        x="9"
        y="6"
        width="10"
        height="6"
        rx="1.5"
        stroke="currentColor"
        strokeWidth="1.7"
      />
      <rect
        x="9"
        y="13"
        width="10"
        height="6"
        rx="1.5"
        stroke="currentColor"
        strokeWidth="1.7"
      />
    </svg>
  );
}

export function HierarchyViewToggle({
  mode,
  automatic,
  restoreFocus = false,
  onChange,
}: HierarchyViewToggleProps): React.JSX.Element {
  return (
    <div
      className="hierarchy-view-toggle"
      role="group"
      aria-label="Hierarchy view"
      data-view-selection={automatic ? "automatic" : "explicit"}
    >
      <button
        type="button"
        autoFocus={restoreFocus && mode === "graph"}
        data-hierarchy-view="graph"
        aria-label="Graph view"
        aria-pressed={mode === "graph"}
        onClick={() => onChange("graph")}
      >
        <GraphIcon />
        <span>Graph</span>
      </button>
      <button
        type="button"
        autoFocus={restoreFocus && mode === "tree"}
        data-hierarchy-view="tree"
        aria-label="Tree view"
        aria-pressed={mode === "tree"}
        onClick={() => onChange("tree")}
      >
        <TreeIcon />
        <span>Tree</span>
      </button>
    </div>
  );
}

import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";

import type { NormalizedHierarchy } from "./model";

export interface CatalogFilterOption {
  readonly value: string;
  readonly label: string;
}

export interface FunctionFilterOption extends CatalogFilterOption {
  readonly departmentId: string;
}

export interface CatalogFilterOptions {
  readonly departments: readonly CatalogFilterOption[];
  readonly functions: readonly FunctionFilterOption[];
  readonly deploymentStates: readonly CatalogFilterOption[];
  readonly recentRunStates: readonly CatalogFilterOption[];
  readonly capabilities: readonly CatalogFilterOption[];
}

export interface CatalogFilters {
  readonly q: string;
  readonly departmentId: string;
  readonly functionId: string;
  readonly deploymentState: string;
  readonly recentRunState: string;
  readonly capabilityId: string;
}

export interface CatalogToolbarProps {
  readonly hierarchy: NormalizedHierarchy;
  readonly options: CatalogFilterOptions;
  readonly filters: CatalogFilters;
  readonly resultCount: number;
  readonly resultAnnouncement?: string | undefined;
  readonly statusAvailable: boolean;
  readonly statusStale: boolean;
  readonly searchRef: RefObject<HTMLInputElement | null>;
  readonly onQueryChange: (value: string) => void;
  readonly onDepartmentChange: (value: string) => void;
  readonly onFunctionChange: (value: string) => void;
  readonly onDeploymentStateChange: (value: string) => void;
  readonly onRecentRunStateChange: (value: string) => void;
  readonly onCapabilityChange: (value: string) => void;
  readonly onClear: () => void;
  readonly modal?: boolean;
}

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not(:disabled)",
  "input:not(:disabled)",
  "select:not(:disabled)",
  "textarea:not(:disabled)",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

interface ActiveFilter {
  readonly key:
    "department" | "function" | "deployment" | "recent-run" | "capability";
  readonly label: string;
  readonly onRemove: () => void;
}

function FunnelIcon(): React.JSX.Element {
  return (
    <svg aria-hidden="true" fill="none" focusable="false" viewBox="0 0 24 24">
      <path
        d="M4 5h16l-6.2 7.2v5.4l-3.6 1.8v-7.2L4 5Z"
        stroke="currentColor"
        strokeLinejoin="round"
        strokeWidth="1.7"
      />
    </svg>
  );
}

function SearchIcon(): React.JSX.Element {
  return (
    <svg aria-hidden="true" fill="none" focusable="false" viewBox="0 0 24 24">
      <circle
        cx="10.5"
        cy="10.5"
        r="5.5"
        stroke="currentColor"
        strokeWidth="1.8"
      />
      <path
        d="m15 15 4 4"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.8"
      />
    </svg>
  );
}

function ChevronIcon(): React.JSX.Element {
  return (
    <svg aria-hidden="true" fill="none" focusable="false" viewBox="0 0 24 24">
      <path
        d="m8 10 4 4 4-4"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.8"
      />
    </svg>
  );
}

function CloseIcon(): React.JSX.Element {
  return (
    <svg aria-hidden="true" fill="none" focusable="false" viewBox="0 0 24 24">
      <path
        d="m8 8 8 8m0-8-8 8"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.8"
      />
    </svg>
  );
}

function isEditableTarget(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    target.closest('input, textarea, select, [contenteditable="true"]') !== null
  );
}

function selectedLabel(
  options: readonly CatalogFilterOption[],
  value: string,
): string | null {
  return options.find((option) => option.value === value)?.label ?? null;
}

export function CatalogToolbar({
  hierarchy,
  options,
  filters,
  resultCount,
  resultAnnouncement,
  statusAvailable,
  statusStale,
  searchRef,
  onQueryChange,
  onDepartmentChange,
  onFunctionChange,
  onDeploymentStateChange,
  onRecentRunStateChange,
  onCapabilityChange,
  onClear,
  modal = false,
}: CatalogToolbarProps): React.JSX.Element {
  const [filterOpen, setFilterOpen] = useState(false);
  const [panelPosition, setPanelPosition] = useState({ top: 0, left: 0 });
  const toolbarId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = `${toolbarId}-filter-panel`;
  const departmentId = `${toolbarId}-department`;
  const functionId = `${toolbarId}-function`;
  const deploymentId = `${toolbarId}-deployment`;
  const recentRunId = `${toolbarId}-recent-run`;
  const recentRunNoteId = `${toolbarId}-recent-run-note`;
  const capabilityId = `${toolbarId}-capability`;

  const functionOptions = useMemo(
    () =>
      options.functions.filter(
        (option) => option.departmentId === filters.departmentId,
      ),
    [filters.departmentId, options.functions],
  );

  const activeFilters = useMemo<readonly ActiveFilter[]>(() => {
    const active: ActiveFilter[] = [];
    const department = selectedLabel(options.departments, filters.departmentId);
    if (department !== null) {
      active.push({
        key: "department",
        label: `Department: ${department}`,
        onRemove: () => onDepartmentChange(""),
      });
    }
    const agentFunction = selectedLabel(options.functions, filters.functionId);
    if (agentFunction !== null) {
      active.push({
        key: "function",
        label: `Function: ${agentFunction}`,
        onRemove: () => onFunctionChange(""),
      });
    }
    const deployment = selectedLabel(
      options.deploymentStates,
      filters.deploymentState,
    );
    if (deployment !== null) {
      active.push({
        key: "deployment",
        label: `Deployment: ${deployment}`,
        onRemove: () => onDeploymentStateChange(""),
      });
    }
    const recentRun = selectedLabel(
      options.recentRunStates,
      filters.recentRunState,
    );
    if (recentRun !== null) {
      active.push({
        key: "recent-run",
        label: `Recent run: ${recentRun}`,
        onRemove: () => onRecentRunStateChange(""),
      });
    }
    const capability = selectedLabel(
      options.capabilities,
      filters.capabilityId,
    );
    if (capability !== null) {
      active.push({
        key: "capability",
        label: `Capability: ${capability}`,
        onRemove: () => onCapabilityChange(""),
      });
    }
    return active;
  }, [
    filters.capabilityId,
    filters.departmentId,
    filters.deploymentState,
    filters.functionId,
    filters.recentRunState,
    onCapabilityChange,
    onDepartmentChange,
    onDeploymentStateChange,
    onFunctionChange,
    onRecentRunStateChange,
    options.capabilities,
    options.departments,
    options.deploymentStates,
    options.functions,
    options.recentRunStates,
  ]);

  const hasQueryState = filters.q.trim() !== "" || activeFilters.length > 0;
  const totalCount = hierarchy.counts.instances;
  const resultText =
    resultAnnouncement ??
    (resultCount === 0
      ? "No agents match"
      : resultCount === totalCount
        ? `${String(resultCount)} agents`
        : `${String(resultCount)} of ${String(totalCount)} agents`);

  const clearAndFocusSearch = (): void => {
    onClear();
    setFilterOpen(false);
    requestAnimationFrame(() => searchRef.current?.focus());
  };
  const closeAndFocusTrigger = useCallback((): void => {
    setFilterOpen(false);
    triggerRef.current?.focus();
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent): void => {
      const eventElement =
        event.target instanceof Element
          ? event.target
          : document.activeElement instanceof Element
            ? document.activeElement
            : null;
      const activeModal =
        eventElement?.closest<HTMLElement>('[aria-modal="true"]') ?? null;
      if (
        event.key === "/" &&
        !event.altKey &&
        !event.ctrlKey &&
        !event.metaKey &&
        !event.defaultPrevented &&
        (activeModal === null || activeModal === panelRef.current) &&
        !isEditableTarget(eventElement)
      ) {
        event.preventDefault();
        setFilterOpen(false);
        searchRef.current?.focus();
        return;
      }
      if (event.key === "Escape" && filterOpen) {
        event.preventDefault();
        closeAndFocusTrigger();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [closeAndFocusTrigger, filterOpen, searchRef]);

  useLayoutEffect(() => {
    if (!filterOpen) return undefined;
    const updatePanelPosition = (): void => {
      const rect = triggerRef.current?.getBoundingClientRect();
      if (rect === undefined) return;
      const panelWidth = Math.min(360, window.innerWidth - 24);
      setPanelPosition({
        top: rect.bottom + 8,
        left: Math.max(
          12,
          Math.min(rect.left, window.innerWidth - panelWidth - 12),
        ),
      });
    };
    updatePanelPosition();
    panelRef.current?.querySelector<HTMLSelectElement>("select")?.focus();
    const trapFocus = (event: KeyboardEvent): void => {
      if (!modal || event.key !== "Tab") return;
      const panel = panelRef.current;
      if (panel === null) return;
      const focusable = [
        ...panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ];
      const first = focusable[0];
      const last = focusable.at(-1);
      const active = document.activeElement;
      if (first === undefined || last === undefined) {
        event.preventDefault();
        panel.focus();
      } else if (active === panel) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (
        event.shiftKey &&
        (active === first ||
          !(active instanceof Node) ||
          !panel.contains(active))
      ) {
        event.preventDefault();
        last.focus();
      } else if (
        !event.shiftKey &&
        (active === last ||
          !(active instanceof Node) ||
          !panel.contains(active))
      ) {
        event.preventDefault();
        first.focus();
      }
    };
    const handlePointerDown = (event: PointerEvent): void => {
      if (modal) return;
      const target = event.target;
      if (
        target instanceof Node &&
        panelRef.current?.contains(target) !== true &&
        triggerRef.current?.contains(target) !== true
      ) {
        setFilterOpen(false);
      }
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", trapFocus);
    window.addEventListener("resize", updatePanelPosition);
    window.addEventListener("scroll", updatePanelPosition, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", trapFocus);
      window.removeEventListener("resize", updatePanelPosition);
      window.removeEventListener("scroll", updatePanelPosition, true);
    };
  }, [filterOpen, modal]);

  return (
    <div
      className="catalog-toolbar"
      role="search"
      aria-label="Catalog search and filters"
    >
      <div className="catalog-toolbar__filter-anchor">
        <button
          ref={triggerRef}
          type="button"
          className="catalog-filter-trigger"
          aria-controls={panelId}
          aria-expanded={filterOpen}
          onClick={() => setFilterOpen((open) => !open)}
        >
          <FunnelIcon />
          <span>Filters</span>
          {activeFilters.length > 0 ? (
            <span className="catalog-filter-trigger__count" aria-hidden="true">
              {activeFilters.length}
            </span>
          ) : null}
          <span className="catalog-filter-trigger__chevron">
            <ChevronIcon />
          </span>
        </button>

        {filterOpen
          ? createPortal(
              <>
                {modal ? (
                  <div
                    aria-hidden="true"
                    className="catalog-filter-backdrop"
                    onPointerDown={(event) => {
                      event.preventDefault();
                      event.stopPropagation();
                      closeAndFocusTrigger();
                    }}
                  />
                ) : null}
                <div
                  ref={panelRef}
                  id={panelId}
                  className="catalog-filter-panel"
                  role="dialog"
                  aria-modal={modal ? "true" : undefined}
                  aria-label="Catalog filters"
                  tabIndex={modal ? -1 : undefined}
                  style={panelPosition}
                >
                  <div className="catalog-filter-panel__heading">
                    <div>
                      <strong>Filter agents</strong>
                      <span>{activeFilters.length} active</span>
                    </div>
                    <button type="button" onClick={closeAndFocusTrigger}>
                      Close
                    </button>
                  </div>

                  <div className="catalog-filter-field">
                    <label htmlFor={departmentId}>Department</label>
                    <select
                      id={departmentId}
                      value={filters.departmentId}
                      onChange={(event) =>
                        onDepartmentChange(event.target.value)
                      }
                    >
                      <option value="">All departments</option>
                      {options.departments.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="catalog-filter-field">
                    <label htmlFor={functionId}>Function</label>
                    <select
                      id={functionId}
                      value={filters.functionId}
                      disabled={filters.departmentId === ""}
                      onChange={(event) => onFunctionChange(event.target.value)}
                    >
                      <option value="">
                        {filters.departmentId === ""
                          ? "Select a department first"
                          : "All functions"}
                      </option>
                      {functionOptions.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="catalog-filter-field">
                    <label htmlFor={deploymentId}>Deployment state</label>
                    <select
                      id={deploymentId}
                      value={filters.deploymentState}
                      onChange={(event) =>
                        onDeploymentStateChange(event.target.value)
                      }
                    >
                      <option value="">Any deployment state</option>
                      {options.deploymentStates.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="catalog-filter-field">
                    <label htmlFor={recentRunId}>Recent run state</label>
                    <select
                      id={recentRunId}
                      value={filters.recentRunState}
                      disabled={!statusAvailable}
                      aria-describedby={recentRunNoteId}
                      onChange={(event) =>
                        onRecentRunStateChange(event.target.value)
                      }
                    >
                      <option value="">Any recent run state</option>
                      {options.recentRunStates.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                    <span
                      id={recentRunNoteId}
                      className="catalog-filter-field__note"
                    >
                      {!statusAvailable
                        ? "Recent run status is unavailable."
                        : statusStale
                          ? "Recent run status may be stale."
                          : "Runtime status is current."}
                    </span>
                  </div>

                  <div className="catalog-filter-field">
                    <label htmlFor={capabilityId}>Capability</label>
                    <select
                      id={capabilityId}
                      value={filters.capabilityId}
                      onChange={(event) =>
                        onCapabilityChange(event.target.value)
                      }
                    >
                      <option value="">Any capability</option>
                      {options.capabilities.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="catalog-filter-panel__footer">
                    <button
                      type="button"
                      onClick={clearAndFocusSearch}
                      disabled={!hasQueryState}
                    >
                      Clear all
                    </button>
                  </div>
                </div>
              </>,
              document.body,
            )
          : null}
      </div>

      <label className="catalog-search">
        <span className="sr-only">Search agents</span>
        <SearchIcon />
        <input
          ref={searchRef}
          type="search"
          value={filters.q}
          placeholder="Search agents, purposes, IDs, or capabilities…"
          onChange={(event) => onQueryChange(event.target.value)}
        />
        <kbd aria-hidden="true">/</kbd>
      </label>

      {activeFilters.length > 0 ? (
        <div className="catalog-active-filters" aria-label="Active filters">
          {activeFilters.map((filter) => (
            <span key={filter.key} className="catalog-filter-chip">
              <span>{filter.label}</span>
              <button
                type="button"
                aria-label={`Remove ${filter.label} filter`}
                onClick={() => {
                  filter.onRemove();
                  requestAnimationFrame(() => triggerRef.current?.focus());
                }}
              >
                <CloseIcon />
              </button>
            </span>
          ))}
        </div>
      ) : null}

      {activeFilters.length > 0 ? (
        <span className="catalog-active-summary">
          {activeFilters.length} active
        </span>
      ) : null}

      {hasQueryState ? (
        <button
          type="button"
          className="catalog-clear-all"
          onClick={clearAndFocusSearch}
        >
          Clear all
        </button>
      ) : null}

      <span
        className="catalog-result-count"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {resultText}
      </span>
    </div>
  );
}

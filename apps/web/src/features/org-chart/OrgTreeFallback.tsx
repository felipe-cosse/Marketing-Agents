import {
  useCallback,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { AgentsIcon, ReadIcon, WriteIcon } from "./icons";
import { presentPurpose } from "./presentation";
import type { ProjectedHierarchy } from "./projectHierarchy";
import {
  buildOrgTreeModel,
  findOrgTreeTypeaheadTarget,
  getOrgTreeNavigationTarget,
  getVisibleOrgTreeNodes,
  type OrgTreeNode,
} from "./treeModel";

interface OrgTreeFallbackProps {
  readonly hierarchy: ProjectedHierarchy;
  readonly selectedInstanceId: string | null;
  readonly onSelectionChange: (instanceId: string | null) => void;
  readonly toolbar?: ReactNode;
  readonly autoExpandMatches?: boolean | undefined;
  readonly emptyTitle?: string;
  readonly emptyMessage?: string;
  readonly onClearFilters?: () => void;
  readonly onFocusSearch: () => void;
}

const TYPEAHEAD_RESET_MS = 700;

function DisclosureIcon({
  expanded,
}: {
  readonly expanded: boolean;
}): React.JSX.Element {
  return (
    <svg
      aria-hidden="true"
      className="org-tree-item__disclosure"
      data-expanded={expanded}
      fill="none"
      focusable="false"
      viewBox="0 0 20 20"
    >
      <path
        d="m7.5 5 5 5-5 5"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.8"
      />
    </svg>
  );
}

function NodeIcon({ node }: { readonly node: OrgTreeNode }): React.JSX.Element {
  if (node.kind === "root") return <AgentsIcon />;
  if (node.kind === "instance") {
    return node.instance.operationClassification === "read_only" ? (
      <ReadIcon />
    ) : (
      <WriteIcon />
    );
  }
  return (
    <svg aria-hidden="true" fill="none" focusable="false" viewBox="0 0 24 24">
      <path
        d={
          node.kind === "department"
            ? "M4 20V8l8-4 8 4v12M8 11h2m4 0h2M8 15h2m4 0h2M3 20h18"
            : "M4 7h16v11H4zM8 7V4h8v3M8 11h8"
        }
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.7"
      />
    </svg>
  );
}

function nodeSummary(node: OrgTreeNode): ReactNode {
  if (node.kind === "root") return "Local control plane";
  if (node.kind === "department") {
    return `${String(node.department.instanceCount)} deployed agents · ${String(node.department.functions.length)} functions`;
  }
  if (node.kind === "function") {
    const count = node.agentFunction.instances.length;
    return `${String(count)} ${count === 1 ? "agent" : "agents"}`;
  }

  const instance = node.instance;
  const duplicated = instance.deploymentCount > 1;
  return (
    <>
      <span>{presentPurpose(instance.purpose)}</span>
      <span className="org-tree-item__metadata">
        <span className={instance.enabled ? "is-enabled" : "is-disabled"}>
          {instance.enabled ? "Enabled" : "Disabled"}
        </span>
        <span>
          {instance.operationClassification === "read_only" ? "Read" : "Write"}
        </span>
        {duplicated ? (
          <span>
            Instance {String(instance.sourceOrdinal)} of{" "}
            {String(instance.deploymentCount)}
          </span>
        ) : null}
      </span>
    </>
  );
}

export function OrgTreeFallback({
  hierarchy,
  selectedInstanceId,
  onSelectionChange,
  toolbar,
  autoExpandMatches = false,
  emptyTitle = "No agents match",
  emptyMessage = "No agents match your search and filters.",
  onClearFilters,
  onFocusSearch,
}: OrgTreeFallbackProps): React.JSX.Element {
  const model = useMemo(() => buildOrgTreeModel(hierarchy), [hierarchy]);
  const [expansionOverrides, setExpansionOverrides] = useState<
    ReadonlyMap<string, boolean>
  >(() => new Map());
  const [focusedId, setFocusedId] = useState<string>(model.rootId);
  const itemRefs = useRef(new Map<string, HTMLButtonElement>());
  const typeaheadRef = useRef({ value: "", updatedAt: 0 });
  const expandedIds = useMemo(() => {
    const next = new Set(model.defaultExpandedIds);
    if (autoExpandMatches) {
      for (const node of model.nodes) {
        if (node.expandable) next.add(node.id);
      }
    }
    for (const [nodeId, expanded] of expansionOverrides) {
      if (!model.nodeById.has(nodeId)) continue;
      if (expanded) next.add(nodeId);
      else next.delete(nodeId);
    }
    let selectedNode =
      selectedInstanceId === null
        ? undefined
        : model.nodeById.get(selectedInstanceId);
    while (selectedNode !== undefined && selectedNode.parentId !== null) {
      const parent = model.nodeById.get(selectedNode.parentId);
      if (parent === undefined) break;
      if (parent.expandable) next.add(parent.id);
      selectedNode = parent;
    }
    return next;
  }, [autoExpandMatches, expansionOverrides, model, selectedInstanceId]);

  const visibleNodes = useMemo(
    () => getVisibleOrgTreeNodes(model, expandedIds),
    [expandedIds, model],
  );
  const effectiveFocusedId = visibleNodes.some(({ id }) => id === focusedId)
    ? focusedId
    : selectedInstanceId !== null &&
        visibleNodes.some(({ id }) => id === selectedInstanceId)
      ? selectedInstanceId
      : (visibleNodes[0]?.id ?? model.rootId);

  const focusNode = useCallback((nodeId: string) => {
    setFocusedId(nodeId);
    requestAnimationFrame(() => itemRefs.current.get(nodeId)?.focus());
  }, []);

  const toggleNode = useCallback(
    (node: OrgTreeNode) => {
      if (!node.expandable) return;
      setExpansionOverrides((current) => {
        const next = new Map(current);
        next.set(node.id, !expandedIds.has(node.id));
        return next;
      });
    },
    [expandedIds],
  );

  const activateNode = useCallback(
    (node: OrgTreeNode) => {
      if (node.kind === "instance") onSelectionChange(node.instance.id);
      else toggleNode(node);
    },
    [onSelectionChange, toggleNode],
  );

  const handleKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    node: OrgTreeNode,
  ): void => {
    let targetId: string | null;
    if (event.key === "ArrowDown") {
      targetId = getOrgTreeNavigationTarget(visibleNodes, node.id, "next");
    } else if (event.key === "ArrowUp") {
      targetId = getOrgTreeNavigationTarget(visibleNodes, node.id, "previous");
    } else if (event.key === "Home") {
      targetId = getOrgTreeNavigationTarget(visibleNodes, node.id, "home");
    } else if (event.key === "End") {
      targetId = getOrgTreeNavigationTarget(visibleNodes, node.id, "end");
    } else if (event.key === "ArrowLeft") {
      if (node.expandable && expandedIds.has(node.id)) {
        event.preventDefault();
        toggleNode(node);
        return;
      }
      targetId = getOrgTreeNavigationTarget(visibleNodes, node.id, "parent");
    } else if (event.key === "ArrowRight") {
      if (node.expandable && !expandedIds.has(node.id)) {
        event.preventDefault();
        toggleNode(node);
        return;
      }
      targetId = getOrgTreeNavigationTarget(
        visibleNodes,
        node.id,
        "first-child",
      );
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      activateNode(node);
      return;
    } else if (
      event.key === "/" &&
      !event.altKey &&
      !event.ctrlKey &&
      !event.metaKey
    ) {
      event.preventDefault();
      onFocusSearch();
      return;
    } else if (
      event.key.length === 1 &&
      !event.altKey &&
      !event.ctrlKey &&
      !event.metaKey
    ) {
      const now = event.timeStamp;
      const nextValue =
        now - typeaheadRef.current.updatedAt > TYPEAHEAD_RESET_MS
          ? event.key
          : `${typeaheadRef.current.value}${event.key}`;
      typeaheadRef.current = { value: nextValue, updatedAt: now };
      const repeatedKey = event.key.repeat(nextValue.length) === nextValue;
      const lookup = repeatedKey ? event.key : nextValue;
      targetId = findOrgTreeTypeaheadTarget(visibleNodes, node.id, lookup);
    } else {
      return;
    }

    if (targetId !== null) {
      event.preventDefault();
      focusNode(targetId);
    }
  };

  return (
    <section
      className="chart-surface org-tree-surface"
      aria-labelledby="org-chart-title"
      data-testid="org-tree-surface"
    >
      <div className="chart-toolbar">{toolbar}</div>
      {hierarchy.departments.length === 0 ? (
        <div
          className="org-tree-empty catalog-empty-state"
          role="region"
          aria-live="polite"
        >
          <strong>{emptyTitle}</strong>
          <p>{emptyMessage}</p>
          {onClearFilters === undefined ? null : (
            <button type="button" onClick={onClearFilters}>
              Clear search and filters
            </button>
          )}
        </div>
      ) : (
        <div className="org-tree-scroll">
          <p id="org-tree-keyboard-help" className="sr-only">
            Use Up and Down to move, Left and Right to collapse or expand, Home
            and End to jump, Enter or Space to open, printable keys to search,
            and slash to focus catalog search.
          </p>
          <div
            className="org-tree"
            role="tree"
            aria-label="Marketing Agents organization tree"
            aria-describedby="org-tree-keyboard-help"
          >
            {visibleNodes.map((node) => {
              const expanded = node.expandable && expandedIds.has(node.id);
              const selected =
                node.kind === "instance" &&
                node.instance.id === selectedInstanceId;
              return (
                <button
                  key={node.id}
                  ref={(element) => {
                    if (element === null) itemRefs.current.delete(node.id);
                    else itemRefs.current.set(node.id, element);
                  }}
                  type="button"
                  className={`org-tree-item org-tree-item--${node.kind}`}
                  role="treeitem"
                  aria-expanded={node.expandable ? expanded : undefined}
                  aria-level={node.level}
                  aria-posinset={node.posInSet}
                  aria-setsize={node.setSize}
                  aria-selected={
                    node.kind === "instance" ? selected : undefined
                  }
                  aria-controls={selected ? "agent-inspector" : undefined}
                  data-node-id={node.id}
                  data-node-kind={node.kind}
                  data-department-id={
                    node.kind === "department"
                      ? node.department.id
                      : node.kind === "function" || node.kind === "instance"
                        ? node.departmentId
                        : undefined
                  }
                  data-function-id={
                    node.kind === "function"
                      ? node.agentFunction.id
                      : node.kind === "instance"
                        ? node.functionId
                        : undefined
                  }
                  data-instance-id={
                    node.kind === "instance" ? node.instance.id : undefined
                  }
                  data-template-id={
                    node.kind === "instance"
                      ? node.instance.templateId
                      : undefined
                  }
                  data-source-ordinal={
                    node.kind === "instance"
                      ? node.instance.sourceOrdinal
                      : undefined
                  }
                  tabIndex={node.id === effectiveFocusedId ? 0 : -1}
                  onClick={() => activateNode(node)}
                  onFocus={() => setFocusedId(node.id)}
                  onKeyDown={(event) => handleKeyDown(event, node)}
                >
                  <span
                    className="org-tree-item__indent"
                    aria-hidden="true"
                    data-level={node.level}
                  />
                  <span className="org-tree-item__branch" aria-hidden="true">
                    {node.expandable ? (
                      <DisclosureIcon expanded={expanded} />
                    ) : null}
                  </span>
                  <span className="org-tree-item__icon" aria-hidden="true">
                    <NodeIcon node={node} />
                  </span>
                  <span className="org-tree-item__content">
                    <strong>{node.label}</strong>
                    <span className="org-tree-item__summary">
                      {nodeSummary(node)}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );
}

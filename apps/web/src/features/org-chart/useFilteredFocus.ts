import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  type RefObject,
} from "react";

import type { NormalizedHierarchy } from "./model";
import type { HierarchyProjection } from "./projectHierarchy";

type FocusedNode =
  | { readonly kind: "root"; readonly id: "root" }
  | {
      readonly kind: "department" | "function" | "instance";
      readonly id: string;
    };

interface InstanceAncestry {
  readonly departmentId: string;
  readonly functionId: string;
}

interface FilteredFocusOptions {
  readonly sourceHierarchy: NormalizedHierarchy;
  readonly projection: HierarchyProjection;
  readonly selectedInstanceId: string | null;
  readonly onSelectionChange: (instanceId: string | null) => void;
  readonly searchRef: RefObject<HTMLInputElement | null>;
}

function nodeFromElement(target: EventTarget | null): FocusedNode | null {
  if (!(target instanceof Element)) return null;
  const node = target.closest<HTMLElement>("[data-node-id]");
  if (node === null) return null;
  const id = node.dataset.nodeId;
  const kind = node.dataset.nodeKind ?? node.dataset.focusNodeKind;
  if (id === "root" && kind === "root") return { kind: "root", id };
  if (
    id !== undefined &&
    (kind === "department" || kind === "function" || kind === "instance")
  ) {
    return { kind, id };
  }
  return null;
}

function focusNode(id: string): boolean {
  const target = [
    ...document.querySelectorAll<HTMLElement>("[data-node-id]"),
  ].find((candidate) => candidate.dataset.nodeId === id);
  target?.focus();
  return target !== undefined;
}

export function useFilteredFocus({
  sourceHierarchy,
  projection,
  selectedInstanceId,
  onSelectionChange,
  searchRef,
}: FilteredFocusOptions): void {
  const focusedNodeRef = useRef<FocusedNode | null>(null);
  const ancestry = useMemo(() => {
    const instance = new Map<string, InstanceAncestry>();
    const agentFunction = new Map<string, string>();
    for (const department of sourceHierarchy.departments) {
      for (const item of department.functions) {
        agentFunction.set(item.id, department.id);
        for (const deployed of item.instances) {
          instance.set(deployed.id, {
            departmentId: department.id,
            functionId: item.id,
          });
        }
      }
    }
    return { instance, agentFunction };
  }, [sourceHierarchy]);

  useEffect(() => {
    const rememberFocus = (event: FocusEvent): void => {
      focusedNodeRef.current = nodeFromElement(event.target);
    };
    document.addEventListener("focusin", rememberFocus);
    return () => document.removeEventListener("focusin", rememberFocus);
  }, []);

  useLayoutEffect(() => {
    if (
      selectedInstanceId !== null &&
      !projection.visibleInstanceIds.has(selectedInstanceId)
    ) {
      onSelectionChange(null);
    }

    const focused = focusedNodeRef.current;
    if (focused === null) return;
    if (focused.kind === "root") {
      if (projection.matchedInstanceCount === 0) {
        searchRef.current?.focus();
        focusedNodeRef.current = null;
      }
      return;
    }
    if (
      (focused.kind === "instance" &&
        projection.visibleInstanceIds.has(focused.id)) ||
      (focused.kind === "function" &&
        projection.visibleFunctionIds.has(focused.id)) ||
      (focused.kind === "department" &&
        projection.visibleDepartmentIds.has(focused.id))
    ) {
      return;
    }

    let fallbackId: string | null = null;
    if (focused.kind === "instance") {
      const parent = ancestry.instance.get(focused.id);
      if (
        parent !== undefined &&
        projection.visibleFunctionIds.has(parent.functionId)
      ) {
        fallbackId = parent.functionId;
      } else if (
        parent !== undefined &&
        projection.visibleDepartmentIds.has(parent.departmentId)
      ) {
        fallbackId = parent.departmentId;
      }
    } else if (focused.kind === "function") {
      const departmentId = ancestry.agentFunction.get(focused.id);
      if (
        departmentId !== undefined &&
        projection.visibleDepartmentIds.has(departmentId)
      ) {
        fallbackId = departmentId;
      }
    }
    if (fallbackId === null && projection.matchedInstanceCount > 0) {
      fallbackId = "root";
    }
    if (fallbackId === null || !focusNode(fallbackId)) {
      searchRef.current?.focus();
      focusedNodeRef.current = null;
    }
  }, [ancestry, onSelectionChange, projection, searchRef, selectedInstanceId]);
}

import {
  MARKETING_AGENTS_ROOT,
  type AgentDepartment,
  type AgentFunction,
  type AgentInstance,
} from "./model";
import type { ProjectedHierarchy } from "./projectHierarchy";

export const ORG_TREE_ROOT_ID = MARKETING_AGENTS_ROOT.id;

export type OrgTreeNodeKind = "root" | "department" | "function" | "instance";

interface OrgTreeNodeBase {
  readonly id: string;
  readonly kind: OrgTreeNodeKind;
  readonly label: string;
  readonly parentId: string | null;
  readonly level: number;
  readonly posInSet: number;
  readonly setSize: number;
  readonly displayOrder: number;
  readonly expandable: boolean;
  readonly childIds: readonly string[];
}

export interface OrgTreeRootNode extends OrgTreeNodeBase {
  readonly id: typeof ORG_TREE_ROOT_ID;
  readonly kind: "root";
  readonly parentId: null;
}

export interface OrgTreeDepartmentNode extends OrgTreeNodeBase {
  readonly kind: "department";
  readonly parentId: typeof ORG_TREE_ROOT_ID;
  readonly department: AgentDepartment;
}

export interface OrgTreeFunctionNode extends OrgTreeNodeBase {
  readonly kind: "function";
  readonly parentId: string;
  readonly departmentId: string;
  readonly agentFunction: AgentFunction;
}

export interface OrgTreeInstanceNode extends OrgTreeNodeBase {
  readonly kind: "instance";
  readonly parentId: string;
  readonly departmentId: string;
  readonly functionId: string;
  readonly instance: AgentInstance;
}

export type OrgTreeNode =
  | OrgTreeRootNode
  | OrgTreeDepartmentNode
  | OrgTreeFunctionNode
  | OrgTreeInstanceNode;

export interface OrgTreeModel {
  readonly rootId: typeof ORG_TREE_ROOT_ID;
  readonly nodes: readonly OrgTreeNode[];
  readonly nodeById: ReadonlyMap<string, OrgTreeNode>;
  readonly defaultExpandedIds: ReadonlySet<string>;
}

function frozenIds(ids: readonly string[]): readonly string[] {
  return Object.freeze([...ids]);
}

export function buildOrgTreeModel(hierarchy: ProjectedHierarchy): OrgTreeModel {
  const nodes: OrgTreeNode[] = [];
  const nodeById = new Map<string, OrgTreeNode>();

  const append = (node: OrgTreeNode): void => {
    if (nodeById.has(node.id)) {
      throw new Error(`Duplicate organization tree node ID: ${node.id}`);
    }
    const frozenNode = Object.freeze(node);
    nodes.push(frozenNode);
    nodeById.set(frozenNode.id, frozenNode);
  };

  append({
    id: ORG_TREE_ROOT_ID,
    kind: "root",
    label: MARKETING_AGENTS_ROOT.displayName,
    parentId: null,
    level: 1,
    posInSet: 1,
    setSize: 1,
    displayOrder: 0,
    expandable: hierarchy.departments.length > 0,
    childIds: frozenIds(hierarchy.departments.map(({ id }) => id)),
  });

  for (const [departmentIndex, department] of hierarchy.departments.entries()) {
    append({
      id: department.id,
      kind: "department",
      label: department.displayName,
      parentId: ORG_TREE_ROOT_ID,
      level: 2,
      posInSet: departmentIndex + 1,
      setSize: hierarchy.departments.length,
      displayOrder: department.displayOrder,
      expandable: department.functions.length > 0,
      childIds: frozenIds(department.functions.map(({ id }) => id)),
      department,
    });

    for (const [
      functionIndex,
      agentFunction,
    ] of department.functions.entries()) {
      append({
        id: agentFunction.id,
        kind: "function",
        label: agentFunction.displayName,
        parentId: department.id,
        level: 3,
        posInSet: functionIndex + 1,
        setSize: department.functions.length,
        displayOrder: agentFunction.displayOrder,
        expandable: agentFunction.instances.length > 0,
        childIds: frozenIds(agentFunction.instances.map(({ id }) => id)),
        departmentId: department.id,
        agentFunction,
      });

      for (const [
        instanceIndex,
        instance,
      ] of agentFunction.instances.entries()) {
        append({
          id: instance.id,
          kind: "instance",
          label: instance.displayName,
          parentId: agentFunction.id,
          level: 4,
          posInSet: instanceIndex + 1,
          setSize: agentFunction.instances.length,
          displayOrder: instance.displayOrder,
          expandable: false,
          childIds: frozenIds([]),
          departmentId: department.id,
          functionId: agentFunction.id,
          instance,
        });
      }
    }
  }

  return Object.freeze({
    rootId: ORG_TREE_ROOT_ID,
    nodes: Object.freeze(nodes),
    nodeById,
    defaultExpandedIds: new Set([
      ORG_TREE_ROOT_ID,
      ...hierarchy.departments.map(({ id }) => id),
    ]),
  });
}

export function getVisibleOrgTreeNodes(
  model: OrgTreeModel,
  expandedIds: ReadonlySet<string>,
): readonly OrgTreeNode[] {
  const visible: OrgTreeNode[] = [];
  const visit = (nodeId: string): void => {
    const node = model.nodeById.get(nodeId);
    if (node === undefined) return;
    visible.push(node);
    if (!node.expandable || !expandedIds.has(node.id)) return;
    for (const childId of node.childIds) visit(childId);
  };

  visit(model.rootId);
  return Object.freeze(visible);
}

export type OrgTreeNavigationCommand =
  "previous" | "next" | "parent" | "first-child" | "home" | "end";

export function getOrgTreeNavigationTarget(
  visibleNodes: readonly OrgTreeNode[],
  currentId: string,
  command: OrgTreeNavigationCommand,
): string | null {
  if (visibleNodes.length === 0) return null;
  const currentIndex = visibleNodes.findIndex(({ id }) => id === currentId);
  if (currentIndex === -1) return null;
  const current = visibleNodes[currentIndex];
  if (current === undefined) return null;

  switch (command) {
    case "previous":
      return visibleNodes[currentIndex - 1]?.id ?? null;
    case "next":
      return visibleNodes[currentIndex + 1]?.id ?? null;
    case "parent":
      return current.parentId !== null &&
        visibleNodes.some(({ id }) => id === current.parentId)
        ? current.parentId
        : null;
    case "first-child": {
      const childId = current.childIds[0];
      return childId !== undefined &&
        visibleNodes.some(({ id }) => id === childId)
        ? childId
        : null;
    }
    case "home":
      return visibleNodes[0]?.id ?? null;
    case "end":
      return visibleNodes.at(-1)?.id ?? null;
  }
}

function normalizeTypeahead(value: string): string {
  return value.normalize("NFKC").trim().toLocaleLowerCase("en-US");
}

export function findOrgTreeTypeaheadTarget(
  visibleNodes: readonly OrgTreeNode[],
  currentId: string,
  query: string,
): string | null {
  const normalizedQuery = normalizeTypeahead(query);
  if (visibleNodes.length === 0 || normalizedQuery === "") return null;

  const currentIndex = visibleNodes.findIndex(({ id }) => id === currentId);
  const startIndex = currentIndex === -1 ? -1 : currentIndex;
  for (let offset = 1; offset <= visibleNodes.length; offset += 1) {
    const index = (startIndex + offset) % visibleNodes.length;
    const candidate = visibleNodes[index];
    if (
      candidate !== undefined &&
      normalizeTypeahead(candidate.label).startsWith(normalizedQuery)
    ) {
      return candidate.id;
    }
  }
  return null;
}

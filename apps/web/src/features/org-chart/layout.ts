import type { AgentDepartment } from "./model";

interface LayoutHierarchyInput {
  readonly departments: readonly AgentDepartment[];
}

export const GEOMETRY = Object.freeze({
  worldWidth: 1480,
  worldHeight: 754,
  rootWidth: 148,
  rootHeight: 38,
  rootTop: 0,
  rootBusY: 64,
  departmentWidth: 140,
  departmentHeight: 34,
  departmentTop: 92,
  departmentBusY: 152,
  departmentGapX: 20,
  functionWidth: 112,
  functionHeaderHeight: 36,
  functionTop: 178,
  functionGapX: 8,
  cardInsetX: 4,
  cardWidth: 104,
  cardHeight: 80,
  cardGapY: 8,
  cardGroupTop: 226,
  cardTop: 230,
} as const);

export interface Rect {
  readonly x: number;
  readonly y: number;
  readonly width: number;
  readonly height: number;
}

export interface LayoutLine {
  readonly id: string;
  readonly x1: number;
  readonly y1: number;
  readonly x2: number;
  readonly y2: number;
}

export interface InstanceLayout extends Rect {
  readonly id: string;
  readonly templateId: string;
  readonly departmentId: string;
  readonly functionId: string;
}

export interface FunctionLayout extends Rect {
  readonly id: string;
  readonly header: Rect;
  readonly instances: readonly InstanceLayout[];
}

export interface DepartmentLayout extends Rect {
  readonly id: string;
  readonly header: Rect;
  readonly functions: readonly FunctionLayout[];
}

export interface HierarchyLayout {
  readonly bounds: Rect;
  readonly root: Rect;
  readonly departments: readonly DepartmentLayout[];
  readonly lines: readonly LayoutLine[];
  readonly instanceById: ReadonlyMap<string, InstanceLayout>;
}

function groupHeight(instanceCount: number): number {
  return (
    8 +
    instanceCount * GEOMETRY.cardHeight +
    Math.max(0, instanceCount - 1) * GEOMETRY.cardGapY
  );
}

function line(
  id: string,
  x1: number,
  y1: number,
  x2: number,
  y2: number,
): LayoutLine {
  return Object.freeze({ id, x1, y1, x2, y2 });
}

export function layoutHierarchy(
  hierarchy: LayoutHierarchyInput,
): HierarchyLayout {
  const contentWidth = hierarchy.departments.reduce(
    (width, department, index) =>
      width +
      department.functions.length * GEOMETRY.functionWidth +
      Math.max(0, department.functions.length - 1) * GEOMETRY.functionGapX +
      (index === 0 ? 0 : GEOMETRY.departmentGapX),
    0,
  );
  const worldWidth = Math.max(contentWidth, GEOMETRY.rootWidth);
  const tallestInstanceCount = hierarchy.departments.reduce(
    (maximum, department) =>
      Math.max(
        maximum,
        ...department.functions.map(
          (agentFunction) => agentFunction.instances.length,
        ),
      ),
    0,
  );
  const worldHeight = GEOMETRY.cardGroupTop + groupHeight(tallestInstanceCount);
  let departmentX = (worldWidth - contentWidth) / 2;
  const instanceById = new Map<string, InstanceLayout>();
  const departments: DepartmentLayout[] = [];

  for (const department of hierarchy.departments) {
    const extentWidth =
      department.functions.length * GEOMETRY.functionWidth +
      Math.max(0, department.functions.length - 1) * GEOMETRY.functionGapX;
    const functions: FunctionLayout[] = department.functions.map(
      (agentFunction, functionIndex) => {
        const x =
          departmentX +
          functionIndex * (GEOMETRY.functionWidth + GEOMETRY.functionGapX);
        const height = groupHeight(agentFunction.instances.length);
        const instances = agentFunction.instances.map(
          (instance, instanceIndex) => {
            const placement = Object.freeze({
              id: instance.id,
              templateId: instance.templateId,
              departmentId: department.id,
              functionId: agentFunction.id,
              x: x + GEOMETRY.cardInsetX,
              y:
                GEOMETRY.cardTop +
                instanceIndex * (GEOMETRY.cardHeight + GEOMETRY.cardGapY),
              width: GEOMETRY.cardWidth,
              height: GEOMETRY.cardHeight,
            });
            instanceById.set(instance.id, placement);
            return placement;
          },
        );
        return Object.freeze({
          id: agentFunction.id,
          x,
          y: GEOMETRY.cardGroupTop,
          width: GEOMETRY.functionWidth,
          height,
          header: Object.freeze({
            x,
            y: GEOMETRY.functionTop,
            width: GEOMETRY.functionWidth,
            height: GEOMETRY.functionHeaderHeight,
          }),
          instances: Object.freeze(instances),
        });
      },
    );
    const center = departmentX + extentWidth / 2;
    departments.push(
      Object.freeze({
        id: department.id,
        x: departmentX,
        y: GEOMETRY.departmentTop,
        width: extentWidth,
        height: worldHeight - GEOMETRY.departmentTop,
        header: Object.freeze({
          x: center - GEOMETRY.departmentWidth / 2,
          y: GEOMETRY.departmentTop,
          width: GEOMETRY.departmentWidth,
          height: GEOMETRY.departmentHeight,
        }),
        functions: Object.freeze(functions),
      }),
    );
    departmentX += extentWidth + GEOMETRY.departmentGapX;
  }

  if (departments.length === 0) {
    throw new Error("The normalized hierarchy must contain departments.");
  }

  const departmentCenters = departments.map(
    (department) => department.x + department.width / 2,
  );
  const rootX = worldWidth / 2 - GEOMETRY.rootWidth / 2;
  const root = Object.freeze({
    x: rootX,
    y: GEOMETRY.rootTop,
    width: GEOMETRY.rootWidth,
    height: GEOMETRY.rootHeight,
  });

  const lines: LayoutLine[] = [
    line(
      "root-trunk",
      worldWidth / 2,
      GEOMETRY.rootHeight,
      worldWidth / 2,
      GEOMETRY.rootBusY,
    ),
    line(
      "root-bus",
      departmentCenters[0] ?? 0,
      GEOMETRY.rootBusY,
      departmentCenters.at(-1) ?? worldWidth,
      GEOMETRY.rootBusY,
    ),
  ];

  for (const [departmentIndex, department] of departments.entries()) {
    const departmentCenter = department.x + department.width / 2;
    lines.push(
      line(
        `department-${String(departmentIndex)}-drop`,
        departmentCenter,
        GEOMETRY.rootBusY,
        departmentCenter,
        GEOMETRY.departmentTop,
      ),
      line(
        `department-${String(departmentIndex)}-trunk`,
        departmentCenter,
        GEOMETRY.departmentTop + GEOMETRY.departmentHeight,
        departmentCenter,
        GEOMETRY.departmentBusY,
      ),
    );

    const functionCenters = department.functions.map(
      (agentFunction) => agentFunction.x + GEOMETRY.functionWidth / 2,
    );
    lines.push(
      line(
        `department-${String(departmentIndex)}-bus`,
        functionCenters[0] ?? departmentCenter,
        GEOMETRY.departmentBusY,
        functionCenters.at(-1) ?? departmentCenter,
        GEOMETRY.departmentBusY,
      ),
    );

    for (const [
      functionIndex,
      agentFunction,
    ] of department.functions.entries()) {
      const functionCenter = agentFunction.x + GEOMETRY.functionWidth / 2;
      lines.push(
        line(
          `department-${String(departmentIndex)}-function-${String(functionIndex)}-drop`,
          functionCenter,
          GEOMETRY.departmentBusY,
          functionCenter,
          GEOMETRY.functionTop,
        ),
        line(
          `department-${String(departmentIndex)}-function-${String(functionIndex)}-stem`,
          functionCenter,
          GEOMETRY.functionTop + GEOMETRY.functionHeaderHeight,
          functionCenter,
          GEOMETRY.cardGroupTop,
        ),
      );
    }
  }

  return Object.freeze({
    bounds: Object.freeze({
      x: 0,
      y: 0,
      width: worldWidth,
      height: worldHeight,
    }),
    root,
    departments: Object.freeze(departments),
    lines: Object.freeze(lines),
    instanceById,
  });
}

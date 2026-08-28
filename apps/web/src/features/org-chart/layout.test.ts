// WEB-01 makes layout geometry deterministic, ordered, and independent of the DOM.
import { describe, expect, it } from "vitest";

import { makeHierarchyPayload } from "../../test/hierarchyFixture";
import { GEOMETRY, layoutHierarchy } from "./layout";
import { normalizeHierarchy } from "./normalizeHierarchy";

describe("WEB-01 deterministic hierarchy layout", () => {
  const hierarchy = normalizeHierarchy(makeHierarchyPayload());

  it("produces the accepted 1480 by 754 world and exact horizontal extents", () => {
    const layout = layoutHierarchy(hierarchy);
    expect(layout.bounds).toEqual({ x: 0, y: 0, width: 1480, height: 754 });
    expect(layout.root).toEqual({ x: 666, y: 0, width: 148, height: 38 });
    expect(layout.departments.map(({ x, width }) => [x, width])).toEqual([
      [0, 352],
      [372, 232],
      [624, 232],
      [876, 352],
      [1248, 232],
    ]);
    expect(
      layout.departments.flatMap((department) =>
        department.functions.map((agentFunction) => agentFunction.x),
      ),
    ).toEqual([0, 120, 240, 372, 492, 624, 744, 876, 996, 1116, 1248, 1368]);
  });

  it("stacks cards without overlap inside every function group", () => {
    const layout = layoutHierarchy(hierarchy);
    expect(layout.departments[0]?.functions[0]?.instances[0]).toEqual({
      id: "inst.social-media.new-content.agent-1.01",
      templateId: "tpl.social-media.new-content.agent-1",
      departmentId: "dept.social-media",
      functionId: "func.social-media.new-content",
      x: 4,
      y: 230,
      width: 104,
      height: 80,
    });
    expect(layout.departments[0]?.functions[0]?.instances[5]).toMatchObject({
      x: 4,
      y: 670,
      width: 104,
      height: 80,
    });
    expect(layout.departments[3]?.functions[0]?.instances[0]).toMatchObject({
      x: 880,
      y: 230,
      width: 104,
      height: 80,
    });
    for (const department of layout.departments) {
      for (const agentFunction of department.functions) {
        for (const [index, card] of agentFunction.instances.entries()) {
          expect(card.x).toBe(agentFunction.x + GEOMETRY.cardInsetX);
          expect(card.y).toBe(
            GEOMETRY.cardTop +
              index * (GEOMETRY.cardHeight + GEOMETRY.cardGapY),
          );
          expect(card.x + card.width).toBeLessThanOrEqual(
            agentFunction.x + agentFunction.width,
          );
          expect(card.y + card.height).toBeLessThanOrEqual(
            agentFunction.y + agentFunction.height,
          );
        }
      }
    }
    const sixCardFunction = layout.departments[0]?.functions[0];
    expect(sixCardFunction?.instances.map((card) => card.y)).toEqual([
      230, 318, 406, 494, 582, 670,
    ]);
  });

  it("emits exactly 41 orthogonal non-random connectors", () => {
    const first = layoutHierarchy(hierarchy);
    const second = layoutHierarchy(hierarchy);
    expect(first.lines).toEqual(second.lines);
    expect(first.lines).toHaveLength(41);
    expect(
      first.lines.every((edge) => edge.x1 === edge.x2 || edge.y1 === edge.y2),
    ).toBe(true);
    const connectorById = new Map(first.lines.map((edge) => [edge.id, edge]));
    expect(connectorById.get("root-trunk")).toEqual({
      id: "root-trunk",
      x1: 740,
      y1: 38,
      x2: 740,
      y2: 64,
    });
    expect(connectorById.get("root-bus")).toEqual({
      id: "root-bus",
      x1: 176,
      y1: 64,
      x2: 1364,
      y2: 64,
    });
    expect(connectorById.get("department-0-drop")).toEqual({
      id: "department-0-drop",
      x1: 176,
      y1: 64,
      x2: 176,
      y2: 92,
    });
    expect(connectorById.get("department-0-trunk")).toEqual({
      id: "department-0-trunk",
      x1: 176,
      y1: 126,
      x2: 176,
      y2: 152,
    });
    expect(connectorById.get("department-0-bus")).toEqual({
      id: "department-0-bus",
      x1: 56,
      y1: 152,
      x2: 296,
      y2: 152,
    });
    expect(connectorById.get("department-0-function-0-drop")).toEqual({
      id: "department-0-function-0-drop",
      x1: 56,
      y1: 152,
      x2: 56,
      y2: 178,
    });
    expect(connectorById.get("department-4-function-1-stem")).toEqual({
      id: "department-4-function-1-stem",
      x1: 1424,
      y1: 214,
      x2: 1424,
      y2: 226,
    });
  });

  it("keeps geometry stable when non-structural presentation changes", () => {
    const changed = makeHierarchyPayload();
    const first = (
      changed.departments as {
        functions: {
          instances: { purpose: string; enabled: boolean }[];
        }[];
      }[]
    )[0]?.functions[0]?.instances[0];
    if (first !== undefined) {
      first.purpose = "Updated presentation only";
      first.enabled = false;
    }
    expect(layoutHierarchy(normalizeHierarchy(changed)).departments).toEqual(
      layoutHierarchy(hierarchy).departments,
    );
  });
});

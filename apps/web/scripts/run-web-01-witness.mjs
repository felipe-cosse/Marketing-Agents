// WEB-01 dependency-free witness imports and executes the pure production implementation under Node 24.
import "./require-pinned-node.mjs";

import assert from "node:assert/strict";

import {
  isUntrustedUpstreamHeader,
  stripUntrustedUpstreamHeaders,
} from "../config/proxyHeaders.ts";
import { layoutHierarchy } from "../src/features/org-chart/layout.ts";
import { presentPurpose } from "../src/features/org-chart/presentation.ts";
import { fitTransform } from "../src/features/org-chart/viewport.ts";

const functionInstanceCounts = [
  [6, 2, 4],
  [3, 3],
  [2, 3],
  [6, 6, 2],
  [5, 1],
];
let instanceNumber = 0;
let communityInstanceNumber = 0;
const departments = functionInstanceCounts.map(
  (functionCounts, departmentIndex) => {
    const departmentId = `department-${String(departmentIndex + 1)}`;
    return {
      id: departmentId,
      functions: functionCounts.map((instanceCount, functionIndex) => {
        const functionId = `${departmentId}-function-${String(functionIndex + 1)}`;
        return {
          id: functionId,
          instances: Array.from({ length: instanceCount }, () => {
            instanceNumber += 1;
            const communityTemplateNumber =
              Math.floor(communityInstanceNumber / 2) + 1;
            const templateId =
              departmentIndex === 3
                ? `community-template-${String(communityTemplateNumber)}`
                : `template-${String(instanceNumber)}`;
            if (departmentIndex === 3) {
              communityInstanceNumber += 1;
            }
            return {
              id: `instance-${String(instanceNumber)}`,
              templateId,
            };
          }),
        };
      }),
    };
  },
);
const hierarchy = {
  counts: { departments: 5, functions: 12, templates: 36, instances: 43 },
  departments,
};

const functions = departments.flatMap((department) => department.functions);
const instances = functions.flatMap((agentFunction) => agentFunction.instances);
assert.equal(departments.length, 5);
assert.equal(functions.length, 12);
assert.equal(instances.length, 43);
assert.equal(
  new Set(instances.map((instance) => instance.templateId)).size,
  36,
);
assert.deepEqual(
  departments.map((department) =>
    department.functions.reduce(
      (count, agentFunction) => count + agentFunction.instances.length,
      0,
    ),
  ),
  [12, 6, 5, 14, 6],
);

const layout = layoutHierarchy(hierarchy);
assert.deepEqual(layout.bounds, { x: 0, y: 0, width: 1480, height: 754 });
assert.deepEqual(layout.root, { x: 666, y: 0, width: 148, height: 38 });
assert.deepEqual(
  layout.departments.map(({ x, width }) => [x, width]),
  [
    [0, 352],
    [372, 232],
    [624, 232],
    [876, 352],
    [1248, 232],
  ],
);
assert.equal(layout.instanceById.size, 43);
assert.equal(layout.lines.length, 41);
assert.ok(
  layout.lines.every((edge) => edge.x1 === edge.x2 || edge.y1 === edge.y2),
);
assert.deepEqual(fitTransform({ width: 1536, height: 856 }, layout.bounds), {
  zoom: 1,
  translateX: 28,
  translateY: 51,
  intent: "auto-fit",
});

assert.equal(
  presentPurpose(
    "Add signups to the configured newsletter system; the source chart names Loops.",
  ),
  "Add signups to the configured newsletter system.",
);
assert.equal(
  presentPurpose("Summarize the source chart names and preserve context."),
  "Summarize the source chart names and preserve context.",
);

const untrustedHeaders = [
  "Forwarded",
  "X-Forwarded-For",
  "Remote-User",
  "X-Forwarded-User",
  "X-Forwarded-Email",
  "X-Forwarded-Actor",
  "X-Forwarded-Role",
  "X-Forwarded-Roles",
  "X-Forwarded-Scope",
  "X-Forwarded-Scopes",
  "X-Actor",
  "X-User",
  "X-Role",
  "X-Scope",
  "X-Principal",
  "X-Auth-Request-User",
];
for (const header of untrustedHeaders) {
  assert.equal(isUntrustedUpstreamHeader(header), true, header);
}
const retainedHeaders = new Set([
  ...untrustedHeaders,
  "Accept",
  "X-CSRF-Token",
]);
stripUntrustedUpstreamHeaders({
  getHeaderNames: () => [...retainedHeaders],
  removeHeader: (header) => retainedHeaders.delete(header),
});
assert.deepEqual(retainedHeaders, new Set(["Accept", "X-CSRF-Token"]));

process.stdout.write(
  `WEB-01 dependency-free witness passed: Node ${process.versions.node}, 5/12/36/43, layout, fit, purpose, and proxy headers.\n`,
);

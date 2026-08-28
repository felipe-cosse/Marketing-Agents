// WEB-02 dependency-free witness imports and executes the pure production implementation under Node 24.
import "./require-pinned-node.mjs";

import assert from "node:assert/strict";
import { registerHooks } from "node:module";

registerHooks({
  resolve(specifier, context, nextResolve) {
    try {
      return nextResolve(specifier, context);
    } catch (error) {
      if (specifier.startsWith(".") && !specifier.endsWith(".ts")) {
        return nextResolve(`${specifier}.ts`, context);
      }
      throw error;
    }
  },
});

const [statusModule, filterModule, filterUrlModule, projectionModule] =
  await Promise.all([
    import("../src/api/instanceStatusSummary.ts"),
    import("../src/features/org-chart/filters.ts"),
    import("../src/features/org-chart/filterUrl.ts"),
    import("../src/features/org-chart/projectHierarchy.ts"),
  ]);
const { normalizeInstanceStatusSummary } = statusModule;
const { DEFAULT_ORG_CHART_FILTERS, deriveFilterOptions } = filterModule;
const { parseOrgChartFilters, serializeOrgChartFilters } = filterUrlModule;
const { projectHierarchy } = projectionModule;

const functionInstanceCounts = [
  [6, 2, 4],
  [3, 3],
  [2, 3],
  [6, 6, 2],
  [5, 1],
];
let instanceNumber = 0;
const departments = functionInstanceCounts.map(
  (functionCounts, departmentIndex) => {
    const departmentId = `dept.${String(departmentIndex + 1)}`;
    const functions = functionCounts.map((instanceCount, functionIndex) => {
      const functionId = `${departmentId}.function.${String(functionIndex + 1)}`;
      const instances = Array.from(
        { length: instanceCount },
        (_, localIndex) => {
          instanceNumber += 1;
          const id = `inst.${String(instanceNumber).padStart(2, "0")}`;
          return {
            id,
            templateId:
              departmentIndex === 3
                ? `tpl.community.${String(Math.floor(localIndex / 2) + 1)}`
                : `tpl.${String(instanceNumber).padStart(2, "0")}`,
            displayName:
              instanceNumber === 1
                ? "LinkedIn Planner"
                : `Agent ${String(instanceNumber)}`,
            purpose:
              instanceNumber === 1
                ? "Draft visible campaign briefs; the source chart names HiddenVendor."
                : "Complete source-backed local work.",
            displayOrder: (localIndex + 1) * 10,
            enabled: instanceNumber % 2 === 1,
            operationClassification: "read_only",
            triggerTypes: ["manual"],
            capabilitySummaries: [
              {
                id:
                  instanceNumber === 1 ? "cap.brief.read" : "cap.catalog.read",
                displayName:
                  instanceNumber === 1 ? "Campaign brief" : "Catalog read",
                connectorFamily: "local",
                effect: "read",
              },
            ],
            sourceOrdinal: 1,
            deploymentCount: 1,
          };
        },
      );
      return {
        id: functionId,
        displayName: `Function ${String(functionIndex + 1)}`,
        displayOrder: (functionIndex + 1) * 10,
        instances,
      };
    });
    const instances = functions.flatMap(
      (agentFunction) => agentFunction.instances,
    );
    return {
      id: departmentId,
      displayName: `Department ${String(departmentIndex + 1)}`,
      displayOrder: (departmentIndex + 1) * 10,
      instanceCount: instances.length,
      templateCount: new Set(instances.map(({ templateId }) => templateId))
        .size,
      functions,
    };
  },
);
const hierarchy = {
  catalogVersion: "1.0.0",
  catalogHash: `catalog-sha256-v1:${"a".repeat(64)}`,
  counts: { departments: 5, functions: 12, templates: 36, instances: 43 },
  departments,
  structuralKey: "full-5-12-43",
};
const ids = departments.flatMap((department) =>
  department.functions.flatMap((agentFunction) =>
    agentFunction.instances.map(({ id }) => id),
  ),
);
assert.equal(ids.length, 43);

const options = deriveFilterOptions(hierarchy);
assert.deepEqual(
  options.departments.map(({ id }) => id),
  ["dept.1", "dept.2", "dept.3", "dept.4", "dept.5"],
);
assert.deepEqual(
  options.functionsByDepartment.get("dept.1")?.map(({ id }) => id),
  ["dept.1.function.1", "dept.1.function.2", "dept.1.function.3"],
);

const parsed = parseOrgChartFilters(
  "?capability=cap.brief.read&run=completed&deployment=enabled&function=dept.1.function.1&department=dept.1&q=%EF%BC%AC%EF%BD%89%EF%BD%8E%EF%BD%8B%EF%BD%85%EF%BD%84%EF%BC%A9%EF%BD%8E",
  hierarchy,
);
assert.equal(
  serializeOrgChartFilters(parsed),
  "?q=LinkedIn&department=dept.1&function=dept.1.function.1&deployment=enabled&run=completed&capability=cap.brief.read",
);
assert.equal(
  parseOrgChartFilters("?q=one&q=two&department=dept.1", hierarchy).q,
  "",
);
assert.equal(
  parseOrgChartFilters(
    "?department=dept.1&function=dept.2.function.1",
    hierarchy,
  ).functionId,
  null,
);

const statuses = new Map(ids.map((id) => [id, "never_run"]));
statuses.set(ids[0], "completed");
const filtered = projectHierarchy(hierarchy, parsed, statuses);
assert.equal(filtered.matchedInstanceCount, 1);
assert.deepEqual([...filtered.visibleDepartmentIds], ["dept.1"]);
assert.deepEqual([...filtered.visibleFunctionIds], ["dept.1.function.1"]);
assert.deepEqual([...filtered.visibleInstanceIds], [ids[0]]);
assert.equal(
  projectHierarchy(hierarchy, {
    ...DEFAULT_ORG_CHART_FILTERS,
    q: "draft visible campaign briefs",
  }).matchedInstanceCount,
  1,
);
assert.equal(
  projectHierarchy(hierarchy, {
    ...DEFAULT_ORG_CHART_FILTERS,
    q: "HiddenVendor",
  }).matchedInstanceCount,
  0,
);
assert.equal(
  projectHierarchy(hierarchy, {
    ...DEFAULT_ORG_CHART_FILTERS,
    q: "Campaign brief",
  }).matchedInstanceCount,
  1,
);
assert.equal(
  projectHierarchy(hierarchy, {
    ...DEFAULT_ORG_CHART_FILTERS,
    runState: "never_run",
  }).matchedInstanceCount,
  0,
);
assert.equal(
  projectHierarchy(hierarchy, DEFAULT_ORG_CHART_FILTERS).hierarchy,
  hierarchy,
);

const watermark = `instance-status-sha256-v1:${"b".repeat(64)}`;
const statusBody = {
  scope: "single-local-installation",
  runtime_watermark: watermark,
  items: ids.map((id, index) => ({
    instance_id: id,
    status: index === 0 ? "completed" : "never_run",
    latest_run_id: index === 0 ? "run.web-02.01" : null,
    latest_run_state: index === 0 ? "completed" : null,
    latest_run_created_at: index === 0 ? "2026-08-28T18:00:00Z" : null,
    latest_run_updated_at: index === 0 ? "2026-08-28T18:01:00Z" : null,
    instance_url: `/api/v1/agent-instances/${id}`,
    latest_run_url: index === 0 ? "/api/v1/runs/run.web-02.01" : null,
  })),
};
const normalizedStatus = normalizeInstanceStatusSummary(statusBody, ids);
assert.equal(normalizedStatus.items.length, 43);
assert.equal(normalizedStatus.items[0]?.status, "completed");
assert.throws(
  () => normalizeInstanceStatusSummary(statusBody, [...ids].reverse()),
  /does not match hierarchy order/u,
);

process.stdout.write(
  `WEB-02 dependency-free witness passed: Node ${process.versions.node}, canonical URL, source-safe search, five AND filters, ancestry, and exact 43-item runtime binding.\n`,
);

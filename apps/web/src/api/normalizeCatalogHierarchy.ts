import {
  EXPECTED_COUNTS,
  EXPECTED_DEPARTMENT_INSTANCE_COUNTS,
  EXPECTED_FUNCTION_COUNTS,
  EXPECTED_FUNCTION_INSTANCE_COUNTS,
  MARKETING_AGENTS_ROOT,
  MARKETING_ORCHESTRATOR_CONTROL_PLANE,
  type AgentDepartment,
  type AgentFunction,
  type AgentInstance,
  type CapabilityEffect,
  type CapabilitySummary,
  type NormalizedHierarchy,
  type OperationClassification,
  type TriggerType,
} from "../contracts/catalogHierarchy";

const CATALOG_HASH_PATTERN = /^catalog-sha256-v1:[a-f0-9]{64}$/u;
const TRIGGER_TYPES = new Set<TriggerType>(["manual", "webhook", "schedule"]);
const EFFECTS = new Set<CapabilityEffect>(["read", "write"]);
const OPERATIONS = new Set<OperationClassification>(["read_only", "mutating"]);
const RESERVED_UI_NODE_IDS = new Set<string>([
  MARKETING_AGENTS_ROOT.id,
  MARKETING_ORCHESTRATOR_CONTROL_PLANE.id,
]);

export class HierarchyContractError extends Error {
  constructor(message: string) {
    super(`Catalog hierarchy contract violation: ${message}`);
    this.name = "HierarchyContractError";
  }
}

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new HierarchyContractError(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function asArray(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) {
    throw new HierarchyContractError(`${label} must be an array`);
  }
  return value;
}

function asString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new HierarchyContractError(`${label} must be a non-empty string`);
  }
  return value;
}

function asSourceNodeId(value: unknown, label: string): string {
  const id = asString(value, label);
  if (RESERVED_UI_NODE_IDS.has(id)) {
    throw new HierarchyContractError(
      `${label} must not use the reserved UI identity ${JSON.stringify(id)}`,
    );
  }
  return id;
}

function asBoolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") {
    throw new HierarchyContractError(`${label} must be a boolean`);
  }
  return value;
}

function asPositiveInteger(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 1) {
    throw new HierarchyContractError(`${label} must be a positive integer`);
  }
  return value as number;
}

function asExactCount(value: unknown, expected: number, label: string): number {
  if (value !== expected) {
    throw new HierarchyContractError(`${label} must equal ${String(expected)}`);
  }
  return expected;
}

function asEnum<T extends string>(
  value: unknown,
  allowed: ReadonlySet<T>,
  label: string,
): T {
  if (typeof value !== "string" || !allowed.has(value as T)) {
    throw new HierarchyContractError(`${label} is unsupported`);
  }
  return value as T;
}

function assertUnique(values: readonly string[], label: string): void {
  if (new Set(values).size !== values.length) {
    throw new HierarchyContractError(`${label} must be unique`);
  }
}

function sortByDisplayOrder<T extends { readonly displayOrder: number }>(
  values: readonly T[],
  label: string,
): readonly T[] {
  const orders = values.map((value) => value.displayOrder);
  if (new Set(orders).size !== orders.length) {
    throw new HierarchyContractError(`${label} display orders must be unique`);
  }
  return [...values].sort(
    (left, right) => left.displayOrder - right.displayOrder,
  );
}

function deepFreeze<T>(value: T): T {
  if (typeof value === "object" && value !== null && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const child of Object.values(value)) {
      deepFreeze(child);
    }
  }
  return value;
}

function parseCapability(value: unknown, label: string): CapabilitySummary {
  const record = asRecord(value, label);
  return {
    id: asString(record.id, `${label}.id`),
    displayName: asString(record.displayName, `${label}.displayName`),
    connectorFamily: asString(
      record.connectorFamily,
      `${label}.connectorFamily`,
    ),
    effect: asEnum(record.effect, EFFECTS, `${label}.effect`),
  };
}

function parseInstance(value: unknown, label: string): AgentInstance {
  const record = asRecord(value, label);
  const id = asSourceNodeId(record.id, `${label}.id`);
  const triggerTypes = asArray(
    record.triggerTypes,
    `${label}.triggerTypes`,
  ).map((trigger, index) =>
    asEnum(trigger, TRIGGER_TYPES, `${label}.triggerTypes[${String(index)}]`),
  );
  assertUnique(triggerTypes, `${label}.triggerTypes`);

  const capabilitySummaries = asArray(
    record.capabilitySummaries,
    `${label}.capabilitySummaries`,
  ).map((capability, index) =>
    parseCapability(
      capability,
      `${label}.capabilitySummaries[${String(index)}]`,
    ),
  );
  assertUnique(
    capabilitySummaries.map((capability) => capability.id),
    `${label}.capabilitySummaries IDs`,
  );

  return {
    id,
    templateId: asString(record.templateId, `${label}.templateId`),
    displayName: asString(record.displayName, `${label}.displayName`),
    purpose: asString(record.purpose, `${label}.purpose`),
    displayOrder: asPositiveInteger(
      record.displayOrder,
      `${label}.displayOrder`,
    ),
    enabled: asBoolean(record.enabled, `${label}.enabled`),
    operationClassification: asEnum(
      record.operationClassification,
      OPERATIONS,
      `${label}.operationClassification`,
    ),
    triggerTypes,
    capabilitySummaries,
    sourceOrdinal: asPositiveInteger(
      record.sourceOrdinal,
      `${label}.sourceOrdinal`,
    ),
    deploymentCount: 0,
  };
}

function parseFunction(value: unknown, label: string): AgentFunction {
  const record = asRecord(value, label);
  const instances = sortByDisplayOrder(
    asArray(record.instances, `${label}.instances`).map((instance, index) =>
      parseInstance(instance, `${label}.instances[${String(index)}]`),
    ),
    `${label}.instances`,
  );
  return {
    id: asSourceNodeId(record.id, `${label}.id`),
    displayName: asString(record.displayName, `${label}.displayName`),
    displayOrder: asPositiveInteger(
      record.displayOrder,
      `${label}.displayOrder`,
    ),
    instances,
  };
}

function parseDepartment(value: unknown, label: string): AgentDepartment {
  const record = asRecord(value, label);
  const functions = sortByDisplayOrder(
    asArray(record.functions, `${label}.functions`).map(
      (agentFunction, index) =>
        parseFunction(agentFunction, `${label}.functions[${String(index)}]`),
    ),
    `${label}.functions`,
  );
  const instances = functions.flatMap(
    (agentFunction) => agentFunction.instances,
  );
  return {
    id: asSourceNodeId(record.id, `${label}.id`),
    displayName: asString(record.displayName, `${label}.displayName`),
    displayOrder: asPositiveInteger(
      record.displayOrder,
      `${label}.displayOrder`,
    ),
    instanceCount: instances.length,
    templateCount: new Set(instances.map((instance) => instance.templateId))
      .size,
    functions,
  };
}

function withDeploymentCounts(
  departments: readonly AgentDepartment[],
): readonly AgentDepartment[] {
  const templateCounts = new Map<string, number>();
  for (const instance of departments.flatMap((department) =>
    department.functions.flatMap((agentFunction) => agentFunction.instances),
  )) {
    templateCounts.set(
      instance.templateId,
      (templateCounts.get(instance.templateId) ?? 0) + 1,
    );
  }

  return departments.map((department) => ({
    ...department,
    functions: department.functions.map((agentFunction) => ({
      ...agentFunction,
      instances: agentFunction.instances.map((instance) => ({
        ...instance,
        deploymentCount: templateCounts.get(instance.templateId) ?? 0,
      })),
    })),
  }));
}

function assertExpectedStructure(
  departments: readonly AgentDepartment[],
): void {
  const functions = departments.flatMap((department) => department.functions);
  const instances = functions.flatMap(
    (agentFunction) => agentFunction.instances,
  );
  const templates = new Set(instances.map((instance) => instance.templateId));

  assertUnique(
    departments.map((department) => department.id),
    "department IDs",
  );
  assertUnique(
    functions.map((agentFunction) => agentFunction.id),
    "function IDs",
  );
  assertUnique(
    instances.map((instance) => instance.id),
    "instance IDs",
  );

  const actualDepartmentCounts = departments.map(
    (department) => department.instanceCount,
  );
  if (
    actualDepartmentCounts.some(
      (count, index) => count !== EXPECTED_DEPARTMENT_INSTANCE_COUNTS[index],
    )
  ) {
    throw new HierarchyContractError(
      "department instance distribution must be 12/6/5/14/6",
    );
  }

  const actualFunctionCounts = departments.map(
    (department) => department.functions.length,
  );
  if (
    actualFunctionCounts.some(
      (count, index) => count !== EXPECTED_FUNCTION_COUNTS[index],
    )
  ) {
    throw new HierarchyContractError(
      "department function distribution must be 3/2/2/3/2",
    );
  }

  const actualFunctionInstanceCounts = functions.map(
    (agentFunction) => agentFunction.instances.length,
  );
  if (
    actualFunctionInstanceCounts.some(
      (count, index) => count !== EXPECTED_FUNCTION_INSTANCE_COUNTS[index],
    )
  ) {
    throw new HierarchyContractError(
      "function instance distribution must be 6/2/4/3/3/2/3/6/6/2/5/1",
    );
  }

  if (
    departments.length !== EXPECTED_COUNTS.departments ||
    functions.length !== EXPECTED_COUNTS.functions ||
    templates.size !== EXPECTED_COUNTS.templates ||
    instances.length !== EXPECTED_COUNTS.instances
  ) {
    throw new HierarchyContractError(
      "computed hierarchy counts must equal 5/12/36/43",
    );
  }

  const community = departments[3];
  if (community?.instanceCount !== 14 || community.templateCount !== 7) {
    throw new HierarchyContractError(
      "Community must contain 14 deployments from 7 templates",
    );
  }
  const communityInstances = community.functions.flatMap(
    (agentFunction) => agentFunction.instances,
  );
  const communityByTemplate = new Map<string, number[]>();
  for (const instance of communityInstances) {
    const ordinals = communityByTemplate.get(instance.templateId) ?? [];
    ordinals.push(instance.sourceOrdinal);
    communityByTemplate.set(instance.templateId, ordinals);
  }
  if (
    communityByTemplate.size !== 7 ||
    [...communityByTemplate.values()].some(
      (ordinals) =>
        ordinals.length !== 2 || ordinals[0] !== 1 || ordinals[1] !== 2,
    )
  ) {
    throw new HierarchyContractError(
      "Community templates must each expose ordered instances 1 and 2",
    );
  }
}

function structuralKey(departments: readonly AgentDepartment[]): string {
  return JSON.stringify(
    departments.map((department) => [
      department.id,
      department.displayOrder,
      department.functions.map((agentFunction) => [
        agentFunction.id,
        agentFunction.displayOrder,
        agentFunction.instances.map((instance) => [
          instance.id,
          instance.templateId,
          instance.displayOrder,
          instance.sourceOrdinal,
        ]),
      ]),
    ]),
  );
}

export function normalizeHierarchy(input: unknown): NormalizedHierarchy {
  const record = asRecord(input, "response");
  const counts = asRecord(record.counts, "response.counts");
  asExactCount(
    counts.departments,
    EXPECTED_COUNTS.departments,
    "counts.departments",
  );
  asExactCount(counts.functions, EXPECTED_COUNTS.functions, "counts.functions");
  asExactCount(counts.templates, EXPECTED_COUNTS.templates, "counts.templates");
  asExactCount(counts.instances, EXPECTED_COUNTS.instances, "counts.instances");

  const catalogHash = asString(record.catalogHash, "response.catalogHash");
  if (!CATALOG_HASH_PATTERN.test(catalogHash)) {
    throw new HierarchyContractError(
      "catalogHash must be a catalog SHA-256 identifier",
    );
  }

  const parsedDepartments = sortByDisplayOrder(
    asArray(record.departments, "response.departments").map(
      (department, index) =>
        parseDepartment(department, `response.departments[${String(index)}]`),
    ),
    "response.departments",
  );
  assertExpectedStructure(parsedDepartments);
  const departments = withDeploymentCounts(parsedDepartments);

  const departmentCounts = asArray(
    record.departmentCounts,
    "response.departmentCounts",
  );
  if (departmentCounts.length !== EXPECTED_COUNTS.departments) {
    throw new HierarchyContractError(
      "departmentCounts must contain five entries",
    );
  }
  const projectedCounts = new Map(
    departmentCounts.map((value, index) => {
      const count = asRecord(
        value,
        `response.departmentCounts[${String(index)}]`,
      );
      return [
        asString(
          count.departmentId,
          `departmentCounts[${String(index)}].departmentId`,
        ),
        asPositiveInteger(
          count.instanceCount,
          `departmentCounts[${String(index)}].instanceCount`,
        ),
      ] as const;
    }),
  );
  if (projectedCounts.size !== departments.length) {
    throw new HierarchyContractError("departmentCounts IDs must be unique");
  }
  for (const department of departments) {
    if (projectedCounts.get(department.id) !== department.instanceCount) {
      throw new HierarchyContractError(
        `departmentCounts drift for ${department.id}`,
      );
    }
  }

  return deepFreeze({
    catalogVersion: asString(record.catalogVersion, "response.catalogVersion"),
    catalogHash,
    counts: EXPECTED_COUNTS,
    departments,
    structuralKey: structuralKey(departments),
  });
}

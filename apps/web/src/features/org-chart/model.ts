export type OperationClassification = "read_only" | "mutating";
export type TriggerType = "manual" | "webhook" | "schedule";
export type CapabilityEffect = "read" | "write";

export interface CapabilitySummary {
  readonly id: string;
  readonly displayName: string;
  readonly connectorFamily: string;
  readonly effect: CapabilityEffect;
}

export interface AgentInstance {
  readonly id: string;
  readonly templateId: string;
  readonly displayName: string;
  readonly purpose: string;
  readonly displayOrder: number;
  readonly enabled: boolean;
  readonly operationClassification: OperationClassification;
  readonly triggerTypes: readonly TriggerType[];
  readonly capabilitySummaries: readonly CapabilitySummary[];
  readonly sourceOrdinal: number;
  readonly deploymentCount: number;
}

export interface AgentFunction {
  readonly id: string;
  readonly displayName: string;
  readonly displayOrder: number;
  readonly instances: readonly AgentInstance[];
}

export interface AgentDepartment {
  readonly id: string;
  readonly displayName: string;
  readonly displayOrder: number;
  readonly instanceCount: number;
  readonly templateCount: number;
  readonly functions: readonly AgentFunction[];
}

export interface HierarchyCounts {
  readonly departments: 5;
  readonly functions: 12;
  readonly templates: 36;
  readonly instances: 43;
}

export interface NormalizedHierarchy {
  readonly catalogVersion: string;
  readonly catalogHash: string;
  readonly counts: HierarchyCounts;
  readonly departments: readonly AgentDepartment[];
  readonly structuralKey: string;
}

export const MARKETING_AGENTS_ROOT = Object.freeze({
  id: "root",
  displayName: "Marketing Agents",
} as const);

export const MARKETING_ORCHESTRATOR_CONTROL_PLANE = Object.freeze({
  id: "control-plane.marketing-orchestrator",
  displayName: "Marketing Orchestrator",
  badgeLabel: "Control plane",
  countsAsInstance: false,
} as const);

export const EXPECTED_COUNTS = Object.freeze({
  departments: 5,
  functions: 12,
  templates: 36,
  instances: 43,
} as const);

export const EXPECTED_DEPARTMENT_INSTANCE_COUNTS = Object.freeze([
  12, 6, 5, 14, 6,
] as const);

export const EXPECTED_FUNCTION_COUNTS = Object.freeze([3, 2, 2, 3, 2] as const);

export const EXPECTED_FUNCTION_INSTANCE_COUNTS = Object.freeze([
  6, 2, 4, 3, 3, 2, 3, 6, 6, 2, 5, 1,
] as const);

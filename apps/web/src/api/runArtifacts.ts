import { ApiRequestError, getJson } from "./client";

const RESOURCE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$/u;
const AUTHORITY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._/-]{0,239}$/u;
const HEX_DIGEST_PATTERN = /^[0-9a-f]{64}$/u;
const CATALOG_HASH_PATTERN = /^(?:catalog-sha256-v1:)?[0-9a-f]{64}$/u;
const CATALOG_CONTENT_HASH_PATTERN = /^catalog-sha256-v1:[0-9a-f]{64}$/u;
const SCHEMA_HASH_PATTERN = /^schema-sha256-v1:[0-9a-f]{64}$/u;
const ARTIFACT_DIGEST_PATTERN = /^artifact-hmac-sha256-v1:[0-9a-f]{64}$/u;
const ISO_UTC_TIMESTAMP_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(?:Z|\+00:00)$/u;
const RUN_CURSOR_PATTERN = /^run-page-v1\.[A-Za-z0-9_-]{1,1012}$/u;
const TIMELINE_CURSOR_PATTERN = /^run-timeline-v1\.[A-Za-z0-9_-]{1,1008}$/u;
const ARTIFACT_CURSOR_PATTERN = /^artifact-page-v1\.[A-Za-z0-9_-]{1,1007}$/u;
const ERROR_CODE_PATTERN = /^[a-z][a-z0-9_]{0,127}$/u;
const UNSAFE_OBJECT_KEYS = new Set(["__proto__", "constructor", "prototype"]);

const DEFAULT_RUN_PAGE_SIZE = 25;
const DEFAULT_TIMELINE_PAGE_SIZE = 50;
const DEFAULT_ARTIFACT_PAGE_SIZE = 25;
const MAX_PAGE_SIZE = 100;
const MAX_CURSOR_LENGTH = 1_024;
const MAX_TEXT_LENGTH = 4_096;
const MAX_JSON_DEPTH = 64;
const MAX_JSON_NODES = 8_192;
const MAX_JSON_ARRAY_ITEMS = 4_096;
const MAX_JSON_OBJECT_FIELDS = 1_024;
const MAX_JSON_KEY_LENGTH = 100;
const MAX_JSON_STRING_LENGTH = 262_144;
const MAX_JSON_BYTES = 1_048_576;

export const RUN_STATES = Object.freeze([
  "received",
  "validated",
  "planned",
  "awaiting_approval",
  "executing",
  "completed",
  "failed",
  "rejected",
  "cancelled",
] as const);
export const STEP_STATES = Object.freeze([
  "pending",
  "ready",
  "awaiting_approval",
  "executing",
  "succeeded",
  "failed",
  "rejected",
  "cancelled",
  "skipped",
] as const);
export const EXTERNAL_ACTION_STATES = Object.freeze([
  "proposed",
  "awaiting_approval",
  "approved",
  "dispatch_reserved",
  "dispatching",
  "succeeded",
  "failed",
  "rejected",
  "cancelled",
  "superseded",
  "outcome_unknown",
] as const);
export const CLASSIFICATIONS = Object.freeze([
  "public",
  "internal",
  "personal",
  "sensitive",
  "secret",
] as const);

const RUN_STATE_SET = new Set<RunState>(RUN_STATES);
const TERMINAL_RUN_STATE_SET = new Set<RunState>([
  "completed",
  "failed",
  "rejected",
  "cancelled",
]);
const STEP_STATE_SET = new Set<StepState>(STEP_STATES);
const TERMINAL_STEP_STATE_SET = new Set<StepState>([
  "succeeded",
  "failed",
  "rejected",
  "cancelled",
  "skipped",
]);
const ACTION_STATE_SET = new Set<ExternalActionState>(EXTERNAL_ACTION_STATES);
const CLASSIFICATION_SET = new Set<Classification>(CLASSIFICATIONS);
const CLASSIFICATION_RANK: Readonly<Record<Classification, number>> =
  Object.freeze({
    public: 0,
    internal: 1,
    personal: 2,
    sensitive: 3,
    secret: 4,
  });

export const RUNS_QUERY_ROOT = Object.freeze(["runs"] as const);
export const ARTIFACTS_QUERY_ROOT = Object.freeze(["artifacts"] as const);

export type RunState = (typeof RUN_STATES)[number];
export type StepState = (typeof STEP_STATES)[number];
export type ExternalActionState = (typeof EXTERNAL_ACTION_STATES)[number];
export type Classification = (typeof CLASSIFICATIONS)[number];
export type RetainableClassification = Exclude<Classification, "secret">;
export type JsonValue =
  null | boolean | number | string | readonly JsonValue[] | JsonObject;
export interface JsonObject {
  readonly [key: string]: JsonValue;
}

export interface RunListQuery {
  readonly state?: RunState;
  readonly instanceId?: string;
  readonly workflowId?: string;
  readonly createdAtFrom?: string;
  readonly createdAtTo?: string;
  readonly cursor?: string;
  readonly limit?: number;
}

export interface PageQuery {
  readonly cursor?: string;
  readonly limit?: number;
}

export interface NormalizedRunListQuery {
  readonly state: RunState | null;
  readonly instanceId: string | null;
  readonly workflowId: string | null;
  readonly createdAtFrom: string | null;
  readonly createdAtTo: string | null;
  readonly cursor: string | null;
  readonly limit: number;
}

export interface NormalizedPageQuery {
  readonly cursor: string | null;
  readonly limit: number;
}

export interface RunSummary {
  readonly id: string;
  readonly workItemId: string;
  readonly instanceId: string;
  readonly workflowId: string;
  readonly triggerId: string;
  readonly source: string;
  readonly mode: "dry_run" | "mock_execution";
  readonly state: RunState;
  readonly catalogHash: string;
  readonly configurationRevision: number;
  readonly approvalRequired: boolean | null;
  readonly terminalReasonCode: string | null;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly version: number;
  readonly runUrl: string;
  readonly timelineUrl: string;
  readonly artifactsUrl: string;
  readonly instanceUrl: string;
}

export interface RunPage {
  readonly items: readonly RunSummary[];
  readonly nextCursor: string | null;
}

export interface RunTransition {
  readonly sequence: number;
  readonly command: string;
  readonly previousState: RunState | null;
  readonly newState: RunState;
  readonly reasonCode: string;
  readonly occurredAt: string;
  readonly expectedVersion: number;
  readonly resultingVersion: number;
  readonly completedEffectCount: number;
  readonly outcomeUnknownEffectCount: number;
}

export interface RunRuntimePolicy {
  readonly maxSteps: number;
  readonly maxModelCalls: number;
  readonly maxToolCalls: number;
  readonly runTimeoutSeconds: number;
}

export interface StepRuntimePolicy {
  readonly operationKey: string;
  readonly attemptKind: "model" | "tool" | "no_call";
  readonly maxAttempts: number;
  readonly backoff: "none" | "bounded_exponential";
  readonly stepTimeoutSeconds: number;
  readonly templateRunTimeoutSeconds: number;
  readonly maxSteps: number;
  readonly maxModelCalls: number;
  readonly maxToolCalls: number;
  readonly maxInputBytes: number;
  readonly maxInputFieldBytes: number;
  readonly maxOutputBytes: number;
  readonly maxModelOutputTokens: number;
  readonly rateLimitScope: string;
  readonly rateLimitKey: string;
  readonly rateLimitMaxCalls: number;
  readonly rateLimitWindowSeconds: number;
}

export interface RunStepTransition {
  readonly sequence: number;
  readonly command: string;
  readonly previousState: StepState | null;
  readonly newState: StepState;
  readonly reasonCode: string;
  readonly occurredAt: string;
  readonly expectedVersion: number;
  readonly resultingVersion: number;
}

export interface RunStep {
  readonly id: string;
  readonly runId: string;
  readonly key: string;
  readonly kind: string;
  readonly selectedInstanceId: string;
  readonly templateId: string;
  readonly dependencyKeys: readonly string[];
  readonly capabilityId: string;
  readonly effect: "read" | "write";
  readonly state: StepState;
  readonly ordinal: number;
  readonly sourceOrder: number;
  readonly configurationRevision: number;
  readonly connectorFamily: string;
  readonly routingSlotKey: string | null;
  readonly bindingId: string | null;
  readonly bindingConfigurationRevision: number | null;
  readonly requestSchemaId: string | null;
  readonly resultSchemaId: string | null;
  readonly resultSchemaHash: string | null;
  readonly dataClassification: Classification;
  readonly idempotencySupport:
    "not_applicable" | "required" | "supported" | "unavailable";
  readonly timeoutSeconds: number | null;
  readonly runtimePolicy: StepRuntimePolicy;
  readonly approvalPolicyId: string;
  readonly approvalRequiredRoles: readonly string[];
  readonly approvalRequiredScopes: readonly string[];
  readonly approvalExpiresAfterSeconds: number | null;
  readonly approvalAllowSelfApproval: boolean | null;
  readonly terminalResult: boolean;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly version: number;
  readonly terminalReasonCode: string | null;
  readonly transitions: readonly RunStepTransition[];
  readonly stepUrl: string;
  readonly runUrl: string;
  readonly instanceUrl: string;
  readonly templateUrl: string;
}

export interface RunSelectedInstance {
  readonly instanceId: string;
  readonly templateId: string;
  readonly configurationRevision: number;
  readonly displayOrder: number;
  readonly sourceOrdinal: number | null;
  readonly selectionOrder: number;
  readonly target: boolean;
  readonly instanceUrl: string;
  readonly templateUrl: string;
}

export interface RunRoutingAssignment {
  readonly slotKey: string;
  readonly instanceId: string;
  readonly templateId: string;
  readonly requiredCapabilityIds: readonly string[];
  readonly assignmentOrder: number;
  readonly instanceUrl: string;
  readonly templateUrl: string;
}

export interface RunPlan {
  readonly planHash: string;
  readonly workflowId: string;
  readonly workflowVersion: number;
  readonly workflowDefinitionHash: string;
  readonly catalogContentHash: string;
  readonly graphHash: string;
  readonly routingHash: string;
  readonly approvalRequired: boolean;
  readonly stepCount: number;
  readonly runtimePolicy: RunRuntimePolicy;
  readonly createdAt: string;
  readonly selectedInstances: readonly RunSelectedInstance[];
  readonly routingAssignments: readonly RunRoutingAssignment[];
  readonly steps: readonly RunStep[];
}

export interface RunExecutionControl {
  readonly runTimeoutSeconds: number;
  readonly maxModelCalls: number;
  readonly maxToolCalls: number;
  readonly modelCalls: number;
  readonly toolCalls: number;
  readonly remainingModelCalls: number;
  readonly remainingToolCalls: number;
  readonly startedAt: string | null;
  readonly deadlineAt: string | null;
  readonly cancelRequestedAt: string | null;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly version: number;
}

export interface PendingApprovalSummary {
  readonly id: string;
  readonly actionId: string;
  readonly stepId: string;
  readonly status: "pending";
  readonly destinationSummary: string;
  readonly requestedAt: string;
  readonly expiresAt: string;
  readonly isExpired: boolean;
  readonly approvalUrl: string;
  readonly actionUrl: string;
  readonly stepUrl: string;
}

export interface ArtifactSummary {
  readonly id: string;
  readonly workItemId: string;
  readonly runId: string;
  readonly stepId: string;
  readonly workflowId: string;
  readonly workflowVersion: string;
  readonly templateId: string;
  readonly instanceId: string;
  readonly outputSchemaId: string;
  readonly outputSchemaVersion: string;
  readonly classification: Classification;
  readonly createdAt: string;
  readonly artifactUrl: string;
  readonly runUrl: string;
  readonly stepUrl: string;
  readonly templateUrl: string;
  readonly instanceUrl: string;
}

export interface ArtifactSource {
  readonly kind: "work_input" | "external_observation" | "parent_artifact";
  readonly sourceId: string;
  readonly classification: RetainableClassification;
}

export interface ArtifactProvider {
  readonly providerKind: "llm" | "connector" | "planner";
  readonly mode: "mock" | "real" | "local";
  readonly name: string;
  readonly version: string;
}

export interface ArtifactResource extends ArtifactSummary {
  readonly classification: RetainableClassification;
  readonly catalogHash: string;
  readonly instanceConfigRevision: number;
  readonly sources: readonly ArtifactSource[];
  readonly parentArtifactIds: readonly string[];
  readonly providers: readonly ArtifactProvider[];
  readonly outputSchemaHash: string;
  readonly redactedPayload: JsonObject;
  readonly payloadDigest: string;
}

export interface ArtifactPage {
  readonly runId: string;
  readonly items: readonly ArtifactSummary[];
  readonly nextCursor: string | null;
}

export interface ExternalAction {
  readonly id: string;
  readonly runId: string;
  readonly stepId: string;
  readonly stepKey: string;
  readonly templateId: string;
  readonly instanceId: string;
  readonly proposalRevision: number;
  readonly actionType: string;
  readonly capabilityId: string;
  readonly connectorFamily: string;
  readonly bindingId: string;
  readonly destinationSummary: string;
  readonly redactedPayload: JsonObject;
  readonly payloadSchemaId: string;
  readonly state: ExternalActionState;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly version: number;
  readonly deliveryAttemptCount: number;
  readonly deliveryAttemptLimit: number;
  readonly approvalPolicyId: string;
  readonly approvalRequiredRoles: readonly string[];
  readonly approvalRequiredScopes: readonly string[];
  readonly approvalExpiresAfterSeconds: number;
  readonly approvalAllowSelfApproval: boolean;
  readonly terminalReasonCode: string | null;
  readonly supersededByActionId: string | null;
  readonly supersededAt: string | null;
  readonly receiptId: string | null;
  readonly resultStatus: string | null;
  readonly resultSafeMetadata: null;
  readonly completedAt: string | null;
  readonly actionUrl: string;
  readonly runUrl: string;
  readonly stepUrl: string;
  readonly instanceUrl: string;
  readonly templateUrl: string;
}

export interface RunTerminalError {
  readonly code: string;
  readonly causeCode: string | null;
  readonly source: "run" | "step" | "read_attempt" | "external_action";
  readonly stepId: string | null;
  readonly actionId: string | null;
  readonly outcome: string | null;
  readonly finalAttemptNumber: number | null;
  readonly retryable: false;
  readonly callDeadlineAt: string | null;
  readonly runDeadlineAt: string | null;
  readonly occurredAt: string;
  readonly stepUrl: string | null;
  readonly actionUrl: string | null;
}

export interface RunResource extends RunSummary {
  readonly transitions: readonly RunTransition[];
  readonly plan: RunPlan | null;
  readonly executionControl: RunExecutionControl | null;
  readonly pendingApprovals: readonly PendingApprovalSummary[];
  readonly artifactSummaries: readonly ArtifactSummary[];
  readonly artifactsTruncated: boolean;
  readonly externalActions: readonly ExternalAction[];
  readonly terminalError: RunTerminalError | null;
}

export interface RunTimelineEvent {
  readonly id: string;
  readonly sequence: number;
  readonly schemaVersion: number;
  readonly eventType: string;
  readonly aggregateType: string;
  readonly aggregateId: string;
  readonly outcome: string;
  readonly actorId: string;
  readonly actorSource: string;
  readonly authMethod: string;
  readonly correlationId: string;
  readonly occurredAt: string;
  readonly stepId: string | null;
  readonly actionId: string | null;
  readonly approvalRequestId: string | null;
  readonly artifactId: string | null;
  readonly attemptedCommand: string | null;
  readonly previousState: string | null;
  readonly newState: string | null;
  readonly reasonCode: string | null;
  readonly metadata: JsonObject;
  readonly metadataClassification: Classification;
  readonly metadataExpiresAt: string;
  readonly metadataExpired: boolean;
  readonly runUrl: string;
  readonly stepUrl: string | null;
  readonly actionUrl: string | null;
  readonly approvalUrl: string | null;
  readonly artifactUrl: string | null;
}

export interface RunTimelinePage {
  readonly runId: string;
  readonly items: readonly RunTimelineEvent[];
  readonly nextCursor: string | null;
}

export class RunArtifactsContractError extends Error {
  constructor(message: string) {
    super(`Run/artifact contract violation: ${message}`);
    this.name = "RunArtifactsContractError";
  }
}

export class RunArtifactsRequestError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "RunArtifactsRequestError";
    this.status = status;
    this.code = code;
  }
}

function fail(message: string): never {
  throw new RunArtifactsContractError(message);
}

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return fail(`${label} must be an object`);
  }
  const prototype = Object.getPrototypeOf(value) as object | null;
  if (prototype !== Object.prototype && prototype !== null) {
    return fail(`${label} must be a plain object`);
  }
  for (const key of Reflect.ownKeys(value)) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (
      typeof key !== "string" ||
      descriptor === undefined ||
      !("value" in descriptor) ||
      !descriptor.enumerable
    ) {
      return fail(`${label} must contain plain data`);
    }
  }
  return value as Record<string, unknown>;
}

function assertExactFields(
  record: Record<string, unknown>,
  expected: ReadonlySet<string>,
  label: string,
): void {
  const keys = Object.keys(record);
  if (
    keys.length !== expected.size ||
    keys.some((key) => !expected.has(key) || UNSAFE_OBJECT_KEYS.has(key))
  ) {
    fail(`${label} fields are unsupported`);
  }
}

function asArray(
  value: unknown,
  label: string,
  maximum: number,
): readonly unknown[] {
  if (!Array.isArray(value) || value.length > maximum) {
    return fail(`${label} must be a bounded array`);
  }
  if (Object.getPrototypeOf(value) !== Array.prototype) {
    return fail(`${label} must be a plain array`);
  }
  for (let index = 0; index < value.length; index += 1) {
    const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
    if (
      descriptor === undefined ||
      !("value" in descriptor) ||
      !descriptor.enumerable
    ) {
      return fail(`${label} must not be sparse`);
    }
  }
  if (Reflect.ownKeys(value).length !== value.length + 1) {
    return fail(`${label} fields are unsupported`);
  }
  return value;
}

function asBoolean(value: unknown, label: string): boolean {
  return typeof value === "boolean"
    ? value
    : fail(`${label} must be a boolean`);
}

function asInteger(
  value: unknown,
  label: string,
  minimum: number,
  maximum = Number.MAX_SAFE_INTEGER,
): number {
  return typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= minimum &&
    value <= maximum
    ? value
    : fail(`${label} must be a bounded integer`);
}

function asBoundedString(
  value: unknown,
  label: string,
  maximum = MAX_TEXT_LENGTH,
): string {
  return typeof value === "string" &&
    value.length > 0 &&
    value.length <= maximum &&
    !hasUnsupportedTextCharacter(value)
    ? value
    : fail(`${label} must be bounded text`);
}

function hasUnsupportedTextCharacter(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code <= 31 || code === 127) return true;
  }
  return false;
}

function asResourceId(value: unknown, label: string): string {
  return typeof value === "string" && RESOURCE_ID_PATTERN.test(value)
    ? value
    : fail(`${label} must be a stable resource ID`);
}

function asNullableResourceId(value: unknown, label: string): string | null {
  return value === null ? null : asResourceId(value, label);
}

function asAuthority(value: unknown, label: string): string {
  return typeof value === "string" && AUTHORITY_PATTERN.test(value)
    ? value
    : fail(`${label} must be a stable authority`);
}

function asNullableAuthority(value: unknown, label: string): string | null {
  return value === null ? null : asAuthority(value, label);
}

function asTimestamp(value: unknown, label: string): string {
  if (typeof value !== "string")
    return fail(`${label} must be a UTC timestamp`);
  const match = ISO_UTC_TIMESTAMP_PATTERN.exec(value);
  if (match === null) return fail(`${label} must be a UTC timestamp`);
  const parts = match.slice(1, 7).map(Number);
  const [year, month, day, hour, minute, second] = parts;
  if (
    year === undefined ||
    month === undefined ||
    day === undefined ||
    hour === undefined ||
    minute === undefined ||
    second === undefined
  ) {
    return fail(`${label} must be a UTC timestamp`);
  }
  const calendar = new Date(0);
  calendar.setUTCFullYear(year, month - 1, day);
  calendar.setUTCHours(hour, minute, second, 0);
  if (
    calendar.getUTCFullYear() !== year ||
    calendar.getUTCMonth() !== month - 1 ||
    calendar.getUTCDate() !== day ||
    calendar.getUTCHours() !== hour ||
    calendar.getUTCMinutes() !== minute ||
    calendar.getUTCSeconds() !== second
  ) {
    return fail(`${label} must be a UTC timestamp`);
  }
  return value;
}

function asNullableTimestamp(value: unknown, label: string): string | null {
  return value === null ? null : asTimestamp(value, label);
}

function timestampNanoseconds(value: string): bigint {
  const normalized = asTimestamp(value, "timestamp");
  const match = ISO_UTC_TIMESTAMP_PATTERN.exec(normalized);
  if (match === null) return fail("timestamp comparison requires UTC values");
  const parts = match.slice(1, 7).map(Number);
  const [year, month, day, hour, minute, second] = parts;
  if (
    year === undefined ||
    month === undefined ||
    day === undefined ||
    hour === undefined ||
    minute === undefined ||
    second === undefined
  ) {
    return fail("timestamp comparison requires UTC values");
  }
  const calendar = new Date(0);
  calendar.setUTCFullYear(year, month - 1, day);
  calendar.setUTCHours(hour, minute, second, 0);
  const fraction = (match[7] ?? "").padEnd(9, "0");
  return BigInt(calendar.getTime()) * 1_000_000n + BigInt(fraction || "0");
}

export function compareRunTimestamps(left: string, right: string): number {
  const leftValue = timestampNanoseconds(left);
  const rightValue = timestampNanoseconds(right);
  return leftValue < rightValue ? -1 : leftValue > rightValue ? 1 : 0;
}

function asEnum<const T extends string>(
  value: unknown,
  values: ReadonlySet<T>,
  label: string,
): T {
  return typeof value === "string" && values.has(value as T)
    ? (value as T)
    : fail(`${label} is unsupported`);
}

function asPattern(value: unknown, pattern: RegExp, label: string): string {
  return typeof value === "string" && pattern.test(value)
    ? value
    : fail(`${label} is invalid`);
}

function asExactUrl(value: unknown, expected: string, label: string): string {
  return value === expected
    ? expected
    : fail(`${label} does not match its resource`);
}

function asNullableExactUrl(
  value: unknown,
  expected: string | null,
  label: string,
): string | null {
  return value === expected
    ? expected
    : fail(`${label} does not match its resource`);
}

function asUniqueStrings(
  value: unknown,
  label: string,
  maximum: number,
  parser: (item: unknown, itemLabel: string) => string = asAuthority,
): readonly string[] {
  const items = asArray(value, label, maximum).map((item, index) =>
    parser(item, `${label}[${String(index)}]`),
  );
  if (new Set(items).size !== items.length) fail(`${label} must be unique`);
  return Object.freeze(items);
}

function haveSameStringMembers(
  left: readonly string[],
  right: readonly string[],
): boolean {
  if (left.length !== right.length) return false;
  const rightMembers = new Set(right);
  return left.every((item) => rightMembers.has(item));
}

function externalActionBindsStep(
  action: ExternalAction,
  step: RunStep | undefined,
): boolean {
  return (
    step?.key === action.stepKey &&
    step.effect === "write" &&
    step.idempotencySupport === "required" &&
    step.templateId === action.templateId &&
    step.selectedInstanceId === action.instanceId &&
    step.capabilityId === action.capabilityId &&
    step.connectorFamily === action.connectorFamily &&
    step.bindingId === action.bindingId &&
    step.requestSchemaId === action.payloadSchemaId &&
    step.approvalPolicyId === action.approvalPolicyId &&
    haveSameStringMembers(
      step.approvalRequiredRoles,
      action.approvalRequiredRoles,
    ) &&
    haveSameStringMembers(
      step.approvalRequiredScopes,
      action.approvalRequiredScopes,
    ) &&
    step.approvalExpiresAfterSeconds === action.approvalExpiresAfterSeconds &&
    step.approvalAllowSelfApproval === action.approvalAllowSelfApproval
  );
}

interface JsonBudget {
  nodes: number;
}

function normalizeJsonValue(
  value: unknown,
  label: string,
  depth: number,
  budget: JsonBudget,
  active: WeakSet<object>,
): JsonValue {
  budget.nodes += 1;
  if (depth > MAX_JSON_DEPTH || budget.nodes > MAX_JSON_NODES) {
    return fail(`${label} exceeds JSON bounds`);
  }
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "number") {
    return Number.isFinite(value)
      ? value
      : fail(`${label} must contain finite numbers`);
  }
  if (typeof value === "string") {
    return value.length <= MAX_JSON_STRING_LENGTH
      ? value
      : fail(`${label} exceeds JSON bounds`);
  }
  if (typeof value !== "object")
    return fail(`${label} must contain JSON values`);
  if (active.has(value)) return fail(`${label} must not be cyclic`);
  active.add(value);
  try {
    if (Array.isArray(value)) {
      return Object.freeze(
        asArray(value, label, MAX_JSON_ARRAY_ITEMS).map((item) =>
          normalizeJsonValue(item, label, depth + 1, budget, active),
        ),
      );
    }
    const record = asRecord(value, label);
    const keys = Object.keys(record);
    if (keys.length > MAX_JSON_OBJECT_FIELDS) {
      return fail(`${label} exceeds JSON bounds`);
    }
    const result = Object.create(null) as Record<string, JsonValue>;
    for (const key of keys) {
      if (
        key.length === 0 ||
        key.length > MAX_JSON_KEY_LENGTH ||
        UNSAFE_OBJECT_KEYS.has(key) ||
        hasUnsupportedTextCharacter(key)
      ) {
        return fail(`${label} has an unsafe key`);
      }
      result[key] = normalizeJsonValue(
        record[key],
        label,
        depth + 1,
        budget,
        active,
      );
    }
    return Object.freeze(result);
  } finally {
    active.delete(value);
  }
}

function normalizeJsonObject(value: unknown, label: string): JsonObject {
  const normalized = normalizeJsonValue(
    value,
    label,
    1,
    { nodes: 0 },
    new WeakSet(),
  );
  if (
    normalized === null ||
    typeof normalized !== "object" ||
    Array.isArray(normalized)
  ) {
    return fail(`${label} must be a JSON object`);
  }
  const serialized = JSON.stringify(normalized);
  if (new TextEncoder().encode(serialized).byteLength > MAX_JSON_BYTES) {
    return fail(`${label} exceeds JSON bounds`);
  }
  return normalized as JsonObject;
}

function runUrl(runId: string): string {
  return `/api/v1/runs/${runId}`;
}

function timelineUrl(runId: string): string {
  return `${runUrl(runId)}/timeline`;
}

function artifactsUrl(runId: string): string {
  return `${runUrl(runId)}/artifacts`;
}

function stepUrl(runId: string, stepId: string): string {
  return `${runUrl(runId)}/steps/${stepId}`;
}

function actionUrl(actionId: string): string {
  return `/api/v1/external-actions/${actionId}`;
}

function approvalUrl(approvalId: string): string {
  return `/api/v1/approvals/${approvalId}`;
}

function artifactUrl(artifactId: string): string {
  return `/api/v1/artifacts/${artifactId}`;
}

function instanceUrl(instanceId: string): string {
  return `/api/v1/agent-instances/${instanceId}`;
}

function templateUrl(templateId: string): string {
  return `/api/v1/agent-templates/${templateId}`;
}

const RUN_SUMMARY_FIELDS = new Set([
  "id",
  "work_item_id",
  "instance_id",
  "workflow_id",
  "trigger_id",
  "source",
  "mode",
  "state",
  "catalog_hash",
  "configuration_revision",
  "approval_required",
  "terminal_reason_code",
  "created_at",
  "updated_at",
  "version",
  "run_url",
  "timeline_url",
  "artifacts_url",
  "instance_url",
]);
const RUN_RESOURCE_FIELDS = new Set([
  ...RUN_SUMMARY_FIELDS,
  "transitions",
  "plan",
  "execution_control",
  "pending_approvals",
  "artifact_summaries",
  "artifacts_truncated",
  "external_actions",
  "terminal_error",
]);
const PAGE_FIELDS = new Set(["items", "next_cursor"]);
const BOUND_PAGE_FIELDS = new Set(["run_id", "items", "next_cursor"]);
const RUN_TRANSITION_FIELDS = new Set([
  "sequence",
  "command",
  "previous_state",
  "new_state",
  "reason_code",
  "occurred_at",
  "expected_version",
  "resulting_version",
  "completed_effect_count",
  "outcome_unknown_effect_count",
]);
const RUN_POLICY_FIELDS = new Set([
  "max_steps",
  "max_model_calls",
  "max_tool_calls",
  "run_timeout_seconds",
]);
const STEP_POLICY_FIELDS = new Set([
  "operation_key",
  "attempt_kind",
  "max_attempts",
  "backoff",
  "step_timeout_seconds",
  "template_run_timeout_seconds",
  "max_steps",
  "max_model_calls",
  "max_tool_calls",
  "max_input_bytes",
  "max_input_field_bytes",
  "max_output_bytes",
  "max_model_output_tokens",
  "rate_limit_scope",
  "rate_limit_key",
  "rate_limit_max_calls",
  "rate_limit_window_seconds",
]);
const STEP_TRANSITION_FIELDS = new Set([
  "sequence",
  "command",
  "previous_state",
  "new_state",
  "reason_code",
  "occurred_at",
  "expected_version",
  "resulting_version",
]);
const STEP_FIELDS = new Set([
  "id",
  "run_id",
  "key",
  "kind",
  "selected_instance_id",
  "template_id",
  "dependency_keys",
  "capability_id",
  "effect",
  "state",
  "ordinal",
  "source_order",
  "configuration_revision",
  "connector_family",
  "routing_slot_key",
  "binding_id",
  "binding_configuration_revision",
  "request_schema_id",
  "result_schema_id",
  "result_schema_hash",
  "data_classification",
  "idempotency_support",
  "timeout_seconds",
  "runtime_policy",
  "approval_policy_id",
  "approval_required_roles",
  "approval_required_scopes",
  "approval_expires_after_seconds",
  "approval_allow_self_approval",
  "terminal_result",
  "created_at",
  "updated_at",
  "version",
  "terminal_reason_code",
  "transitions",
  "step_url",
  "run_url",
  "instance_url",
  "template_url",
]);
const SELECTED_INSTANCE_FIELDS = new Set([
  "instance_id",
  "template_id",
  "configuration_revision",
  "display_order",
  "source_ordinal",
  "selection_order",
  "target",
  "instance_url",
  "template_url",
]);
const ROUTING_ASSIGNMENT_FIELDS = new Set([
  "slot_key",
  "instance_id",
  "template_id",
  "required_capability_ids",
  "assignment_order",
  "instance_url",
  "template_url",
]);
const PLAN_FIELDS = new Set([
  "plan_hash",
  "workflow_id",
  "workflow_version",
  "workflow_definition_hash",
  "catalog_content_hash",
  "graph_hash",
  "routing_hash",
  "approval_required",
  "step_count",
  "runtime_policy",
  "created_at",
  "selected_instances",
  "routing_assignments",
  "steps",
]);
const EXECUTION_CONTROL_FIELDS = new Set([
  "run_timeout_seconds",
  "max_model_calls",
  "max_tool_calls",
  "model_calls",
  "tool_calls",
  "remaining_model_calls",
  "remaining_tool_calls",
  "started_at",
  "deadline_at",
  "cancel_requested_at",
  "created_at",
  "updated_at",
  "version",
]);
const PENDING_APPROVAL_FIELDS = new Set([
  "id",
  "action_id",
  "step_id",
  "status",
  "destination_summary",
  "requested_at",
  "expires_at",
  "is_expired",
  "approval_url",
  "action_url",
  "step_url",
]);
const ARTIFACT_SUMMARY_FIELDS = new Set([
  "id",
  "work_item_id",
  "run_id",
  "step_id",
  "workflow_id",
  "workflow_version",
  "template_id",
  "instance_id",
  "output_schema_id",
  "output_schema_version",
  "classification",
  "created_at",
  "artifact_url",
  "run_url",
  "step_url",
  "template_url",
  "instance_url",
]);
const ARTIFACT_RESOURCE_FIELDS = new Set([
  ...ARTIFACT_SUMMARY_FIELDS,
  "catalog_hash",
  "instance_config_revision",
  "sources",
  "parent_artifact_ids",
  "providers",
  "output_schema_hash",
  "redacted_payload",
  "payload_digest",
]);
const ARTIFACT_SOURCE_FIELDS = new Set(["kind", "source_id", "classification"]);
const ARTIFACT_PROVIDER_FIELDS = new Set([
  "provider_kind",
  "mode",
  "name",
  "version",
]);
const EXTERNAL_ACTION_FIELDS = new Set([
  "id",
  "run_id",
  "step_id",
  "step_key",
  "template_id",
  "instance_id",
  "proposal_revision",
  "action_type",
  "capability_id",
  "connector_family",
  "binding_id",
  "destination_summary",
  "redacted_payload",
  "payload_schema_id",
  "state",
  "created_at",
  "updated_at",
  "version",
  "delivery_attempt_count",
  "delivery_attempt_limit",
  "approval_policy_id",
  "approval_required_roles",
  "approval_required_scopes",
  "approval_expires_after_seconds",
  "approval_allow_self_approval",
  "terminal_reason_code",
  "superseded_by_action_id",
  "superseded_at",
  "receipt_id",
  "result_status",
  "result_safe_metadata",
  "completed_at",
  "action_url",
  "run_url",
  "step_url",
  "instance_url",
  "template_url",
]);
const TERMINAL_ERROR_FIELDS = new Set([
  "code",
  "cause_code",
  "source",
  "step_id",
  "action_id",
  "outcome",
  "final_attempt_number",
  "retryable",
  "call_deadline_at",
  "run_deadline_at",
  "occurred_at",
  "step_url",
  "action_url",
]);
const TIMELINE_EVENT_FIELDS = new Set([
  "id",
  "sequence",
  "schema_version",
  "event_type",
  "aggregate_type",
  "aggregate_id",
  "outcome",
  "actor_id",
  "actor_source",
  "auth_method",
  "correlation_id",
  "occurred_at",
  "step_id",
  "action_id",
  "approval_request_id",
  "artifact_id",
  "attempted_command",
  "previous_state",
  "new_state",
  "reason_code",
  "metadata",
  "metadata_classification",
  "metadata_expires_at",
  "metadata_expired",
  "run_url",
  "step_url",
  "action_url",
  "approval_url",
  "artifact_url",
]);

function normalizeRunSummary(
  value: unknown,
  label = "run summary",
): RunSummary {
  const record = asRecord(value, label);
  assertExactFields(record, RUN_SUMMARY_FIELDS, label);
  const id = asResourceId(record.id, `${label}.id`);
  const instanceId = asResourceId(record.instance_id, `${label}.instance_id`);
  const createdAt = asTimestamp(record.created_at, `${label}.created_at`);
  const updatedAt = asTimestamp(record.updated_at, `${label}.updated_at`);
  if (compareRunTimestamps(updatedAt, createdAt) < 0) {
    fail(`${label}.updated_at precedes creation`);
  }
  const approvalRequired = record.approval_required;
  if (approvalRequired !== null && typeof approvalRequired !== "boolean") {
    fail(`${label}.approval_required is invalid`);
  }
  const mode = record.mode;
  if (mode !== "dry_run" && mode !== "mock_execution") {
    fail(`${label}.mode is unsupported`);
  }
  const state = asEnum(record.state, RUN_STATE_SET, `${label}.state`);
  const terminalReasonCode = asNullableAuthority(
    record.terminal_reason_code,
    `${label}.terminal_reason_code`,
  );
  if (TERMINAL_RUN_STATE_SET.has(state) !== (terminalReasonCode !== null)) {
    fail(`${label} terminal state and reason are incoherent`);
  }
  return Object.freeze({
    id,
    workItemId: asResourceId(record.work_item_id, `${label}.work_item_id`),
    instanceId,
    workflowId: asResourceId(record.workflow_id, `${label}.workflow_id`),
    triggerId: asResourceId(record.trigger_id, `${label}.trigger_id`),
    source: asAuthority(record.source, `${label}.source`),
    mode,
    state,
    catalogHash: asPattern(
      record.catalog_hash,
      CATALOG_HASH_PATTERN,
      `${label}.catalog_hash`,
    ),
    configurationRevision: asInteger(
      record.configuration_revision,
      `${label}.configuration_revision`,
      1,
    ),
    approvalRequired,
    terminalReasonCode,
    createdAt,
    updatedAt,
    version: asInteger(record.version, `${label}.version`, 1),
    runUrl: asExactUrl(record.run_url, runUrl(id), `${label}.run_url`),
    timelineUrl: asExactUrl(
      record.timeline_url,
      timelineUrl(id),
      `${label}.timeline_url`,
    ),
    artifactsUrl: asExactUrl(
      record.artifacts_url,
      artifactsUrl(id),
      `${label}.artifacts_url`,
    ),
    instanceUrl: asExactUrl(
      record.instance_url,
      instanceUrl(instanceId),
      `${label}.instance_url`,
    ),
  });
}

function normalizeArtifactSummary(
  value: unknown,
  label = "artifact summary",
): ArtifactSummary {
  const record = asRecord(value, label);
  assertExactFields(record, ARTIFACT_SUMMARY_FIELDS, label);
  const id = asResourceId(record.id, `${label}.id`);
  const runId = asResourceId(record.run_id, `${label}.run_id`);
  const stepId = asResourceId(record.step_id, `${label}.step_id`);
  const templateId = asResourceId(record.template_id, `${label}.template_id`);
  const instanceId = asResourceId(record.instance_id, `${label}.instance_id`);
  return Object.freeze({
    id,
    workItemId: asResourceId(record.work_item_id, `${label}.work_item_id`),
    runId,
    stepId,
    workflowId: asResourceId(record.workflow_id, `${label}.workflow_id`),
    workflowVersion: asBoundedString(
      record.workflow_version,
      `${label}.workflow_version`,
      100,
    ),
    templateId,
    instanceId,
    outputSchemaId: asResourceId(
      record.output_schema_id,
      `${label}.output_schema_id`,
    ),
    outputSchemaVersion: asBoundedString(
      record.output_schema_version,
      `${label}.output_schema_version`,
      100,
    ),
    classification: asEnum(
      record.classification,
      CLASSIFICATION_SET,
      `${label}.classification`,
    ),
    createdAt: asTimestamp(record.created_at, `${label}.created_at`),
    artifactUrl: asExactUrl(
      record.artifact_url,
      artifactUrl(id),
      `${label}.artifact_url`,
    ),
    runUrl: asExactUrl(record.run_url, runUrl(runId), `${label}.run_url`),
    stepUrl: asExactUrl(
      record.step_url,
      stepUrl(runId, stepId),
      `${label}.step_url`,
    ),
    templateUrl: asExactUrl(
      record.template_url,
      templateUrl(templateId),
      `${label}.template_url`,
    ),
    instanceUrl: asExactUrl(
      record.instance_url,
      instanceUrl(instanceId),
      `${label}.instance_url`,
    ),
  });
}

export function normalizeArtifactResource(
  value: unknown,
  expectedArtifactId?: string,
): ArtifactResource {
  const label = "artifact";
  const record = asRecord(value, label);
  assertExactFields(record, ARTIFACT_RESOURCE_FIELDS, label);
  const summary = normalizeArtifactSummary(
    Object.fromEntries(
      Object.entries(record).filter(([key]) =>
        ARTIFACT_SUMMARY_FIELDS.has(key),
      ),
    ),
    label,
  );
  if (
    expectedArtifactId !== undefined &&
    summary.id !== asResourceId(expectedArtifactId, "artifact ID")
  ) {
    fail("artifact does not match its request");
  }
  if (summary.classification === "secret") {
    fail("artifact classification is not retainable");
  }
  const sources = asArray(record.sources, "artifact.sources", 256).map(
    (value, index): ArtifactSource => {
      const itemLabel = `artifact.sources[${String(index)}]`;
      const item = asRecord(value, itemLabel);
      assertExactFields(item, ARTIFACT_SOURCE_FIELDS, itemLabel);
      const classification = asEnum(
        item.classification,
        CLASSIFICATION_SET,
        `${itemLabel}.classification`,
      );
      if (classification === "secret") {
        fail(`${itemLabel}.classification is not retainable`);
      }
      const kind = item.kind;
      if (
        kind !== "work_input" &&
        kind !== "external_observation" &&
        kind !== "parent_artifact"
      ) {
        fail(`${itemLabel}.kind is unsupported`);
      }
      return Object.freeze({
        kind,
        sourceId: asResourceId(item.source_id, `${itemLabel}.source_id`),
        classification,
      });
    },
  );
  if (sources.length === 0) fail("artifact.sources must not be empty");
  const parentArtifactIds = asUniqueStrings(
    record.parent_artifact_ids,
    "artifact.parent_artifact_ids",
    256,
    asResourceId,
  );
  if (parentArtifactIds.includes(summary.id)) {
    fail("artifact cannot be its own parent");
  }
  if (
    new Set(sources.map(({ sourceId }) => sourceId)).size !== sources.length
  ) {
    fail("artifact source IDs must be unique");
  }
  const parentSourceIds = sources
    .filter(({ kind }) => kind === "parent_artifact")
    .map(({ sourceId }) => sourceId);
  if (!haveSameStringMembers(parentSourceIds, parentArtifactIds)) {
    fail("artifact parent sources do not match its parent IDs");
  }
  if (
    sources.some(
      ({ classification }) =>
        CLASSIFICATION_RANK[classification] >
        CLASSIFICATION_RANK[summary.classification],
    )
  ) {
    fail("artifact classification is lower than a source");
  }
  const providers = asArray(record.providers, "artifact.providers", 32).map(
    (value, index): ArtifactProvider => {
      const itemLabel = `artifact.providers[${String(index)}]`;
      const item = asRecord(value, itemLabel);
      assertExactFields(item, ARTIFACT_PROVIDER_FIELDS, itemLabel);
      const providerKind = item.provider_kind;
      const mode = item.mode;
      if (
        providerKind !== "llm" &&
        providerKind !== "connector" &&
        providerKind !== "planner"
      ) {
        fail(`${itemLabel}.provider_kind is unsupported`);
      }
      if (mode !== "mock" && mode !== "real" && mode !== "local") {
        fail(`${itemLabel}.mode is unsupported`);
      }
      return Object.freeze({
        providerKind,
        mode,
        name: asBoundedString(item.name, `${itemLabel}.name`, 100),
        version: asBoundedString(item.version, `${itemLabel}.version`, 100),
      });
    },
  );
  if (providers.length === 0) fail("artifact.providers must not be empty");
  return Object.freeze({
    ...summary,
    classification: summary.classification,
    catalogHash: asPattern(
      record.catalog_hash,
      CATALOG_HASH_PATTERN,
      "artifact.catalog_hash",
    ),
    instanceConfigRevision: asInteger(
      record.instance_config_revision,
      "artifact.instance_config_revision",
      1,
    ),
    sources: Object.freeze(sources),
    parentArtifactIds,
    providers: Object.freeze(providers),
    outputSchemaHash: asPattern(
      record.output_schema_hash,
      SCHEMA_HASH_PATTERN,
      "artifact.output_schema_hash",
    ),
    redactedPayload: normalizeJsonObject(
      record.redacted_payload,
      "artifact.redacted_payload",
    ),
    payloadDigest: asPattern(
      record.payload_digest,
      ARTIFACT_DIGEST_PATTERN,
      "artifact.payload_digest",
    ),
  });
}

export function normalizeExternalAction(
  value: unknown,
  expectedActionId?: string,
  expectedRunId?: string,
): ExternalAction {
  const label = "external action";
  const record = asRecord(value, label);
  assertExactFields(record, EXTERNAL_ACTION_FIELDS, label);
  const id = asResourceId(record.id, `${label}.id`);
  const runId = asResourceId(record.run_id, `${label}.run_id`);
  const stepId = asResourceId(record.step_id, `${label}.step_id`);
  const templateId = asResourceId(record.template_id, `${label}.template_id`);
  const instanceId = asResourceId(record.instance_id, `${label}.instance_id`);
  if (
    expectedActionId !== undefined &&
    id !== asResourceId(expectedActionId, "external action ID")
  ) {
    fail("external action does not match its request");
  }
  if (
    expectedRunId !== undefined &&
    runId !== asResourceId(expectedRunId, "external action run ID")
  ) {
    fail("external action does not match its run");
  }
  const createdAt = asTimestamp(record.created_at, `${label}.created_at`);
  const updatedAt = asTimestamp(record.updated_at, `${label}.updated_at`);
  if (compareRunTimestamps(updatedAt, createdAt) < 0) {
    fail(`${label}.updated_at precedes creation`);
  }
  const attemptCount = asInteger(
    record.delivery_attempt_count,
    `${label}.delivery_attempt_count`,
    0,
    10,
  );
  const attemptLimit = asInteger(
    record.delivery_attempt_limit,
    `${label}.delivery_attempt_limit`,
    1,
    10,
  );
  if (attemptCount > attemptLimit) fail(`${label} exceeds its attempt limit`);
  const supersededByActionId = asNullableResourceId(
    record.superseded_by_action_id,
    `${label}.superseded_by_action_id`,
  );
  const supersededAt = asNullableTimestamp(
    record.superseded_at,
    `${label}.superseded_at`,
  );
  if ((supersededByActionId === null) !== (supersededAt === null)) {
    fail(`${label} supersession fields are incoherent`);
  }
  if (supersededByActionId === id) fail(`${label} cannot supersede itself`);
  const completedAt = asNullableTimestamp(
    record.completed_at,
    `${label}.completed_at`,
  );
  for (const timestamp of [supersededAt, completedAt]) {
    if (timestamp !== null && compareRunTimestamps(timestamp, createdAt) < 0) {
      fail(`${label} lifecycle time precedes creation`);
    }
  }
  if (record.result_safe_metadata !== null) {
    fail(`${label}.result_safe_metadata must be null`);
  }
  return Object.freeze({
    id,
    runId,
    stepId,
    stepKey: asAuthority(record.step_key, `${label}.step_key`),
    templateId,
    instanceId,
    proposalRevision: asInteger(
      record.proposal_revision,
      `${label}.proposal_revision`,
      1,
    ),
    actionType: asAuthority(record.action_type, `${label}.action_type`),
    capabilityId: asAuthority(record.capability_id, `${label}.capability_id`),
    connectorFamily: asAuthority(
      record.connector_family,
      `${label}.connector_family`,
    ),
    bindingId: asAuthority(record.binding_id, `${label}.binding_id`),
    destinationSummary: asBoundedString(
      record.destination_summary,
      `${label}.destination_summary`,
    ),
    redactedPayload: normalizeJsonObject(
      record.redacted_payload,
      `${label}.redacted_payload`,
    ),
    payloadSchemaId: asResourceId(
      record.payload_schema_id,
      `${label}.payload_schema_id`,
    ),
    state: asEnum(record.state, ACTION_STATE_SET, `${label}.state`),
    createdAt,
    updatedAt,
    version: asInteger(record.version, `${label}.version`, 1),
    deliveryAttemptCount: attemptCount,
    deliveryAttemptLimit: attemptLimit,
    approvalPolicyId: asResourceId(
      record.approval_policy_id,
      `${label}.approval_policy_id`,
    ),
    approvalRequiredRoles: asUniqueStrings(
      record.approval_required_roles,
      `${label}.approval_required_roles`,
      128,
    ),
    approvalRequiredScopes: asUniqueStrings(
      record.approval_required_scopes,
      `${label}.approval_required_scopes`,
      128,
    ),
    approvalExpiresAfterSeconds: asInteger(
      record.approval_expires_after_seconds,
      `${label}.approval_expires_after_seconds`,
      1,
      31_536_000,
    ),
    approvalAllowSelfApproval: asBoolean(
      record.approval_allow_self_approval,
      `${label}.approval_allow_self_approval`,
    ),
    terminalReasonCode: asNullableAuthority(
      record.terminal_reason_code,
      `${label}.terminal_reason_code`,
    ),
    supersededByActionId,
    supersededAt,
    receiptId: asNullableResourceId(record.receipt_id, `${label}.receipt_id`),
    resultStatus: asNullableAuthority(
      record.result_status,
      `${label}.result_status`,
    ),
    resultSafeMetadata: null,
    completedAt,
    actionUrl: asExactUrl(
      record.action_url,
      actionUrl(id),
      `${label}.action_url`,
    ),
    runUrl: asExactUrl(record.run_url, runUrl(runId), `${label}.run_url`),
    stepUrl: asExactUrl(
      record.step_url,
      stepUrl(runId, stepId),
      `${label}.step_url`,
    ),
    instanceUrl: asExactUrl(
      record.instance_url,
      instanceUrl(instanceId),
      `${label}.instance_url`,
    ),
    templateUrl: asExactUrl(
      record.template_url,
      templateUrl(templateId),
      `${label}.template_url`,
    ),
  });
}

function normalizeTimelineEvent(
  value: unknown,
  runId: string,
  label: string,
): RunTimelineEvent {
  const record = asRecord(value, label);
  assertExactFields(record, TIMELINE_EVENT_FIELDS, label);
  const stepId = asNullableResourceId(record.step_id, `${label}.step_id`);
  const actionId = asNullableResourceId(record.action_id, `${label}.action_id`);
  const approvalRequestId = asNullableResourceId(
    record.approval_request_id,
    `${label}.approval_request_id`,
  );
  const artifactId = asNullableResourceId(
    record.artifact_id,
    `${label}.artifact_id`,
  );
  const metadata = normalizeJsonObject(record.metadata, `${label}.metadata`);
  const metadataExpired = asBoolean(
    record.metadata_expired,
    `${label}.metadata_expired`,
  );
  if (metadataExpired && Object.keys(metadata).length !== 0) {
    fail(`${label}.metadata must be empty after expiry`);
  }
  const aggregateType = asAuthority(
    record.aggregate_type,
    `${label}.aggregate_type`,
  );
  const aggregateId = asResourceId(
    record.aggregate_id,
    `${label}.aggregate_id`,
  );
  if (aggregateType === "run" && aggregateId !== runId) {
    fail(`${label}.aggregate_id does not match its run`);
  }
  return Object.freeze({
    id: asResourceId(record.id, `${label}.id`),
    sequence: asInteger(record.sequence, `${label}.sequence`, 1),
    schemaVersion: asInteger(
      record.schema_version,
      `${label}.schema_version`,
      1,
    ),
    eventType: asAuthority(record.event_type, `${label}.event_type`),
    aggregateType,
    aggregateId,
    outcome: asAuthority(record.outcome, `${label}.outcome`),
    actorId: asResourceId(record.actor_id, `${label}.actor_id`),
    actorSource: asAuthority(record.actor_source, `${label}.actor_source`),
    authMethod: asAuthority(record.auth_method, `${label}.auth_method`),
    correlationId: asResourceId(
      record.correlation_id,
      `${label}.correlation_id`,
    ),
    occurredAt: asTimestamp(record.occurred_at, `${label}.occurred_at`),
    stepId,
    actionId,
    approvalRequestId,
    artifactId,
    attemptedCommand: asNullableAuthority(
      record.attempted_command,
      `${label}.attempted_command`,
    ),
    previousState: asNullableAuthority(
      record.previous_state,
      `${label}.previous_state`,
    ),
    newState: asNullableAuthority(record.new_state, `${label}.new_state`),
    reasonCode: asNullableAuthority(record.reason_code, `${label}.reason_code`),
    metadata,
    metadataClassification: asEnum(
      record.metadata_classification,
      CLASSIFICATION_SET,
      `${label}.metadata_classification`,
    ),
    metadataExpiresAt: asTimestamp(
      record.metadata_expires_at,
      `${label}.metadata_expires_at`,
    ),
    metadataExpired,
    runUrl: asExactUrl(record.run_url, runUrl(runId), `${label}.run_url`),
    stepUrl: asNullableExactUrl(
      record.step_url,
      stepId === null ? null : stepUrl(runId, stepId),
      `${label}.step_url`,
    ),
    actionUrl: asNullableExactUrl(
      record.action_url,
      actionId === null ? null : actionUrl(actionId),
      `${label}.action_url`,
    ),
    approvalUrl: asNullableExactUrl(
      record.approval_url,
      approvalRequestId === null ? null : approvalUrl(approvalRequestId),
      `${label}.approval_url`,
    ),
    artifactUrl: asNullableExactUrl(
      record.artifact_url,
      artifactId === null ? null : artifactUrl(artifactId),
      `${label}.artifact_url`,
    ),
  });
}

function normalizeRunTransition(value: unknown, label: string): RunTransition {
  const record = asRecord(value, label);
  assertExactFields(record, RUN_TRANSITION_FIELDS, label);
  const previousState =
    record.previous_state === null
      ? null
      : asEnum(record.previous_state, RUN_STATE_SET, `${label}.previous_state`);
  return Object.freeze({
    sequence: asInteger(record.sequence, `${label}.sequence`, 1),
    command: asAuthority(record.command, `${label}.command`),
    previousState,
    newState: asEnum(record.new_state, RUN_STATE_SET, `${label}.new_state`),
    reasonCode: asAuthority(record.reason_code, `${label}.reason_code`),
    occurredAt: asTimestamp(record.occurred_at, `${label}.occurred_at`),
    expectedVersion: asInteger(
      record.expected_version,
      `${label}.expected_version`,
      0,
    ),
    resultingVersion: asInteger(
      record.resulting_version,
      `${label}.resulting_version`,
      1,
    ),
    completedEffectCount: asInteger(
      record.completed_effect_count,
      `${label}.completed_effect_count`,
      0,
    ),
    outcomeUnknownEffectCount: asInteger(
      record.outcome_unknown_effect_count,
      `${label}.outcome_unknown_effect_count`,
      0,
    ),
  });
}

function normalizeRunRuntimePolicy(
  value: unknown,
  label: string,
): RunRuntimePolicy {
  const record = asRecord(value, label);
  assertExactFields(record, RUN_POLICY_FIELDS, label);
  return Object.freeze({
    maxSteps: asInteger(record.max_steps, `${label}.max_steps`, 1, 20),
    maxModelCalls: asInteger(
      record.max_model_calls,
      `${label}.max_model_calls`,
      0,
      10,
    ),
    maxToolCalls: asInteger(
      record.max_tool_calls,
      `${label}.max_tool_calls`,
      0,
      20,
    ),
    runTimeoutSeconds: asInteger(
      record.run_timeout_seconds,
      `${label}.run_timeout_seconds`,
      1,
      600,
    ),
  });
}

function normalizeStepRuntimePolicy(
  value: unknown,
  label: string,
): StepRuntimePolicy {
  const record = asRecord(value, label);
  assertExactFields(record, STEP_POLICY_FIELDS, label);
  const attemptKind = record.attempt_kind;
  if (
    attemptKind !== "model" &&
    attemptKind !== "tool" &&
    attemptKind !== "no_call"
  ) {
    fail(`${label}.attempt_kind is unsupported`);
  }
  const backoff = record.backoff;
  if (backoff !== "none" && backoff !== "bounded_exponential") {
    fail(`${label}.backoff is unsupported`);
  }
  return Object.freeze({
    operationKey: asAuthority(record.operation_key, `${label}.operation_key`),
    attemptKind,
    maxAttempts: asInteger(record.max_attempts, `${label}.max_attempts`, 1, 3),
    backoff,
    stepTimeoutSeconds: asInteger(
      record.step_timeout_seconds,
      `${label}.step_timeout_seconds`,
      1,
      120,
    ),
    templateRunTimeoutSeconds: asInteger(
      record.template_run_timeout_seconds,
      `${label}.template_run_timeout_seconds`,
      1,
      600,
    ),
    maxSteps: asInteger(record.max_steps, `${label}.max_steps`, 1, 20),
    maxModelCalls: asInteger(
      record.max_model_calls,
      `${label}.max_model_calls`,
      0,
      10,
    ),
    maxToolCalls: asInteger(
      record.max_tool_calls,
      `${label}.max_tool_calls`,
      0,
      20,
    ),
    maxInputBytes: asInteger(
      record.max_input_bytes,
      `${label}.max_input_bytes`,
      1,
      1_048_576,
    ),
    maxInputFieldBytes: asInteger(
      record.max_input_field_bytes,
      `${label}.max_input_field_bytes`,
      1,
      262_144,
    ),
    maxOutputBytes: asInteger(
      record.max_output_bytes,
      `${label}.max_output_bytes`,
      1,
      4_194_304,
    ),
    maxModelOutputTokens: asInteger(
      record.max_model_output_tokens,
      `${label}.max_model_output_tokens`,
      1,
      32_768,
    ),
    rateLimitScope: asAuthority(
      record.rate_limit_scope,
      `${label}.rate_limit_scope`,
    ),
    rateLimitKey: asAuthority(record.rate_limit_key, `${label}.rate_limit_key`),
    rateLimitMaxCalls: asInteger(
      record.rate_limit_max_calls,
      `${label}.rate_limit_max_calls`,
      1,
      100,
    ),
    rateLimitWindowSeconds: asInteger(
      record.rate_limit_window_seconds,
      `${label}.rate_limit_window_seconds`,
      1,
      3_600,
    ),
  });
}

function normalizeStepTransition(
  value: unknown,
  label: string,
): RunStepTransition {
  const record = asRecord(value, label);
  assertExactFields(record, STEP_TRANSITION_FIELDS, label);
  const previousState =
    record.previous_state === null
      ? null
      : asEnum(
          record.previous_state,
          STEP_STATE_SET,
          `${label}.previous_state`,
        );
  return Object.freeze({
    sequence: asInteger(record.sequence, `${label}.sequence`, 1),
    command: asAuthority(record.command, `${label}.command`),
    previousState,
    newState: asEnum(record.new_state, STEP_STATE_SET, `${label}.new_state`),
    reasonCode: asAuthority(record.reason_code, `${label}.reason_code`),
    occurredAt: asTimestamp(record.occurred_at, `${label}.occurred_at`),
    expectedVersion: asInteger(
      record.expected_version,
      `${label}.expected_version`,
      0,
    ),
    resultingVersion: asInteger(
      record.resulting_version,
      `${label}.resulting_version`,
      1,
    ),
  });
}

function normalizeRunStep(
  value: unknown,
  expectedRunId: string,
  label: string,
): RunStep {
  const record = asRecord(value, label);
  assertExactFields(record, STEP_FIELDS, label);
  const id = asResourceId(record.id, `${label}.id`);
  const runId = asResourceId(record.run_id, `${label}.run_id`);
  const selectedInstanceId = asResourceId(
    record.selected_instance_id,
    `${label}.selected_instance_id`,
  );
  const templateId = asResourceId(record.template_id, `${label}.template_id`);
  if (runId !== expectedRunId) fail(`${label}.run_id does not match its run`);
  const state = asEnum(record.state, STEP_STATE_SET, `${label}.state`);
  const createdAt = asTimestamp(record.created_at, `${label}.created_at`);
  const updatedAt = asTimestamp(record.updated_at, `${label}.updated_at`);
  if (compareRunTimestamps(updatedAt, createdAt) < 0) {
    fail(`${label}.updated_at precedes creation`);
  }
  const version = asInteger(record.version, `${label}.version`, 1);
  const transitions = asArray(
    record.transitions,
    `${label}.transitions`,
    64,
  ).map((item, index) =>
    normalizeStepTransition(item, `${label}.transitions[${String(index)}]`),
  );
  if (
    transitions.some(({ sequence }, index) => sequence !== index + 1) ||
    (transitions.length > 0 &&
      (transitions.at(-1)?.newState !== state ||
        transitions.at(-1)?.resultingVersion !== version))
  ) {
    fail(`${label}.transitions are incoherent`);
  }
  const requestSchemaId = asNullableResourceId(
    record.request_schema_id,
    `${label}.request_schema_id`,
  );
  const resultSchemaId = asNullableResourceId(
    record.result_schema_id,
    `${label}.result_schema_id`,
  );
  const resultSchemaHash =
    record.result_schema_hash === null
      ? null
      : asPattern(
          record.result_schema_hash,
          SCHEMA_HASH_PATTERN,
          `${label}.result_schema_hash`,
        );
  if ((resultSchemaId === null) !== (resultSchemaHash === null)) {
    fail(`${label} result schema fields are incoherent`);
  }
  const bindingId = asNullableAuthority(
    record.binding_id,
    `${label}.binding_id`,
  );
  const bindingConfigurationRevision =
    record.binding_configuration_revision === null
      ? null
      : asInteger(
          record.binding_configuration_revision,
          `${label}.binding_configuration_revision`,
          1,
        );
  if ((bindingId === null) !== (bindingConfigurationRevision === null)) {
    fail(`${label} binding fields are incoherent`);
  }
  const configurationRevision = asInteger(
    record.configuration_revision,
    `${label}.configuration_revision`,
    1,
  );
  if (
    bindingConfigurationRevision !== null &&
    bindingConfigurationRevision !== configurationRevision
  ) {
    fail(`${label} binding revision differs from its configuration revision`);
  }
  const connectorFamily = asAuthority(
    record.connector_family,
    `${label}.connector_family`,
  );
  const routingSlotKey = asNullableAuthority(
    record.routing_slot_key,
    `${label}.routing_slot_key`,
  );
  const dataClassification = asEnum(
    record.data_classification,
    CLASSIFICATION_SET,
    `${label}.data_classification`,
  );
  const timeoutSeconds =
    record.timeout_seconds === null
      ? null
      : asInteger(record.timeout_seconds, `${label}.timeout_seconds`, 1, 120);
  const runtimePolicy = normalizeStepRuntimePolicy(
    record.runtime_policy,
    `${label}.runtime_policy`,
  );
  const approvalRequiredRoles = asUniqueStrings(
    record.approval_required_roles,
    `${label}.approval_required_roles`,
    128,
  );
  const approvalRequiredScopes = asUniqueStrings(
    record.approval_required_scopes,
    `${label}.approval_required_scopes`,
    128,
  );
  const approvalExpiresAfterSeconds =
    record.approval_expires_after_seconds === null
      ? null
      : asInteger(
          record.approval_expires_after_seconds,
          `${label}.approval_expires_after_seconds`,
          1,
          31_536_000,
        );
  const approvalAllowSelfApproval = record.approval_allow_self_approval;
  if (
    approvalAllowSelfApproval !== null &&
    typeof approvalAllowSelfApproval !== "boolean"
  ) {
    fail(`${label}.approval_allow_self_approval is invalid`);
  }
  const effect = record.effect;
  if (effect !== "read" && effect !== "write") {
    fail(`${label}.effect is unsupported`);
  }
  const idempotencySupport = record.idempotency_support;
  if (
    idempotencySupport !== "not_applicable" &&
    idempotencySupport !== "required" &&
    idempotencySupport !== "supported" &&
    idempotencySupport !== "unavailable"
  ) {
    fail(`${label}.idempotency_support is unsupported`);
  }
  if (
    (effect === "read" && idempotencySupport !== "not_applicable") ||
    (effect === "write" && idempotencySupport !== "required")
  ) {
    fail(`${label} idempotency contract is incoherent`);
  }
  const nonConnector =
    connectorFamily === "model" || connectorFamily === "artifact";
  const expectedAttemptKind =
    connectorFamily === "model"
      ? "model"
      : connectorFamily === "artifact"
        ? "no_call"
        : "tool";
  if (runtimePolicy.attemptKind !== expectedAttemptKind) {
    fail(`${label} runtime attempt kind differs from its connector family`);
  }
  if (nonConnector) {
    if (
      bindingId !== null ||
      bindingConfigurationRevision !== null ||
      timeoutSeconds !== null ||
      dataClassification !== "internal"
    ) {
      fail(`${label} non-connector contract snapshot is incoherent`);
    }
    if (
      (connectorFamily === "model" &&
        (requestSchemaId === null || resultSchemaId === null)) ||
      (connectorFamily === "artifact" &&
        (requestSchemaId !== null || resultSchemaId !== null))
    ) {
      fail(`${label} non-connector schema snapshot is incoherent`);
    }
  } else if (
    bindingId === null ||
    bindingConfigurationRevision === null ||
    timeoutSeconds === null ||
    requestSchemaId === null ||
    resultSchemaId === null
  ) {
    fail(`${label} external connector contract snapshot is incomplete`);
  }
  if (effect === "read") {
    if (
      state === "awaiting_approval" ||
      state === "rejected" ||
      approvalRequiredRoles.length > 0 ||
      approvalRequiredScopes.length > 0 ||
      approvalExpiresAfterSeconds !== null ||
      approvalAllowSelfApproval !== null
    ) {
      fail(`${label} READ approval authority is incoherent`);
    }
  } else if (
    nonConnector ||
    approvalRequiredRoles.length === 0 ||
    approvalRequiredScopes.length === 0 ||
    approvalExpiresAfterSeconds === null ||
    approvalAllowSelfApproval === null ||
    requestSchemaId === null ||
    resultSchemaId === null
  ) {
    fail(`${label} WRITE approval snapshot is incomplete`);
  }
  const terminalReasonCode = asNullableAuthority(
    record.terminal_reason_code,
    `${label}.terminal_reason_code`,
  );
  if (TERMINAL_STEP_STATE_SET.has(state) !== (terminalReasonCode !== null)) {
    fail(`${label} terminal state and reason are incoherent`);
  }
  if (
    TERMINAL_STEP_STATE_SET.has(state) &&
    transitions.length > 0 &&
    transitions.at(-1)?.reasonCode !== terminalReasonCode
  ) {
    fail(`${label} terminal reason differs from its terminal transition`);
  }
  return Object.freeze({
    id,
    runId,
    key: asAuthority(record.key, `${label}.key`),
    kind: asAuthority(record.kind, `${label}.kind`),
    selectedInstanceId,
    templateId,
    dependencyKeys: asUniqueStrings(
      record.dependency_keys,
      `${label}.dependency_keys`,
      20,
    ),
    capabilityId: asAuthority(record.capability_id, `${label}.capability_id`),
    effect,
    state,
    ordinal: asInteger(record.ordinal, `${label}.ordinal`, 1, 20),
    sourceOrder: asInteger(record.source_order, `${label}.source_order`, 1),
    configurationRevision,
    connectorFamily,
    routingSlotKey,
    bindingId,
    bindingConfigurationRevision,
    requestSchemaId,
    resultSchemaId,
    resultSchemaHash,
    dataClassification,
    idempotencySupport,
    timeoutSeconds,
    runtimePolicy,
    approvalPolicyId: asResourceId(
      record.approval_policy_id,
      `${label}.approval_policy_id`,
    ),
    approvalRequiredRoles,
    approvalRequiredScopes,
    approvalExpiresAfterSeconds,
    approvalAllowSelfApproval,
    terminalResult: asBoolean(
      record.terminal_result,
      `${label}.terminal_result`,
    ),
    createdAt,
    updatedAt,
    version,
    terminalReasonCode,
    transitions: Object.freeze(transitions),
    stepUrl: asExactUrl(
      record.step_url,
      stepUrl(runId, id),
      `${label}.step_url`,
    ),
    runUrl: asExactUrl(record.run_url, runUrl(runId), `${label}.run_url`),
    instanceUrl: asExactUrl(
      record.instance_url,
      instanceUrl(selectedInstanceId),
      `${label}.instance_url`,
    ),
    templateUrl: asExactUrl(
      record.template_url,
      templateUrl(templateId),
      `${label}.template_url`,
    ),
  });
}

function normalizeSelectedInstance(
  value: unknown,
  label: string,
): RunSelectedInstance {
  const record = asRecord(value, label);
  assertExactFields(record, SELECTED_INSTANCE_FIELDS, label);
  const instanceId = asResourceId(record.instance_id, `${label}.instance_id`);
  const templateId = asResourceId(record.template_id, `${label}.template_id`);
  return Object.freeze({
    instanceId,
    templateId,
    configurationRevision: asInteger(
      record.configuration_revision,
      `${label}.configuration_revision`,
      1,
    ),
    displayOrder: asInteger(record.display_order, `${label}.display_order`, 1),
    sourceOrdinal:
      record.source_ordinal === null
        ? null
        : asInteger(record.source_ordinal, `${label}.source_ordinal`, 1),
    selectionOrder: asInteger(
      record.selection_order,
      `${label}.selection_order`,
      1,
    ),
    target: asBoolean(record.target, `${label}.target`),
    instanceUrl: asExactUrl(
      record.instance_url,
      instanceUrl(instanceId),
      `${label}.instance_url`,
    ),
    templateUrl: asExactUrl(
      record.template_url,
      templateUrl(templateId),
      `${label}.template_url`,
    ),
  });
}

function normalizeRoutingAssignment(
  value: unknown,
  label: string,
): RunRoutingAssignment {
  const record = asRecord(value, label);
  assertExactFields(record, ROUTING_ASSIGNMENT_FIELDS, label);
  const instanceId = asResourceId(record.instance_id, `${label}.instance_id`);
  const templateId = asResourceId(record.template_id, `${label}.template_id`);
  return Object.freeze({
    slotKey: asAuthority(record.slot_key, `${label}.slot_key`),
    instanceId,
    templateId,
    requiredCapabilityIds: asUniqueStrings(
      record.required_capability_ids,
      `${label}.required_capability_ids`,
      100,
    ),
    assignmentOrder: asInteger(
      record.assignment_order,
      `${label}.assignment_order`,
      1,
    ),
    instanceUrl: asExactUrl(
      record.instance_url,
      instanceUrl(instanceId),
      `${label}.instance_url`,
    ),
    templateUrl: asExactUrl(
      record.template_url,
      templateUrl(templateId),
      `${label}.template_url`,
    ),
  });
}

function normalizeRunPlan(
  value: unknown,
  runId: string,
  expectedWorkflowId: string,
): RunPlan {
  const label = "run.plan";
  const record = asRecord(value, label);
  assertExactFields(record, PLAN_FIELDS, label);
  const workflowId = asResourceId(record.workflow_id, `${label}.workflow_id`);
  if (workflowId !== expectedWorkflowId) {
    fail(`${label}.workflow_id does not match its run`);
  }
  const selectedInstances = asArray(
    record.selected_instances,
    `${label}.selected_instances`,
    100,
  ).map((item, index) =>
    normalizeSelectedInstance(
      item,
      `${label}.selected_instances[${String(index)}]`,
    ),
  );
  if (
    selectedInstances.length === 0 ||
    selectedInstances.some(
      ({ selectionOrder }, index) => selectionOrder !== index + 1,
    ) ||
    new Set(selectedInstances.map(({ instanceId }) => instanceId)).size !==
      selectedInstances.length
  ) {
    fail(`${label}.selected_instances are incoherent`);
  }
  const routingAssignments = asArray(
    record.routing_assignments,
    `${label}.routing_assignments`,
    100,
  ).map((item, index) =>
    normalizeRoutingAssignment(
      item,
      `${label}.routing_assignments[${String(index)}]`,
    ),
  );
  if (
    routingAssignments.some(
      ({ assignmentOrder }, index) => assignmentOrder !== index + 1,
    )
  ) {
    fail(`${label}.routing_assignments are incoherent`);
  }
  const steps = asArray(record.steps, `${label}.steps`, 20).map((item, index) =>
    normalizeRunStep(item, runId, `${label}.steps[${String(index)}]`),
  );
  const stepCount = asInteger(record.step_count, `${label}.step_count`, 1, 20);
  if (
    steps.length !== stepCount ||
    steps.some(({ ordinal }, index) => ordinal !== index + 1) ||
    new Set(steps.map(({ id }) => id)).size !== steps.length
  ) {
    fail(`${label}.steps are incoherent`);
  }
  const selectedById = new Map(
    selectedInstances.map((selected) => [selected.instanceId, selected]),
  );
  const targets = selectedInstances.filter(({ target }) => target);
  const assignmentBySlot = new Map(
    routingAssignments.map((assignment) => [assignment.slotKey, assignment]),
  );
  const consumedRoutingSlots = new Set(
    steps.flatMap(({ routingSlotKey }) =>
      routingSlotKey === null ? [] : [routingSlotKey],
    ),
  );
  const expectedSelectedIds = new Set([
    ...targets.map(({ instanceId }) => instanceId),
    ...routingAssignments.map(({ instanceId }) => instanceId),
    ...steps.map(({ selectedInstanceId }) => selectedInstanceId),
  ]);
  if (
    targets.length !== 1 ||
    expectedSelectedIds.size !== selectedById.size ||
    [...selectedById.keys()].some((id) => !expectedSelectedIds.has(id))
  ) {
    fail(`${label} selected instance snapshot is incoherent`);
  }
  if (
    assignmentBySlot.size !== routingAssignments.length ||
    assignmentBySlot.size !== consumedRoutingSlots.size ||
    [...assignmentBySlot.keys()].some(
      (slotKey) => !consumedRoutingSlots.has(slotKey),
    ) ||
    routingAssignments.some((assignment) => {
      const selected = selectedById.get(assignment.instanceId);
      return selected?.templateId !== assignment.templateId;
    }) ||
    steps.some((step) => {
      const selected = selectedById.get(step.selectedInstanceId);
      if (
        selected?.templateId !== step.templateId ||
        selected.configurationRevision !== step.configurationRevision
      ) {
        return true;
      }
      if (step.routingSlotKey === null) {
        return step.selectedInstanceId !== targets[0]?.instanceId;
      }
      const assignment = assignmentBySlot.get(step.routingSlotKey);
      return (
        assignment?.instanceId !== step.selectedInstanceId ||
        assignment.templateId !== step.templateId ||
        (assignment.requiredCapabilityIds.length > 0 &&
          !assignment.requiredCapabilityIds.includes(step.capabilityId))
      );
    })
  ) {
    fail(`${label} routing snapshot does not bind its sealed steps`);
  }
  return Object.freeze({
    planHash: asPattern(
      record.plan_hash,
      HEX_DIGEST_PATTERN,
      `${label}.plan_hash`,
    ),
    workflowId,
    workflowVersion: asInteger(
      record.workflow_version,
      `${label}.workflow_version`,
      1,
    ),
    workflowDefinitionHash: asPattern(
      record.workflow_definition_hash,
      HEX_DIGEST_PATTERN,
      `${label}.workflow_definition_hash`,
    ),
    catalogContentHash: asPattern(
      record.catalog_content_hash,
      CATALOG_CONTENT_HASH_PATTERN,
      `${label}.catalog_content_hash`,
    ),
    graphHash: asPattern(
      record.graph_hash,
      HEX_DIGEST_PATTERN,
      `${label}.graph_hash`,
    ),
    routingHash: asPattern(
      record.routing_hash,
      HEX_DIGEST_PATTERN,
      `${label}.routing_hash`,
    ),
    approvalRequired: asBoolean(
      record.approval_required,
      `${label}.approval_required`,
    ),
    stepCount,
    runtimePolicy: normalizeRunRuntimePolicy(
      record.runtime_policy,
      `${label}.runtime_policy`,
    ),
    createdAt: asTimestamp(record.created_at, `${label}.created_at`),
    selectedInstances: Object.freeze(selectedInstances),
    routingAssignments: Object.freeze(routingAssignments),
    steps: Object.freeze(steps),
  });
}

function normalizeExecutionControl(value: unknown): RunExecutionControl {
  const label = "run.execution_control";
  const record = asRecord(value, label);
  assertExactFields(record, EXECUTION_CONTROL_FIELDS, label);
  const maxModelCalls = asInteger(
    record.max_model_calls,
    `${label}.max_model_calls`,
    0,
  );
  const maxToolCalls = asInteger(
    record.max_tool_calls,
    `${label}.max_tool_calls`,
    0,
  );
  const modelCalls = asInteger(record.model_calls, `${label}.model_calls`, 0);
  const toolCalls = asInteger(record.tool_calls, `${label}.tool_calls`, 0);
  const remainingModelCalls = asInteger(
    record.remaining_model_calls,
    `${label}.remaining_model_calls`,
    0,
  );
  const remainingToolCalls = asInteger(
    record.remaining_tool_calls,
    `${label}.remaining_tool_calls`,
    0,
  );
  if (
    modelCalls + remainingModelCalls !== maxModelCalls ||
    toolCalls + remainingToolCalls !== maxToolCalls
  ) {
    fail(`${label} call budgets are incoherent`);
  }
  const startedAt = asNullableTimestamp(
    record.started_at,
    `${label}.started_at`,
  );
  const deadlineAt = asNullableTimestamp(
    record.deadline_at,
    `${label}.deadline_at`,
  );
  if ((startedAt === null) !== (deadlineAt === null)) {
    fail(`${label} start/deadline fields are incoherent`);
  }
  const createdAt = asTimestamp(record.created_at, `${label}.created_at`);
  const updatedAt = asTimestamp(record.updated_at, `${label}.updated_at`);
  if (compareRunTimestamps(updatedAt, createdAt) < 0) {
    fail(`${label}.updated_at precedes creation`);
  }
  if (startedAt !== null && compareRunTimestamps(startedAt, createdAt) < 0) {
    fail(`${label}.started_at precedes creation`);
  }
  if (
    startedAt !== null &&
    deadlineAt !== null &&
    compareRunTimestamps(deadlineAt, startedAt) <= 0
  ) {
    fail(`${label}.deadline_at must follow start`);
  }
  return Object.freeze({
    runTimeoutSeconds: asInteger(
      record.run_timeout_seconds,
      `${label}.run_timeout_seconds`,
      1,
      3_600,
    ),
    maxModelCalls,
    maxToolCalls,
    modelCalls,
    toolCalls,
    remainingModelCalls,
    remainingToolCalls,
    startedAt,
    deadlineAt,
    cancelRequestedAt: asNullableTimestamp(
      record.cancel_requested_at,
      `${label}.cancel_requested_at`,
    ),
    createdAt,
    updatedAt,
    version: asInteger(record.version, `${label}.version`, 1),
  });
}

function normalizePendingApproval(
  value: unknown,
  runId: string,
  label: string,
): PendingApprovalSummary {
  const record = asRecord(value, label);
  assertExactFields(record, PENDING_APPROVAL_FIELDS, label);
  if (record.status !== "pending") fail(`${label}.status is unsupported`);
  const id = asResourceId(record.id, `${label}.id`);
  const actionId = asResourceId(record.action_id, `${label}.action_id`);
  const stepId = asResourceId(record.step_id, `${label}.step_id`);
  const requestedAt = asTimestamp(record.requested_at, `${label}.requested_at`);
  const expiresAt = asTimestamp(record.expires_at, `${label}.expires_at`);
  if (compareRunTimestamps(expiresAt, requestedAt) <= 0) {
    fail(`${label}.expires_at must follow request`);
  }
  return Object.freeze({
    id,
    actionId,
    stepId,
    status: "pending",
    destinationSummary: asBoundedString(
      record.destination_summary,
      `${label}.destination_summary`,
    ),
    requestedAt,
    expiresAt,
    isExpired: asBoolean(record.is_expired, `${label}.is_expired`),
    approvalUrl: asExactUrl(
      record.approval_url,
      approvalUrl(id),
      `${label}.approval_url`,
    ),
    actionUrl: asExactUrl(
      record.action_url,
      actionUrl(actionId),
      `${label}.action_url`,
    ),
    stepUrl: asExactUrl(
      record.step_url,
      stepUrl(runId, stepId),
      `${label}.step_url`,
    ),
  });
}

function normalizeTerminalError(
  value: unknown,
  runId: string,
): RunTerminalError {
  const label = "run.terminal_error";
  const record = asRecord(value, label);
  assertExactFields(record, TERMINAL_ERROR_FIELDS, label);
  const source = record.source;
  if (
    source !== "run" &&
    source !== "step" &&
    source !== "read_attempt" &&
    source !== "external_action"
  ) {
    fail(`${label}.source is unsupported`);
  }
  const stepId = asNullableResourceId(record.step_id, `${label}.step_id`);
  const actionId = asNullableResourceId(record.action_id, `${label}.action_id`);
  if (
    (source === "run" && (stepId !== null || actionId !== null)) ||
    ((source === "step" || source === "read_attempt") &&
      (stepId === null || actionId !== null)) ||
    (source === "external_action" && (stepId === null || actionId === null))
  ) {
    fail(`${label} source binding is incoherent`);
  }
  if (record.retryable !== false) fail(`${label}.retryable must be false`);
  return Object.freeze({
    code: asPattern(record.code, ERROR_CODE_PATTERN, `${label}.code`),
    causeCode:
      record.cause_code === null
        ? null
        : asPattern(
            record.cause_code,
            ERROR_CODE_PATTERN,
            `${label}.cause_code`,
          ),
    source,
    stepId,
    actionId,
    outcome: asNullableAuthority(record.outcome, `${label}.outcome`),
    finalAttemptNumber:
      record.final_attempt_number === null
        ? null
        : asInteger(
            record.final_attempt_number,
            `${label}.final_attempt_number`,
            1,
            10,
          ),
    retryable: false,
    callDeadlineAt: asNullableTimestamp(
      record.call_deadline_at,
      `${label}.call_deadline_at`,
    ),
    runDeadlineAt: asNullableTimestamp(
      record.run_deadline_at,
      `${label}.run_deadline_at`,
    ),
    occurredAt: asTimestamp(record.occurred_at, `${label}.occurred_at`),
    stepUrl: asNullableExactUrl(
      record.step_url,
      stepId === null ? null : stepUrl(runId, stepId),
      `${label}.step_url`,
    ),
    actionUrl: asNullableExactUrl(
      record.action_url,
      actionId === null ? null : actionUrl(actionId),
      `${label}.action_url`,
    ),
  });
}

export function normalizeRunResource(
  value: unknown,
  expectedRunId?: string,
): RunResource {
  const label = "run";
  const record = asRecord(value, label);
  assertExactFields(record, RUN_RESOURCE_FIELDS, label);
  const summary = normalizeRunSummary(
    Object.fromEntries(
      Object.entries(record).filter(([key]) => RUN_SUMMARY_FIELDS.has(key)),
    ),
    label,
  );
  if (
    expectedRunId !== undefined &&
    summary.id !== asResourceId(expectedRunId, "run ID")
  ) {
    fail("run does not match its request");
  }
  const transitions = asArray(record.transitions, "run.transitions", 64).map(
    (item, index) =>
      normalizeRunTransition(item, `run.transitions[${String(index)}]`),
  );
  if (
    transitions.length === 0 ||
    transitions.some(({ sequence }, index) => sequence !== index + 1) ||
    transitions.at(-1)?.newState !== summary.state ||
    transitions.at(-1)?.resultingVersion !== summary.version
  ) {
    fail("run.transitions are incoherent");
  }
  if (
    TERMINAL_RUN_STATE_SET.has(summary.state) &&
    transitions.at(-1)?.reasonCode !== summary.terminalReasonCode
  ) {
    fail("run terminal reason differs from its terminal transition");
  }
  const plan =
    record.plan === null
      ? null
      : normalizeRunPlan(record.plan, summary.id, summary.workflowId);
  if (
    plan !== null &&
    summary.approvalRequired !== null &&
    plan.approvalRequired !== summary.approvalRequired
  ) {
    fail("run.plan approval requirement does not match its run");
  }
  const executionControl =
    record.execution_control === null
      ? null
      : normalizeExecutionControl(record.execution_control);
  const pendingApprovals = asArray(
    record.pending_approvals,
    "run.pending_approvals",
    100,
  ).map((item, index) =>
    normalizePendingApproval(
      item,
      summary.id,
      `run.pending_approvals[${String(index)}]`,
    ),
  );
  const artifactSummaries = asArray(
    record.artifact_summaries,
    "run.artifact_summaries",
    10,
  ).map((item, index) =>
    normalizeArtifactSummary(item, `run.artifact_summaries[${String(index)}]`),
  );
  const externalActions = asArray(
    record.external_actions,
    "run.external_actions",
    100,
  ).map((item, index) => {
    try {
      return normalizeExternalAction(item, undefined, summary.id);
    } catch (error) {
      if (error instanceof RunArtifactsContractError) {
        throw new RunArtifactsContractError(
          `run.external_actions[${String(index)}] is invalid`,
        );
      }
      throw error;
    }
  });
  const uniqueSets: readonly (readonly string[])[] = [
    pendingApprovals.map(({ id }) => id),
    artifactSummaries.map(({ id }) => id),
    externalActions.map(({ id }) => id),
  ];
  if (uniqueSets.some((ids) => new Set(ids).size !== ids.length)) {
    fail("run child resource IDs must be unique");
  }
  if (artifactSummaries.some(({ runId }) => runId !== summary.id)) {
    fail("run artifact summary does not match its run");
  }
  const planStepsById = new Map(
    (plan?.steps ?? []).map((step) => [step.id, step] as const),
  );
  for (const action of externalActions) {
    const step = planStepsById.get(action.stepId);
    if (!externalActionBindsStep(action, step)) {
      fail("run external action does not bind its sealed WRITE step");
    }
  }
  const actionsById = new Map(
    externalActions.map((action) => [action.id, action] as const),
  );
  if (
    pendingApprovals.some(({ actionId, stepId }) => {
      return actionsById.get(actionId)?.stepId !== stepId;
    })
  ) {
    fail("run pending approval does not bind its external action step");
  }
  const terminalError =
    record.terminal_error === null
      ? null
      : normalizeTerminalError(record.terminal_error, summary.id);
  return Object.freeze({
    ...summary,
    transitions: Object.freeze(transitions),
    plan,
    executionControl,
    pendingApprovals: Object.freeze(pendingApprovals),
    artifactSummaries: Object.freeze(artifactSummaries),
    artifactsTruncated: asBoolean(
      record.artifacts_truncated,
      "run.artifacts_truncated",
    ),
    externalActions: Object.freeze(externalActions),
    terminalError,
  });
}

function assertQueryFields(
  value: object,
  allowed: ReadonlySet<string>,
  label: string,
): Record<string, unknown> {
  const record = asRecord(value, label);
  if (
    Object.keys(record).some(
      (key) => !allowed.has(key) || UNSAFE_OBJECT_KEYS.has(key),
    )
  ) {
    fail(`${label} fields are unsupported`);
  }
  return record;
}

const RUN_QUERY_FIELDS = new Set([
  "state",
  "instanceId",
  "workflowId",
  "createdAtFrom",
  "createdAtTo",
  "cursor",
  "limit",
]);
const PAGE_QUERY_FIELDS = new Set(["cursor", "limit"]);

function normalizeRunListQuery(
  query: RunListQuery = {},
): NormalizedRunListQuery {
  const record = assertQueryFields(query, RUN_QUERY_FIELDS, "run list query");
  const state =
    record.state === undefined
      ? null
      : asEnum(record.state, RUN_STATE_SET, "run list state");
  const instanceId =
    record.instanceId === undefined
      ? null
      : asResourceId(record.instanceId, "run list instance ID");
  const workflowId =
    record.workflowId === undefined
      ? null
      : asResourceId(record.workflowId, "run list workflow ID");
  const createdAtFrom =
    record.createdAtFrom === undefined
      ? null
      : asTimestamp(record.createdAtFrom, "run list lower time bound");
  const createdAtTo =
    record.createdAtTo === undefined
      ? null
      : asTimestamp(record.createdAtTo, "run list upper time bound");
  if (
    createdAtFrom !== null &&
    createdAtTo !== null &&
    compareRunTimestamps(createdAtFrom, createdAtTo) > 0
  ) {
    fail("run list lower time bound follows upper bound");
  }
  const cursor = record.cursor ?? null;
  if (
    cursor !== null &&
    (typeof cursor !== "string" ||
      cursor.length > MAX_CURSOR_LENGTH ||
      !RUN_CURSOR_PATTERN.test(cursor))
  ) {
    fail("run list cursor is invalid");
  }
  return Object.freeze({
    state,
    instanceId,
    workflowId,
    createdAtFrom,
    createdAtTo,
    cursor,
    limit: asInteger(
      record.limit ?? DEFAULT_RUN_PAGE_SIZE,
      "run list limit",
      1,
      MAX_PAGE_SIZE,
    ),
  });
}

function normalizePageQuery(
  query: PageQuery,
  kind: "timeline" | "artifacts",
): NormalizedPageQuery {
  const label =
    kind === "timeline" ? "run timeline query" : "run artifacts query";
  const record = assertQueryFields(query, PAGE_QUERY_FIELDS, label);
  const cursor = record.cursor ?? null;
  const pattern =
    kind === "timeline" ? TIMELINE_CURSOR_PATTERN : ARTIFACT_CURSOR_PATTERN;
  if (
    cursor !== null &&
    (typeof cursor !== "string" ||
      cursor.length > MAX_CURSOR_LENGTH ||
      !pattern.test(cursor))
  ) {
    fail(`${label} cursor is invalid`);
  }
  return Object.freeze({
    cursor,
    limit: asInteger(
      record.limit ??
        (kind === "timeline"
          ? DEFAULT_TIMELINE_PAGE_SIZE
          : DEFAULT_ARTIFACT_PAGE_SIZE),
      `${label} limit`,
      1,
      MAX_PAGE_SIZE,
    ),
  });
}

function normalizeNextCursor(
  value: unknown,
  pattern: RegExp,
  itemCount: number,
  limit: number,
  label: string,
): string | null {
  if (value === null) return null;
  if (
    typeof value !== "string" ||
    value.length > MAX_CURSOR_LENGTH ||
    !pattern.test(value) ||
    itemCount === 0 ||
    itemCount > limit
  ) {
    return fail(`${label} cursor is invalid`);
  }
  return value;
}

export function normalizeRunPage(
  value: unknown,
  query: RunListQuery = {},
): RunPage {
  const expected = normalizeRunListQuery(query);
  const record = asRecord(value, "run page");
  assertExactFields(record, PAGE_FIELDS, "run page");
  const items = asArray(record.items, "run page.items", expected.limit).map(
    (item, index) =>
      normalizeRunSummary(item, `run page.items[${String(index)}]`),
  );
  if (new Set(items.map(({ id }) => id)).size !== items.length) {
    fail("run page IDs must be unique");
  }
  if (
    items.some(
      (item) =>
        (expected.state !== null && item.state !== expected.state) ||
        (expected.instanceId !== null &&
          item.instanceId !== expected.instanceId) ||
        (expected.workflowId !== null &&
          item.workflowId !== expected.workflowId) ||
        (expected.createdAtFrom !== null &&
          compareRunTimestamps(item.createdAt, expected.createdAtFrom) < 0) ||
        (expected.createdAtTo !== null &&
          compareRunTimestamps(item.createdAt, expected.createdAtTo) > 0),
    )
  ) {
    fail("run page does not match its filters");
  }
  for (let index = 1; index < items.length; index += 1) {
    const previous = items[index - 1];
    const current = items[index];
    if (previous === undefined || current === undefined) continue;
    const order = compareRunTimestamps(previous.createdAt, current.createdAt);
    if (order < 0 || (order === 0 && previous.id <= current.id)) {
      fail("run page must use descending keyset order");
    }
  }
  return Object.freeze({
    items: Object.freeze(items),
    nextCursor: normalizeNextCursor(
      record.next_cursor,
      RUN_CURSOR_PATTERN,
      items.length,
      expected.limit,
      "run page",
    ),
  });
}

export function normalizeRunTimelinePage(
  value: unknown,
  expectedRunId: string,
  query: PageQuery = {},
): RunTimelinePage {
  const runId = asResourceId(expectedRunId, "run timeline run ID");
  const expected = normalizePageQuery(query, "timeline");
  const record = asRecord(value, "run timeline page");
  assertExactFields(record, BOUND_PAGE_FIELDS, "run timeline page");
  if (record.run_id !== runId) fail("run timeline page does not match its run");
  const items = asArray(
    record.items,
    "run timeline page.items",
    expected.limit,
  ).map((item, index) =>
    normalizeTimelineEvent(
      item,
      runId,
      `run timeline page.items[${String(index)}]`,
    ),
  );
  if (
    new Set(items.map(({ id }) => id)).size !== items.length ||
    new Set(items.map(({ sequence }) => sequence)).size !== items.length
  ) {
    fail("run timeline identities and sequences must be unique");
  }
  if (
    items.some(
      (item, index) =>
        index > 0 &&
        (items[index - 1]?.sequence ?? item.sequence) >= item.sequence,
    )
  ) {
    fail("run timeline must use ascending sequence order");
  }
  return Object.freeze({
    runId,
    items: Object.freeze(items),
    nextCursor: normalizeNextCursor(
      record.next_cursor,
      TIMELINE_CURSOR_PATTERN,
      items.length,
      expected.limit,
      "run timeline page",
    ),
  });
}

export function normalizeRunArtifactsPage(
  value: unknown,
  expectedRunId: string,
  query: PageQuery = {},
): ArtifactPage {
  const runId = asResourceId(expectedRunId, "run artifacts run ID");
  const expected = normalizePageQuery(query, "artifacts");
  const record = asRecord(value, "run artifacts page");
  assertExactFields(record, BOUND_PAGE_FIELDS, "run artifacts page");
  if (record.run_id !== runId)
    fail("run artifacts page does not match its run");
  const items = asArray(
    record.items,
    "run artifacts page.items",
    expected.limit,
  ).map((item, index) =>
    normalizeArtifactSummary(
      item,
      `run artifacts page.items[${String(index)}]`,
    ),
  );
  if (
    new Set(items.map(({ id }) => id)).size !== items.length ||
    items.some(({ runId: itemRunId }) => itemRunId !== runId)
  ) {
    fail("run artifacts page resource binding is invalid");
  }
  for (let index = 1; index < items.length; index += 1) {
    const previous = items[index - 1];
    const current = items[index];
    if (previous === undefined || current === undefined) continue;
    const order = compareRunTimestamps(previous.createdAt, current.createdAt);
    if (order > 0 || (order === 0 && previous.id >= current.id)) {
      fail("run artifacts page must use ascending keyset order");
    }
  }
  return Object.freeze({
    runId,
    items: Object.freeze(items),
    nextCursor: normalizeNextCursor(
      record.next_cursor,
      ARTIFACT_CURSOR_PATTERN,
      items.length,
      expected.limit,
      "run artifacts page",
    ),
  });
}

export function runListQueryKey(
  query: RunListQuery = {},
): readonly ["runs", "list", NormalizedRunListQuery] {
  return Object.freeze([
    RUNS_QUERY_ROOT[0],
    "list",
    normalizeRunListQuery(query),
  ] as const);
}

export function runResourceQueryKey(
  runId: string,
): readonly ["runs", "detail", string] {
  return Object.freeze([
    RUNS_QUERY_ROOT[0],
    "detail",
    asResourceId(runId, "run ID"),
  ] as const);
}

export function runTimelineQueryKey(
  runId: string,
  query: PageQuery = {},
): readonly ["runs", "timeline", string, NormalizedPageQuery] {
  return Object.freeze([
    RUNS_QUERY_ROOT[0],
    "timeline",
    asResourceId(runId, "run ID"),
    normalizePageQuery(query, "timeline"),
  ] as const);
}

export function runArtifactsQueryKey(
  runId: string,
  query: PageQuery = {},
): readonly ["runs", "artifacts", string, NormalizedPageQuery] {
  return Object.freeze([
    RUNS_QUERY_ROOT[0],
    "artifacts",
    asResourceId(runId, "run ID"),
    normalizePageQuery(query, "artifacts"),
  ] as const);
}

export function artifactResourceQueryKey(
  artifactId: string,
): readonly ["artifacts", "detail", string] {
  return Object.freeze([
    ARTIFACTS_QUERY_ROOT[0],
    "detail",
    asResourceId(artifactId, "artifact ID"),
  ] as const);
}

export function externalActionQueryKey(
  actionId: string,
): readonly ["external-actions", "detail", string] {
  return Object.freeze([
    "external-actions",
    "detail",
    asResourceId(actionId, "external action ID"),
  ] as const);
}

function runListPath(query: NormalizedRunListQuery): string {
  const parameters = new URLSearchParams();
  if (query.state !== null) parameters.set("state", query.state);
  if (query.instanceId !== null)
    parameters.set("instance_id", query.instanceId);
  if (query.workflowId !== null)
    parameters.set("workflow_id", query.workflowId);
  if (query.createdAtFrom !== null) {
    parameters.set("created_at_from", query.createdAtFrom);
  }
  if (query.createdAtTo !== null)
    parameters.set("created_at_to", query.createdAtTo);
  if (query.cursor !== null) parameters.set("cursor", query.cursor);
  parameters.set("limit", String(query.limit));
  return `/api/v1/runs?${parameters.toString()}`;
}

function pagePath(base: string, query: NormalizedPageQuery): string {
  const parameters = new URLSearchParams();
  if (query.cursor !== null) parameters.set("cursor", query.cursor);
  parameters.set("limit", String(query.limit));
  return `${base}?${parameters.toString()}`;
}

type RequestKind =
  | "run list"
  | "run"
  | "timeline"
  | "artifact list"
  | "artifact"
  | "external action";

function requestError(
  error: ApiRequestError,
  kind: RequestKind,
): RunArtifactsRequestError {
  const noun =
    kind === "run list"
      ? "run list"
      : kind === "timeline"
        ? "run timeline"
        : kind === "artifact list"
          ? "run artifacts"
          : kind;
  const message =
    error.status === 0
      ? "The local API is not ready. Start it and try again."
      : error.status === 403
        ? `This local session cannot read the ${noun}.`
        : error.status === 404
          ? `The requested ${noun} was not found.`
          : error.status === 503
            ? `The ${noun} is temporarily unavailable.`
            : `The local API could not load the ${noun}.`;
  const safeCode =
    error.status === 0
      ? "api_unreachable"
      : error.status === 403
        ? "runtime_read_forbidden"
        : error.status === 404
          ? "resource_not_found"
          : error.status === 503
            ? "resource_temporarily_unavailable"
            : "resource_read_failed";
  return new RunArtifactsRequestError(error.status, safeCode, message);
}

async function getResource(
  path: string,
  signal: AbortSignal | undefined,
  kind: RequestKind,
) {
  try {
    return await getJson(path, signal);
  } catch (error) {
    if (!(error instanceof ApiRequestError)) throw error;
    throw requestError(error, kind);
  }
}

export async function fetchRunPage(
  query: RunListQuery = {},
  signal?: AbortSignal,
): Promise<RunPage> {
  const normalized = normalizeRunListQuery(query);
  const value = await getResource(runListPath(normalized), signal, "run list");
  return normalizeRunPage(value, query);
}

export async function fetchRunResource(
  runId: string,
  signal?: AbortSignal,
): Promise<RunResource> {
  const id = asResourceId(runId, "run ID");
  const value = await getResource(runUrl(id), signal, "run");
  return normalizeRunResource(value, id);
}

export async function fetchRunTimelinePage(
  runId: string,
  query: PageQuery = {},
  signal?: AbortSignal,
): Promise<RunTimelinePage> {
  const id = asResourceId(runId, "run timeline run ID");
  const normalized = normalizePageQuery(query, "timeline");
  const value = await getResource(
    pagePath(timelineUrl(id), normalized),
    signal,
    "timeline",
  );
  return normalizeRunTimelinePage(value, id, query);
}

export async function fetchRunArtifactsPage(
  runId: string,
  query: PageQuery = {},
  signal?: AbortSignal,
): Promise<ArtifactPage> {
  const id = asResourceId(runId, "run artifacts run ID");
  const normalized = normalizePageQuery(query, "artifacts");
  const value = await getResource(
    pagePath(artifactsUrl(id), normalized),
    signal,
    "artifact list",
  );
  return normalizeRunArtifactsPage(value, id, query);
}

export async function fetchArtifactResource(
  artifactId: string,
  signal?: AbortSignal,
): Promise<ArtifactResource> {
  const id = asResourceId(artifactId, "artifact ID");
  const value = await getResource(artifactUrl(id), signal, "artifact");
  return normalizeArtifactResource(value, id);
}

export async function fetchExternalAction(
  actionId: string,
  signal?: AbortSignal,
): Promise<ExternalAction> {
  const id = asResourceId(actionId, "external action ID");
  const value = await getResource(actionUrl(id), signal, "external action");
  return normalizeExternalAction(value, id);
}

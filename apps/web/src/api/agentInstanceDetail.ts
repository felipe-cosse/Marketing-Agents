import { ApiRequestError } from "./client";

const DETAIL_ROOT_FIELDS = new Set([
  "catalogVersion",
  "catalogHash",
  "instance",
  "template",
  "sharedTemplateDeploymentCount",
  "capabilities",
  "approvalPolicy",
  "inputSchema",
  "outputSchema",
  "templateSourceReferences",
  "templateImplementationNotes",
  "configurationSchema",
]);
const RUNTIME_ROOT_FIELDS = new Set([
  ...DETAIL_ROOT_FIELDS,
  "runtimeWatermark",
  "runtimeStatus",
  "recentRuns",
]);
const INSTANCE_FIELDS = new Set([
  "id",
  "templateId",
  "displayOrder",
  "enabled",
  "sourceOrdinal",
  "variantLabel",
  "triggerBindings",
  "connectorBindings",
  "schedule",
  "configurationRevision",
  "configurationEtag",
]);
const TEMPLATE_FIELDS = new Set([
  "id",
  "displayName",
  "departmentId",
  "functionId",
  "displayOrder",
  "purpose",
  "inputSchemaId",
  "outputSchemaId",
  "allowedToolCapabilityIds",
  "supportedTriggerTypes",
  "operationClassification",
  "outputHandling",
  "approvalPolicyId",
  "retryPolicy",
  "timeoutPolicy",
  "budgetPolicy",
  "rateLimitPolicy",
  "sourceConfidence",
  "sourceReferences",
  "implementationNotes",
]);
const CAPABILITY_FIELDS = new Set([
  "id",
  "displayName",
  "description",
  "effect",
  "connectorFamily",
  "idempotencySupport",
  "defaultTimeoutSeconds",
  "dataClassification",
]);
const APPROVAL_POLICY_FIELDS = new Set([
  "id",
  "kind",
  "requiredRoles",
  "expirySeconds",
  "allowSelfApproval",
]);
const TRIGGER_FIELDS = new Set([
  "type",
  "enabled",
  "eventSource",
  "cron",
  "timezone",
  "misfirePolicy",
  "misfireGraceSeconds",
]);
const CONNECTOR_FIELDS = new Set(["connectorFamily", "bindingId", "enabled"]);
const SCHEDULE_FIELDS = new Set([
  "cron",
  "timezone",
  "misfirePolicy",
  "misfireGraceSeconds",
]);
const RETRY_FIELDS = new Set(["maxAttempts", "backoff"]);
const TIMEOUT_FIELDS = new Set(["stepSeconds", "runSeconds"]);
const BUDGET_FIELDS = new Set([
  "maxSteps",
  "maxModelCalls",
  "maxToolCalls",
  "maxInputBytes",
  "maxInputFieldBytes",
  "maxOutputBytes",
  "maxModelOutputTokens",
]);
const RATE_LIMIT_FIELDS = new Set(["maxCalls", "windowSeconds"]);
const RUNTIME_STATUS_FIELDS = new Set([
  "status",
  "latestRunId",
  "latestRunState",
  "latestRunCreatedAt",
  "latestRunUpdatedAt",
  "latestRunUrl",
]);
const RECENT_RUN_FIELDS = new Set([
  "id",
  "state",
  "workflowId",
  "createdAt",
  "updatedAt",
  "runUrl",
]);

const CATALOG_HASH_PATTERN = /^catalog-sha256-v1:[0-9a-f]{64}$/u;
const RUNTIME_WATERMARK_PATTERN = /^instance-status-sha256-v1:[0-9a-f]{64}$/u;
const CONFIGURATION_ETAG_PATTERN = /^"instance-configuration-v1-[1-9][0-9]*"$/u;
const REPRESENTATION_ETAG_PATTERN = /^"[0-9a-f]{64}"$/u;
const ISO_TIMESTAMP_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/u;
const NO_RECENT_RUNS: readonly [] = Object.freeze([]);

export type RunState =
  | "received"
  | "validated"
  | "planned"
  | "awaiting_approval"
  | "executing"
  | "completed"
  | "failed"
  | "rejected"
  | "cancelled";
export type InstanceRuntimeState = "never_run" | RunState;
export type TriggerType = "manual" | "webhook" | "schedule";
export type MisfirePolicy = "skip" | "run_once";
export type CapabilityEffect = "read" | "write";
export type OperationClassification = "read_only" | "mutating";

export interface AgentInstanceDetailIdentity {
  readonly instanceId: string;
  readonly templateId: string;
  readonly departmentId: string;
  readonly functionId: string;
  readonly sourceOrdinal: number;
  readonly sharedTemplateDeploymentCount: number;
  readonly catalogVersion: string;
  readonly catalogHash: string;
}

export interface TriggerBinding {
  readonly type: TriggerType;
  readonly enabled: boolean;
  readonly eventSource: string | null;
  readonly cron: string | null;
  readonly timezone: string | null;
  readonly misfirePolicy: MisfirePolicy | null;
  readonly misfireGraceSeconds: number | null;
}

export interface ConnectorBinding {
  readonly connectorFamily: string;
  readonly bindingId: string;
  readonly enabled: boolean;
}

export interface ScheduleBinding {
  readonly cron: string;
  readonly timezone: string;
  readonly misfirePolicy: MisfirePolicy;
  readonly misfireGraceSeconds: number;
}

export interface AgentInstanceView {
  readonly id: string;
  readonly templateId: string;
  readonly displayOrder: number;
  readonly enabled: boolean;
  readonly sourceOrdinal: number;
  readonly variantLabel: string | null;
  readonly triggerBindings: readonly TriggerBinding[];
  readonly connectorBindings: Readonly<Record<string, ConnectorBinding>>;
  readonly schedule: ScheduleBinding | null;
  readonly configurationRevision: number;
  readonly configurationEtag: string;
}

export interface RetryPolicy {
  readonly maxAttempts: number;
  readonly backoff: "none" | "bounded_exponential";
}

export interface TimeoutPolicy {
  readonly stepSeconds: number;
  readonly runSeconds: number;
}

export interface BudgetPolicy {
  readonly maxSteps: number;
  readonly maxModelCalls: number;
  readonly maxToolCalls: number;
  readonly maxInputBytes: number;
  readonly maxInputFieldBytes: number;
  readonly maxOutputBytes: number;
  readonly maxModelOutputTokens: number;
}

export interface RateLimitPolicy {
  readonly maxCalls: number;
  readonly windowSeconds: number;
}

export interface AgentTemplateView {
  readonly id: string;
  readonly displayName: string;
  readonly departmentId: string;
  readonly functionId: string;
  readonly displayOrder: number;
  readonly purpose: string;
  readonly inputSchemaId: string;
  readonly outputSchemaId: string;
  readonly allowedToolCapabilityIds: readonly string[];
  readonly supportedTriggerTypes: readonly TriggerType[];
  readonly operationClassification: OperationClassification;
  readonly outputHandling: "standard" | "advisory";
  readonly approvalPolicyId: string;
  readonly retryPolicy: RetryPolicy;
  readonly timeoutPolicy: TimeoutPolicy;
  readonly budgetPolicy: BudgetPolicy;
  readonly rateLimitPolicy: RateLimitPolicy;
  readonly sourceConfidence: "high" | "medium" | "low";
  readonly sourceReferences: readonly string[];
  readonly implementationNotes: string;
}

export interface ToolCapability {
  readonly id: string;
  readonly displayName: string;
  readonly description: string;
  readonly effect: CapabilityEffect;
  readonly connectorFamily: string;
  readonly idempotencySupport:
    "not_applicable" | "required" | "supported" | "unavailable";
  readonly defaultTimeoutSeconds: number;
  readonly dataClassification: "public" | "internal" | "personal" | "sensitive";
}

export interface ApprovalPolicy {
  readonly id: string;
  readonly kind: "none" | "human_external_write";
  readonly requiredRoles: readonly string[];
  readonly expirySeconds: number;
  readonly allowSelfApproval: boolean;
}

export type JsonValue =
  | null
  | boolean
  | number
  | string
  | readonly JsonValue[]
  | { readonly [key: string]: JsonValue };
export type JsonObject = Readonly<Record<string, JsonValue>>;

export interface InstanceRuntimeStatus {
  readonly status: InstanceRuntimeState;
  readonly latestRunId: string | null;
  readonly latestRunState: RunState | null;
  readonly latestRunCreatedAt: string | null;
  readonly latestRunUpdatedAt: string | null;
  readonly latestRunUrl: string | null;
}

export interface InstanceRecentRun {
  readonly id: string;
  readonly state: RunState;
  readonly workflowId: string;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly runUrl: string;
}

interface AgentInstanceDetailCommon {
  readonly etag: string;
  readonly catalogVersion: string;
  readonly catalogHash: string;
  readonly instance: AgentInstanceView;
  readonly template: AgentTemplateView;
  readonly sharedTemplateDeploymentCount: number;
  readonly capabilities: readonly ToolCapability[];
  readonly approvalPolicy: ApprovalPolicy;
  readonly inputSchema: JsonObject;
  readonly outputSchema: JsonObject;
  readonly templateSourceReferences: readonly string[];
  readonly templateImplementationNotes: string;
  readonly configurationSchema: string;
}

export interface StaticAgentInstanceDetail extends AgentInstanceDetailCommon {
  readonly runtimeAvailable: false;
  readonly runtimeWatermark: null;
  readonly runtimeStatus: null;
  readonly recentRuns: readonly [];
}

export interface RuntimeAgentInstanceDetail extends AgentInstanceDetailCommon {
  readonly runtimeAvailable: true;
  readonly runtimeWatermark: string;
  readonly runtimeStatus: InstanceRuntimeStatus;
  readonly recentRuns: readonly InstanceRecentRun[];
}

export type AgentInstanceDetail =
  StaticAgentInstanceDetail | RuntimeAgentInstanceDetail;

export interface FetchAgentInstanceDetailOptions {
  readonly previous?: AgentInstanceDetail;
  readonly signal?: AbortSignal;
}

export class AgentInstanceDetailContractError extends Error {
  constructor(message: string) {
    super(`Agent instance detail contract violation: ${message}`);
    this.name = "AgentInstanceDetailContractError";
  }
}

const RUN_STATES = new Set<RunState>([
  "received",
  "validated",
  "planned",
  "awaiting_approval",
  "executing",
  "completed",
  "failed",
  "rejected",
  "cancelled",
]);
const TRIGGER_TYPES = new Set<TriggerType>(["manual", "webhook", "schedule"]);
const MISFIRE_POLICIES = new Set<MisfirePolicy>(["skip", "run_once"]);

function fail(message: string): never {
  throw new AgentInstanceDetailContractError(message);
}

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    fail(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function assertExactFields(
  record: Record<string, unknown>,
  expected: ReadonlySet<string>,
  label: string,
): void {
  const actual = Object.keys(record);
  if (
    actual.length !== expected.size ||
    actual.some((field) => !expected.has(field))
  ) {
    fail(`${label} fields are unsupported`);
  }
}

function asArray(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) fail(`${label} must be an array`);
  return value;
}

function asString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    fail(`${label} must be a non-empty string`);
  }
  return value;
}

function asNullableString(value: unknown, label: string): string | null {
  return value === null ? null : asString(value, label);
}

function asBoolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") fail(`${label} must be a boolean`);
  return value;
}

function asInteger(
  value: unknown,
  minimum: number,
  maximum: number,
  label: string,
): number {
  if (
    !Number.isSafeInteger(value) ||
    (value as number) < minimum ||
    (value as number) > maximum
  ) {
    fail(`${label} is outside its supported integer range`);
  }
  return value as number;
}

function asNullableInteger(
  value: unknown,
  minimum: number,
  maximum: number,
  label: string,
): number | null {
  return value === null ? null : asInteger(value, minimum, maximum, label);
}

function asEnum<T extends string>(
  value: unknown,
  allowed: ReadonlySet<T>,
  label: string,
): T {
  if (typeof value !== "string" || !allowed.has(value as T)) {
    fail(`${label} is unsupported`);
  }
  return value as T;
}

function asStrings(value: unknown, label: string): readonly string[] {
  const items = asArray(value, label).map((item, index) =>
    asString(item, `${label}[${String(index)}]`),
  );
  if (new Set(items).size !== items.length) fail(`${label} must be unique`);
  return Object.freeze(items);
}

function asTimestamp(value: unknown, label: string): string {
  const timestamp = asString(value, label);
  const match = ISO_TIMESTAMP_PATTERN.exec(timestamp);
  if (match === null || !Number.isFinite(Date.parse(timestamp))) {
    fail(`${label} must be an ISO timestamp`);
  }
  const [year, month, day, hour, minute, second] = match
    .slice(1, 7)
    .map(Number);
  const calendar = new Date(0);
  if (
    year === undefined ||
    month === undefined ||
    day === undefined ||
    hour === undefined ||
    minute === undefined ||
    second === undefined
  ) {
    fail(`${label} must be an ISO timestamp`);
  }
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
    fail(`${label} must be an ISO timestamp`);
  }
  return timestamp;
}

function asNullableTimestamp(value: unknown, label: string): string | null {
  return value === null ? null : asTimestamp(value, label);
}

function parseTrigger(value: unknown, label: string): TriggerBinding {
  const record = asRecord(value, label);
  assertExactFields(record, TRIGGER_FIELDS, label);
  return Object.freeze({
    type: asEnum(record.type, TRIGGER_TYPES, `${label}.type`),
    enabled: asBoolean(record.enabled, `${label}.enabled`),
    eventSource: asNullableString(record.eventSource, `${label}.eventSource`),
    cron: asNullableString(record.cron, `${label}.cron`),
    timezone: asNullableString(record.timezone, `${label}.timezone`),
    misfirePolicy:
      record.misfirePolicy === null
        ? null
        : asEnum(
            record.misfirePolicy,
            MISFIRE_POLICIES,
            `${label}.misfirePolicy`,
          ),
    misfireGraceSeconds: asNullableInteger(
      record.misfireGraceSeconds,
      0,
      86_400,
      `${label}.misfireGraceSeconds`,
    ),
  });
}

function parseSchedule(value: unknown, label: string): ScheduleBinding | null {
  if (value === null) return null;
  const record = asRecord(value, label);
  assertExactFields(record, SCHEDULE_FIELDS, label);
  return Object.freeze({
    cron: asString(record.cron, `${label}.cron`),
    timezone: asString(record.timezone, `${label}.timezone`),
    misfirePolicy: asEnum(
      record.misfirePolicy,
      MISFIRE_POLICIES,
      `${label}.misfirePolicy`,
    ),
    misfireGraceSeconds: asInteger(
      record.misfireGraceSeconds,
      0,
      86_400,
      `${label}.misfireGraceSeconds`,
    ),
  });
}

function parseConnectorBindings(
  value: unknown,
  label: string,
): Readonly<Record<string, ConnectorBinding>> {
  const record = asRecord(value, label);
  if (Object.keys(record).length > 16) fail(`${label} is too large`);
  const bindings: Record<string, ConnectorBinding> = {};
  for (const [family, raw] of Object.entries(record)) {
    if (family.length === 0) fail(`${label} keys must be non-empty`);
    const binding = asRecord(raw, `${label}.${family}`);
    assertExactFields(binding, CONNECTOR_FIELDS, `${label}.${family}`);
    const connectorFamily = asString(
      binding.connectorFamily,
      `${label}.${family}.connectorFamily`,
    );
    if (connectorFamily !== family) fail(`${label}.${family} is incoherent`);
    bindings[family] = Object.freeze({
      connectorFamily,
      bindingId: asString(binding.bindingId, `${label}.${family}.bindingId`),
      enabled: asBoolean(binding.enabled, `${label}.${family}.enabled`),
    });
  }
  return Object.freeze(bindings);
}

function assertScheduleCoherence(
  triggers: readonly TriggerBinding[],
  schedule: ScheduleBinding | null,
  label: string,
): void {
  const scheduleTriggers = triggers.filter(
    (trigger) => trigger.type === "schedule",
  );
  const enabled = scheduleTriggers.length === 1 && scheduleTriggers[0]?.enabled;
  if ((schedule !== null) !== enabled) fail(`${label} schedule is incoherent`);
  for (const trigger of triggers) {
    const hasScheduleFields =
      trigger.cron !== null ||
      trigger.timezone !== null ||
      trigger.misfirePolicy !== null ||
      trigger.misfireGraceSeconds !== null;
    if (trigger.type === "schedule") {
      if (
        trigger.enabled &&
        (trigger.cron === null ||
          trigger.timezone === null ||
          trigger.misfirePolicy === null ||
          trigger.misfireGraceSeconds === null)
      ) {
        fail(`${label} schedule trigger fields are incomplete`);
      } else if (!trigger.enabled && hasScheduleFields) {
        fail(`${label} disabled schedule trigger retains schedule fields`);
      }
    } else if (hasScheduleFields) {
      fail(`${label} non-schedule trigger has schedule fields`);
    }
  }
  const scheduleTrigger = scheduleTriggers[0];
  if (
    schedule !== null &&
    (scheduleTrigger?.cron !== schedule.cron ||
      scheduleTrigger.timezone !== schedule.timezone ||
      scheduleTrigger.misfirePolicy !== schedule.misfirePolicy ||
      scheduleTrigger.misfireGraceSeconds !== schedule.misfireGraceSeconds)
  ) {
    fail(`${label} schedule trigger does not match schedule`);
  }
}

function parseInstance(value: unknown, label: string): AgentInstanceView {
  const record = asRecord(value, label);
  assertExactFields(record, INSTANCE_FIELDS, label);
  const triggers = asArray(
    record.triggerBindings,
    `${label}.triggerBindings`,
  ).map((trigger, index) =>
    parseTrigger(trigger, `${label}.triggerBindings[${String(index)}]`),
  );
  if (triggers.length > 16) fail(`${label}.triggerBindings is too large`);
  if (
    new Set(triggers.map((trigger) => trigger.type)).size !== triggers.length
  ) {
    fail(`${label}.triggerBindings types must be unique`);
  }
  const schedule = parseSchedule(record.schedule, `${label}.schedule`);
  assertScheduleCoherence(triggers, schedule, label);
  const configurationEtag = asString(
    record.configurationEtag,
    `${label}.configurationEtag`,
  );
  if (!CONFIGURATION_ETAG_PATTERN.test(configurationEtag)) {
    fail(`${label}.configurationEtag is invalid`);
  }
  return Object.freeze({
    id: asString(record.id, `${label}.id`),
    templateId: asString(record.templateId, `${label}.templateId`),
    displayOrder: asInteger(
      record.displayOrder,
      1,
      Number.MAX_SAFE_INTEGER,
      `${label}.displayOrder`,
    ),
    enabled: asBoolean(record.enabled, `${label}.enabled`),
    sourceOrdinal: asInteger(
      record.sourceOrdinal,
      1,
      99,
      `${label}.sourceOrdinal`,
    ),
    variantLabel: asNullableString(
      record.variantLabel,
      `${label}.variantLabel`,
    ),
    triggerBindings: Object.freeze(triggers),
    connectorBindings: parseConnectorBindings(
      record.connectorBindings,
      `${label}.connectorBindings`,
    ),
    schedule,
    configurationRevision: asInteger(
      record.configurationRevision,
      1,
      Number.MAX_SAFE_INTEGER,
      `${label}.configurationRevision`,
    ),
    configurationEtag,
  });
}

function parseRetryPolicy(value: unknown, label: string): RetryPolicy {
  const record = asRecord(value, label);
  assertExactFields(record, RETRY_FIELDS, label);
  return Object.freeze({
    maxAttempts: asInteger(record.maxAttempts, 1, 3, `${label}.maxAttempts`),
    backoff: asEnum(
      record.backoff,
      new Set(["none", "bounded_exponential"] as const),
      `${label}.backoff`,
    ),
  });
}

function parseTimeoutPolicy(value: unknown, label: string): TimeoutPolicy {
  const record = asRecord(value, label);
  assertExactFields(record, TIMEOUT_FIELDS, label);
  return Object.freeze({
    stepSeconds: asInteger(record.stepSeconds, 1, 120, `${label}.stepSeconds`),
    runSeconds: asInteger(record.runSeconds, 1, 600, `${label}.runSeconds`),
  });
}

function parseBudgetPolicy(value: unknown, label: string): BudgetPolicy {
  const record = asRecord(value, label);
  assertExactFields(record, BUDGET_FIELDS, label);
  return Object.freeze({
    maxSteps: asInteger(record.maxSteps, 1, 20, `${label}.maxSteps`),
    maxModelCalls: asInteger(
      record.maxModelCalls,
      0,
      10,
      `${label}.maxModelCalls`,
    ),
    maxToolCalls: asInteger(
      record.maxToolCalls,
      0,
      20,
      `${label}.maxToolCalls`,
    ),
    maxInputBytes: asInteger(
      record.maxInputBytes,
      1,
      1_048_576,
      `${label}.maxInputBytes`,
    ),
    maxInputFieldBytes: asInteger(
      record.maxInputFieldBytes,
      1,
      262_144,
      `${label}.maxInputFieldBytes`,
    ),
    maxOutputBytes: asInteger(
      record.maxOutputBytes,
      1,
      4_194_304,
      `${label}.maxOutputBytes`,
    ),
    maxModelOutputTokens: asInteger(
      record.maxModelOutputTokens,
      1,
      32_768,
      `${label}.maxModelOutputTokens`,
    ),
  });
}

function parseRateLimitPolicy(value: unknown, label: string): RateLimitPolicy {
  const record = asRecord(value, label);
  assertExactFields(record, RATE_LIMIT_FIELDS, label);
  return Object.freeze({
    maxCalls: asInteger(record.maxCalls, 1, 100, `${label}.maxCalls`),
    windowSeconds: asInteger(
      record.windowSeconds,
      1,
      3_600,
      `${label}.windowSeconds`,
    ),
  });
}

function parseTemplate(value: unknown, label: string): AgentTemplateView {
  const record = asRecord(value, label);
  assertExactFields(record, TEMPLATE_FIELDS, label);
  const allowedToolCapabilityIds = asStrings(
    record.allowedToolCapabilityIds,
    `${label}.allowedToolCapabilityIds`,
  );
  if (allowedToolCapabilityIds.length === 0) {
    fail(`${label}.allowedToolCapabilityIds must not be empty`);
  }
  const supportedTriggerTypes = asArray(
    record.supportedTriggerTypes,
    `${label}.supportedTriggerTypes`,
  ).map((item, index) =>
    asEnum(
      item,
      TRIGGER_TYPES,
      `${label}.supportedTriggerTypes[${String(index)}]`,
    ),
  );
  if (
    supportedTriggerTypes.length === 0 ||
    new Set(supportedTriggerTypes).size !== supportedTriggerTypes.length
  ) {
    fail(`${label}.supportedTriggerTypes must be non-empty and unique`);
  }
  return Object.freeze({
    id: asString(record.id, `${label}.id`),
    displayName: asString(record.displayName, `${label}.displayName`),
    departmentId: asString(record.departmentId, `${label}.departmentId`),
    functionId: asString(record.functionId, `${label}.functionId`),
    displayOrder: asInteger(
      record.displayOrder,
      1,
      Number.MAX_SAFE_INTEGER,
      `${label}.displayOrder`,
    ),
    purpose: asString(record.purpose, `${label}.purpose`),
    inputSchemaId: asString(record.inputSchemaId, `${label}.inputSchemaId`),
    outputSchemaId: asString(record.outputSchemaId, `${label}.outputSchemaId`),
    allowedToolCapabilityIds,
    supportedTriggerTypes: Object.freeze(supportedTriggerTypes),
    operationClassification: asEnum(
      record.operationClassification,
      new Set(["read_only", "mutating"] as const),
      `${label}.operationClassification`,
    ),
    outputHandling: asEnum(
      record.outputHandling,
      new Set(["standard", "advisory"] as const),
      `${label}.outputHandling`,
    ),
    approvalPolicyId: asString(
      record.approvalPolicyId,
      `${label}.approvalPolicyId`,
    ),
    retryPolicy: parseRetryPolicy(record.retryPolicy, `${label}.retryPolicy`),
    timeoutPolicy: parseTimeoutPolicy(
      record.timeoutPolicy,
      `${label}.timeoutPolicy`,
    ),
    budgetPolicy: parseBudgetPolicy(
      record.budgetPolicy,
      `${label}.budgetPolicy`,
    ),
    rateLimitPolicy: parseRateLimitPolicy(
      record.rateLimitPolicy,
      `${label}.rateLimitPolicy`,
    ),
    sourceConfidence: asEnum(
      record.sourceConfidence,
      new Set(["high", "medium", "low"] as const),
      `${label}.sourceConfidence`,
    ),
    sourceReferences: asStrings(
      record.sourceReferences,
      `${label}.sourceReferences`,
    ),
    implementationNotes: asString(
      record.implementationNotes,
      `${label}.implementationNotes`,
    ),
  });
}

function parseCapability(value: unknown, label: string): ToolCapability {
  const record = asRecord(value, label);
  assertExactFields(record, CAPABILITY_FIELDS, label);
  return Object.freeze({
    id: asString(record.id, `${label}.id`),
    displayName: asString(record.displayName, `${label}.displayName`),
    description: asString(record.description, `${label}.description`),
    effect: asEnum(
      record.effect,
      new Set(["read", "write"] as const),
      `${label}.effect`,
    ),
    connectorFamily: asString(
      record.connectorFamily,
      `${label}.connectorFamily`,
    ),
    idempotencySupport: asEnum(
      record.idempotencySupport,
      new Set([
        "not_applicable",
        "required",
        "supported",
        "unavailable",
      ] as const),
      `${label}.idempotencySupport`,
    ),
    defaultTimeoutSeconds: asInteger(
      record.defaultTimeoutSeconds,
      1,
      120,
      `${label}.defaultTimeoutSeconds`,
    ),
    dataClassification: asEnum(
      record.dataClassification,
      new Set(["public", "internal", "personal", "sensitive"] as const),
      `${label}.dataClassification`,
    ),
  });
}

function parseApprovalPolicy(value: unknown, label: string): ApprovalPolicy {
  const record = asRecord(value, label);
  assertExactFields(record, APPROVAL_POLICY_FIELDS, label);
  return Object.freeze({
    id: asString(record.id, `${label}.id`),
    kind: asEnum(
      record.kind,
      new Set(["none", "human_external_write"] as const),
      `${label}.kind`,
    ),
    requiredRoles: asStrings(record.requiredRoles, `${label}.requiredRoles`),
    expirySeconds: asInteger(
      record.expirySeconds,
      60,
      86_400,
      `${label}.expirySeconds`,
    ),
    allowSelfApproval: asBoolean(
      record.allowSelfApproval,
      `${label}.allowSelfApproval`,
    ),
  });
}

function cloneJson(value: unknown, label: string): JsonValue {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) fail(`${label} must contain JSON values`);
    return value;
  }
  if (Array.isArray(value)) {
    return Object.freeze(
      value.map((item, index) => cloneJson(item, `${label}[${String(index)}]`)),
    );
  }
  const record = asRecord(value, label);
  const result: Record<string, JsonValue> = {};
  for (const [key, item] of Object.entries(record)) {
    if (key === "__proto__" || key === "prototype" || key === "constructor") {
      fail(`${label} contains an unsafe field`);
    }
    result[key] = cloneJson(item, `${label}.${key}`);
  }
  return Object.freeze(result);
}

function parseSchema(
  value: unknown,
  expectedId: string,
  label: string,
): JsonObject {
  const schema = cloneJson(value, label);
  if (schema === null || Array.isArray(schema) || typeof schema !== "object") {
    fail(`${label} must be an object`);
  }
  const object = schema as JsonObject;
  if (object.$id !== expectedId) fail(`${label}.$id is incoherent`);
  return object;
}

function parseRuntimeStatus(
  value: unknown,
  label: string,
): InstanceRuntimeStatus {
  const record = asRecord(value, label);
  assertExactFields(record, RUNTIME_STATUS_FIELDS, label);
  const statusValue = asString(record.status, `${label}.status`);
  const status: InstanceRuntimeState =
    statusValue === "never_run"
      ? "never_run"
      : asEnum(statusValue, RUN_STATES, `${label}.status`);
  const latestRunState =
    record.latestRunState === null
      ? null
      : asEnum(record.latestRunState, RUN_STATES, `${label}.latestRunState`);
  const result = Object.freeze({
    status,
    latestRunId: asNullableString(record.latestRunId, `${label}.latestRunId`),
    latestRunState,
    latestRunCreatedAt: asNullableTimestamp(
      record.latestRunCreatedAt,
      `${label}.latestRunCreatedAt`,
    ),
    latestRunUpdatedAt: asNullableTimestamp(
      record.latestRunUpdatedAt,
      `${label}.latestRunUpdatedAt`,
    ),
    latestRunUrl: asNullableString(
      record.latestRunUrl,
      `${label}.latestRunUrl`,
    ),
  });
  if (result.status === "never_run") {
    if (
      result.latestRunId !== null ||
      result.latestRunState !== null ||
      result.latestRunCreatedAt !== null ||
      result.latestRunUpdatedAt !== null ||
      result.latestRunUrl !== null
    ) {
      fail(`${label} never_run fields must all be null`);
    }
  } else if (
    result.latestRunId === null ||
    result.latestRunState !== result.status ||
    result.latestRunCreatedAt === null ||
    result.latestRunUpdatedAt === null ||
    result.latestRunUrl !== expectedRunUrl(result.latestRunId) ||
    Date.parse(result.latestRunUpdatedAt) <
      Date.parse(result.latestRunCreatedAt)
  ) {
    fail(`${label} latest run fields are incoherent`);
  }
  return result;
}

function expectedRunUrl(runId: string): string {
  return `/api/v1/runs/${encodeURIComponent(runId)}`;
}

function parseRecentRun(value: unknown, label: string): InstanceRecentRun {
  const record = asRecord(value, label);
  assertExactFields(record, RECENT_RUN_FIELDS, label);
  const id = asString(record.id, `${label}.id`);
  const createdAt = asTimestamp(record.createdAt, `${label}.createdAt`);
  const updatedAt = asTimestamp(record.updatedAt, `${label}.updatedAt`);
  const runUrl = asString(record.runUrl, `${label}.runUrl`);
  if (runUrl !== expectedRunUrl(id)) fail(`${label}.runUrl is invalid`);
  if (Date.parse(updatedAt) < Date.parse(createdAt)) {
    fail(`${label}.updatedAt precedes creation`);
  }
  return Object.freeze({
    id,
    state: asEnum(record.state, RUN_STATES, `${label}.state`),
    workflowId: asString(record.workflowId, `${label}.workflowId`),
    createdAt,
    updatedAt,
    runUrl,
  });
}

function parseRecentRuns(
  value: unknown,
  label: string,
): readonly InstanceRecentRun[] {
  const raw = asArray(value, label);
  if (raw.length > 5) fail(`${label} must contain at most five runs`);
  const runs = raw.map((run, index) =>
    parseRecentRun(run, `${label}[${String(index)}]`),
  );
  if (new Set(runs.map((run) => run.id)).size !== runs.length) {
    fail(`${label} IDs must be unique`);
  }
  for (let index = 1; index < runs.length; index += 1) {
    const previous = runs[index - 1];
    const current = runs[index];
    if (previous === undefined || current === undefined)
      fail(`${label} is invalid`);
    const previousCreated = Date.parse(previous.createdAt);
    const currentCreated = Date.parse(current.createdAt);
    if (
      previousCreated < currentCreated ||
      (previousCreated === currentCreated && previous.id < current.id)
    ) {
      fail(`${label} must be ordered newest first`);
    }
  }
  return Object.freeze(runs);
}

function validateIdentity(identity: AgentInstanceDetailIdentity): void {
  for (const [label, value] of [
    ["instanceId", identity.instanceId],
    ["templateId", identity.templateId],
    ["departmentId", identity.departmentId],
    ["functionId", identity.functionId],
    ["catalogVersion", identity.catalogVersion],
  ] as const) {
    asString(value, `expected.${label}`);
  }
  asInteger(identity.sourceOrdinal, 1, 99, "expected.sourceOrdinal");
  asInteger(
    identity.sharedTemplateDeploymentCount,
    1,
    Number.MAX_SAFE_INTEGER,
    "expected.sharedTemplateDeploymentCount",
  );
  if (!CATALOG_HASH_PATTERN.test(identity.catalogHash)) {
    fail("expected.catalogHash is invalid");
  }
}

function assertIdentity(
  catalogVersion: string,
  catalogHash: string,
  instance: AgentInstanceView,
  template: AgentTemplateView,
  deploymentCount: number,
  expected: AgentInstanceDetailIdentity,
): void {
  if (
    catalogVersion !== expected.catalogVersion ||
    catalogHash !== expected.catalogHash ||
    instance.id !== expected.instanceId ||
    instance.templateId !== expected.templateId ||
    instance.sourceOrdinal !== expected.sourceOrdinal ||
    template.id !== expected.templateId ||
    template.departmentId !== expected.departmentId ||
    template.functionId !== expected.functionId ||
    deploymentCount !== expected.sharedTemplateDeploymentCount
  ) {
    fail("response does not match the expected hierarchy identity");
  }
}

function assertStaticCoherence(
  instance: AgentInstanceView,
  template: AgentTemplateView,
  capabilities: readonly ToolCapability[],
  approvalPolicy: ApprovalPolicy,
  templateSourceReferences: readonly string[],
  templateImplementationNotes: string,
): void {
  const capabilityIds = capabilities.map((capability) => capability.id);
  if (
    capabilityIds.length !== template.allowedToolCapabilityIds.length ||
    capabilityIds.some(
      (identifier, index) =>
        identifier !== template.allowedToolCapabilityIds[index],
    ) ||
    approvalPolicy.id !== template.approvalPolicyId ||
    templateSourceReferences.length !== template.sourceReferences.length ||
    templateSourceReferences.some(
      (reference, index) => reference !== template.sourceReferences[index],
    ) ||
    templateImplementationNotes !== template.implementationNotes
  ) {
    fail("template capability, policy, or evidence references are incoherent");
  }
  if (
    instance.triggerBindings.some(
      (binding) => !template.supportedTriggerTypes.includes(binding.type),
    )
  ) {
    fail("instance trigger bindings are unsupported by its template");
  }
}

function assertRuntimeCoherence(
  status: InstanceRuntimeStatus,
  runs: readonly InstanceRecentRun[],
): void {
  const latest = runs[0];
  if (latest === undefined) {
    if (status.status !== "never_run") fail("empty recent runs are incoherent");
    return;
  }
  if (
    status.status === "never_run" ||
    status.latestRunId !== latest.id ||
    status.latestRunState !== latest.state ||
    status.latestRunCreatedAt !== latest.createdAt ||
    status.latestRunUpdatedAt !== latest.updatedAt ||
    status.latestRunUrl !== latest.runUrl ||
    status.status !== latest.state
  ) {
    fail("runtime status does not match the latest recent run");
  }
}

function expectedConfigurationSchema(instanceId: string): string {
  return `/api/v1/agent-instances/${encodeURIComponent(instanceId)}/configuration-schema`;
}

export function normalizeAgentInstanceDetail(
  value: unknown,
  expected: AgentInstanceDetailIdentity,
  etag: string,
): AgentInstanceDetail {
  validateIdentity(expected);
  if (!REPRESENTATION_ETAG_PATTERN.test(etag)) fail("response ETag is invalid");
  const record = asRecord(value, "detail");
  const hasAnyRuntimeField =
    "runtimeWatermark" in record ||
    "runtimeStatus" in record ||
    "recentRuns" in record;
  assertExactFields(
    record,
    hasAnyRuntimeField ? RUNTIME_ROOT_FIELDS : DETAIL_ROOT_FIELDS,
    "detail",
  );

  const catalogVersion = asString(
    record.catalogVersion,
    "detail.catalogVersion",
  );
  const catalogHash = asString(record.catalogHash, "detail.catalogHash");
  if (!CATALOG_HASH_PATTERN.test(catalogHash))
    fail("detail.catalogHash is invalid");
  const instance = parseInstance(record.instance, "detail.instance");
  const template = parseTemplate(record.template, "detail.template");
  const sharedTemplateDeploymentCount = asInteger(
    record.sharedTemplateDeploymentCount,
    1,
    Number.MAX_SAFE_INTEGER,
    "detail.sharedTemplateDeploymentCount",
  );
  const capabilities = asArray(record.capabilities, "detail.capabilities").map(
    (capability, index) =>
      parseCapability(capability, `detail.capabilities[${String(index)}]`),
  );
  if (
    new Set(capabilities.map((capability) => capability.id)).size !==
    capabilities.length
  ) {
    fail("detail.capabilities IDs must be unique");
  }
  const approvalPolicy = parseApprovalPolicy(
    record.approvalPolicy,
    "detail.approvalPolicy",
  );
  const templateSourceReferences = asStrings(
    record.templateSourceReferences,
    "detail.templateSourceReferences",
  );
  const templateImplementationNotes = asString(
    record.templateImplementationNotes,
    "detail.templateImplementationNotes",
  );
  const configurationSchema = asString(
    record.configurationSchema,
    "detail.configurationSchema",
  );
  if (configurationSchema !== expectedConfigurationSchema(instance.id)) {
    fail("detail.configurationSchema is invalid");
  }

  assertIdentity(
    catalogVersion,
    catalogHash,
    instance,
    template,
    sharedTemplateDeploymentCount,
    expected,
  );
  assertStaticCoherence(
    instance,
    template,
    capabilities,
    approvalPolicy,
    templateSourceReferences,
    templateImplementationNotes,
  );
  const common = {
    etag,
    catalogVersion,
    catalogHash,
    instance,
    template,
    sharedTemplateDeploymentCount,
    capabilities: Object.freeze(capabilities),
    approvalPolicy,
    inputSchema: parseSchema(
      record.inputSchema,
      template.inputSchemaId,
      "detail.inputSchema",
    ),
    outputSchema: parseSchema(
      record.outputSchema,
      template.outputSchemaId,
      "detail.outputSchema",
    ),
    templateSourceReferences,
    templateImplementationNotes,
    configurationSchema,
  } satisfies AgentInstanceDetailCommon;

  if (!hasAnyRuntimeField) {
    return Object.freeze({
      ...common,
      runtimeAvailable: false,
      runtimeWatermark: null,
      runtimeStatus: null,
      recentRuns: NO_RECENT_RUNS,
    });
  }
  const runtimeWatermark = asString(
    record.runtimeWatermark,
    "detail.runtimeWatermark",
  );
  if (!RUNTIME_WATERMARK_PATTERN.test(runtimeWatermark)) {
    fail("detail.runtimeWatermark is invalid");
  }
  const runtimeStatus = parseRuntimeStatus(
    record.runtimeStatus,
    "detail.runtimeStatus",
  );
  const recentRuns = parseRecentRuns(record.recentRuns, "detail.recentRuns");
  assertRuntimeCoherence(runtimeStatus, recentRuns);
  return Object.freeze({
    ...common,
    runtimeAvailable: true,
    runtimeWatermark,
    runtimeStatus,
    recentRuns,
  });
}

function previousMatches(
  previous: AgentInstanceDetail,
  expected: AgentInstanceDetailIdentity,
): boolean {
  return (
    REPRESENTATION_ETAG_PATTERN.test(previous.etag) &&
    previous.catalogVersion === expected.catalogVersion &&
    previous.catalogHash === expected.catalogHash &&
    previous.instance.id === expected.instanceId &&
    previous.instance.templateId === expected.templateId &&
    previous.instance.sourceOrdinal === expected.sourceOrdinal &&
    previous.template.id === expected.templateId &&
    previous.template.departmentId === expected.departmentId &&
    previous.template.functionId === expected.functionId &&
    previous.sharedTemplateDeploymentCount ===
      expected.sharedTemplateDeploymentCount
  );
}

async function stableResponseError(
  response: Response,
): Promise<ApiRequestError> {
  let code = "api_request_failed";
  let detail = "The local API could not complete this request.";
  try {
    const problem: unknown = await response.json();
    if (
      typeof problem === "object" &&
      problem !== null &&
      !Array.isArray(problem)
    ) {
      const record = problem as Record<string, unknown>;
      if (typeof record.code === "string") code = record.code;
      if (typeof record.detail === "string") detail = record.detail;
      if (typeof record.message === "string") detail = record.message;
    }
  } catch {
    // Stable local fallbacks intentionally replace untrusted diagnostics.
  }
  return new ApiRequestError(response.status, code, detail);
}

export async function fetchAgentInstanceDetail(
  expected: AgentInstanceDetailIdentity,
  options: FetchAgentInstanceDetailOptions = {},
): Promise<AgentInstanceDetail> {
  try {
    validateIdentity(expected);
  } catch (error) {
    if (!(error instanceof AgentInstanceDetailContractError)) throw error;
    throw new ApiRequestError(
      0,
      "invalid_agent_instance_identity",
      "The selected agent instance identity is invalid.",
    );
  }
  const previous = options.previous;
  if (previous !== undefined && !previousMatches(previous, expected)) {
    throw new ApiRequestError(
      0,
      "invalid_previous_agent_instance_detail",
      "The previous agent instance detail is invalid.",
    );
  }
  const headers: Record<string, string> = { Accept: "application/json" };
  if (previous !== undefined) headers["If-None-Match"] = previous.etag;
  const request: RequestInit = {
    method: "GET",
    cache: "no-store",
    credentials: "same-origin",
    headers,
  };
  if (options.signal !== undefined) request.signal = options.signal;

  let response: Response;
  try {
    response = await fetch(
      `/api/v1/agent-instances/${encodeURIComponent(expected.instanceId)}`,
      request,
    );
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw new ApiRequestError(
      0,
      "api_unreachable",
      "The local API is not ready. Start it and try again.",
    );
  }

  if (response.status === 304) {
    if (response.headers.get("ETag") !== previous?.etag) {
      throw new ApiRequestError(
        response.status,
        "invalid_agent_instance_detail_response",
        "The local API returned an invalid agent instance detail.",
      );
    }
    return previous;
  }
  if (!response.ok) throw await stableResponseError(response);

  let body: unknown;
  try {
    body = (await response.json()) as unknown;
  } catch {
    throw new ApiRequestError(
      response.status,
      "invalid_json_response",
      "The local API returned an invalid response.",
    );
  }
  try {
    const etag = response.headers.get("ETag") ?? "";
    return normalizeAgentInstanceDetail(body, expected, etag);
  } catch (error) {
    if (!(error instanceof AgentInstanceDetailContractError)) throw error;
    throw new ApiRequestError(
      response.status,
      "invalid_agent_instance_detail_response",
      "The local API returned an invalid agent instance detail.",
    );
  }
}

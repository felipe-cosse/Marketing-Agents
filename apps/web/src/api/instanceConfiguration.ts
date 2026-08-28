const SESSION_PATH = "/api/v1/session";
const CONFIGURATION_SCHEMA_SUFFIX = "/configuration-schema";
const CONFIGURATION_SUFFIX = "/configuration";

const INSTANCE_ID_PATTERN =
  /^inst\.[a-z0-9-]+\.[a-z0-9-]+\.[a-z0-9-]+\.[0-9]{2}$/u;
const TEMPLATE_ID_PATTERN = /^tpl\.[a-z0-9-]+\.[a-z0-9-]+\.[a-z0-9-]+$/u;
const CONFIGURATION_ETAG_PATTERN =
  /^"instance-configuration-v1-([1-9][0-9]*)"$/u;
const AUTHORITY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._/-]{0,239}$/u;
const SAFE_CODE_PATTERN = /^[a-z][a-z0-9_]{0,127}$/u;
const CORRELATION_ID_PATTERN = /^correlation\.api\.[0-9a-f]{32}$/u;
const CONNECTOR_FAMILY_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/u;
const CONNECTOR_BINDING_ID_PATTERN = /^[A-Za-z0-9._-]+$/u;
const EVENT_SOURCE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$/u;
const CSRF_TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,128}$/u;
const CONFIGURATION_SCHEMA_DESCRIPTION =
  "Structural deployment PATCH schema. The API additionally enforces registered bindings, recurrence validity, and exact trigger/schedule value consistency.";

const SESSION_FIELDS = new Set([
  "actorId",
  "roles",
  "scopes",
  "authMode",
  "environment",
  "modelMode",
  "connectorMode",
  "networkPermission",
  "warning",
  "csrfToken",
  "csrfHeaderName",
]);
const CONFIGURATION_SCHEMA_RESPONSE_FIELDS = new Set([
  "projectionVersion",
  "instanceId",
  "templateId",
  "configurationSchema",
]);
const JSON_SCHEMA_FIELDS = new Set([
  "$schema",
  "$id",
  "type",
  "description",
  "additionalProperties",
  "minProperties",
  "properties",
]);
const CONFIGURATION_PROPERTY_FIELDS = new Set([
  "enabled",
  "variantLabel",
  "triggerBindings",
  "connectorBindings",
  "schedule",
]);
const TRIGGER_PROPERTY_FIELDS = new Set([
  "type",
  "enabled",
  "eventSource",
  "cron",
  "timezone",
  "misfirePolicy",
  "misfireGraceSeconds",
]);
const PATCH_FIELDS = CONFIGURATION_PROPERTY_FIELDS;
const SCHEDULE_FIELDS = new Set([
  "cron",
  "timezone",
  "misfirePolicy",
  "misfireGraceSeconds",
]);
const CONFIGURATION_RESPONSE_FIELDS = new Set([
  "projectionVersion",
  "configuration",
]);
const CONFIGURATION_FIELDS = new Set([
  "instanceId",
  "enabled",
  "variantLabel",
  "triggerBindings",
  "connectorBindings",
  "schedule",
  "configurationRevision",
]);
const TRIGGER_VIEW_FIELDS = new Set([
  "type",
  "enabled",
  "eventSource",
  "cron",
  "timezone",
  "misfirePolicy",
  "misfireGraceSeconds",
]);
const CONNECTOR_VIEW_FIELDS = new Set([
  "connectorFamily",
  "bindingId",
  "enabled",
]);
const PROBLEM_REQUIRED_FIELDS = new Set([
  "type",
  "title",
  "status",
  "detail",
  "instance",
  "code",
  "correlation_id",
]);
const PROBLEM_OPTIONAL_FIELDS = new Set([
  "field_errors",
  "retry_after_seconds",
  "current_resource_version",
]);
const FIELD_ERROR_FIELDS = new Set(["pointer", "code", "message"]);

const NULL_SCHEMA_FIELDS = new Set(["type"]);
const BOOLEAN_SCHEMA_FIELDS = new Set(["type"]);
const BOOLEAN_DEFAULT_SCHEMA_FIELDS = new Set(["type", "default"]);
const CONST_SCHEMA_FIELDS = new Set(["const"]);
const CONST_DEFAULT_SCHEMA_FIELDS = new Set(["const", "default"]);
const STRING_SCHEMA_FIELDS = new Set(["type", "minLength", "maxLength"]);
const EVENT_SOURCE_SCHEMA_FIELDS = new Set([
  "type",
  "minLength",
  "maxLength",
  "pattern",
]);
const INTEGER_SCHEMA_FIELDS = new Set(["type", "minimum", "maximum"]);
const ENUM_SCHEMA_FIELDS = new Set(["enum"]);

export type SupportedTriggerType = "manual" | "webhook" | "schedule";
export type MisfirePolicy = "skip" | "run_once";

export interface LocalSession {
  readonly actorId: string;
  readonly roles: readonly string[];
  readonly scopes: readonly string[];
  readonly authMode: "local";
  readonly environment: "local" | "test" | "production";
  readonly modelMode: "mock" | "real";
  readonly connectorMode: string;
  readonly networkPermission: boolean;
  readonly warning: "Local identity — not production authentication";
}

export interface ConnectorFamilyOption {
  readonly connectorFamily: string;
  readonly bindingIds: readonly string[];
}

export interface InstanceConfigurationSchema {
  readonly projectionVersion: "instance-configuration-schema-v1";
  readonly instanceId: string;
  readonly templateId: string;
  readonly supportedTriggerTypes: readonly SupportedTriggerType[];
  readonly connectorFamilies: readonly ConnectorFamilyOption[];
  readonly scheduleSupported: boolean;
  readonly variantLabelMaxLength: 100;
  readonly maxTriggerBindings: 16;
  readonly maxConnectorBindings: 16;
}

export interface ManualTriggerBindingPatch {
  readonly type: "manual";
  readonly enabled?: boolean;
}

export interface WebhookTriggerBindingPatch {
  readonly type: "webhook";
  readonly enabled?: boolean;
  readonly eventSource: string;
}

export interface DisabledScheduleTriggerBindingPatch {
  readonly type: "schedule";
  readonly enabled: false;
}

export interface EnabledScheduleTriggerBindingPatch {
  readonly type: "schedule";
  readonly enabled?: true;
  readonly cron: string;
  readonly timezone: string;
  readonly misfirePolicy: MisfirePolicy;
  readonly misfireGraceSeconds: number;
}

export type TriggerBindingPatch =
  | ManualTriggerBindingPatch
  | WebhookTriggerBindingPatch
  | DisabledScheduleTriggerBindingPatch
  | EnabledScheduleTriggerBindingPatch;

export interface ConnectorBindingPatch {
  readonly connectorFamily: string;
  readonly bindingId: string;
  readonly enabled?: boolean;
}

export interface SchedulePatch {
  readonly cron: string;
  readonly timezone: string;
  readonly misfirePolicy: MisfirePolicy;
  readonly misfireGraceSeconds: number;
}

export interface InstanceConfigurationPatch {
  readonly enabled?: boolean;
  readonly variantLabel?: string | null;
  readonly triggerBindings?: readonly TriggerBindingPatch[];
  readonly connectorBindings?: Readonly<Record<string, ConnectorBindingPatch>>;
  readonly schedule?: SchedulePatch | null;
}

export interface InstanceTriggerBinding {
  readonly type: SupportedTriggerType;
  readonly enabled: boolean;
  readonly eventSource: string | null;
  readonly cron: string | null;
  readonly timezone: string | null;
  readonly misfirePolicy: MisfirePolicy | null;
  readonly misfireGraceSeconds: number | null;
}

export interface InstanceConnectorBinding {
  readonly connectorFamily: string;
  readonly bindingId: string;
  readonly enabled: boolean;
}

export interface InstanceSchedule {
  readonly cron: string;
  readonly timezone: string;
  readonly misfirePolicy: MisfirePolicy;
  readonly misfireGraceSeconds: number;
}

export interface InstanceConfigurationResult {
  readonly projectionVersion: "instance-configuration-v1";
  readonly configuration: {
    readonly instanceId: string;
    readonly enabled: boolean;
    readonly variantLabel: string | null;
    readonly triggerBindings: readonly InstanceTriggerBinding[];
    readonly connectorBindings: Readonly<
      Record<string, InstanceConnectorBinding>
    >;
    readonly schedule: InstanceSchedule | null;
    readonly configurationRevision: number;
  };
  readonly configurationEtag: string;
}

export interface InstanceConfigurationFieldError {
  readonly pointer: string;
  readonly code: string;
  readonly message: string;
}

export class InstanceConfigurationRequestError extends Error {
  readonly status: number;
  readonly code: string;
  readonly currentResourceVersion: number | string | null;
  readonly fieldErrors: readonly InstanceConfigurationFieldError[];

  constructor(
    status: number,
    code: string,
    message: string,
    options: {
      readonly currentResourceVersion?: number | string | null;
      readonly fieldErrors?: readonly InstanceConfigurationFieldError[];
    } = {},
  ) {
    super(message);
    this.name = "InstanceConfigurationRequestError";
    this.status = status;
    this.code = code;
    this.currentResourceVersion = options.currentResourceVersion ?? null;
    this.fieldErrors = Object.freeze([...(options.fieldErrors ?? [])]);
  }
}

interface SessionEnvelope {
  readonly session: LocalSession;
  readonly csrfToken: string;
  readonly csrfHeaderName: "X-CSRF-Token";
}

interface SafeProblem {
  readonly code: string;
  readonly currentResourceVersion: number | string | null;
  readonly fieldErrors: readonly InstanceConfigurationFieldError[];
}

type JsonObject = Record<string, unknown>;

let currentSession: SessionEnvelope | undefined;

class ContractViolation extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ContractViolation";
  }
}

function asRecord(value: unknown, label: string): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ContractViolation(`${label} must be an object`);
  }
  return value as JsonObject;
}

function assertExactFields(
  value: JsonObject,
  expected: ReadonlySet<string>,
  label: string,
): void {
  const fields = Object.keys(value);
  if (
    fields.length !== expected.size ||
    fields.some((field) => !expected.has(field))
  ) {
    throw new ContractViolation(`${label} fields are unsupported`);
  }
}

function assertAllowedAndRequiredFields(
  value: JsonObject,
  required: ReadonlySet<string>,
  optional: ReadonlySet<string>,
  label: string,
): void {
  const fields = Object.keys(value);
  if (
    [...required].some((field) => !(field in value)) ||
    fields.some((field) => !required.has(field) && !optional.has(field))
  ) {
    throw new ContractViolation(`${label} fields are unsupported`);
  }
}

function hasOwn(value: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function asString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new ContractViolation(`${label} must be a non-empty string`);
  }
  return value;
}

function asTrimmedString(
  value: unknown,
  label: string,
  maximum: number,
): string {
  const result = asString(value, label);
  if (result.trim() !== result || Array.from(result).length > maximum) {
    throw new ContractViolation(`${label} must be trimmed and bounded`);
  }
  return result;
}

function asBoolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") {
    throw new ContractViolation(`${label} must be a boolean`);
  }
  return value;
}

function asInteger(
  value: unknown,
  label: string,
  minimum: number,
  maximum: number,
): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < minimum ||
    value > maximum
  ) {
    throw new ContractViolation(`${label} must be a bounded integer`);
  }
  return value;
}

function asNullableString(value: unknown, label: string): string | null {
  return value === null ? null : asString(value, label);
}

function assertLiteral(value: unknown, expected: unknown, label: string): void {
  if (value !== expected) {
    throw new ContractViolation(`${label} is unsupported`);
  }
}

function assertStringArray(
  value: unknown,
  expected: readonly string[],
  label: string,
): void {
  if (
    !Array.isArray(value) ||
    value.length !== expected.length ||
    value.some((item, index) => item !== expected[index])
  ) {
    throw new ContractViolation(`${label} is unsupported`);
  }
}

function validateIdentifier(
  value: unknown,
  pattern: RegExp,
  label: string,
): string {
  const identifier = asString(value, label);
  if (!pattern.test(identifier)) {
    throw new ContractViolation(`${label} is invalid`);
  }
  return identifier;
}

function validateSortedAuthorities(
  value: unknown,
  label: string,
  minimum: number,
  maximum: number,
): readonly string[] {
  if (
    !Array.isArray(value) ||
    value.length < minimum ||
    value.length > maximum
  ) {
    throw new ContractViolation(`${label} must be a bounded array`);
  }
  const authorities = value.map((item, index) => {
    const authority = asString(item, `${label}[${String(index)}]`);
    if (!AUTHORITY_PATTERN.test(authority)) {
      throw new ContractViolation(`${label} contains an invalid authority`);
    }
    return authority;
  });
  if (
    new Set(authorities).size !== authorities.length ||
    authorities.some(
      (authority, index) =>
        index > 0 && (authorities[index - 1] ?? authority) >= authority,
    )
  ) {
    throw new ContractViolation(`${label} must be unique and sorted`);
  }
  return Object.freeze(authorities);
}

function normalizeLocalSessionEnvelope(value: unknown): SessionEnvelope {
  const record = asRecord(value, "session");
  assertExactFields(record, SESSION_FIELDS, "session");
  const actorId = asString(record.actorId, "session.actorId");
  if (!AUTHORITY_PATTERN.test(actorId)) {
    throw new ContractViolation("session.actorId is invalid");
  }
  const roles = validateSortedAuthorities(record.roles, "session.roles", 1, 64);
  const scopes = validateSortedAuthorities(
    record.scopes,
    "session.scopes",
    0,
    128,
  );
  assertLiteral(record.authMode, "local", "session.authMode");
  if (
    record.environment !== "local" &&
    record.environment !== "test" &&
    record.environment !== "production"
  ) {
    throw new ContractViolation("session.environment is unsupported");
  }
  if (record.modelMode !== "mock" && record.modelMode !== "real") {
    throw new ContractViolation("session.modelMode is unsupported");
  }
  const connectorMode = asTrimmedString(
    record.connectorMode,
    "session.connectorMode",
    64,
  );
  const networkPermission = asBoolean(
    record.networkPermission,
    "session.networkPermission",
  );
  assertLiteral(
    record.warning,
    "Local identity — not production authentication",
    "session.warning",
  );
  const csrfToken = asString(record.csrfToken, "session.csrfToken");
  if (!CSRF_TOKEN_PATTERN.test(csrfToken)) {
    throw new ContractViolation("session.csrfToken is invalid");
  }
  assertLiteral(
    record.csrfHeaderName,
    "X-CSRF-Token",
    "session.csrfHeaderName",
  );
  const session: LocalSession = Object.freeze({
    actorId,
    roles,
    scopes,
    authMode: "local",
    environment: record.environment,
    modelMode: record.modelMode,
    connectorMode,
    networkPermission,
    warning: "Local identity — not production authentication",
  });
  return Object.freeze({
    session,
    csrfToken,
    csrfHeaderName: "X-CSRF-Token",
  });
}

export function normalizeLocalSession(value: unknown): LocalSession {
  return normalizeLocalSessionEnvelope(value).session;
}

function assertNullSchema(value: unknown, label: string): void {
  const record = asRecord(value, label);
  assertExactFields(record, NULL_SCHEMA_FIELDS, label);
  assertLiteral(record.type, "null", `${label}.type`);
}

function assertBooleanSchema(value: unknown, label: string): void {
  const record = asRecord(value, label);
  assertExactFields(record, BOOLEAN_SCHEMA_FIELDS, label);
  assertLiteral(record.type, "boolean", `${label}.type`);
}

function assertBooleanDefaultSchema(value: unknown, label: string): void {
  const record = asRecord(value, label);
  assertExactFields(record, BOOLEAN_DEFAULT_SCHEMA_FIELDS, label);
  assertLiteral(record.type, "boolean", `${label}.type`);
  assertLiteral(record.default, true, `${label}.default`);
}

function assertConstSchema(
  value: unknown,
  expected: unknown,
  label: string,
): void {
  const record = asRecord(value, label);
  assertExactFields(record, CONST_SCHEMA_FIELDS, label);
  assertLiteral(record.const, expected, `${label}.const`);
}

function assertConstDefaultSchema(
  value: unknown,
  expected: unknown,
  label: string,
): void {
  const record = asRecord(value, label);
  assertExactFields(record, CONST_DEFAULT_SCHEMA_FIELDS, label);
  assertLiteral(record.const, expected, `${label}.const`);
  assertLiteral(record.default, true, `${label}.default`);
}

function assertStringSchema(value: unknown, label: string): void {
  const record = asRecord(value, label);
  assertExactFields(record, STRING_SCHEMA_FIELDS, label);
  assertLiteral(record.type, "string", `${label}.type`);
  assertLiteral(record.minLength, 1, `${label}.minLength`);
  assertLiteral(record.maxLength, 100, `${label}.maxLength`);
}

function assertEventSourceSchema(value: unknown, label: string): void {
  const record = asRecord(value, label);
  assertExactFields(record, EVENT_SOURCE_SCHEMA_FIELDS, label);
  assertLiteral(record.type, "string", `${label}.type`);
  assertLiteral(record.minLength, 1, `${label}.minLength`);
  assertLiteral(record.maxLength, 100, `${label}.maxLength`);
  assertLiteral(
    record.pattern,
    "^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$",
    `${label}.pattern`,
  );
}

function assertMisfireSchema(value: unknown, label: string): void {
  const record = asRecord(value, label);
  assertExactFields(record, ENUM_SCHEMA_FIELDS, label);
  assertStringArray(record.enum, ["skip", "run_once"], `${label}.enum`);
}

function assertGraceSchema(value: unknown, label: string): void {
  const record = asRecord(value, label);
  assertExactFields(record, INTEGER_SCHEMA_FIELDS, label);
  assertLiteral(record.type, "integer", `${label}.type`);
  assertLiteral(record.minimum, 0, `${label}.minimum`);
  assertLiteral(record.maximum, 86_400, `${label}.maximum`);
}

function triggerVariantBase(
  value: unknown,
  label: string,
): { readonly record: JsonObject; readonly properties: JsonObject } {
  const record = asRecord(value, label);
  assertExactFields(
    record,
    new Set(["type", "additionalProperties", "required", "properties"]),
    label,
  );
  assertLiteral(record.type, "object", `${label}.type`);
  assertLiteral(
    record.additionalProperties,
    false,
    `${label}.additionalProperties`,
  );
  const properties = asRecord(record.properties, `${label}.properties`);
  assertExactFields(properties, TRIGGER_PROPERTY_FIELDS, `${label}.properties`);
  return { record, properties };
}

function assertManualTriggerSchema(value: unknown, label: string): void {
  const { record, properties } = triggerVariantBase(value, label);
  assertStringArray(record.required, ["type"], `${label}.required`);
  assertConstSchema(properties.type, "manual", `${label}.properties.type`);
  assertBooleanDefaultSchema(properties.enabled, `${label}.properties.enabled`);
  for (const field of [
    "eventSource",
    "cron",
    "timezone",
    "misfirePolicy",
    "misfireGraceSeconds",
  ]) {
    assertNullSchema(properties[field], `${label}.properties.${field}`);
  }
}

function assertWebhookTriggerSchema(value: unknown, label: string): void {
  const { record, properties } = triggerVariantBase(value, label);
  assertStringArray(
    record.required,
    ["type", "eventSource"],
    `${label}.required`,
  );
  assertConstSchema(properties.type, "webhook", `${label}.properties.type`);
  assertBooleanDefaultSchema(properties.enabled, `${label}.properties.enabled`);
  assertEventSourceSchema(
    properties.eventSource,
    `${label}.properties.eventSource`,
  );
  for (const field of [
    "cron",
    "timezone",
    "misfirePolicy",
    "misfireGraceSeconds",
  ]) {
    assertNullSchema(properties[field], `${label}.properties.${field}`);
  }
}

function assertDisabledScheduleTriggerSchema(
  value: unknown,
  label: string,
): void {
  const { record, properties } = triggerVariantBase(value, label);
  assertStringArray(record.required, ["type", "enabled"], `${label}.required`);
  assertConstSchema(properties.type, "schedule", `${label}.properties.type`);
  assertConstSchema(properties.enabled, false, `${label}.properties.enabled`);
  for (const field of [
    "eventSource",
    "cron",
    "timezone",
    "misfirePolicy",
    "misfireGraceSeconds",
  ]) {
    assertNullSchema(properties[field], `${label}.properties.${field}`);
  }
}

function assertEnabledScheduleTriggerSchema(
  value: unknown,
  label: string,
): void {
  const { record, properties } = triggerVariantBase(value, label);
  assertStringArray(
    record.required,
    ["type", "cron", "timezone", "misfirePolicy", "misfireGraceSeconds"],
    `${label}.required`,
  );
  assertConstSchema(properties.type, "schedule", `${label}.properties.type`);
  assertConstDefaultSchema(
    properties.enabled,
    true,
    `${label}.properties.enabled`,
  );
  assertNullSchema(properties.eventSource, `${label}.properties.eventSource`);
  assertStringSchema(properties.cron, `${label}.properties.cron`);
  assertStringSchema(properties.timezone, `${label}.properties.timezone`);
  assertMisfireSchema(
    properties.misfirePolicy,
    `${label}.properties.misfirePolicy`,
  );
  assertGraceSchema(
    properties.misfireGraceSeconds,
    `${label}.properties.misfireGraceSeconds`,
  );
}

function normalizeTriggerSchemas(
  value: unknown,
): readonly SupportedTriggerType[] {
  const items = asRecord(
    value,
    "configurationSchema.properties.triggerBindings.items",
  );
  assertExactFields(items, new Set(["oneOf"]), "triggerBindings.items");
  if (!Array.isArray(items.oneOf) || items.oneOf.length === 0) {
    throw new ContractViolation("triggerBindings.items.oneOf is invalid");
  }
  const kinds: SupportedTriggerType[] = [];
  let sawDisabledSchedule = false;
  let sawEnabledSchedule = false;
  for (const [index, variant] of items.oneOf.entries()) {
    const label = `triggerBindings.items.oneOf[${String(index)}]`;
    const variantRecord = asRecord(variant, label);
    const properties = asRecord(
      variantRecord.properties,
      `${label}.properties`,
    );
    const typeSchema = asRecord(properties.type, `${label}.properties.type`);
    if (typeSchema.const === "manual") {
      assertManualTriggerSchema(variant, label);
      if (kinds.includes("manual")) {
        throw new ContractViolation("manual trigger schema is duplicated");
      }
      kinds.push("manual");
    } else if (typeSchema.const === "webhook") {
      assertWebhookTriggerSchema(variant, label);
      if (kinds.includes("webhook")) {
        throw new ContractViolation("webhook trigger schema is duplicated");
      }
      kinds.push("webhook");
    } else if (typeSchema.const === "schedule") {
      const enabledSchema = asRecord(
        properties.enabled,
        `${label}.properties.enabled`,
      );
      if (enabledSchema.const === false) {
        assertDisabledScheduleTriggerSchema(variant, label);
        if (sawDisabledSchedule || sawEnabledSchedule) {
          throw new ContractViolation(
            "disabled schedule schema is duplicated or out of order",
          );
        }
        sawDisabledSchedule = true;
      } else if (enabledSchema.const === true) {
        assertEnabledScheduleTriggerSchema(variant, label);
        if (!sawDisabledSchedule || sawEnabledSchedule) {
          throw new ContractViolation(
            "enabled schedule schema is duplicated or out of order",
          );
        }
        sawEnabledSchedule = true;
      } else {
        throw new ContractViolation("schedule trigger schema is invalid");
      }
      if (!kinds.includes("schedule")) kinds.push("schedule");
    } else {
      throw new ContractViolation("trigger schema kind is unsupported");
    }
  }
  if (sawDisabledSchedule !== sawEnabledSchedule) {
    throw new ContractViolation("schedule trigger variants are incomplete");
  }
  const canonical = ["manual", "webhook", "schedule"].filter((kind) =>
    kinds.includes(kind as SupportedTriggerType),
  );
  if (canonical.some((kind, index) => kind !== kinds[index])) {
    throw new ContractViolation("trigger schema order is invalid");
  }
  return Object.freeze(kinds);
}

function assertScheduleObjectSchema(value: unknown, label: string): void {
  const record = asRecord(value, label);
  assertExactFields(
    record,
    new Set(["type", "additionalProperties", "required", "properties"]),
    label,
  );
  assertLiteral(record.type, "object", `${label}.type`);
  assertLiteral(
    record.additionalProperties,
    false,
    `${label}.additionalProperties`,
  );
  assertStringArray(
    record.required,
    ["cron", "timezone", "misfirePolicy", "misfireGraceSeconds"],
    `${label}.required`,
  );
  const properties = asRecord(record.properties, `${label}.properties`);
  assertExactFields(properties, SCHEDULE_FIELDS, `${label}.properties`);
  assertStringSchema(properties.cron, `${label}.properties.cron`);
  assertStringSchema(properties.timezone, `${label}.properties.timezone`);
  assertMisfireSchema(
    properties.misfirePolicy,
    `${label}.properties.misfirePolicy`,
  );
  assertGraceSchema(
    properties.misfireGraceSeconds,
    `${label}.properties.misfireGraceSeconds`,
  );
}

function normalizeScheduleSupported(value: unknown): boolean {
  const record = asRecord(value, "configurationSchema.properties.schedule");
  if (hasOwn(record, "type")) {
    assertNullSchema(record, "configurationSchema.properties.schedule");
    return false;
  }
  assertExactFields(
    record,
    new Set(["oneOf"]),
    "configurationSchema.properties.schedule",
  );
  if (!Array.isArray(record.oneOf) || record.oneOf.length !== 2) {
    throw new ContractViolation("configuration schedule schema is invalid");
  }
  assertNullSchema(record.oneOf[0], "configuration schedule null variant");
  assertScheduleObjectSchema(
    record.oneOf[1],
    "configuration schedule object variant",
  );
  return true;
}

function normalizeConnectorSchemas(
  value: unknown,
): readonly ConnectorFamilyOption[] {
  const record = asRecord(
    value,
    "configurationSchema.properties.connectorBindings",
  );
  assertExactFields(
    record,
    new Set(["type", "maxProperties", "properties", "additionalProperties"]),
    "configurationSchema.properties.connectorBindings",
  );
  assertLiteral(record.type, "object", "connectorBindings.type");
  assertLiteral(record.maxProperties, 16, "connectorBindings.maxProperties");
  assertLiteral(
    record.additionalProperties,
    false,
    "connectorBindings.additionalProperties",
  );
  const properties = asRecord(
    record.properties,
    "connectorBindings.properties",
  );
  const families = Object.keys(properties);
  if (
    families.some(
      (family) => family.length > 100 || !CONNECTOR_FAMILY_PATTERN.test(family),
    ) ||
    families.some(
      (family, index) => index > 0 && (families[index - 1] ?? family) >= family,
    )
  ) {
    throw new ContractViolation("connector family schema order is invalid");
  }
  return Object.freeze(
    families.map((family) => {
      const label = `connectorBindings.properties.${family}`;
      const familySchema = asRecord(properties[family], label);
      assertExactFields(
        familySchema,
        new Set(["type", "additionalProperties", "required", "properties"]),
        label,
      );
      assertLiteral(familySchema.type, "object", `${label}.type`);
      assertLiteral(
        familySchema.additionalProperties,
        false,
        `${label}.additionalProperties`,
      );
      assertStringArray(
        familySchema.required,
        ["connectorFamily", "bindingId"],
        `${label}.required`,
      );
      const fields = asRecord(familySchema.properties, `${label}.properties`);
      assertExactFields(fields, CONNECTOR_VIEW_FIELDS, `${label}.properties`);
      assertConstSchema(
        fields.connectorFamily,
        family,
        `${label}.properties.connectorFamily`,
      );
      const bindingIdSchema = asRecord(
        fields.bindingId,
        `${label}.properties.bindingId`,
      );
      assertExactFields(
        bindingIdSchema,
        ENUM_SCHEMA_FIELDS,
        `${label}.properties.bindingId`,
      );
      if (
        !Array.isArray(bindingIdSchema.enum) ||
        bindingIdSchema.enum.length === 0
      ) {
        throw new ContractViolation(`${label}.bindingId enum is invalid`);
      }
      const bindingIds = bindingIdSchema.enum.map((bindingId, index) => {
        const result = asString(
          bindingId,
          `${label}.bindingId[${String(index)}]`,
        );
        if (result.length > 120 || !CONNECTOR_BINDING_ID_PATTERN.test(result)) {
          throw new ContractViolation(`${label}.bindingId is invalid`);
        }
        return result;
      });
      if (
        new Set(bindingIds).size !== bindingIds.length ||
        bindingIds.some(
          (bindingId, index) =>
            index > 0 && (bindingIds[index - 1] ?? bindingId) >= bindingId,
        )
      ) {
        throw new ContractViolation(
          `${label}.bindingIds must be unique and sorted`,
        );
      }
      assertBooleanDefaultSchema(fields.enabled, `${label}.properties.enabled`);
      return Object.freeze({
        connectorFamily: family,
        bindingIds: Object.freeze(bindingIds),
      });
    }),
  );
}

export function normalizeInstanceConfigurationSchema(
  value: unknown,
  expected: { readonly instanceId: string; readonly templateId: string },
): InstanceConfigurationSchema {
  const expectedInstanceId = validateIdentifier(
    expected.instanceId,
    INSTANCE_ID_PATTERN,
    "expected instance ID",
  );
  const expectedTemplateId = validateIdentifier(
    expected.templateId,
    TEMPLATE_ID_PATTERN,
    "expected template ID",
  );
  const response = asRecord(value, "configuration schema response");
  assertExactFields(
    response,
    CONFIGURATION_SCHEMA_RESPONSE_FIELDS,
    "configuration schema response",
  );
  assertLiteral(
    response.projectionVersion,
    "instance-configuration-schema-v1",
    "configuration schema projectionVersion",
  );
  assertLiteral(
    response.instanceId,
    expectedInstanceId,
    "configuration schema instanceId",
  );
  assertLiteral(
    response.templateId,
    expectedTemplateId,
    "configuration schema templateId",
  );
  const schema = asRecord(response.configurationSchema, "configurationSchema");
  assertExactFields(schema, JSON_SCHEMA_FIELDS, "configurationSchema");
  assertLiteral(
    schema.$schema,
    "https://json-schema.org/draft/2020-12/schema",
    "configurationSchema.$schema",
  );
  assertLiteral(
    schema.$id,
    `urn:marketing-agents:instance-configuration:${expectedInstanceId}:v1`,
    "configurationSchema.$id",
  );
  assertLiteral(schema.type, "object", "configurationSchema.type");
  assertLiteral(
    schema.description,
    CONFIGURATION_SCHEMA_DESCRIPTION,
    "configurationSchema.description",
  );
  assertLiteral(
    schema.additionalProperties,
    false,
    "configurationSchema.additionalProperties",
  );
  assertLiteral(schema.minProperties, 1, "configurationSchema.minProperties");
  const properties = asRecord(
    schema.properties,
    "configurationSchema.properties",
  );
  assertExactFields(
    properties,
    CONFIGURATION_PROPERTY_FIELDS,
    "configurationSchema.properties",
  );
  assertBooleanSchema(
    properties.enabled,
    "configurationSchema.properties.enabled",
  );

  const variantLabel = asRecord(
    properties.variantLabel,
    "configurationSchema.properties.variantLabel",
  );
  assertExactFields(
    variantLabel,
    new Set(["type", "minLength", "maxLength"]),
    "configurationSchema.properties.variantLabel",
  );
  assertStringArray(
    variantLabel.type,
    ["string", "null"],
    "configurationSchema.properties.variantLabel.type",
  );
  assertLiteral(
    variantLabel.minLength,
    1,
    "configurationSchema.properties.variantLabel.minLength",
  );
  assertLiteral(
    variantLabel.maxLength,
    100,
    "configurationSchema.properties.variantLabel.maxLength",
  );

  const triggerBindings = asRecord(
    properties.triggerBindings,
    "configurationSchema.properties.triggerBindings",
  );
  assertExactFields(
    triggerBindings,
    new Set(["type", "maxItems", "items"]),
    "configurationSchema.properties.triggerBindings",
  );
  assertLiteral(triggerBindings.type, "array", "triggerBindings.type");
  assertLiteral(triggerBindings.maxItems, 16, "triggerBindings.maxItems");
  const supportedTriggerTypes = normalizeTriggerSchemas(triggerBindings.items);
  const connectorFamilies = normalizeConnectorSchemas(
    properties.connectorBindings,
  );
  const scheduleSupported = normalizeScheduleSupported(properties.schedule);
  if (supportedTriggerTypes.includes("schedule") !== scheduleSupported) {
    throw new ContractViolation(
      "schedule support does not match the supported trigger schemas",
    );
  }
  return Object.freeze({
    projectionVersion: "instance-configuration-schema-v1",
    instanceId: expectedInstanceId,
    templateId: expectedTemplateId,
    supportedTriggerTypes,
    connectorFamilies,
    scheduleSupported,
    variantLabelMaxLength: 100,
    maxTriggerBindings: 16,
    maxConnectorBindings: 16,
  });
}

function normalizeMisfirePolicy(value: unknown, label: string): MisfirePolicy {
  if (value !== "skip" && value !== "run_once") {
    throw new ContractViolation(`${label} is unsupported`);
  }
  return value;
}

function normalizeScheduleInput(
  value: unknown,
  label: string,
): InstanceSchedule {
  const record = asRecord(value, label);
  assertExactFields(record, SCHEDULE_FIELDS, label);
  return Object.freeze({
    cron: asTrimmedString(record.cron, `${label}.cron`, 100),
    timezone: asTrimmedString(record.timezone, `${label}.timezone`, 100),
    misfirePolicy: normalizeMisfirePolicy(
      record.misfirePolicy,
      `${label}.misfirePolicy`,
    ),
    misfireGraceSeconds: asInteger(
      record.misfireGraceSeconds,
      `${label}.misfireGraceSeconds`,
      0,
      86_400,
    ),
  });
}

function normalizeTriggerPatch(
  value: unknown,
  index: number,
  schema?: InstanceConfigurationSchema,
): JsonObject {
  const label = `patch.triggerBindings[${String(index)}]`;
  const record = asRecord(value, label);
  const type = asString(record.type, `${label}.type`);
  if (
    schema !== undefined &&
    !schema.supportedTriggerTypes.includes(type as SupportedTriggerType)
  ) {
    throw new ContractViolation(
      `${label}.type is unsupported for this template`,
    );
  }
  if (type === "manual") {
    const allowed = new Set(["type", "enabled"]);
    if (Object.keys(record).some((field) => !allowed.has(field))) {
      throw new ContractViolation(`${label} fields are unsupported`);
    }
    const enabled = hasOwn(record, "enabled")
      ? asBoolean(record.enabled, `${label}.enabled`)
      : true;
    return Object.freeze({ type: "manual", enabled });
  }
  if (type === "webhook") {
    assertAllowedAndRequiredFields(
      record,
      new Set(["type", "eventSource"]),
      new Set(["enabled"]),
      label,
    );
    const eventSource = asString(record.eventSource, `${label}.eventSource`);
    if (!EVENT_SOURCE_PATTERN.test(eventSource)) {
      throw new ContractViolation(`${label}.eventSource is invalid`);
    }
    const enabled = hasOwn(record, "enabled")
      ? asBoolean(record.enabled, `${label}.enabled`)
      : true;
    return Object.freeze({ type: "webhook", enabled, eventSource });
  }
  if (type === "schedule") {
    const enabled = hasOwn(record, "enabled")
      ? asBoolean(record.enabled, `${label}.enabled`)
      : true;
    if (!enabled) {
      assertExactFields(record, new Set(["type", "enabled"]), label);
      return Object.freeze({ type: "schedule", enabled: false });
    }
    assertAllowedAndRequiredFields(
      record,
      new Set([
        "type",
        "cron",
        "timezone",
        "misfirePolicy",
        "misfireGraceSeconds",
      ]),
      new Set(["enabled"]),
      label,
    );
    const schedule = normalizeScheduleInput(
      {
        cron: record.cron,
        timezone: record.timezone,
        misfirePolicy: record.misfirePolicy,
        misfireGraceSeconds: record.misfireGraceSeconds,
      },
      label,
    );
    return Object.freeze({ type: "schedule", enabled: true, ...schedule });
  }
  throw new ContractViolation(`${label}.type is unsupported`);
}

function normalizeConnectorPatch(
  value: unknown,
  family: string,
  schema?: InstanceConfigurationSchema,
): JsonObject {
  const label = `patch.connectorBindings.${family}`;
  const record = asRecord(value, label);
  assertAllowedAndRequiredFields(
    record,
    new Set(["connectorFamily", "bindingId"]),
    new Set(["enabled"]),
    label,
  );
  if (family.length > 100 || !CONNECTOR_FAMILY_PATTERN.test(family)) {
    throw new ContractViolation(`${label} key is invalid`);
  }
  assertLiteral(record.connectorFamily, family, `${label}.connectorFamily`);
  const bindingId = asString(record.bindingId, `${label}.bindingId`);
  if (bindingId.length > 120 || !CONNECTOR_BINDING_ID_PATTERN.test(bindingId)) {
    throw new ContractViolation(`${label}.bindingId is invalid`);
  }
  if (schema !== undefined) {
    const familyOption = schema.connectorFamilies.find(
      (option) => option.connectorFamily === family,
    );
    if (!familyOption?.bindingIds.includes(bindingId)) {
      throw new ContractViolation(`${label}.bindingId is not registered`);
    }
  }
  const enabled = hasOwn(record, "enabled")
    ? asBoolean(record.enabled, `${label}.enabled`)
    : true;
  return Object.freeze({ connectorFamily: family, bindingId, enabled });
}

function schedulesEqual(
  left: InstanceSchedule,
  right: InstanceSchedule,
): boolean {
  return (
    left.cron === right.cron &&
    left.timezone === right.timezone &&
    left.misfirePolicy === right.misfirePolicy &&
    left.misfireGraceSeconds === right.misfireGraceSeconds
  );
}

export function serializeInstanceConfigurationPatch(
  value: InstanceConfigurationPatch,
  schema?: InstanceConfigurationSchema,
): Readonly<JsonObject> {
  let record: JsonObject;
  try {
    record = asRecord(value, "configuration patch");
  } catch (error) {
    if (!(error instanceof ContractViolation)) throw error;
    throw new InstanceConfigurationRequestError(
      0,
      "invalid_configuration_patch",
      "The instance configuration changes are invalid.",
    );
  }
  const fields = Object.keys(record);
  if (fields.length === 0 || fields.some((field) => !PATCH_FIELDS.has(field))) {
    throw new InstanceConfigurationRequestError(
      0,
      "invalid_configuration_patch",
      "The instance configuration changes are invalid.",
    );
  }
  try {
    const result: JsonObject = {};
    if (hasOwn(record, "enabled")) {
      result.enabled = asBoolean(record.enabled, "patch.enabled");
    }
    if (hasOwn(record, "variantLabel")) {
      if (record.variantLabel === null) {
        result.variantLabel = null;
      } else {
        result.variantLabel = asTrimmedString(
          record.variantLabel,
          "patch.variantLabel",
          schema?.variantLabelMaxLength ?? 100,
        ).normalize("NFC");
      }
    }
    let triggers: readonly JsonObject[] | undefined;
    if (hasOwn(record, "triggerBindings")) {
      if (
        !Array.isArray(record.triggerBindings) ||
        record.triggerBindings.length > (schema?.maxTriggerBindings ?? 16)
      ) {
        throw new ContractViolation("patch.triggerBindings is invalid");
      }
      triggers = Object.freeze(
        record.triggerBindings.map((trigger, index) =>
          normalizeTriggerPatch(trigger, index, schema),
        ),
      );
      const kinds = triggers.map((trigger) => trigger.type);
      if (new Set(kinds).size !== kinds.length) {
        throw new ContractViolation("patch trigger kinds must be unique");
      }
      result.triggerBindings = triggers;
    }
    if (hasOwn(record, "connectorBindings")) {
      const connectors = asRecord(
        record.connectorBindings,
        "patch.connectorBindings",
      );
      const families = Object.keys(connectors);
      if (families.length > (schema?.maxConnectorBindings ?? 16)) {
        throw new ContractViolation("patch.connectorBindings is too large");
      }
      const normalized: JsonObject = {};
      for (const family of families.sort()) {
        normalized[family] = normalizeConnectorPatch(
          connectors[family],
          family,
          schema,
        );
      }
      result.connectorBindings = Object.freeze(normalized);
    }
    let schedule: InstanceSchedule | null | undefined;
    if (hasOwn(record, "schedule")) {
      schedule =
        record.schedule === null
          ? null
          : normalizeScheduleInput(record.schedule, "patch.schedule");
      if (
        schedule !== null &&
        schema !== undefined &&
        !schema.scheduleSupported
      ) {
        throw new ContractViolation(
          "patch.schedule is unsupported for this template",
        );
      }
      result.schedule = schedule;
    }
    if (triggers !== undefined && schedule !== undefined) {
      const enabledSchedule = triggers.find(
        (trigger) => trigger.type === "schedule" && trigger.enabled === true,
      );
      if (
        (schedule === null && enabledSchedule !== undefined) ||
        (schedule !== null &&
          (enabledSchedule === undefined ||
            !schedulesEqual(
              schedule,
              enabledSchedule as unknown as InstanceSchedule,
            )))
      ) {
        throw new ContractViolation(
          "patch schedule and schedule trigger must exactly match",
        );
      }
    }
    return Object.freeze(result);
  } catch (error) {
    if (error instanceof InstanceConfigurationRequestError) throw error;
    if (!(error instanceof ContractViolation)) throw error;
    throw new InstanceConfigurationRequestError(
      0,
      "invalid_configuration_patch",
      "The instance configuration changes are invalid.",
    );
  }
}

function normalizeTriggerView(
  value: unknown,
  index: number,
): InstanceTriggerBinding {
  const label = `configuration.triggerBindings[${String(index)}]`;
  const record = asRecord(value, label);
  assertExactFields(record, TRIGGER_VIEW_FIELDS, label);
  const type = asString(record.type, `${label}.type`);
  if (type !== "manual" && type !== "webhook" && type !== "schedule") {
    throw new ContractViolation(`${label}.type is unsupported`);
  }
  const enabled = asBoolean(record.enabled, `${label}.enabled`);
  const eventSource = asNullableString(
    record.eventSource,
    `${label}.eventSource`,
  );
  const cron = asNullableString(record.cron, `${label}.cron`);
  const timezone = asNullableString(record.timezone, `${label}.timezone`);
  const misfirePolicy =
    record.misfirePolicy === null
      ? null
      : normalizeMisfirePolicy(record.misfirePolicy, `${label}.misfirePolicy`);
  const misfireGraceSeconds =
    record.misfireGraceSeconds === null
      ? null
      : asInteger(
          record.misfireGraceSeconds,
          `${label}.misfireGraceSeconds`,
          0,
          86_400,
        );
  if (type === "manual") {
    if (
      eventSource !== null ||
      cron !== null ||
      timezone !== null ||
      misfirePolicy !== null ||
      misfireGraceSeconds !== null
    ) {
      throw new ContractViolation(`${label} manual fields are incoherent`);
    }
  } else if (type === "webhook") {
    if (
      eventSource === null ||
      !EVENT_SOURCE_PATTERN.test(eventSource) ||
      cron !== null ||
      timezone !== null ||
      misfirePolicy !== null ||
      misfireGraceSeconds !== null
    ) {
      throw new ContractViolation(`${label} webhook fields are incoherent`);
    }
  } else if (
    eventSource !== null ||
    (enabled &&
      (cron === null ||
        timezone === null ||
        misfirePolicy === null ||
        misfireGraceSeconds === null)) ||
    (!enabled &&
      (cron !== null ||
        timezone !== null ||
        misfirePolicy !== null ||
        misfireGraceSeconds !== null))
  ) {
    throw new ContractViolation(`${label} schedule fields are incoherent`);
  }
  return Object.freeze({
    type,
    enabled,
    eventSource,
    cron,
    timezone,
    misfirePolicy,
    misfireGraceSeconds,
  });
}

function normalizeConnectorViews(
  value: unknown,
): Readonly<Record<string, InstanceConnectorBinding>> {
  const record = asRecord(value, "configuration.connectorBindings");
  const families = Object.keys(record);
  if (families.length > 16) {
    throw new ContractViolation("configuration.connectorBindings is too large");
  }
  const result: Record<string, InstanceConnectorBinding> = {};
  for (const family of families) {
    if (family.length > 100 || !CONNECTOR_FAMILY_PATTERN.test(family)) {
      throw new ContractViolation("configuration connector family is invalid");
    }
    const binding = asRecord(
      record[family],
      `configuration.connectorBindings.${family}`,
    );
    assertExactFields(
      binding,
      CONNECTOR_VIEW_FIELDS,
      `configuration.connectorBindings.${family}`,
    );
    assertLiteral(
      binding.connectorFamily,
      family,
      `configuration.connectorBindings.${family}.connectorFamily`,
    );
    const bindingId = asString(
      binding.bindingId,
      `configuration.connectorBindings.${family}.bindingId`,
    );
    if (
      bindingId.length > 120 ||
      !CONNECTOR_BINDING_ID_PATTERN.test(bindingId)
    ) {
      throw new ContractViolation("configuration binding ID is invalid");
    }
    result[family] = Object.freeze({
      connectorFamily: family,
      bindingId,
      enabled: asBoolean(
        binding.enabled,
        `configuration.connectorBindings.${family}.enabled`,
      ),
    });
  }
  return Object.freeze(result);
}

function normalizeConfigurationResult(
  value: unknown,
  expectedInstanceId: string,
  etag: string | null,
): InstanceConfigurationResult {
  const response = asRecord(value, "configuration response");
  assertExactFields(
    response,
    CONFIGURATION_RESPONSE_FIELDS,
    "configuration response",
  );
  assertLiteral(
    response.projectionVersion,
    "instance-configuration-v1",
    "configuration response projectionVersion",
  );
  const configuration = asRecord(response.configuration, "configuration");
  assertExactFields(configuration, CONFIGURATION_FIELDS, "configuration");
  assertLiteral(
    configuration.instanceId,
    expectedInstanceId,
    "configuration.instanceId",
  );
  const variantLabel =
    configuration.variantLabel === null
      ? null
      : asTrimmedString(
          configuration.variantLabel,
          "configuration.variantLabel",
          100,
        );
  if (variantLabel !== null && variantLabel.normalize("NFC") !== variantLabel) {
    throw new ContractViolation("configuration.variantLabel is not normalized");
  }
  if (
    !Array.isArray(configuration.triggerBindings) ||
    configuration.triggerBindings.length > 16
  ) {
    throw new ContractViolation("configuration.triggerBindings is invalid");
  }
  const triggerBindings = Object.freeze(
    configuration.triggerBindings.map(normalizeTriggerView),
  );
  const triggerKinds = triggerBindings.map((trigger) => trigger.type);
  if (new Set(triggerKinds).size !== triggerKinds.length) {
    throw new ContractViolation("configuration trigger kinds are duplicated");
  }
  const connectorBindings = normalizeConnectorViews(
    configuration.connectorBindings,
  );
  const schedule =
    configuration.schedule === null
      ? null
      : normalizeScheduleInput(
          configuration.schedule,
          "configuration.schedule",
        );
  const enabledSchedule = triggerBindings.find(
    (trigger) => trigger.type === "schedule" && trigger.enabled,
  );
  if (
    (schedule === null && enabledSchedule !== undefined) ||
    (schedule !== null &&
      (enabledSchedule?.cron !== schedule.cron ||
        enabledSchedule.timezone !== schedule.timezone ||
        enabledSchedule.misfirePolicy !== schedule.misfirePolicy ||
        enabledSchedule.misfireGraceSeconds !== schedule.misfireGraceSeconds))
  ) {
    throw new ContractViolation(
      "configuration schedule and schedule trigger are incoherent",
    );
  }
  const configurationRevision = asInteger(
    configuration.configurationRevision,
    "configuration.configurationRevision",
    1,
    Number.MAX_SAFE_INTEGER,
  );
  const expectedEtag = `"instance-configuration-v1-${String(configurationRevision)}"`;
  if (etag !== expectedEtag) {
    throw new ContractViolation("configuration response ETag is invalid");
  }
  return Object.freeze({
    projectionVersion: "instance-configuration-v1",
    configuration: Object.freeze({
      instanceId: expectedInstanceId,
      enabled: asBoolean(configuration.enabled, "configuration.enabled"),
      variantLabel,
      triggerBindings,
      connectorBindings,
      schedule,
      configurationRevision,
    }),
    configurationEtag: expectedEtag,
  });
}

function safeString(value: unknown, maximum: number): string | null {
  return typeof value === "string" &&
    value.length > 0 &&
    value.length <= maximum
    ? value
    : null;
}

function normalizeFieldErrors(
  value: unknown,
): readonly InstanceConfigurationFieldError[] {
  if (!Array.isArray(value) || value.length > 32) return Object.freeze([]);
  const errors: InstanceConfigurationFieldError[] = [];
  for (const item of value) {
    try {
      const record = asRecord(item, "problem field error");
      assertExactFields(record, FIELD_ERROR_FIELDS, "problem field error");
      const pointer = asString(record.pointer, "problem field error pointer");
      const code = asString(record.code, "problem field error code");
      const message = asString(record.message, "problem field error message");
      if (
        pointer.length > 1_000 ||
        !/^\/(?:body|path|query|header|cookie|request|input(?:\/[A-Za-z0-9_.-]{1,100}){0,64})$/u.test(
          pointer,
        ) ||
        !SAFE_CODE_PATTERN.test(code) ||
        message.length > 240
      ) {
        return Object.freeze([]);
      }
      errors.push(Object.freeze({ pointer, code, message }));
    } catch {
      return Object.freeze([]);
    }
  }
  return Object.freeze(errors);
}

function normalizeSafeProblem(
  value: unknown,
  status: number,
): SafeProblem | null {
  try {
    const record = asRecord(value, "problem");
    assertAllowedAndRequiredFields(
      record,
      PROBLEM_REQUIRED_FIELDS,
      PROBLEM_OPTIONAL_FIELDS,
      "problem",
    );
    const code = asString(record.code, "problem.code");
    const correlationId = asString(
      record.correlation_id,
      "problem.correlation_id",
    );
    if (
      !SAFE_CODE_PATTERN.test(code) ||
      record.status !== status ||
      record.type !== `urn:marketing-agents:problem:${code}` ||
      !CORRELATION_ID_PATTERN.test(correlationId) ||
      record.instance !== `urn:marketing-agents:request:${correlationId}` ||
      safeString(record.title, 120) === null ||
      safeString(record.detail, 500) === null
    ) {
      return null;
    }
    let currentResourceVersion: number | string | null = null;
    if (hasOwn(record, "current_resource_version")) {
      const current = record.current_resource_version;
      if (
        (typeof current === "number" &&
          Number.isSafeInteger(current) &&
          current >= 0) ||
        (typeof current === "string" &&
          current.length <= 128 &&
          /^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$/u.test(current))
      ) {
        currentResourceVersion = current;
      } else {
        return null;
      }
    }
    if (hasOwn(record, "retry_after_seconds")) {
      asInteger(
        record.retry_after_seconds,
        "problem.retry_after_seconds",
        0,
        86_400,
      );
    }
    const fieldErrors = hasOwn(record, "field_errors")
      ? normalizeFieldErrors(record.field_errors)
      : Object.freeze([]);
    return Object.freeze({ code, currentResourceVersion, fieldErrors });
  } catch {
    return null;
  }
}

function errorMessage(status: number, code: string): string {
  if (code === "configuration_revision_conflict") {
    return "The instance configuration changed. Reload it and try again.";
  }
  if (
    code === "request_validation_failed" ||
    code === "configuration_invalid"
  ) {
    return "The instance configuration is invalid.";
  }
  if (code === "csrf_token_invalid") {
    return "The local session expired. Refresh it and try again.";
  }
  if (status === 403) return "This local session cannot change configuration.";
  if (status === 404) return "The selected agent instance was not found.";
  if (status === 503)
    return "Instance configuration is temporarily unavailable.";
  return "The local API could not complete this request.";
}

async function responseError(
  response: Response,
): Promise<InstanceConfigurationRequestError> {
  let problem: SafeProblem | null = null;
  if (
    response.headers
      .get("Content-Type")
      ?.split(";", 1)[0]
      ?.trim()
      .toLowerCase() === "application/problem+json"
  ) {
    try {
      problem = normalizeSafeProblem(
        (await response.json()) as unknown,
        response.status,
      );
    } catch {
      problem = null;
    }
  }
  const code = problem?.code ?? "api_request_failed";
  return new InstanceConfigurationRequestError(
    response.status,
    code,
    errorMessage(response.status, code),
    {
      currentResourceVersion: problem?.currentResourceVersion ?? null,
      fieldErrors: problem?.fieldErrors ?? [],
    },
  );
}

function assertJsonSuccess(response: Response, label: string): void {
  const contentType = response.headers
    .get("Content-Type")
    ?.split(";", 1)[0]
    ?.trim()
    .toLowerCase();
  if (contentType !== "application/json") {
    throw new InstanceConfigurationRequestError(
      response.status,
      "invalid_json_response",
      `The local API returned an invalid ${label}.`,
    );
  }
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

async function sameOriginFetch(
  path: string,
  request: RequestInit,
): Promise<Response> {
  try {
    return await fetch(path, request);
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new InstanceConfigurationRequestError(
      0,
      "api_unreachable",
      "The local API is not ready. Start it and try again.",
    );
  }
}

function getRequestInit(
  method: "GET" | "PATCH",
  headers: Readonly<Record<string, string>>,
  signal?: AbortSignal,
  body?: string,
): RequestInit {
  const request: RequestInit = {
    method,
    cache: "no-store",
    credentials: "same-origin",
    headers,
  };
  if (signal !== undefined) request.signal = signal;
  if (body !== undefined) request.body = body;
  return request;
}

export function clearLocalSession(): void {
  currentSession = undefined;
}

export async function fetchLocalSession(
  signal?: AbortSignal,
): Promise<LocalSession> {
  const response = await sameOriginFetch(
    SESSION_PATH,
    getRequestInit("GET", { Accept: "application/json" }, signal),
  );
  if (!response.ok) throw await responseError(response);
  assertJsonSuccess(response, "local session");
  let body: unknown;
  try {
    body = (await response.json()) as unknown;
  } catch {
    throw new InstanceConfigurationRequestError(
      response.status,
      "invalid_json_response",
      "The local API returned an invalid local session.",
    );
  }
  try {
    const normalized = normalizeLocalSessionEnvelope(body);
    currentSession = normalized;
    return normalized.session;
  } catch (error) {
    if (!(error instanceof ContractViolation)) throw error;
    throw new InstanceConfigurationRequestError(
      response.status,
      "invalid_session_response",
      "The local API returned an invalid local session.",
    );
  }
}

export async function fetchInstanceConfigurationSchema(
  expected: { readonly instanceId: string; readonly templateId: string },
  signal?: AbortSignal,
): Promise<InstanceConfigurationSchema> {
  let instanceId: string;
  try {
    instanceId = validateIdentifier(
      expected.instanceId,
      INSTANCE_ID_PATTERN,
      "expected instance ID",
    );
    validateIdentifier(
      expected.templateId,
      TEMPLATE_ID_PATTERN,
      "expected template ID",
    );
  } catch (error) {
    if (!(error instanceof ContractViolation)) throw error;
    throw new InstanceConfigurationRequestError(
      0,
      "invalid_configuration_identity",
      "The selected instance configuration identity is invalid.",
    );
  }
  const path = `/api/v1/agent-instances/${encodeURIComponent(instanceId)}${CONFIGURATION_SCHEMA_SUFFIX}`;
  const response = await sameOriginFetch(
    path,
    getRequestInit("GET", { Accept: "application/json" }, signal),
  );
  if (!response.ok) throw await responseError(response);
  assertJsonSuccess(response, "instance configuration schema");
  let body: unknown;
  try {
    body = (await response.json()) as unknown;
  } catch {
    throw new InstanceConfigurationRequestError(
      response.status,
      "invalid_json_response",
      "The local API returned an invalid instance configuration schema.",
    );
  }
  try {
    return normalizeInstanceConfigurationSchema(body, expected);
  } catch (error) {
    if (!(error instanceof ContractViolation)) throw error;
    throw new InstanceConfigurationRequestError(
      response.status,
      "invalid_configuration_schema_response",
      "The local API returned an invalid instance configuration schema.",
    );
  }
}

function validateConfigurationEtag(value: string): string {
  if (!CONFIGURATION_ETAG_PATTERN.test(value) || value.length > 200) {
    throw new InstanceConfigurationRequestError(
      0,
      "invalid_configuration_etag",
      "The instance configuration revision validator is invalid.",
    );
  }
  return value;
}

async function requiredSession(signal?: AbortSignal): Promise<SessionEnvelope> {
  if (currentSession === undefined) await fetchLocalSession(signal);
  if (currentSession === undefined) {
    throw new InstanceConfigurationRequestError(
      0,
      "invalid_session_response",
      "The local API returned an invalid local session.",
    );
  }
  return currentSession;
}

async function sendConfigurationPatch(
  path: string,
  configurationEtag: string,
  body: string,
  session: SessionEnvelope,
  signal?: AbortSignal,
): Promise<Response> {
  return await sameOriginFetch(
    path,
    getRequestInit(
      "PATCH",
      {
        Accept: "application/json",
        "Content-Type": "application/json",
        "If-Match": configurationEtag,
        [session.csrfHeaderName]: session.csrfToken,
      },
      signal,
      body,
    ),
  );
}

export async function updateInstanceConfiguration(input: {
  readonly instanceId: string;
  readonly configurationEtag: string;
  readonly patch: InstanceConfigurationPatch;
  readonly signal?: AbortSignal;
}): Promise<InstanceConfigurationResult> {
  let instanceId: string;
  try {
    instanceId = validateIdentifier(
      input.instanceId,
      INSTANCE_ID_PATTERN,
      "instance ID",
    );
  } catch (error) {
    if (!(error instanceof ContractViolation)) throw error;
    throw new InstanceConfigurationRequestError(
      0,
      "invalid_configuration_identity",
      "The selected instance configuration identity is invalid.",
    );
  }
  const configurationEtag = validateConfigurationEtag(input.configurationEtag);
  const patch = serializeInstanceConfigurationPatch(input.patch);
  const requestBody = JSON.stringify(patch);
  const path = `/api/v1/agent-instances/${encodeURIComponent(instanceId)}${CONFIGURATION_SUFFIX}`;
  let session = await requiredSession(input.signal);
  let response = await sendConfigurationPatch(
    path,
    configurationEtag,
    requestBody,
    session,
    input.signal,
  );
  if (!response.ok) {
    const firstError = await responseError(response);
    if (firstError.status !== 403 || firstError.code !== "csrf_token_invalid") {
      throw firstError;
    }
    await fetchLocalSession(input.signal);
    session = await requiredSession(input.signal);
    response = await sendConfigurationPatch(
      path,
      configurationEtag,
      requestBody,
      session,
      input.signal,
    );
    if (!response.ok) throw await responseError(response);
  }
  assertJsonSuccess(response, "instance configuration response");
  let body: unknown;
  try {
    body = (await response.json()) as unknown;
  } catch {
    throw new InstanceConfigurationRequestError(
      response.status,
      "invalid_json_response",
      "The local API returned an invalid instance configuration response.",
    );
  }
  try {
    return normalizeConfigurationResult(
      body,
      instanceId,
      response.headers.get("ETag"),
    );
  } catch (error) {
    if (!(error instanceof ContractViolation)) throw error;
    throw new InstanceConfigurationRequestError(
      response.status,
      "invalid_configuration_response",
      "The local API returned an invalid instance configuration response.",
    );
  }
}

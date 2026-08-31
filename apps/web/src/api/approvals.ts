import { ApiRequestError, getJson } from "./client";
import {
  assertLocalJsonResponse,
  LocalApiRequestError,
  sendLocalJsonMutation,
} from "./localSession";

const RESOURCE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$/u;
const AUTHORITY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._/-]{0,239}$/u;
const DIGEST_PATTERN = /^[0-9a-f]{64}$/u;
const CURSOR_PATTERN = /^approval-page-v1\.[A-Za-z0-9_-]{1,1007}$/u;
const ISO_UTC_TIMESTAMP_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(?:Z|\+00:00)$/u;
const UNSAFE_OBJECT_KEYS = new Set(["__proto__", "constructor", "prototype"]);

const DEFAULT_PAGE_SIZE = 25;
const MAX_PAGE_SIZE = 100;
const MAX_CURSOR_LENGTH = 1_024;
const MAX_PAYLOAD_DEPTH = 64;
const MAX_PAYLOAD_NODES = 8_192;
const MAX_PAYLOAD_ARRAY_ITEMS = 4_096;
const MAX_PAYLOAD_OBJECT_FIELDS = 1_024;
const MAX_PAYLOAD_KEY_LENGTH = 100;
const MAX_PAYLOAD_STRING_LENGTH = 262_144;
const MAX_PAYLOAD_BYTES = 1_048_576;

const SUMMARY_FIELDS = new Set([
  "id",
  "status",
  "resource_version",
  "generation",
  "action_id",
  "action_type",
  "destination_summary",
  "run_id",
  "template_id",
  "instance_id",
  "requested_at",
  "expires_at",
  "is_expired",
  "is_actionable",
  "approval_url",
  "action_url",
  "run_url",
]);
const DETAIL_FIELDS = new Set([
  "id",
  "status",
  "resource_version",
  "generation",
  "one_time_use_state",
  "action_id",
  "action_type",
  "capability_id",
  "connector_family",
  "binding_id",
  "destination_summary",
  "redacted_payload",
  "payload_hash",
  "run_id",
  "step_id",
  "template_id",
  "instance_id",
  "policy_id",
  "required_roles",
  "required_scopes",
  "allow_self_approval",
  "requested_by",
  "requested_at",
  "expires_at",
  "updated_at",
  "is_expired",
  "is_actionable",
  "decision_id",
  "decision_kind",
  "decision_actor_id",
  "decision_reason_code",
  "decision_reason",
  "decided_at",
  "expired_at",
  "replacement_approval_id",
  "renewed_at",
  "superseded_at",
  "superseded_reason_code",
  "consumed_at",
  "approval_url",
  "action_url",
  "run_url",
  "step_url",
  "template_url",
  "instance_url",
]);
const PAGE_FIELDS = new Set(["items", "next_cursor"]);
const DECISION_FIELDS = new Set([
  "approval_id",
  "decision_id",
  "action_id",
  "run_id",
  "status",
]);
const DECISION_RESOURCE_FIELDS = new Set([...DECISION_FIELDS, "approval"]);

export const APPROVAL_STATUSES = Object.freeze([
  "pending",
  "approved",
  "rejected",
  "expired",
  "consumed",
  "superseded",
] as const);
const APPROVAL_STATUS_SET = new Set<ApprovalStatus>(APPROVAL_STATUSES);
const SUPERSEDED_REASON_CODES = new Set([
  "approval_set_rejected",
  "approval_set_superseded",
  "run_cancelled",
]);

export const APPROVALS_QUERY_ROOT = Object.freeze(["approvals"] as const);
export const APPROVAL_LIST_QUERY_ROOT = Object.freeze([
  APPROVALS_QUERY_ROOT[0],
  "list",
] as const);

export type ApprovalStatus = (typeof APPROVAL_STATUSES)[number];
export type ApprovalDecisionKind = "approve" | "reject";
export type JsonValue =
  null | boolean | number | string | readonly JsonValue[] | JsonObject;
export interface JsonObject {
  readonly [key: string]: JsonValue;
}

export interface ApprovalListQuery {
  readonly status?: ApprovalStatus;
  readonly runId?: string;
  readonly actionId?: string;
  readonly cursor?: string;
  readonly limit?: number;
}

export interface NormalizedApprovalListQuery {
  readonly status: ApprovalStatus | null;
  readonly runId: string | null;
  readonly actionId: string | null;
  readonly cursor: string | null;
  readonly limit: number;
}

export interface ApprovalSummary {
  readonly id: string;
  readonly status: ApprovalStatus;
  readonly resourceVersion: number;
  readonly generation: number;
  readonly actionId: string;
  readonly actionType: string;
  readonly destinationSummary: string;
  readonly runId: string;
  readonly templateId: string;
  readonly instanceId: string;
  readonly requestedAt: string;
  readonly expiresAt: string;
  readonly isExpired: boolean;
  readonly isActionable: boolean;
  readonly approvalUrl: string;
  readonly actionUrl: string;
  readonly runUrl: string;
}

export interface ApprovalPage {
  readonly items: readonly ApprovalSummary[];
  readonly nextCursor: string | null;
}

export interface ApprovalDetail extends ApprovalSummary {
  readonly oneTimeUseState: "unused" | "consumed";
  readonly capabilityId: string;
  readonly connectorFamily: string;
  readonly bindingId: string;
  readonly redactedPayload: JsonObject;
  readonly payloadHash: string;
  readonly stepId: string;
  readonly policyId: string;
  readonly requiredRoles: readonly string[];
  readonly requiredScopes: readonly string[];
  readonly allowSelfApproval: boolean;
  readonly requestedBy: string;
  readonly updatedAt: string;
  readonly decisionId: string | null;
  readonly decisionKind: ApprovalDecisionKind | null;
  readonly decisionActorId: string | null;
  readonly decisionReasonCode: "approval_granted" | "approval_rejected" | null;
  readonly decisionReason: string | null;
  readonly decidedAt: string | null;
  readonly expiredAt: string | null;
  readonly replacementApprovalId: string | null;
  readonly renewedAt: string | null;
  readonly supersededAt: string | null;
  readonly supersededReasonCode: string | null;
  readonly consumedAt: string | null;
  readonly stepUrl: string;
  readonly templateUrl: string;
  readonly instanceUrl: string;
}

export interface DecideApprovalInput {
  readonly approvalId: string;
  readonly decision: ApprovalDecisionKind;
  readonly expectedGeneration: number;
  readonly expectedPayloadHash: string;
  readonly expectedActionId: string;
  readonly expectedRunId: string;
  readonly signal?: AbortSignal;
}

export interface ApprovalDecisionResult {
  readonly approvalId: string;
  readonly decisionId: string;
  readonly actionId: string;
  readonly runId: string;
  readonly status: "approved" | "rejected";
  readonly approval: ApprovalDetail | null;
}

export interface ApprovalDecisionExpectation {
  readonly approvalId: string;
  readonly decision: ApprovalDecisionKind;
  readonly expectedGeneration: number;
  readonly expectedPayloadHash: string;
  readonly expectedActionId: string;
  readonly expectedRunId: string;
}

export class ApprovalContractError extends Error {
  constructor(message: string) {
    super(`Approval contract violation: ${message}`);
    this.name = "ApprovalContractError";
  }
}

export class ApprovalRequestError extends Error {
  readonly status: number;
  readonly code: string;
  readonly currentResourceVersion: number | string | null;

  constructor(
    status: number,
    code: string,
    message: string,
    currentResourceVersion: number | string | null = null,
  ) {
    super(message);
    this.name = "ApprovalRequestError";
    this.status = status;
    this.code = code;
    this.currentResourceVersion = currentResourceVersion;
  }
}

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ApprovalContractError(`${label} must be an object`);
  }
  const prototype = Object.getPrototypeOf(value) as object | null;
  if (prototype !== Object.prototype && prototype !== null) {
    throw new ApprovalContractError(`${label} must be a plain object`);
  }
  for (const key of Reflect.ownKeys(value)) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (
      typeof key !== "string" ||
      descriptor === undefined ||
      !("value" in descriptor) ||
      !descriptor.enumerable
    ) {
      throw new ApprovalContractError(`${label} must contain plain data`);
    }
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
    throw new ApprovalContractError(`${label} fields are unsupported`);
  }
}

function asArray(
  value: unknown,
  label: string,
  maximum: number,
): readonly unknown[] {
  if (!Array.isArray(value) || value.length > maximum) {
    throw new ApprovalContractError(`${label} must be a bounded array`);
  }
  if (Object.getPrototypeOf(value) !== Array.prototype) {
    throw new ApprovalContractError(`${label} must be a plain array`);
  }
  for (let index = 0; index < value.length; index += 1) {
    const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
    if (
      descriptor === undefined ||
      !("value" in descriptor) ||
      !descriptor.enumerable
    ) {
      throw new ApprovalContractError(`${label} must not be sparse`);
    }
  }
  if (Reflect.ownKeys(value).length !== value.length + 1) {
    throw new ApprovalContractError(`${label} fields are unsupported`);
  }
  return value;
}

function asBoolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") {
    throw new ApprovalContractError(`${label} must be a boolean`);
  }
  return value;
}

function asInteger(
  value: unknown,
  label: string,
  minimum: number,
  maximum = Number.MAX_SAFE_INTEGER,
): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < minimum ||
    value > maximum
  ) {
    throw new ApprovalContractError(`${label} must be a bounded integer`);
  }
  return value;
}

function asResourceId(value: unknown, label: string): string {
  if (typeof value !== "string" || !RESOURCE_ID_PATTERN.test(value)) {
    throw new ApprovalContractError(`${label} must be a stable resource ID`);
  }
  return value;
}

function asAuthority(value: unknown, label: string): string {
  if (typeof value !== "string" || !AUTHORITY_PATTERN.test(value)) {
    throw new ApprovalContractError(`${label} must be a stable authority`);
  }
  return value;
}

function asDigest(value: unknown, label: string): string {
  if (typeof value !== "string" || !DIGEST_PATTERN.test(value)) {
    throw new ApprovalContractError(
      `${label} must be a lowercase SHA-256 digest`,
    );
  }
  return value;
}

function hasUnsupportedTextCharacter(value: string): boolean {
  return Array.from(value).some((character) => {
    const codePoint = character.codePointAt(0);
    return (
      codePoint === undefined ||
      codePoint < 0x20 ||
      (codePoint >= 0x7f && codePoint <= 0x9f) ||
      (codePoint >= 0xd800 && codePoint <= 0xdfff)
    );
  });
}

function asDisplayText(value: unknown, label: string, maximum: number): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > maximum ||
    value.trim() !== value ||
    hasUnsupportedTextCharacter(value)
  ) {
    throw new ApprovalContractError(`${label} must be safe bounded text`);
  }
  return value;
}

function asNullableDisplayText(
  value: unknown,
  label: string,
  maximum: number,
): string | null {
  return value === null ? null : asDisplayText(value, label, maximum);
}

function asApprovalStatus(value: unknown, label: string): ApprovalStatus {
  if (
    typeof value !== "string" ||
    !APPROVAL_STATUS_SET.has(value as ApprovalStatus)
  ) {
    throw new ApprovalContractError(`${label} is unsupported`);
  }
  return value as ApprovalStatus;
}

function asTimestamp(value: unknown, label: string): string {
  if (typeof value !== "string") {
    throw new ApprovalContractError(`${label} must be a UTC timestamp`);
  }
  const match = ISO_UTC_TIMESTAMP_PATTERN.exec(value);
  if (match === null || !Number.isFinite(Date.parse(value))) {
    throw new ApprovalContractError(`${label} must be a UTC timestamp`);
  }
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
    throw new ApprovalContractError(`${label} must be a UTC timestamp`);
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
    throw new ApprovalContractError(`${label} must be a UTC timestamp`);
  }
  return value;
}

function asNullableTimestamp(value: unknown, label: string): string | null {
  return value === null ? null : asTimestamp(value, label);
}

function timestampNanoseconds(value: string): bigint {
  const match = ISO_UTC_TIMESTAMP_PATTERN.exec(value);
  if (match === null) {
    throw new ApprovalContractError("timestamp comparison requires UTC values");
  }
  const wholeSecond = `${value.slice(0, 19)}Z`;
  const fraction = (match[7] ?? "").padEnd(9, "0");
  return BigInt(Date.parse(wholeSecond)) * 1_000_000n + BigInt(fraction || "0");
}

export function compareApprovalTimestamps(left: string, right: string): number {
  const leftValue = timestampNanoseconds(left);
  const rightValue = timestampNanoseconds(right);
  return leftValue < rightValue ? -1 : leftValue > rightValue ? 1 : 0;
}

function sameTimestamp(left: string, right: string): boolean {
  return compareApprovalTimestamps(left, right) === 0;
}

function asAuthorities(
  value: unknown,
  label: string,
  maximum: number,
): readonly string[] {
  const items = asArray(value, label, maximum).map((item, index) =>
    asAuthority(item, `${label}[${String(index)}]`),
  );
  if (
    new Set(items).size !== items.length ||
    items.some((item, index) => index > 0 && (items[index - 1] ?? item) >= item)
  ) {
    throw new ApprovalContractError(`${label} must be unique and sorted`);
  }
  return Object.freeze(items);
}

interface PayloadBudget {
  nodes: number;
}

function normalizeJsonValue(
  value: unknown,
  label: string,
  depth: number,
  budget: PayloadBudget,
  active: WeakSet<object>,
): JsonValue {
  budget.nodes += 1;
  if (depth > MAX_PAYLOAD_DEPTH || budget.nodes > MAX_PAYLOAD_NODES) {
    throw new ApprovalContractError(`${label} exceeds payload bounds`);
  }
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "string") {
    if (value.length > MAX_PAYLOAD_STRING_LENGTH) {
      throw new ApprovalContractError(`${label} exceeds payload bounds`);
    }
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new ApprovalContractError(`${label} must contain finite numbers`);
    }
    return value;
  }
  if (typeof value !== "object") {
    throw new ApprovalContractError(`${label} must contain JSON values`);
  }
  if (active.has(value)) {
    throw new ApprovalContractError(`${label} must not be cyclic`);
  }
  active.add(value);
  try {
    if (Array.isArray(value)) {
      const items = asArray(value, label, MAX_PAYLOAD_ARRAY_ITEMS).map(
        (item, index) =>
          normalizeJsonValue(
            item,
            `${label}[${String(index)}]`,
            depth + 1,
            budget,
            active,
          ),
      );
      return Object.freeze(items);
    }
    const record = asRecord(value, label);
    const keys = Object.keys(record);
    if (keys.length > MAX_PAYLOAD_OBJECT_FIELDS) {
      throw new ApprovalContractError(`${label} exceeds payload bounds`);
    }
    const result = Object.create(null) as Record<string, JsonValue>;
    for (const key of keys) {
      if (
        key.length === 0 ||
        key.length > MAX_PAYLOAD_KEY_LENGTH ||
        UNSAFE_OBJECT_KEYS.has(key) ||
        hasUnsupportedTextCharacter(key)
      ) {
        throw new ApprovalContractError(`${label} has an unsafe key`);
      }
      result[key] = normalizeJsonValue(
        record[key],
        `${label}.${key}`,
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

export function normalizeApprovalSafeJsonObject(
  value: unknown,
  label = "approval payload",
): JsonObject {
  const normalized = normalizeJsonValue(
    value,
    label,
    1,
    { nodes: 0 },
    new WeakSet(),
  );
  if (
    normalized === null ||
    Array.isArray(normalized) ||
    typeof normalized !== "object"
  ) {
    throw new ApprovalContractError(`${label} must be a JSON object`);
  }
  const serialized = JSON.stringify(normalized);
  if (new TextEncoder().encode(serialized).byteLength > MAX_PAYLOAD_BYTES) {
    throw new ApprovalContractError(`${label} exceeds payload bounds`);
  }
  return normalized as JsonObject;
}

function expectedApprovalUrl(approvalId: string): string {
  return `/api/v1/approvals/${encodeURIComponent(approvalId)}`;
}

function expectedActionUrl(actionId: string): string {
  return `/api/v1/external-actions/${encodeURIComponent(actionId)}`;
}

function expectedRunUrl(runId: string): string {
  return `/api/v1/runs/${encodeURIComponent(runId)}`;
}

function expectedStepUrl(runId: string, stepId: string): string {
  return `/api/v1/runs/${encodeURIComponent(runId)}/steps/${encodeURIComponent(stepId)}`;
}

function expectedTemplateUrl(templateId: string): string {
  return `/api/v1/agent-templates/${encodeURIComponent(templateId)}`;
}

function expectedInstanceUrl(instanceId: string): string {
  return `/api/v1/agent-instances/${encodeURIComponent(instanceId)}`;
}

function asExactUrl(value: unknown, expected: string, label: string): string {
  if (value !== expected) {
    throw new ApprovalContractError(`${label} does not match its resource`);
  }
  return expected;
}

function normalizeSummary(value: unknown, label: string): ApprovalSummary {
  const record = asRecord(value, label);
  assertExactFields(record, SUMMARY_FIELDS, label);
  const id = asResourceId(record.id, `${label}.id`);
  const actionId = asResourceId(record.action_id, `${label}.action_id`);
  const runId = asResourceId(record.run_id, `${label}.run_id`);
  const templateId = asResourceId(record.template_id, `${label}.template_id`);
  const instanceId = asResourceId(record.instance_id, `${label}.instance_id`);
  const status = asApprovalStatus(record.status, `${label}.status`);
  const isExpired = asBoolean(record.is_expired, `${label}.is_expired`);
  const isActionable = asBoolean(
    record.is_actionable,
    `${label}.is_actionable`,
  );
  if (isActionable !== (status === "pending" && !isExpired)) {
    throw new ApprovalContractError(`${label}.is_actionable is incoherent`);
  }
  const requestedAt = asTimestamp(record.requested_at, `${label}.requested_at`);
  const expiresAt = asTimestamp(record.expires_at, `${label}.expires_at`);
  if (compareApprovalTimestamps(expiresAt, requestedAt) <= 0) {
    throw new ApprovalContractError(`${label} expiry must follow its request`);
  }
  return Object.freeze({
    id,
    status,
    resourceVersion: asInteger(
      record.resource_version,
      `${label}.resource_version`,
      1,
    ),
    generation: asInteger(record.generation, `${label}.generation`, 1),
    actionId,
    actionType: asAuthority(record.action_type, `${label}.action_type`),
    destinationSummary: asDisplayText(
      record.destination_summary,
      `${label}.destination_summary`,
      300,
    ),
    runId,
    templateId,
    instanceId,
    requestedAt,
    expiresAt,
    isExpired,
    isActionable,
    approvalUrl: asExactUrl(
      record.approval_url,
      expectedApprovalUrl(id),
      `${label}.approval_url`,
    ),
    actionUrl: asExactUrl(
      record.action_url,
      expectedActionUrl(actionId),
      `${label}.action_url`,
    ),
    runUrl: asExactUrl(
      record.run_url,
      expectedRunUrl(runId),
      `${label}.run_url`,
    ),
  });
}

function summaryProjection(
  record: Record<string, unknown>,
): Record<string, unknown> {
  const summary: Record<string, unknown> = {};
  for (const field of SUMMARY_FIELDS) summary[field] = record[field];
  return summary;
}

export function normalizeApprovalDetail(value: unknown): ApprovalDetail {
  const record = asRecord(value, "approval");
  assertExactFields(record, DETAIL_FIELDS, "approval");
  const summary = normalizeSummary(summaryProjection(record), "approval");
  const stepId = asResourceId(record.step_id, "approval.step_id");
  const capabilityId = asAuthority(
    record.capability_id,
    "approval.capability_id",
  );
  const connectorFamily = asAuthority(
    record.connector_family,
    "approval.connector_family",
  );
  const bindingId = asAuthority(record.binding_id, "approval.binding_id");
  const policyId = asAuthority(record.policy_id, "approval.policy_id");
  const oneTimeUseState = record.one_time_use_state;
  if (oneTimeUseState !== "unused" && oneTimeUseState !== "consumed") {
    throw new ApprovalContractError(
      "approval.one_time_use_state is unsupported",
    );
  }
  if ((summary.status === "consumed") !== (oneTimeUseState === "consumed")) {
    throw new ApprovalContractError("approval one-time state is incoherent");
  }
  const updatedAt = asTimestamp(record.updated_at, "approval.updated_at");
  if (compareApprovalTimestamps(updatedAt, summary.requestedAt) < 0) {
    throw new ApprovalContractError("approval.updated_at precedes its request");
  }
  const decisionId =
    record.decision_id === null
      ? null
      : asResourceId(record.decision_id, "approval.decision_id");
  const decisionKind = record.decision_kind;
  if (
    decisionKind !== null &&
    decisionKind !== "approve" &&
    decisionKind !== "reject"
  ) {
    throw new ApprovalContractError("approval.decision_kind is unsupported");
  }
  const decisionActorId =
    record.decision_actor_id === null
      ? null
      : asAuthority(record.decision_actor_id, "approval.decision_actor_id");
  const decisionReasonCode = record.decision_reason_code;
  if (
    decisionReasonCode !== null &&
    decisionReasonCode !== "approval_granted" &&
    decisionReasonCode !== "approval_rejected"
  ) {
    throw new ApprovalContractError(
      "approval.decision_reason_code is unsupported",
    );
  }
  const decisionReason = asNullableDisplayText(
    record.decision_reason,
    "approval.decision_reason",
    500,
  );
  const decidedAt = asNullableTimestamp(
    record.decided_at,
    "approval.decided_at",
  );
  if (
    decisionId === null
      ? decisionKind !== null ||
        decisionActorId !== null ||
        decisionReasonCode !== null ||
        decisionReason !== null ||
        decidedAt !== null
      : decisionKind === null ||
        decisionActorId === null ||
        decisionReasonCode === null ||
        decidedAt === null
  ) {
    throw new ApprovalContractError("approval decision fields are incoherent");
  }
  if (decisionKind === "approve" && decisionReasonCode !== "approval_granted") {
    throw new ApprovalContractError(
      "approval approve reason code is incoherent",
    );
  }
  if (decisionKind === "reject" && decisionReasonCode !== "approval_rejected") {
    throw new ApprovalContractError(
      "approval reject reason code is incoherent",
    );
  }
  if (
    decidedAt !== null &&
    (compareApprovalTimestamps(decidedAt, summary.requestedAt) < 0 ||
      compareApprovalTimestamps(decidedAt, summary.expiresAt) >= 0)
  ) {
    throw new ApprovalContractError(
      "approval decision falls outside its request lifetime",
    );
  }
  const expiredAt = asNullableTimestamp(
    record.expired_at,
    "approval.expired_at",
  );
  const replacementApprovalId =
    record.replacement_approval_id === null
      ? null
      : asResourceId(
          record.replacement_approval_id,
          "approval.replacement_approval_id",
        );
  const renewedAt = asNullableTimestamp(
    record.renewed_at,
    "approval.renewed_at",
  );
  if ((replacementApprovalId === null) !== (renewedAt === null)) {
    throw new ApprovalContractError("approval renewal fields are incoherent");
  }
  if (replacementApprovalId === summary.id) {
    throw new ApprovalContractError("approval cannot renew itself");
  }
  const supersededAt = asNullableTimestamp(
    record.superseded_at,
    "approval.superseded_at",
  );
  const supersededReasonCode = asNullableDisplayText(
    record.superseded_reason_code,
    "approval.superseded_reason_code",
    128,
  );
  if (
    (supersededAt === null) !== (supersededReasonCode === null) ||
    (supersededReasonCode !== null &&
      !SUPERSEDED_REASON_CODES.has(supersededReasonCode))
  ) {
    throw new ApprovalContractError(
      "approval supersession fields are incoherent",
    );
  }
  const consumedAt = asNullableTimestamp(
    record.consumed_at,
    "approval.consumed_at",
  );
  if ((consumedAt !== null) !== (oneTimeUseState === "consumed")) {
    throw new ApprovalContractError(
      "approval consumption fields are incoherent",
    );
  }
  const noDecision = decisionId === null;
  const noExpiryOrClosure =
    expiredAt === null &&
    replacementApprovalId === null &&
    renewedAt === null &&
    supersededAt === null &&
    supersededReasonCode === null &&
    consumedAt === null;
  const approvedDecision = decisionKind === "approve";
  const updatedMatches = (value: string | null): boolean =>
    value !== null && sameTimestamp(updatedAt, value);
  let lifecycleIsCoherent = false;
  if (summary.status === "pending") {
    lifecycleIsCoherent =
      summary.resourceVersion === 1 &&
      noDecision &&
      noExpiryOrClosure &&
      sameTimestamp(updatedAt, summary.requestedAt);
  } else if (summary.status === "approved" || summary.status === "rejected") {
    lifecycleIsCoherent =
      summary.resourceVersion === 2 &&
      !noDecision &&
      decisionKind === (summary.status === "approved" ? "approve" : "reject") &&
      (summary.status === "approved" || !summary.isExpired) &&
      noExpiryOrClosure &&
      updatedMatches(decidedAt);
  } else if (summary.status === "expired") {
    const baseVersion = noDecision ? 2 : 3;
    if (expiredAt !== null) {
      const expirationIsValid =
        compareApprovalTimestamps(expiredAt, summary.expiresAt) >= 0 &&
        consumedAt === null &&
        supersededAt === null &&
        supersededReasonCode === null &&
        (noDecision || approvedDecision);
      lifecycleIsCoherent =
        summary.isExpired &&
        expirationIsValid &&
        (replacementApprovalId === null
          ? renewedAt === null &&
            summary.resourceVersion === baseVersion &&
            updatedMatches(expiredAt)
          : renewedAt !== null &&
            compareApprovalTimestamps(renewedAt, expiredAt) >= 0 &&
            summary.resourceVersion === baseVersion + 1 &&
            updatedMatches(renewedAt));
    }
  } else if (summary.status === "consumed") {
    lifecycleIsCoherent =
      summary.resourceVersion === 3 &&
      !summary.isExpired &&
      approvedDecision &&
      expiredAt === null &&
      replacementApprovalId === null &&
      renewedAt === null &&
      supersededAt === null &&
      supersededReasonCode === null &&
      consumedAt !== null &&
      decidedAt !== null &&
      compareApprovalTimestamps(consumedAt, decidedAt) >= 0 &&
      compareApprovalTimestamps(consumedAt, summary.expiresAt) < 0 &&
      updatedMatches(consumedAt);
  } else {
    lifecycleIsCoherent =
      summary.resourceVersion === (noDecision ? 2 : 3) &&
      !summary.isExpired &&
      (noDecision || approvedDecision) &&
      expiredAt === null &&
      replacementApprovalId === null &&
      renewedAt === null &&
      supersededAt !== null &&
      supersededReasonCode !== null &&
      consumedAt === null &&
      updatedMatches(supersededAt);
  }
  if (!lifecycleIsCoherent) {
    throw new ApprovalContractError("approval lifecycle is incoherent");
  }
  return Object.freeze({
    ...summary,
    oneTimeUseState,
    capabilityId,
    connectorFamily,
    bindingId,
    redactedPayload: normalizeApprovalSafeJsonObject(
      record.redacted_payload,
      "approval.redacted_payload",
    ),
    payloadHash: asDigest(record.payload_hash, "approval.payload_hash"),
    stepId,
    policyId,
    requiredRoles: asAuthorities(
      record.required_roles,
      "approval.required_roles",
      64,
    ),
    requiredScopes: asAuthorities(
      record.required_scopes,
      "approval.required_scopes",
      128,
    ),
    allowSelfApproval: asBoolean(
      record.allow_self_approval,
      "approval.allow_self_approval",
    ),
    requestedBy: asAuthority(record.requested_by, "approval.requested_by"),
    updatedAt,
    decisionId,
    decisionKind,
    decisionActorId,
    decisionReasonCode,
    decisionReason,
    decidedAt,
    expiredAt,
    replacementApprovalId,
    renewedAt,
    supersededAt,
    supersededReasonCode,
    consumedAt,
    stepUrl: asExactUrl(
      record.step_url,
      expectedStepUrl(summary.runId, stepId),
      "approval.step_url",
    ),
    templateUrl: asExactUrl(
      record.template_url,
      expectedTemplateUrl(summary.templateId),
      "approval.template_url",
    ),
    instanceUrl: asExactUrl(
      record.instance_url,
      expectedInstanceUrl(summary.instanceId),
      "approval.instance_url",
    ),
  });
}

function normalizeQuery(
  query: ApprovalListQuery = {},
): NormalizedApprovalListQuery {
  const status = query.status ?? null;
  if (status !== null && !APPROVAL_STATUS_SET.has(status)) {
    throw new ApprovalContractError("approval list status is unsupported");
  }
  const runId =
    query.runId === undefined
      ? null
      : asResourceId(query.runId, "approval list run ID");
  const actionId =
    query.actionId === undefined
      ? null
      : asResourceId(query.actionId, "approval list action ID");
  const cursor = query.cursor ?? null;
  if (
    cursor !== null &&
    (typeof cursor !== "string" ||
      cursor.length > MAX_CURSOR_LENGTH ||
      !CURSOR_PATTERN.test(cursor))
  ) {
    throw new ApprovalContractError("approval list cursor is invalid");
  }
  const limit = asInteger(
    query.limit ?? DEFAULT_PAGE_SIZE,
    "approval list limit",
    1,
    MAX_PAGE_SIZE,
  );
  return Object.freeze({ status, runId, actionId, cursor, limit });
}

export function approvalListQueryKey(
  query: ApprovalListQuery = {},
): readonly ["approvals", "list", NormalizedApprovalListQuery] {
  return Object.freeze([
    APPROVAL_LIST_QUERY_ROOT[0],
    APPROVAL_LIST_QUERY_ROOT[1],
    normalizeQuery(query),
  ] as const);
}

export function approvalDetailQueryKey(
  approvalId: string,
): readonly ["approvals", "detail", string] {
  return Object.freeze([
    APPROVALS_QUERY_ROOT[0],
    "detail",
    asResourceId(approvalId, "approval ID"),
  ] as const);
}

export function normalizeApprovalPage(
  value: unknown,
  query: ApprovalListQuery = {},
): ApprovalPage {
  const expected = normalizeQuery(query);
  const record = asRecord(value, "approval page");
  assertExactFields(record, PAGE_FIELDS, "approval page");
  const items = asArray(
    record.items,
    "approval page.items",
    expected.limit,
  ).map((item, index) =>
    normalizeSummary(item, `approval page.items[${String(index)}]`),
  );
  if (new Set(items.map(({ id }) => id)).size !== items.length) {
    throw new ApprovalContractError("approval page IDs must be unique");
  }
  if (
    items.some(
      (item) =>
        (expected.status !== null && item.status !== expected.status) ||
        (expected.runId !== null && item.runId !== expected.runId) ||
        (expected.actionId !== null && item.actionId !== expected.actionId),
    )
  ) {
    throw new ApprovalContractError("approval page does not match its filters");
  }
  for (let index = 1; index < items.length; index += 1) {
    const previous = items[index - 1];
    const current = items[index];
    if (previous === undefined || current === undefined) continue;
    const requestedOrder = compareApprovalTimestamps(
      current.requestedAt,
      previous.requestedAt,
    );
    if (
      requestedOrder > 0 ||
      (requestedOrder === 0 && current.id >= previous.id)
    ) {
      throw new ApprovalContractError(
        "approval page must use descending keyset order",
      );
    }
  }
  const nextCursor = record.next_cursor;
  if (
    nextCursor !== null &&
    (typeof nextCursor !== "string" ||
      nextCursor.length > MAX_CURSOR_LENGTH ||
      !CURSOR_PATTERN.test(nextCursor) ||
      items.length !== expected.limit)
  ) {
    throw new ApprovalContractError("approval page cursor is invalid");
  }
  return Object.freeze({ items: Object.freeze(items), nextCursor });
}

function approvalListPath(query: NormalizedApprovalListQuery): string {
  const parameters = new URLSearchParams();
  if (query.status !== null) parameters.set("status", query.status);
  if (query.runId !== null) parameters.set("run_id", query.runId);
  if (query.actionId !== null) parameters.set("action_id", query.actionId);
  if (query.cursor !== null) parameters.set("cursor", query.cursor);
  parameters.set("limit", String(query.limit));
  return `/api/v1/approvals?${parameters.toString()}`;
}

function requestError(
  error: ApiRequestError | LocalApiRequestError,
): ApprovalRequestError {
  const message =
    error.status === 0
      ? error.message
      : error.status === 403
        ? "This local session cannot perform that approval operation."
        : error.status === 404
          ? "The approval request was not found."
          : error.status === 409
            ? "The approval changed. Refresh its authoritative state before deciding."
            : error.status === 503
              ? "Approval resources are temporarily unavailable."
              : "The local API could not complete this approval request.";
  return new ApprovalRequestError(
    error.status,
    error.code,
    message,
    error instanceof LocalApiRequestError ? error.currentResourceVersion : null,
  );
}

export async function fetchApprovalPage(
  query: ApprovalListQuery = {},
  signal?: AbortSignal,
): Promise<ApprovalPage> {
  const normalized = normalizeQuery(query);
  let value: unknown;
  try {
    value = await getJson(approvalListPath(normalized), signal);
  } catch (error) {
    if (!(error instanceof ApiRequestError)) throw error;
    throw requestError(error);
  }
  return normalizeApprovalPage(value, query);
}

export async function fetchApprovalDetail(
  approvalId: string,
  signal?: AbortSignal,
): Promise<ApprovalDetail> {
  const id = asResourceId(approvalId, "approval ID");
  let value: unknown;
  try {
    value = await getJson(expectedApprovalUrl(id), signal);
  } catch (error) {
    if (!(error instanceof ApiRequestError)) throw error;
    throw requestError(error);
  }
  const detail = normalizeApprovalDetail(value);
  if (detail.id !== id) {
    throw new ApprovalContractError(
      "approval detail does not match its request",
    );
  }
  return detail;
}

export function normalizeApprovalDecisionResult(
  value: unknown,
  expected: ApprovalDecisionExpectation,
): ApprovalDecisionResult {
  const record = asRecord(value, "approval decision");
  const embedded = Object.hasOwn(record, "approval");
  assertExactFields(
    record,
    embedded ? DECISION_RESOURCE_FIELDS : DECISION_FIELDS,
    "approval decision",
  );
  const approvalId = asResourceId(
    record.approval_id,
    "approval decision.approval_id",
  );
  const decisionId = asResourceId(
    record.decision_id,
    "approval decision.decision_id",
  );
  const actionId = asResourceId(
    record.action_id,
    "approval decision.action_id",
  );
  const runId = asResourceId(record.run_id, "approval decision.run_id");
  const expectedStatus =
    expected.decision === "approve" ? "approved" : "rejected";
  if (
    approvalId !== expected.approvalId ||
    actionId !== expected.expectedActionId ||
    runId !== expected.expectedRunId ||
    record.status !== expectedStatus
  ) {
    throw new ApprovalContractError(
      "approval decision does not match its immutable request",
    );
  }
  const approval = embedded ? normalizeApprovalDetail(record.approval) : null;
  if (
    approval !== null &&
    (approval.id !== approvalId ||
      approval.actionId !== actionId ||
      approval.runId !== runId ||
      approval.generation !== expected.expectedGeneration ||
      approval.payloadHash !== expected.expectedPayloadHash ||
      approval.decisionId !== decisionId ||
      approval.decisionKind !== expected.decision ||
      (expected.decision === "approve"
        ? approval.status !== "approved" && approval.status !== "consumed"
        : approval.status !== "rejected"))
  ) {
    throw new ApprovalContractError(
      "embedded approval does not match its decision",
    );
  }
  return Object.freeze({
    approvalId,
    decisionId,
    actionId,
    runId,
    status: expectedStatus,
    approval,
  });
}

export async function decideApproval(
  input: DecideApprovalInput,
): Promise<ApprovalDecisionResult> {
  const decision: unknown = input.decision;
  if (decision !== "approve" && decision !== "reject") {
    throw new ApprovalContractError("approval decision kind is unsupported");
  }
  const expected: ApprovalDecisionExpectation = Object.freeze({
    approvalId: asResourceId(input.approvalId, "approval decision ID"),
    decision,
    expectedGeneration: asInteger(
      input.expectedGeneration,
      "approval decision generation",
      1,
    ),
    expectedPayloadHash: asDigest(
      input.expectedPayloadHash,
      "approval decision payload hash",
    ),
    expectedActionId: asResourceId(
      input.expectedActionId,
      "approval decision action ID",
    ),
    expectedRunId: asResourceId(
      input.expectedRunId,
      "approval decision run ID",
    ),
  });
  const body = JSON.stringify({
    expected_generation: expected.expectedGeneration,
    expected_payload_hash: expected.expectedPayloadHash,
  });
  const path = `${expectedApprovalUrl(expected.approvalId)}/${expected.decision}`;
  let response: Response;
  try {
    response = await sendLocalJsonMutation({
      path,
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body,
      ...(input.signal === undefined ? {} : { signal: input.signal }),
    });
  } catch (error) {
    if (!(error instanceof LocalApiRequestError)) throw error;
    throw requestError(error);
  }
  if (response.status !== 200) {
    throw new ApprovalContractError("approval decision status is unsupported");
  }
  assertLocalJsonResponse(response, "approval decision");
  if (
    response.headers.get("Location") !==
    expectedApprovalUrl(expected.approvalId)
  ) {
    throw new ApprovalContractError("approval decision Location is invalid");
  }
  let value: unknown;
  try {
    value = (await response.json()) as unknown;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw new ApprovalContractError("approval decision is not valid JSON");
  }
  return normalizeApprovalDecisionResult(value, expected);
}

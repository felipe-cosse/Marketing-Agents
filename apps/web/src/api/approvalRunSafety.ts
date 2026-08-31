import { ApiRequestError, getJson } from "./client";
import {
  ApprovalContractError,
  ApprovalRequestError,
  compareApprovalTimestamps,
  normalizeApprovalSafeJsonObject,
} from "./approvals";

const RESOURCE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$/u;
const AUTHORITY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._/-]{0,239}$/u;
const CATALOG_HASH_PATTERN = /^(?:catalog-sha256-v1:)?[0-9a-f]{64}$/u;
const ISO_UTC_TIMESTAMP_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(?:Z|\+00:00)$/u;

const RUN_FIELDS = new Set([
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
  "transitions",
  "plan",
  "execution_control",
  "pending_approvals",
  "artifact_summaries",
  "artifacts_truncated",
  "external_actions",
  "terminal_error",
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

const RUN_STATES = new Set<ApprovalRunState>([
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
const ACTION_STATES = new Set<ApprovalRunActionState>([
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
]);

export const APPROVAL_RUN_SAFETY_QUERY_ROOT = Object.freeze([
  "runs",
  "approval-safety",
] as const);

export type ApprovalRunState =
  | "received"
  | "validated"
  | "planned"
  | "awaiting_approval"
  | "executing"
  | "completed"
  | "failed"
  | "rejected"
  | "cancelled";

export type ApprovalRunActionState =
  | "proposed"
  | "awaiting_approval"
  | "approved"
  | "dispatch_reserved"
  | "dispatching"
  | "succeeded"
  | "failed"
  | "rejected"
  | "cancelled"
  | "superseded"
  | "outcome_unknown";

export interface PendingRunApproval {
  readonly id: string;
  readonly actionId: string;
  readonly stepId: string;
  readonly destinationSummary: string;
  readonly requestedAt: string;
  readonly expiresAt: string;
  readonly isExpired: boolean;
  readonly approvalUrl: string;
  readonly actionUrl: string;
  readonly stepUrl: string;
}

export interface ApprovalRunActionSafety {
  readonly id: string;
  readonly stepId: string;
  readonly actionType: string;
  readonly state: ApprovalRunActionState;
  readonly deliveryAttemptCount: number;
  readonly receiptId: string | null;
  readonly resultStatus: string | null;
  readonly completedAt: string | null;
  readonly actionUrl: string;
  readonly stepUrl: string;
}

export interface ApprovalRunSafety {
  readonly runId: string;
  readonly mode: "dry_run" | "mock_execution";
  readonly state: ApprovalRunState;
  readonly approvalRequired: boolean | null;
  readonly pendingApprovals: readonly PendingRunApproval[];
  readonly externalActions: readonly ApprovalRunActionSafety[];
  readonly zeroMockConnectorCallsConfirmed: boolean;
  readonly runUrl: string;
  readonly timelineUrl: string;
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
  const fields = Object.keys(record);
  if (
    fields.length !== expected.size ||
    fields.some((field) => !expected.has(field))
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

function asTimestamp(value: unknown, label: string): string {
  if (typeof value !== "string") {
    throw new ApprovalContractError(`${label} must be a UTC timestamp`);
  }
  const match = ISO_UTC_TIMESTAMP_PATTERN.exec(value);
  if (match === null || !Number.isFinite(Date.parse(value))) {
    throw new ApprovalContractError(`${label} must be a UTC timestamp`);
  }
  const [year, month, day, hour, minute, second] = match
    .slice(1, 7)
    .map(Number);
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

function asNullableResourceId(value: unknown, label: string): string | null {
  return value === null ? null : asResourceId(value, label);
}

function asNullableAuthority(value: unknown, label: string): string | null {
  return value === null ? null : asAuthority(value, label);
}

function asPlainOptionalRecord(value: unknown, label: string): void {
  if (value !== null) asRecord(value, label);
}

function asExactUrl(value: unknown, expected: string, label: string): string {
  if (value !== expected) {
    throw new ApprovalContractError(`${label} does not match its resource`);
  }
  return expected;
}

function runUrl(runId: string): string {
  return `/api/v1/runs/${encodeURIComponent(runId)}`;
}

function timelineUrl(runId: string): string {
  return `${runUrl(runId)}/timeline`;
}

function artifactsUrl(runId: string): string {
  return `${runUrl(runId)}/artifacts`;
}

function actionUrl(actionId: string): string {
  return `/api/v1/external-actions/${encodeURIComponent(actionId)}`;
}

function approvalUrl(approvalId: string): string {
  return `/api/v1/approvals/${encodeURIComponent(approvalId)}`;
}

function stepUrl(runId: string, stepId: string): string {
  return `${runUrl(runId)}/steps/${encodeURIComponent(stepId)}`;
}

function instanceUrl(instanceId: string): string {
  return `/api/v1/agent-instances/${encodeURIComponent(instanceId)}`;
}

function templateUrl(templateId: string): string {
  return `/api/v1/agent-templates/${encodeURIComponent(templateId)}`;
}

function destination(value: unknown, label: string): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > 300 ||
    value.trim() !== value
  ) {
    throw new ApprovalContractError(`${label} must be bounded text`);
  }
  return value;
}

function authorities(value: unknown, label: string): readonly string[] {
  const values = asArray(value, label, 128).map((item, index) =>
    asAuthority(item, `${label}[${String(index)}]`),
  );
  if (
    new Set(values).size !== values.length ||
    values.some(
      (item, index) => index > 0 && (values[index - 1] ?? item) >= item,
    )
  ) {
    throw new ApprovalContractError(`${label} must be unique and sorted`);
  }
  return Object.freeze(values);
}

function normalizePendingApproval(
  value: unknown,
  runId: string,
  index: number,
): PendingRunApproval {
  const label = `run.pending_approvals[${String(index)}]`;
  const record = asRecord(value, label);
  assertExactFields(record, PENDING_APPROVAL_FIELDS, label);
  if (record.status !== "pending") {
    throw new ApprovalContractError(`${label}.status is unsupported`);
  }
  const id = asResourceId(record.id, `${label}.id`);
  const actionId = asResourceId(record.action_id, `${label}.action_id`);
  const stepId = asResourceId(record.step_id, `${label}.step_id`);
  const requestedAt = asTimestamp(record.requested_at, `${label}.requested_at`);
  const expiresAt = asTimestamp(record.expires_at, `${label}.expires_at`);
  if (compareApprovalTimestamps(expiresAt, requestedAt) <= 0) {
    throw new ApprovalContractError(`${label} expiry must follow its request`);
  }
  return Object.freeze({
    id,
    actionId,
    stepId,
    destinationSummary: destination(
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

function normalizeExternalAction(
  value: unknown,
  runId: string,
  index: number,
): ApprovalRunActionSafety {
  const label = `run.external_actions[${String(index)}]`;
  const record = asRecord(value, label);
  assertExactFields(record, EXTERNAL_ACTION_FIELDS, label);
  if (record.run_id !== runId) {
    throw new ApprovalContractError(`${label}.run_id does not match its run`);
  }
  const id = asResourceId(record.id, `${label}.id`);
  const stepId = asResourceId(record.step_id, `${label}.step_id`);
  const templateId = asResourceId(record.template_id, `${label}.template_id`);
  const instanceId = asResourceId(record.instance_id, `${label}.instance_id`);
  const state = record.state;
  if (
    typeof state !== "string" ||
    !ACTION_STATES.has(state as ApprovalRunActionState)
  ) {
    throw new ApprovalContractError(`${label}.state is unsupported`);
  }
  const deliveryAttemptCount = asInteger(
    record.delivery_attempt_count,
    `${label}.delivery_attempt_count`,
    0,
    10,
  );
  const deliveryAttemptLimit = asInteger(
    record.delivery_attempt_limit,
    `${label}.delivery_attempt_limit`,
    1,
    10,
  );
  if (deliveryAttemptCount > deliveryAttemptLimit) {
    throw new ApprovalContractError(`${label} exceeds its attempt limit`);
  }
  asAuthority(record.step_key, `${label}.step_key`);
  asInteger(record.proposal_revision, `${label}.proposal_revision`, 1);
  const actionType = asAuthority(record.action_type, `${label}.action_type`);
  asAuthority(record.capability_id, `${label}.capability_id`);
  asAuthority(record.connector_family, `${label}.connector_family`);
  asAuthority(record.binding_id, `${label}.binding_id`);
  destination(record.destination_summary, `${label}.destination_summary`);
  normalizeApprovalSafeJsonObject(
    record.redacted_payload,
    `${label}.redacted_payload`,
  );
  asAuthority(record.payload_schema_id, `${label}.payload_schema_id`);
  const createdAt = asTimestamp(record.created_at, `${label}.created_at`);
  const updatedAt = asTimestamp(record.updated_at, `${label}.updated_at`);
  if (compareApprovalTimestamps(updatedAt, createdAt) < 0) {
    throw new ApprovalContractError(`${label}.updated_at precedes creation`);
  }
  asInteger(record.version, `${label}.version`, 1);
  asAuthority(record.approval_policy_id, `${label}.approval_policy_id`);
  authorities(
    record.approval_required_roles,
    `${label}.approval_required_roles`,
  );
  authorities(
    record.approval_required_scopes,
    `${label}.approval_required_scopes`,
  );
  asInteger(
    record.approval_expires_after_seconds,
    `${label}.approval_expires_after_seconds`,
    1,
    86_400,
  );
  asBoolean(
    record.approval_allow_self_approval,
    `${label}.approval_allow_self_approval`,
  );
  asNullableAuthority(
    record.terminal_reason_code,
    `${label}.terminal_reason_code`,
  );
  asNullableResourceId(
    record.superseded_by_action_id,
    `${label}.superseded_by_action_id`,
  );
  asNullableTimestamp(record.superseded_at, `${label}.superseded_at`);
  const receiptId = asNullableResourceId(
    record.receipt_id,
    `${label}.receipt_id`,
  );
  const resultStatus = asNullableAuthority(
    record.result_status,
    `${label}.result_status`,
  );
  if (record.result_safe_metadata !== null) {
    throw new ApprovalContractError(
      `${label}.result_safe_metadata must be null`,
    );
  }
  const completedAt = asNullableTimestamp(
    record.completed_at,
    `${label}.completed_at`,
  );
  asExactUrl(record.run_url, runUrl(runId), `${label}.run_url`);
  asExactUrl(
    record.instance_url,
    instanceUrl(instanceId),
    `${label}.instance_url`,
  );
  asExactUrl(
    record.template_url,
    templateUrl(templateId),
    `${label}.template_url`,
  );
  return Object.freeze({
    id,
    stepId,
    actionType,
    state: state as ApprovalRunActionState,
    deliveryAttemptCount,
    receiptId,
    resultStatus,
    completedAt,
    actionUrl: asExactUrl(
      record.action_url,
      actionUrl(id),
      `${label}.action_url`,
    ),
    stepUrl: asExactUrl(
      record.step_url,
      stepUrl(runId, stepId),
      `${label}.step_url`,
    ),
  });
}

export function normalizeApprovalRunSafety(
  value: unknown,
  expectedRunId?: string,
): ApprovalRunSafety {
  const record = asRecord(value, "run");
  assertExactFields(record, RUN_FIELDS, "run");
  const runId = asResourceId(record.id, "run.id");
  if (
    expectedRunId !== undefined &&
    runId !== asResourceId(expectedRunId, "run ID")
  ) {
    throw new ApprovalContractError("run does not match its request");
  }
  asResourceId(record.work_item_id, "run.work_item_id");
  const selectedInstanceId = asResourceId(
    record.instance_id,
    "run.instance_id",
  );
  asResourceId(record.workflow_id, "run.workflow_id");
  asResourceId(record.trigger_id, "run.trigger_id");
  asAuthority(record.source, "run.source");
  const mode = record.mode;
  if (mode !== "dry_run" && mode !== "mock_execution") {
    throw new ApprovalContractError("run.mode is unsupported");
  }
  const state = record.state;
  if (typeof state !== "string" || !RUN_STATES.has(state as ApprovalRunState)) {
    throw new ApprovalContractError("run.state is unsupported");
  }
  if (
    typeof record.catalog_hash !== "string" ||
    !CATALOG_HASH_PATTERN.test(record.catalog_hash)
  ) {
    throw new ApprovalContractError("run.catalog_hash is invalid");
  }
  asInteger(record.configuration_revision, "run.configuration_revision", 1);
  const approvalRequired = record.approval_required;
  if (approvalRequired !== null && typeof approvalRequired !== "boolean") {
    throw new ApprovalContractError("run.approval_required is invalid");
  }
  asNullableAuthority(record.terminal_reason_code, "run.terminal_reason_code");
  const createdAt = asTimestamp(record.created_at, "run.created_at");
  const updatedAt = asTimestamp(record.updated_at, "run.updated_at");
  if (compareApprovalTimestamps(updatedAt, createdAt) < 0) {
    throw new ApprovalContractError("run.updated_at precedes creation");
  }
  asInteger(record.version, "run.version", 1);
  const expectedRunUrl = runUrl(runId);
  const expectedTimelineUrl = timelineUrl(runId);
  asExactUrl(record.run_url, expectedRunUrl, "run.run_url");
  asExactUrl(record.timeline_url, expectedTimelineUrl, "run.timeline_url");
  asExactUrl(record.artifacts_url, artifactsUrl(runId), "run.artifacts_url");
  asExactUrl(
    record.instance_url,
    instanceUrl(selectedInstanceId),
    "run.instance_url",
  );
  const transitions = asArray(record.transitions, "run.transitions", 64);
  if (transitions.length === 0) {
    throw new ApprovalContractError("run.transitions must not be empty");
  }
  asPlainOptionalRecord(record.plan, "run.plan");
  asPlainOptionalRecord(record.execution_control, "run.execution_control");
  const pendingApprovals = asArray(
    record.pending_approvals,
    "run.pending_approvals",
    100,
  ).map((item, index) => normalizePendingApproval(item, runId, index));
  asArray(record.artifact_summaries, "run.artifact_summaries", 10);
  asBoolean(record.artifacts_truncated, "run.artifacts_truncated");
  const externalActions = asArray(
    record.external_actions,
    "run.external_actions",
    100,
  ).map((item, index) => normalizeExternalAction(item, runId, index));
  asPlainOptionalRecord(record.terminal_error, "run.terminal_error");
  if (
    new Set(pendingApprovals.map(({ id }) => id)).size !==
      pendingApprovals.length ||
    new Set(externalActions.map(({ id }) => id)).size !== externalActions.length
  ) {
    throw new ApprovalContractError(
      "run approval/action identities must be unique",
    );
  }
  const actionIds = new Set(externalActions.map(({ id }) => id));
  if (pendingApprovals.some(({ actionId }) => !actionIds.has(actionId))) {
    throw new ApprovalContractError(
      "run pending approval does not bind an external action",
    );
  }
  const zeroMockConnectorCallsConfirmed =
    mode === "mock_execution" &&
    externalActions.length > 0 &&
    externalActions.every(
      ({ deliveryAttemptCount, receiptId, resultStatus, completedAt }) =>
        deliveryAttemptCount === 0 &&
        receiptId === null &&
        resultStatus === null &&
        completedAt === null,
    );
  return Object.freeze({
    runId,
    mode,
    state: state as ApprovalRunState,
    approvalRequired,
    pendingApprovals: Object.freeze(pendingApprovals),
    externalActions: Object.freeze(externalActions),
    zeroMockConnectorCallsConfirmed,
    runUrl: expectedRunUrl,
    timelineUrl: expectedTimelineUrl,
  });
}

export function approvalRunSafetyQueryKey(
  runId: string,
): readonly ["runs", "approval-safety", string] {
  return Object.freeze([
    APPROVAL_RUN_SAFETY_QUERY_ROOT[0],
    APPROVAL_RUN_SAFETY_QUERY_ROOT[1],
    asResourceId(runId, "run ID"),
  ] as const);
}

export async function fetchApprovalRunSafety(
  runId: string,
  signal?: AbortSignal,
): Promise<ApprovalRunSafety> {
  const id = asResourceId(runId, "run ID");
  let value: unknown;
  try {
    value = await getJson(runUrl(id), signal);
  } catch (error) {
    if (!(error instanceof ApiRequestError)) throw error;
    throw new ApprovalRequestError(
      error.status,
      error.code,
      error.status === 0
        ? error.message
        : error.status === 404
          ? "The approval run was not found."
          : error.status === 503
            ? "The approval run safety summary is temporarily unavailable."
            : "The local API could not load the approval run safety summary.",
    );
  }
  return normalizeApprovalRunSafety(value, id);
}

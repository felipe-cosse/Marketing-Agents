import { ApiRequestError } from "./client";

const STATUS_SUMMARY_PATH = "/api/v1/agent-instances/status-summary";
const EXPECTED_INSTANCE_COUNT = 43;
const WATERMARK_PATTERN = /^instance-status-sha256-v1:[0-9a-f]{64}$/u;
const ISO_TIMESTAMP_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/u;

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

const SUMMARY_FIELDS = new Set(["scope", "runtime_watermark", "items"]);
const ITEM_FIELDS = new Set([
  "instance_id",
  "status",
  "latest_run_id",
  "latest_run_state",
  "latest_run_created_at",
  "latest_run_updated_at",
  "instance_url",
  "latest_run_url",
]);

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

export interface InstanceRuntimeStatus {
  readonly instanceId: string;
  readonly status: InstanceRuntimeState;
  readonly latestRunId: string | null;
  readonly latestRunState: RunState | null;
  readonly latestRunCreatedAt: string | null;
  readonly latestRunUpdatedAt: string | null;
  readonly instanceUrl: string;
  readonly latestRunUrl: string | null;
}

export interface InstanceStatusSummary {
  readonly scope: "single-local-installation";
  readonly runtimeWatermark: string;
  readonly items: readonly InstanceRuntimeStatus[];
}

export interface FetchInstanceStatusSummaryOptions {
  readonly previous?: InstanceStatusSummary;
  readonly signal?: AbortSignal;
}

export class InstanceStatusContractError extends Error {
  constructor(message: string) {
    super(`Instance status contract violation: ${message}`);
    this.name = "InstanceStatusContractError";
  }
}

function asRecord(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new InstanceStatusContractError(`${label} must be an object`);
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
    throw new InstanceStatusContractError(`${label} fields are unsupported`);
  }
}

function asString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new InstanceStatusContractError(
      `${label} must be a non-empty string`,
    );
  }
  return value;
}

function asNullableString(value: unknown, label: string): string | null {
  return value === null ? null : asString(value, label);
}

function asRunState(value: unknown, label: string): RunState {
  if (typeof value !== "string" || !RUN_STATES.has(value as RunState)) {
    throw new InstanceStatusContractError(`${label} is unsupported`);
  }
  return value as RunState;
}

function asNullableRunState(value: unknown, label: string): RunState | null {
  return value === null ? null : asRunState(value, label);
}

function asTimestamp(value: unknown, label: string): string {
  const timestamp = asString(value, label);
  const match = ISO_TIMESTAMP_PATTERN.exec(timestamp);
  if (match === null || !Number.isFinite(Date.parse(timestamp))) {
    throw new InstanceStatusContractError(`${label} must be an ISO timestamp`);
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
    throw new InstanceStatusContractError(`${label} must be an ISO timestamp`);
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
    throw new InstanceStatusContractError(`${label} must be an ISO timestamp`);
  }
  return timestamp;
}

function asNullableTimestamp(value: unknown, label: string): string | null {
  return value === null ? null : asTimestamp(value, label);
}

function expectedInstanceUrl(instanceId: string): string {
  return `/api/v1/agent-instances/${encodeURIComponent(instanceId)}`;
}

function expectedRunUrl(runId: string): string {
  return `/api/v1/runs/${encodeURIComponent(runId)}`;
}

function normalizeItem(
  value: unknown,
  expectedInstanceId: string,
  index: number,
): InstanceRuntimeStatus {
  const label = `items[${String(index)}]`;
  const record = asRecord(value, label);
  assertExactFields(record, ITEM_FIELDS, label);

  const instanceId = asString(record.instance_id, `${label}.instance_id`);
  if (instanceId !== expectedInstanceId) {
    throw new InstanceStatusContractError(
      `${label}.instance_id does not match hierarchy order`,
    );
  }
  const instanceUrl = asString(record.instance_url, `${label}.instance_url`);
  if (instanceUrl !== expectedInstanceUrl(instanceId)) {
    throw new InstanceStatusContractError(`${label}.instance_url is invalid`);
  }

  const statusValue = asString(record.status, `${label}.status`);
  const latestRunId = asNullableString(
    record.latest_run_id,
    `${label}.latest_run_id`,
  );
  const latestRunState = asNullableRunState(
    record.latest_run_state,
    `${label}.latest_run_state`,
  );
  const latestRunCreatedAt = asNullableTimestamp(
    record.latest_run_created_at,
    `${label}.latest_run_created_at`,
  );
  const latestRunUpdatedAt = asNullableTimestamp(
    record.latest_run_updated_at,
    `${label}.latest_run_updated_at`,
  );
  const latestRunUrl = asNullableString(
    record.latest_run_url,
    `${label}.latest_run_url`,
  );

  let status: InstanceRuntimeState;
  if (statusValue === "never_run") {
    if (
      latestRunId !== null ||
      latestRunState !== null ||
      latestRunCreatedAt !== null ||
      latestRunUpdatedAt !== null ||
      latestRunUrl !== null
    ) {
      throw new InstanceStatusContractError(
        `${label} never_run fields must all be null`,
      );
    }
    status = "never_run";
  } else {
    status = asRunState(statusValue, `${label}.status`);
    if (
      latestRunId === null ||
      latestRunState !== status ||
      latestRunCreatedAt === null ||
      latestRunUpdatedAt === null ||
      latestRunUrl !== expectedRunUrl(latestRunId)
    ) {
      throw new InstanceStatusContractError(
        `${label} latest Run fields are incoherent`,
      );
    }
    if (Date.parse(latestRunUpdatedAt) < Date.parse(latestRunCreatedAt)) {
      throw new InstanceStatusContractError(
        `${label}.latest_run_updated_at precedes creation`,
      );
    }
  }

  return Object.freeze({
    instanceId,
    status,
    latestRunId,
    latestRunState,
    latestRunCreatedAt,
    latestRunUpdatedAt,
    instanceUrl,
    latestRunUrl,
  });
}

function validateExpectedInstanceIds(
  expectedInstanceIds: readonly string[],
): void {
  if (
    expectedInstanceIds.length !== EXPECTED_INSTANCE_COUNT ||
    new Set(expectedInstanceIds).size !== EXPECTED_INSTANCE_COUNT ||
    expectedInstanceIds.some(
      (value) =>
        typeof value !== "string" ||
        value.length === 0 ||
        value.trim() !== value,
    )
  ) {
    throw new InstanceStatusContractError(
      "expected hierarchy IDs must be the exact 43 unique instance IDs",
    );
  }
}

export function normalizeInstanceStatusSummary(
  value: unknown,
  expectedInstanceIds: readonly string[],
): InstanceStatusSummary {
  validateExpectedInstanceIds(expectedInstanceIds);
  const record = asRecord(value, "summary");
  assertExactFields(record, SUMMARY_FIELDS, "summary");
  if (record.scope !== "single-local-installation") {
    throw new InstanceStatusContractError("summary.scope is unsupported");
  }
  const runtimeWatermark = asString(
    record.runtime_watermark,
    "summary.runtime_watermark",
  );
  if (!WATERMARK_PATTERN.test(runtimeWatermark)) {
    throw new InstanceStatusContractError(
      "summary.runtime_watermark is invalid",
    );
  }
  if (
    !Array.isArray(record.items) ||
    record.items.length !== EXPECTED_INSTANCE_COUNT
  ) {
    throw new InstanceStatusContractError(
      "summary.items must contain exactly 43 items",
    );
  }
  const items = record.items.map((item, index) => {
    const expectedInstanceId = expectedInstanceIds[index];
    if (expectedInstanceId === undefined) {
      throw new InstanceStatusContractError(
        "summary.items do not bind the expected hierarchy",
      );
    }
    return normalizeItem(item, expectedInstanceId, index);
  });
  return Object.freeze({
    scope: "single-local-installation",
    runtimeWatermark,
    items: Object.freeze(items),
  });
}

function quotedEtag(watermark: string): string {
  return `"${watermark}"`;
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
    }
  } catch {
    // Stable local fallbacks intentionally replace untrusted response diagnostics.
  }
  return new ApiRequestError(response.status, code, detail);
}

export async function fetchInstanceStatusSummary(
  expectedInstanceIds: readonly string[],
  options: FetchInstanceStatusSummaryOptions = {},
): Promise<InstanceStatusSummary> {
  validateExpectedInstanceIds(expectedInstanceIds);
  const previous = options.previous;
  if (
    previous !== undefined &&
    (!WATERMARK_PATTERN.test(previous.runtimeWatermark) ||
      previous.items.length !== EXPECTED_INSTANCE_COUNT ||
      previous.items.some(
        (item, index) => item.instanceId !== expectedInstanceIds[index],
      ))
  ) {
    throw new ApiRequestError(
      0,
      "invalid_previous_status_summary",
      "The previous instance status summary is invalid.",
    );
  }

  const headers: Record<string, string> = { Accept: "application/json" };
  if (previous !== undefined) {
    headers["If-None-Match"] = quotedEtag(previous.runtimeWatermark);
  }
  const request: RequestInit = {
    method: "GET",
    cache: "no-store",
    credentials: "same-origin",
    headers,
  };
  if (options.signal !== undefined) request.signal = options.signal;

  let response: Response;
  try {
    response = await fetch(STATUS_SUMMARY_PATH, request);
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
    if (
      previous === undefined ||
      response.headers.get("ETag") !== quotedEtag(previous.runtimeWatermark)
    ) {
      throw new ApiRequestError(
        response.status,
        "invalid_status_summary_response",
        "The local API returned an invalid instance status summary.",
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
    const normalized = normalizeInstanceStatusSummary(
      body,
      expectedInstanceIds,
    );
    if (
      response.headers.get("ETag") !== quotedEtag(normalized.runtimeWatermark)
    ) {
      throw new InstanceStatusContractError(
        "response ETag does not match its body",
      );
    }
    return normalized;
  } catch (error) {
    if (!(error instanceof InstanceStatusContractError)) throw error;
    throw new ApiRequestError(
      response.status,
      "invalid_status_summary_response",
      "The local API returned an invalid instance status summary.",
    );
  }
}

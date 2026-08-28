import {
  assertLocalJsonResponse,
  LocalApiRequestError,
  sendLocalJsonMutation,
} from "./localSession";

const INSTANCE_ID_PATTERN =
  /^inst\.[a-z0-9-]+\.[a-z0-9-]+\.[a-z0-9-]+\.[0-9]{2}$/u;
const IDEMPOTENCY_KEY_PATTERN = /^[\x21-\x7e]{8,240}$/u;
const RESOURCE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._/-]{0,239}$/u;
const MANUAL_EVENT_ID_PATTERN = /^manual-event-hmac-sha256-v1:[0-9a-f]{64}$/u;
const MAX_REQUEST_BYTES = 1_048_576;
const MAX_INPUT_DEPTH = 64;
const RECEIPT_FIELDS = new Set([
  "status",
  "disposition",
  "eventId",
  "workId",
  "runId",
  "executionMode",
  "instanceUrl",
  "runUrl",
]);
const UNSAFE_OBJECT_KEYS = new Set(["__proto__", "constructor", "prototype"]);

type JsonValue =
  | null
  | boolean
  | number
  | string
  | readonly JsonValue[]
  | { readonly [key: string]: JsonValue };
type JsonObject = Record<string, unknown>;

export type ManualDryRunExecutionMode = "dry_run" | "mock_execute";

export interface CreateManualDryRunInput {
  readonly instanceId: string;
  readonly input: Readonly<Record<string, unknown>>;
  readonly executionMode: ManualDryRunExecutionMode;
  readonly idempotencyKey: string;
  readonly signal?: AbortSignal;
}

export interface ManualDryRunReceipt {
  readonly status: "accepted";
  readonly disposition: "created" | "replayed";
  readonly eventId: string;
  readonly workId: string;
  readonly runId: string;
  readonly executionMode: ManualDryRunExecutionMode;
  readonly instanceUrl: string;
  readonly runUrl: string;
}

export interface ManualDryRunFieldError {
  readonly pointer: string;
  readonly code: string;
  readonly message: string;
}

export class ManualDryRunRequestError extends Error {
  readonly status: number;
  readonly code: string;
  readonly fieldErrors: readonly ManualDryRunFieldError[];

  constructor(
    status: number,
    code: string,
    message: string,
    fieldErrors: readonly ManualDryRunFieldError[] = [],
  ) {
    super(message);
    this.name = "ManualDryRunRequestError";
    this.status = status;
    this.code = code;
    this.fieldErrors = Object.freeze(
      fieldErrors.map((error) => Object.freeze({ ...error })),
    );
  }
}

class ContractViolation extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ContractViolation";
  }
}

function invalidRequest(): ManualDryRunRequestError {
  return new ManualDryRunRequestError(
    0,
    "invalid_manual_dry_run_request",
    "The manual dry-run request is invalid.",
  );
}

function invalidResponse(status: number): ManualDryRunRequestError {
  return new ManualDryRunRequestError(
    status,
    "invalid_manual_dry_run_response",
    "The local API returned an invalid manual dry-run receipt.",
  );
}

function errorMessage(status: number, code: string): string {
  if (code === "idempotency_conflict") {
    return "This retry key is already bound to different work.";
  }
  if (
    code === "request_validation_failed" ||
    code === "dry_run_input_invalid"
  ) {
    return "The manual dry-run input is invalid.";
  }
  if (code === "csrf_token_invalid") {
    return "The local session expired. Refresh it and try again.";
  }
  if (status === 403) return "This local session cannot create dry runs.";
  if (status === 404) return "The selected agent instance was not found.";
  if (status === 409) return "The selected agent cannot accept this dry run.";
  if (status === 503)
    return "Manual dry-run creation is temporarily unavailable.";
  return "The local API could not create this dry run.";
}

function fromLocalError(error: LocalApiRequestError): ManualDryRunRequestError {
  const message =
    error.status === 0 ||
    error.code === "invalid_json_response" ||
    error.code === "invalid_session_response"
      ? error.message
      : errorMessage(error.status, error.code);
  return new ManualDryRunRequestError(
    error.status,
    error.code,
    message,
    error.fieldErrors,
  );
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
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

function asString(value: unknown, label: string): string {
  if (typeof value !== "string") {
    throw new ContractViolation(`${label} must be a string`);
  }
  return value;
}

function normalizeInputValue(
  value: unknown,
  depth: number,
  active: WeakSet<object>,
): JsonValue {
  if (depth > MAX_INPUT_DEPTH) {
    throw new ContractViolation("manual dry-run input is too deep");
  }
  if (
    value === null ||
    typeof value === "boolean" ||
    typeof value === "string"
  ) {
    if (typeof value === "string" && value.length > MAX_REQUEST_BYTES) {
      throw new ContractViolation("manual dry-run input is too large");
    }
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new ContractViolation(
        "manual dry-run input numbers must be finite",
      );
    }
    return value;
  }
  if (typeof value !== "object") {
    throw new ContractViolation(
      "manual dry-run input must contain JSON values",
    );
  }
  if (active.has(value)) {
    throw new ContractViolation("manual dry-run input must not be cyclic");
  }
  active.add(value);
  try {
    if (Array.isArray(value)) {
      if (Object.getPrototypeOf(value) !== Array.prototype) {
        throw new ContractViolation(
          "manual dry-run input arrays must be plain",
        );
      }
      const keys = Reflect.ownKeys(value);
      if (
        value.length > MAX_REQUEST_BYTES ||
        keys.length !== value.length + 1 ||
        !keys.includes("length")
      ) {
        throw new ContractViolation("manual dry-run input arrays are invalid");
      }
      const result: JsonValue[] = [];
      for (let index = 0; index < value.length; index += 1) {
        const key = String(index);
        const descriptor = Object.getOwnPropertyDescriptor(value, key);
        if (
          descriptor === undefined ||
          !("value" in descriptor) ||
          !descriptor.enumerable
        ) {
          throw new ContractViolation(
            "manual dry-run input arrays are invalid",
          );
        }
        result.push(normalizeInputValue(descriptor.value, depth + 1, active));
      }
      return result;
    }

    const prototype = Object.getPrototypeOf(value) as object | null;
    if (prototype !== Object.prototype && prototype !== null) {
      throw new ContractViolation("manual dry-run input objects must be plain");
    }
    const keys = Reflect.ownKeys(value);
    if (keys.length > MAX_REQUEST_BYTES) {
      throw new ContractViolation("manual dry-run input objects are too large");
    }
    const result: Record<string, JsonValue> = {};
    for (const key of keys) {
      if (typeof key !== "string" || UNSAFE_OBJECT_KEYS.has(key)) {
        throw new ContractViolation(
          "manual dry-run input object keys are invalid",
        );
      }
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      if (
        descriptor === undefined ||
        !("value" in descriptor) ||
        !descriptor.enumerable
      ) {
        throw new ContractViolation("manual dry-run input objects are invalid");
      }
      result[key] = normalizeInputValue(descriptor.value, depth + 1, active);
    }
    return result;
  } finally {
    active.delete(value);
  }
}

function isExecutionMode(value: unknown): value is ManualDryRunExecutionMode {
  return value === "dry_run" || value === "mock_execute";
}

function serializeRequest(input: CreateManualDryRunInput): string {
  if (
    typeof input.instanceId !== "string" ||
    !INSTANCE_ID_PATTERN.test(input.instanceId) ||
    !isExecutionMode(input.executionMode) ||
    typeof input.idempotencyKey !== "string" ||
    !IDEMPOTENCY_KEY_PATTERN.test(input.idempotencyKey)
  ) {
    throw invalidRequest();
  }
  let normalized: JsonValue;
  try {
    normalized = normalizeInputValue(input.input, 1, new WeakSet());
  } catch {
    throw invalidRequest();
  }
  if (
    typeof normalized !== "object" ||
    normalized === null ||
    Array.isArray(normalized)
  ) {
    throw invalidRequest();
  }
  const body = JSON.stringify({
    input: normalized,
    executionMode: input.executionMode,
  });
  if (new TextEncoder().encode(body).byteLength > MAX_REQUEST_BYTES) {
    throw invalidRequest();
  }
  return body;
}

function resourceId(value: unknown, prefix: "work." | "run."): string {
  const result = asString(value, "manual dry-run resource ID");
  if (!result.startsWith(prefix) || !RESOURCE_ID_PATTERN.test(result)) {
    throw new ContractViolation("manual dry-run resource ID is invalid");
  }
  return result;
}

function normalizeReceipt(
  value: unknown,
  expected: {
    readonly instanceId: string;
    readonly executionMode: ManualDryRunExecutionMode;
  },
): ManualDryRunReceipt {
  const record = asRecord(value, "manual dry-run receipt");
  assertExactFields(record, RECEIPT_FIELDS, "manual dry-run receipt");
  if (record.status !== "accepted") {
    throw new ContractViolation("manual dry-run status is invalid");
  }
  if (record.disposition !== "created" && record.disposition !== "replayed") {
    throw new ContractViolation("manual dry-run disposition is invalid");
  }
  if (record.executionMode !== expected.executionMode) {
    throw new ContractViolation("manual dry-run execution mode is invalid");
  }
  const eventId = asString(record.eventId, "manual dry-run event ID");
  if (!MANUAL_EVENT_ID_PATTERN.test(eventId)) {
    throw new ContractViolation("manual dry-run event ID is invalid");
  }
  const workId = resourceId(record.workId, "work.");
  const runId = resourceId(record.runId, "run.");
  const instanceUrl = asString(
    record.instanceUrl,
    "manual dry-run instance URL",
  );
  const runUrl = asString(record.runUrl, "manual dry-run run URL");
  if (
    instanceUrl !==
      `/api/v1/agent-instances/${encodeURIComponent(expected.instanceId)}` ||
    runUrl !== `/api/v1/runs/${encodeURIComponent(runId)}`
  ) {
    throw new ContractViolation("manual dry-run resource URLs are invalid");
  }
  return Object.freeze({
    status: "accepted",
    disposition: record.disposition,
    eventId,
    workId,
    runId,
    executionMode: expected.executionMode,
    instanceUrl,
    runUrl,
  });
}

export function generateManualDryRunIdempotencyKey(): string {
  try {
    return globalThis.crypto.randomUUID();
  } catch {
    throw new ManualDryRunRequestError(
      0,
      "idempotency_key_unavailable",
      "A secure manual dry-run retry key could not be generated.",
    );
  }
}

export async function createManualDryRun(
  input: CreateManualDryRunInput,
): Promise<ManualDryRunReceipt> {
  const body = serializeRequest(input);
  const path = `/api/v1/agent-instances/${encodeURIComponent(input.instanceId)}/dry-runs`;
  let response: Response;
  try {
    response = await sendLocalJsonMutation({
      path,
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": input.idempotencyKey,
      },
      body,
      ...(input.signal === undefined ? {} : { signal: input.signal }),
    });
  } catch (error) {
    if (!(error instanceof LocalApiRequestError)) throw error;
    throw fromLocalError(error);
  }

  if (response.status !== 202) throw invalidResponse(response.status);
  try {
    assertLocalJsonResponse(response, "manual dry-run receipt");
  } catch (error) {
    if (!(error instanceof LocalApiRequestError)) throw error;
    throw invalidResponse(response.status);
  }
  let value: unknown;
  try {
    value = (await response.json()) as unknown;
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw invalidResponse(response.status);
  }
  try {
    return normalizeReceipt(value, {
      instanceId: input.instanceId,
      executionMode: input.executionMode,
    });
  } catch (error) {
    if (!(error instanceof ContractViolation)) throw error;
    throw invalidResponse(response.status);
  }
}

const SESSION_PATH = "/api/v1/session";
const API_PREFIX = "/api/v1/";

const AUTHORITY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._/-]{0,239}$/u;
const SAFE_CODE_PATTERN = /^[a-z][a-z0-9_]{0,127}$/u;
const CORRELATION_ID_PATTERN = /^correlation\.api\.[0-9a-f]{32}$/u;
const CSRF_TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,128}$/u;
const RESOURCE_VERSION_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$/u;
const FIELD_POINTER_PATTERN =
  /^\/(?:body|path|query|header|cookie|request|input(?:\/[A-Za-z0-9_.-]{1,100}){0,64})$/u;

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

type JsonObject = Record<string, unknown>;

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

export interface LocalApiFieldError {
  readonly pointer: string;
  readonly code: string;
  readonly message: string;
}

export class LocalApiRequestError extends Error {
  readonly status: number;
  readonly code: string;
  readonly currentResourceVersion: number | string | null;
  readonly fieldErrors: readonly LocalApiFieldError[];

  constructor(
    status: number,
    code: string,
    message: string,
    options: {
      readonly currentResourceVersion?: number | string | null;
      readonly fieldErrors?: readonly LocalApiFieldError[];
    } = {},
  ) {
    super(message);
    this.name = "LocalApiRequestError";
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
  readonly fieldErrors: readonly LocalApiFieldError[];
}

export interface LocalJsonMutation {
  readonly path: string;
  readonly method: "POST" | "PATCH";
  readonly headers: Readonly<Record<string, string>>;
  readonly body: string;
  readonly signal?: AbortSignal;
}

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

function hasOwn(value: JsonObject, field: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, field);
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
    [...required].some((field) => !hasOwn(value, field)) ||
    fields.some((field) => !required.has(field) && !optional.has(field))
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

function asBoundedString(
  value: unknown,
  label: string,
  maximum: number,
): string {
  const result = asString(value, label);
  if (result.length === 0 || result.length > maximum) {
    throw new ContractViolation(`${label} is invalid`);
  }
  return result;
}

function asBoolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") {
    throw new ContractViolation(`${label} must be a boolean`);
  }
  return value;
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
    throw new ContractViolation(`${label} is invalid`);
  }
  const authorities = value.map((item, index) =>
    asString(item, `${label}[${String(index)}]`),
  );
  if (
    authorities.some((authority) => !AUTHORITY_PATTERN.test(authority)) ||
    new Set(authorities).size !== authorities.length ||
    authorities.some(
      (authority, index) =>
        index > 0 && (authorities[index - 1] ?? authority) >= authority,
    )
  ) {
    throw new ContractViolation(`${label} is invalid`);
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
  if (record.authMode !== "local") {
    throw new ContractViolation("session.authMode is unsupported");
  }
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
  const connectorMode = asString(record.connectorMode, "session.connectorMode");
  if (
    connectorMode.length === 0 ||
    connectorMode.trim() !== connectorMode ||
    Array.from(connectorMode).length > 64
  ) {
    throw new ContractViolation("session.connectorMode is invalid");
  }
  const networkPermission = asBoolean(
    record.networkPermission,
    "session.networkPermission",
  );
  if (record.warning !== "Local identity — not production authentication") {
    throw new ContractViolation("session.warning is unsupported");
  }
  const csrfToken = asString(record.csrfToken, "session.csrfToken");
  if (!CSRF_TOKEN_PATTERN.test(csrfToken)) {
    throw new ContractViolation("session.csrfToken is invalid");
  }
  if (record.csrfHeaderName !== "X-CSRF-Token") {
    throw new ContractViolation("session.csrfHeaderName is unsupported");
  }
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

function normalizeFieldErrors(value: unknown): readonly LocalApiFieldError[] {
  if (!Array.isArray(value) || value.length > 32) {
    throw new ContractViolation("problem field errors are invalid");
  }
  return Object.freeze(
    value.map((item, index) => {
      const record = asRecord(item, `problem.field_errors[${String(index)}]`);
      assertExactFields(record, FIELD_ERROR_FIELDS, "problem field error");
      const pointer = asBoundedString(
        record.pointer,
        "problem field error pointer",
        1_000,
      );
      const code = asBoundedString(
        record.code,
        "problem field error code",
        128,
      );
      const message = asBoundedString(
        record.message,
        "problem field error message",
        240,
      );
      if (
        !FIELD_POINTER_PATTERN.test(pointer) ||
        !SAFE_CODE_PATTERN.test(code)
      ) {
        throw new ContractViolation("problem field error is invalid");
      }
      return Object.freeze({ pointer, code, message });
    }),
  );
}

function normalizeSafeProblem(value: unknown, response: Response): SafeProblem {
  const record = asRecord(value, "problem");
  assertAllowedAndRequiredFields(
    record,
    PROBLEM_REQUIRED_FIELDS,
    PROBLEM_OPTIONAL_FIELDS,
    "problem",
  );
  const code = asBoundedString(record.code, "problem.code", 128);
  const correlationId = asBoundedString(
    record.correlation_id,
    "problem.correlation_id",
    128,
  );
  if (
    !SAFE_CODE_PATTERN.test(code) ||
    record.status !== response.status ||
    record.type !== `urn:marketing-agents:problem:${code}` ||
    !CORRELATION_ID_PATTERN.test(correlationId) ||
    record.instance !== `urn:marketing-agents:request:${correlationId}` ||
    response.headers.get("X-Correlation-ID") !== correlationId
  ) {
    throw new ContractViolation("problem identity is invalid");
  }
  asBoundedString(record.title, "problem.title", 120);
  asBoundedString(record.detail, "problem.detail", 500);

  let currentResourceVersion: number | string | null = null;
  if (hasOwn(record, "current_resource_version")) {
    const current = record.current_resource_version;
    if (
      typeof current === "number" &&
      Number.isSafeInteger(current) &&
      current >= 0
    ) {
      currentResourceVersion = current;
    } else if (
      typeof current === "string" &&
      RESOURCE_VERSION_PATTERN.test(current)
    ) {
      currentResourceVersion = current;
    } else {
      throw new ContractViolation(
        "problem current resource version is invalid",
      );
    }
  }
  if (hasOwn(record, "retry_after_seconds")) {
    const retryAfter = record.retry_after_seconds;
    if (
      typeof retryAfter !== "number" ||
      !Number.isSafeInteger(retryAfter) ||
      retryAfter < 0 ||
      retryAfter > 86_400
    ) {
      throw new ContractViolation("problem retry delay is invalid");
    }
  }
  const fieldErrors = hasOwn(record, "field_errors")
    ? normalizeFieldErrors(record.field_errors)
    : Object.freeze([]);
  return Object.freeze({ code, currentResourceVersion, fieldErrors });
}

function mediaType(response: Response): string | null {
  return (
    response.headers
      .get("Content-Type")
      ?.split(";", 1)[0]
      ?.trim()
      .toLowerCase() ?? null
  );
}

function isSameOriginApiPath(path: string): boolean {
  return path.startsWith(API_PREFIX) && !path.startsWith("//");
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

async function sameOriginFetch(
  path: string,
  request: RequestInit,
): Promise<Response> {
  if (!isSameOriginApiPath(path)) {
    throw new LocalApiRequestError(
      0,
      "unsafe_api_path",
      "The API request path is not allowed.",
    );
  }
  try {
    return await fetch(path, request);
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new LocalApiRequestError(
      0,
      "api_unreachable",
      "The local API is not ready. Start it and try again.",
    );
  }
}

export function normalizeLocalSession(value: unknown): LocalSession {
  return normalizeLocalSessionEnvelope(value).session;
}

export function clearLocalSession(): void {
  currentSession = undefined;
}

export async function localApiResponseError(
  response: Response,
): Promise<LocalApiRequestError> {
  let problem: SafeProblem | null = null;
  if (mediaType(response) === "application/problem+json") {
    try {
      problem = normalizeSafeProblem(
        (await response.json()) as unknown,
        response,
      );
    } catch (error) {
      if (isAbortError(error)) throw error;
      problem = null;
    }
  }
  return new LocalApiRequestError(
    response.status,
    problem?.code ?? "api_request_failed",
    "The local API could not complete this request.",
    {
      currentResourceVersion: problem?.currentResourceVersion ?? null,
      fieldErrors: problem?.fieldErrors ?? [],
    },
  );
}

export function assertLocalJsonResponse(
  response: Response,
  label: string,
): void {
  if (mediaType(response) !== "application/json") {
    throw new LocalApiRequestError(
      response.status,
      "invalid_json_response",
      `The local API returned an invalid ${label}.`,
    );
  }
}

export async function fetchLocalSession(
  signal?: AbortSignal,
): Promise<LocalSession> {
  const request: RequestInit = {
    method: "GET",
    cache: "no-store",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  };
  if (signal !== undefined) request.signal = signal;
  const response = await sameOriginFetch(SESSION_PATH, request);
  if (!response.ok) throw await localApiResponseError(response);
  assertLocalJsonResponse(response, "local session");
  let body: unknown;
  try {
    body = (await response.json()) as unknown;
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new LocalApiRequestError(
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
    throw new LocalApiRequestError(
      response.status,
      "invalid_session_response",
      "The local API returned an invalid local session.",
    );
  }
}

async function requiredSession(signal?: AbortSignal): Promise<SessionEnvelope> {
  if (currentSession === undefined) await fetchLocalSession(signal);
  if (currentSession === undefined) {
    throw new LocalApiRequestError(
      0,
      "invalid_session_response",
      "The local API returned an invalid local session.",
    );
  }
  return currentSession;
}

function mutationRequest(
  input: LocalJsonMutation,
  session: SessionEnvelope,
): RequestInit {
  if (
    Object.keys(input.headers).some(
      (name) => name.toLowerCase() === "x-csrf-token",
    )
  ) {
    throw new LocalApiRequestError(
      0,
      "csrf_header_forbidden",
      "The CSRF header is controlled by the local session boundary.",
    );
  }
  const request: RequestInit = {
    method: input.method,
    cache: "no-store",
    credentials: "same-origin",
    headers: {
      ...input.headers,
      [session.csrfHeaderName]: session.csrfToken,
    },
    body: input.body,
  };
  if (input.signal !== undefined) request.signal = input.signal;
  return request;
}

export async function sendLocalJsonMutation(
  input: LocalJsonMutation,
): Promise<Response> {
  let session = await requiredSession(input.signal);
  let response = await sameOriginFetch(
    input.path,
    mutationRequest(input, session),
  );
  if (response.ok) return response;

  const firstError = await localApiResponseError(response);
  if (firstError.status !== 403 || firstError.code !== "csrf_token_invalid") {
    throw firstError;
  }

  await fetchLocalSession(input.signal);
  session = await requiredSession(input.signal);
  response = await sameOriginFetch(input.path, mutationRequest(input, session));
  if (!response.ok) throw await localApiResponseError(response);
  return response;
}

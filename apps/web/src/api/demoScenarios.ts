import {
  assertLocalJsonResponse,
  localApiResponseError,
  LocalApiRequestError,
  sendLocalJsonMutation,
} from "./localSession";

export const SOCIAL_DRAFT_SCENARIO_ID =
  "demo.social-media.content-draft.v1" as const;
export const SOCIAL_DRAFT_TEMPLATE_ID =
  "tpl.social-media.new-content.linkedin-post-drafter" as const;
export const SOCIAL_DRAFT_INSTANCE_ID =
  "inst.social-media.new-content.linkedin-post-drafter.01" as const;
export const BLOG_CONTENT_REVIEW_SCENARIO_ID =
  "demo.blog-seo.content-review.v1" as const;
export const BLOG_CONTENT_REVIEW_TEMPLATE_ID =
  "tpl.blog-seo.new-content.blog-post-updater" as const;
export const BLOG_CONTENT_REVIEW_INSTANCE_ID =
  "inst.blog-seo.new-content.blog-post-updater.01" as const;
export const EMAIL_SIGNUP_SCENARIO_ID =
  "demo.email.signup-onboarding.v1" as const;
export const EMAIL_NEWSLETTER_TEMPLATE_ID =
  "tpl.email.newsletter.newsletter-subscriber" as const;
export const EMAIL_NEWSLETTER_INSTANCE_ID =
  "inst.email.newsletter.newsletter-subscriber.01" as const;
export const EMAIL_ONBOARDING_TEMPLATE_ID =
  "tpl.email.lifecycle-marketing.customer-onboarder" as const;
export const EMAIL_ONBOARDING_INSTANCE_ID =
  "inst.email.lifecycle-marketing.customer-onboarder.01" as const;
export const COMMUNITY_REMINDER_SCENARIO_ID =
  "demo.community.reminder-draft.v1" as const;
export const COMMUNITY_REMINDER_TEMPLATE_ID =
  "tpl.community.events.live-session-reminder" as const;
export const COMMUNITY_REMINDER_INSTANCE_ID =
  "inst.community.events.live-session-reminder.01" as const;

const DISCOVERY_PATH = "/api/v1/demo-scenarios";
const IDEMPOTENCY_KEY_PATTERN = /^[\x21-\x7e]{8,240}$/u;
const RESOURCE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$/u;
const STATE_NAME_PATTERN = /^[a-z][a-z0-9_]{0,63}$/u;
const SAFE_SUBMIT_VERB_PATTERN = /^[A-Za-z][A-Za-z0-9 ,.'&/-]{0,79}$/u;
const SAFE_SUBMIT_MARKERS = [
  "draft",
  "review",
  "simulate",
  "propose",
  "approve",
] as const;
const UNSAFE_SUBMIT_MARKERS = [
  "publish",
  "send",
  "subscribe",
  "enroll",
  "crm",
  "cms",
  "calendar",
] as const;
const MANUAL_EVENT_ID_PATTERN = /^manual-event-hmac-sha256-v1:[0-9a-f]{64}$/u;
const MAX_JSON_DEPTH = 64;
const MAX_REQUEST_BYTES = 1_048_576;
const MAX_SCENARIOS = 16;
const UNSAFE_OBJECT_KEYS = new Set(["__proto__", "constructor", "prototype"]);

const LIST_FIELDS = new Set(["items"]);
const SCENARIO_FIELDS = new Set([
  "id",
  "version",
  "displayName",
  "description",
  "workflowId",
  "effect",
  "mode",
  "selectedAgents",
  "inputSchema",
  "preset",
  "safeSubmitVerb",
  "expected",
]);
const SELECTED_AGENT_FIELDS = new Set(["templateId", "instanceId"]);
const EXPECTED_FIELDS = new Set([
  "statePath",
  "modelCalls",
  "connectorCalls",
  "externalActions",
  "approvals",
  "externalWrites",
]);
const RECEIPT_FIELDS = new Set([
  "status",
  "disposition",
  "scenarioId",
  "eventId",
  "workId",
  "runId",
  "executionMode",
  "instanceUrl",
  "runUrl",
  "timelineUrl",
  "artifactsUrl",
]);

export type DemoJsonValue =
  null | boolean | number | string | readonly DemoJsonValue[] | DemoJsonObject;

export interface DemoJsonObject {
  readonly [key: string]: DemoJsonValue;
}

export interface DemoScenario {
  readonly id: string;
  readonly version: number;
  readonly displayName: string;
  readonly description: string;
  readonly workflowId: string;
  readonly effect: "read_only" | "mutating";
  readonly mode: "deterministic_mock";
  readonly selectedAgents: readonly {
    readonly templateId: string;
    readonly instanceId: string;
  }[];
  readonly inputSchema: DemoJsonObject;
  readonly preset: DemoJsonObject;
  readonly safeSubmitVerb: string;
  readonly expected: {
    readonly statePath: readonly string[];
    readonly modelCalls: number;
    readonly connectorCalls: number;
    readonly externalActions: number;
    readonly approvals: number;
    readonly externalWrites: number;
  };
}

export interface CreateDemoScenarioRunInput {
  readonly scenarioId: string;
  readonly instanceId: string;
  readonly overrides: Readonly<Record<string, unknown>>;
  readonly idempotencyKey: string;
  readonly expectedExecutionMode: "dry_run" | "mock_execute";
  readonly signal?: AbortSignal;
}

export interface DemoScenarioRunReceipt {
  readonly status: "accepted";
  readonly disposition: "created" | "replayed";
  readonly scenarioId: string;
  readonly eventId: string;
  readonly workId: string;
  readonly runId: string;
  readonly executionMode: "dry_run" | "mock_execute";
  readonly instanceUrl: string;
  readonly runUrl: string;
  readonly timelineUrl: string;
  readonly artifactsUrl: string;
}

export class DemoScenarioRequestError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "DemoScenarioRequestError";
    this.status = status;
    this.code = code;
  }
}

class ContractViolation extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ContractViolation";
  }
}

type JsonRecord = Record<string, unknown>;

function invalidDiscovery(status = 0): DemoScenarioRequestError {
  return new DemoScenarioRequestError(
    status,
    "invalid_demo_scenarios_response",
    "The local API returned an invalid demo scenario catalog.",
  );
}

function invalidRequest(): DemoScenarioRequestError {
  return new DemoScenarioRequestError(
    0,
    "invalid_demo_scenario_request",
    "The demo scenario request is invalid.",
  );
}

function invalidReceipt(status: number): DemoScenarioRequestError {
  return new DemoScenarioRequestError(
    status,
    "invalid_demo_scenario_response",
    "The local API returned an invalid demo scenario receipt.",
  );
}

function fromLocalError(error: LocalApiRequestError): DemoScenarioRequestError {
  let message = "The local API could not create this demo run.";
  if (error.status === 0) message = error.message;
  else if (error.code === "idempotency_conflict") {
    message = "This retry key is already bound to different work.";
  } else if (
    error.code === "request_validation_failed" ||
    error.code === "dry_run_input_invalid" ||
    error.code === "demo_scenario_invalid" ||
    error.code === "demo_scenario_input_invalid"
  ) {
    message = "The safe demo preset is no longer valid.";
  } else if (error.code === "csrf_token_invalid") {
    message = "The local session expired. Refresh it and try again.";
  } else if (error.status === 403) {
    message = "This local session cannot create demo runs.";
  } else if (error.status === 404) {
    message = "The selected demo scenario was not found.";
  } else if (error.status === 409) {
    message = "The selected demo scenario cannot run right now.";
  } else if (error.status === 503) {
    message = "Demo run creation is temporarily unavailable.";
  }
  return new DemoScenarioRequestError(error.status, error.code, message);
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function asRecord(value: unknown, label: string): JsonRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ContractViolation(`${label} must be an object`);
  }
  const prototype = Object.getPrototypeOf(value) as object | null;
  if (prototype !== Object.prototype && prototype !== null) {
    throw new ContractViolation(`${label} must be a plain object`);
  }
  return value as JsonRecord;
}

function assertExactFields(
  value: JsonRecord,
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

function boundedString(value: unknown, label: string, maximum: number): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > maximum ||
    value.trim() !== value
  ) {
    throw new ContractViolation(`${label} is invalid`);
  }
  return value;
}

function resourceId(value: unknown, label: string): string {
  const result = boundedString(value, label, 240);
  if (!RESOURCE_ID_PATTERN.test(result)) {
    throw new ContractViolation(`${label} is invalid`);
  }
  return result;
}

function safeSubmitVerb(value: unknown): string {
  const result = boundedString(value, "demo scenario submit verb", 80);
  const normalized = result.toLowerCase();
  if (
    !SAFE_SUBMIT_VERB_PATTERN.test(result) ||
    !SAFE_SUBMIT_MARKERS.some((marker) => normalized.includes(marker)) ||
    UNSAFE_SUBMIT_MARKERS.some((marker) => normalized.includes(marker))
  ) {
    throw new ContractViolation("demo scenario submit verb is unsafe");
  }
  return result;
}

function normalizeJson(
  value: unknown,
  depth: number,
  active: WeakSet<object>,
): DemoJsonValue {
  if (depth > MAX_JSON_DEPTH) {
    throw new ContractViolation("demo JSON is too deep");
  }
  if (
    value === null ||
    typeof value === "boolean" ||
    typeof value === "string"
  ) {
    if (typeof value === "string" && value.length > MAX_REQUEST_BYTES) {
      throw new ContractViolation("demo JSON is too large");
    }
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new ContractViolation("demo JSON numbers must be finite");
    }
    return value;
  }
  if (typeof value !== "object" || active.has(value)) {
    throw new ContractViolation("demo JSON is invalid");
  }
  active.add(value);
  try {
    if (Array.isArray(value)) {
      if (Object.getPrototypeOf(value) !== Array.prototype) {
        throw new ContractViolation("demo JSON arrays must be plain");
      }
      const keys = Reflect.ownKeys(value);
      if (
        value.length > MAX_REQUEST_BYTES ||
        keys.length !== value.length + 1 ||
        !keys.includes("length")
      ) {
        throw new ContractViolation("demo JSON array is invalid");
      }
      const result: DemoJsonValue[] = [];
      for (let index = 0; index < value.length; index += 1) {
        const descriptor = Object.getOwnPropertyDescriptor(
          value,
          String(index),
        );
        if (
          descriptor === undefined ||
          !("value" in descriptor) ||
          !descriptor.enumerable
        ) {
          throw new ContractViolation("demo JSON array is invalid");
        }
        result.push(normalizeJson(descriptor.value, depth + 1, active));
      }
      return Object.freeze(result);
    }
    const record = asRecord(value, "demo JSON object");
    const keys = Reflect.ownKeys(record);
    if (keys.length > MAX_REQUEST_BYTES) {
      throw new ContractViolation("demo JSON object is too large");
    }
    const result: Record<string, DemoJsonValue> = {};
    for (const key of keys) {
      if (typeof key !== "string" || UNSAFE_OBJECT_KEYS.has(key)) {
        throw new ContractViolation("demo JSON object key is invalid");
      }
      const descriptor = Object.getOwnPropertyDescriptor(record, key);
      if (
        descriptor === undefined ||
        !("value" in descriptor) ||
        !descriptor.enumerable
      ) {
        throw new ContractViolation("demo JSON object is invalid");
      }
      result[key] = normalizeJson(descriptor.value, depth + 1, active);
    }
    return Object.freeze(result);
  } finally {
    active.delete(value);
  }
}

function jsonObject(value: unknown, label: string): DemoJsonObject {
  const result = normalizeJson(value, 1, new WeakSet());
  if (typeof result !== "object" || result === null || Array.isArray(result)) {
    throw new ContractViolation(`${label} must be an object`);
  }
  return result as DemoJsonObject;
}

function statePath(value: unknown): readonly string[] {
  if (!Array.isArray(value) || value.length === 0 || value.length > 32) {
    throw new ContractViolation("demo scenario state path is invalid");
  }
  const states: string[] = [];
  for (const state of value as unknown[]) {
    if (typeof state !== "string" || !STATE_NAME_PATTERN.test(state)) {
      throw new ContractViolation("demo scenario state path is invalid");
    }
    states.push(state);
  }
  if (new Set(states).size !== states.length) {
    throw new ContractViolation("demo scenario state path is invalid");
  }
  return Object.freeze(states);
}

function expectedBehavior(value: unknown): DemoScenario["expected"] {
  const record = asRecord(value, "demo scenario expected behavior");
  assertExactFields(record, EXPECTED_FIELDS, "demo scenario expected behavior");
  const states = statePath(record.statePath);
  const counts = [
    record.modelCalls,
    record.connectorCalls,
    record.externalActions,
    record.approvals,
    record.externalWrites,
  ];
  if (
    counts.some(
      (count) =>
        typeof count !== "number" ||
        !Number.isSafeInteger(count) ||
        count < 0 ||
        count > 1_000,
    )
  ) {
    throw new ContractViolation("demo scenario expected behavior is invalid");
  }
  return Object.freeze({
    statePath: states,
    modelCalls: record.modelCalls as number,
    connectorCalls: record.connectorCalls as number,
    externalActions: record.externalActions as number,
    approvals: record.approvals as number,
    externalWrites: record.externalWrites as number,
  });
}

function normalizeScenario(value: unknown): DemoScenario {
  const record = asRecord(value, "demo scenario");
  assertExactFields(record, SCENARIO_FIELDS, "demo scenario");
  const id = resourceId(record.id, "demo scenario ID");
  if (
    typeof record.version !== "number" ||
    !Number.isSafeInteger(record.version) ||
    record.version < 1 ||
    record.version > 1_000_000
  ) {
    throw new ContractViolation("demo scenario version is invalid");
  }
  if (
    !Array.isArray(record.selectedAgents) ||
    record.selectedAgents.length === 0 ||
    record.selectedAgents.length > 16
  ) {
    throw new ContractViolation("demo scenario agent selection is invalid");
  }
  const selectedAgents = record.selectedAgents.map((value) => {
    const selectedAgent = asRecord(value, "demo scenario selected agent");
    assertExactFields(
      selectedAgent,
      SELECTED_AGENT_FIELDS,
      "demo scenario selected agent",
    );
    const templateId = resourceId(
      selectedAgent.templateId,
      "demo scenario template ID",
    );
    const instanceId = resourceId(
      selectedAgent.instanceId,
      "demo scenario instance ID",
    );
    if (!templateId.startsWith("tpl.") || !instanceId.startsWith("inst.")) {
      throw new ContractViolation("demo scenario agent selection is invalid");
    }
    return Object.freeze({ templateId, instanceId });
  });
  if (
    new Set(selectedAgents.map((agent) => agent.instanceId)).size !==
    selectedAgents.length
  ) {
    throw new ContractViolation("demo scenario agent selection is invalid");
  }
  if (
    (record.effect !== "read_only" && record.effect !== "mutating") ||
    record.mode !== "deterministic_mock"
  ) {
    throw new ContractViolation("demo scenario safety contract is invalid");
  }
  const submitVerb = safeSubmitVerb(record.safeSubmitVerb);
  return Object.freeze({
    id,
    version: record.version,
    displayName: boundedString(record.displayName, "demo scenario name", 120),
    description: boundedString(
      record.description,
      "demo scenario description",
      500,
    ),
    workflowId: resourceId(record.workflowId, "demo scenario workflow ID"),
    effect: record.effect,
    mode: "deterministic_mock",
    selectedAgents: Object.freeze(selectedAgents),
    inputSchema: jsonObject(record.inputSchema, "demo scenario input schema"),
    preset: jsonObject(record.preset, "demo scenario preset"),
    safeSubmitVerb: submitVerb,
    expected: expectedBehavior(record.expected),
  });
}

function normalizeScenarioList(value: unknown): readonly DemoScenario[] {
  const record = asRecord(value, "demo scenario catalog");
  assertExactFields(record, LIST_FIELDS, "demo scenario catalog");
  if (
    !Array.isArray(record.items) ||
    record.items.length === 0 ||
    record.items.length > MAX_SCENARIOS
  ) {
    throw new ContractViolation("demo scenario catalog items are invalid");
  }
  const items = record.items.map((item) => normalizeScenario(item));
  if (new Set(items.map((item) => item.id)).size !== items.length) {
    throw new ContractViolation("demo scenario IDs must be unique");
  }
  return Object.freeze(items);
}

function serializeRequest(input: CreateDemoScenarioRunInput): string {
  if (
    typeof input.scenarioId !== "string" ||
    !RESOURCE_ID_PATTERN.test(input.scenarioId) ||
    typeof input.instanceId !== "string" ||
    !RESOURCE_ID_PATTERN.test(input.instanceId) ||
    typeof input.idempotencyKey !== "string" ||
    !IDEMPOTENCY_KEY_PATTERN.test(input.idempotencyKey)
  ) {
    throw invalidRequest();
  }
  let overrides: DemoJsonObject;
  try {
    overrides = jsonObject(input.overrides, "demo scenario overrides");
  } catch {
    throw invalidRequest();
  }
  const body = JSON.stringify({ overrides });
  if (new TextEncoder().encode(body).byteLength > MAX_REQUEST_BYTES) {
    throw invalidRequest();
  }
  return body;
}

function prefixedResourceId(value: unknown, prefix: "work." | "run."): string {
  const result = resourceId(value, "demo scenario resource ID");
  if (!result.startsWith(prefix)) {
    throw new ContractViolation("demo scenario resource ID is invalid");
  }
  return result;
}

function normalizeReceipt(
  value: unknown,
  input: Pick<
    CreateDemoScenarioRunInput,
    "scenarioId" | "instanceId" | "expectedExecutionMode"
  >,
): DemoScenarioRunReceipt {
  const record = asRecord(value, "demo scenario receipt");
  assertExactFields(record, RECEIPT_FIELDS, "demo scenario receipt");
  if (
    record.status !== "accepted" ||
    (record.disposition !== "created" && record.disposition !== "replayed") ||
    record.scenarioId !== input.scenarioId ||
    record.executionMode !== input.expectedExecutionMode
  ) {
    throw new ContractViolation("demo scenario receipt binding is invalid");
  }
  const eventId = boundedString(record.eventId, "demo scenario event ID", 128);
  if (!MANUAL_EVENT_ID_PATTERN.test(eventId)) {
    throw new ContractViolation("demo scenario event ID is invalid");
  }
  const workId = prefixedResourceId(record.workId, "work.");
  const runId = prefixedResourceId(record.runId, "run.");
  const instanceUrl = boundedString(
    record.instanceUrl,
    "demo scenario instance URL",
    512,
  );
  const runUrl = boundedString(record.runUrl, "demo scenario run URL", 512);
  const timelineUrl = boundedString(
    record.timelineUrl,
    "demo scenario timeline URL",
    512,
  );
  const artifactsUrl = boundedString(
    record.artifactsUrl,
    "demo scenario artifacts URL",
    512,
  );
  if (
    instanceUrl !==
      `/api/v1/agent-instances/${encodeURIComponent(input.instanceId)}` ||
    runUrl !== `/api/v1/runs/${encodeURIComponent(runId)}` ||
    timelineUrl !== `/api/v1/runs/${encodeURIComponent(runId)}/timeline` ||
    artifactsUrl !== `/api/v1/runs/${encodeURIComponent(runId)}/artifacts`
  ) {
    throw new ContractViolation("demo scenario receipt URLs are invalid");
  }
  return Object.freeze({
    status: "accepted",
    disposition: record.disposition,
    scenarioId: input.scenarioId,
    eventId,
    workId,
    runId,
    executionMode: input.expectedExecutionMode,
    instanceUrl,
    runUrl,
    timelineUrl,
    artifactsUrl,
  });
}

export function generateDemoScenarioIdempotencyKey(): string {
  try {
    return globalThis.crypto.randomUUID();
  } catch {
    throw new DemoScenarioRequestError(
      0,
      "idempotency_key_unavailable",
      "A secure demo retry key could not be generated.",
    );
  }
}

export async function fetchDemoScenarios(
  signal?: AbortSignal,
): Promise<readonly DemoScenario[]> {
  const request: RequestInit = {
    method: "GET",
    cache: "no-store",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  };
  if (signal !== undefined) request.signal = signal;
  let response: Response;
  try {
    response = await fetch(DISCOVERY_PATH, request);
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new DemoScenarioRequestError(
      0,
      "api_unreachable",
      "The local API is not ready. Start it and try again.",
    );
  }
  if (!response.ok) {
    throw fromLocalError(await localApiResponseError(response));
  }
  try {
    assertLocalJsonResponse(response, "demo scenario catalog");
  } catch {
    throw invalidDiscovery(response.status);
  }
  let value: unknown;
  try {
    value = (await response.json()) as unknown;
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw invalidDiscovery(response.status);
  }
  try {
    return normalizeScenarioList(value);
  } catch (error) {
    if (!(error instanceof ContractViolation)) throw error;
    throw invalidDiscovery(response.status);
  }
}

export async function createDemoScenarioRun(
  input: CreateDemoScenarioRunInput,
): Promise<DemoScenarioRunReceipt> {
  const body = serializeRequest(input);
  const path = `/api/v1/demo-scenarios/${encodeURIComponent(input.scenarioId)}/runs`;
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
  if (response.status !== 202) throw invalidReceipt(response.status);
  try {
    assertLocalJsonResponse(response, "demo scenario receipt");
  } catch {
    throw invalidReceipt(response.status);
  }
  let value: unknown;
  try {
    value = (await response.json()) as unknown;
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw invalidReceipt(response.status);
  }
  try {
    return normalizeReceipt(value, input);
  } catch (error) {
    if (!(error instanceof ContractViolation)) throw error;
    throw invalidReceipt(response.status);
  }
}

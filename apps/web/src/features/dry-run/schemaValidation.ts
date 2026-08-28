// WEB-04 validates UI drafts without retaining or reflecting submitted values.
import {
  INPUT_POINTER,
  type CompiledArraySchema,
  type CompiledObjectSchema,
  type CompiledSchema,
  type CompiledStringSchema,
  type JsonInputObject,
  type JsonInputValue,
} from "./schemaModel";

const MAX_ISSUES = 64;
const NUMBER_DRAFT = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/u;
const INTEGER_DRAFT = /^[+-]?\d+$/u;
const DATE_VALUE = /^(\d{4})-(\d{2})-(\d{2})$/u;
const DATE_TIME_VALUE =
  /^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$/u;
const URI_SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*:/u;
const EMAIL_VALUE = /^[^\s@]+@[^\s@]+$/u;

export type SchemaValidationIssueCode =
  | "required"
  | "type"
  | "enum"
  | "min_length"
  | "max_length"
  | "pattern"
  | "format"
  | "minimum"
  | "maximum"
  | "min_items"
  | "max_items"
  | "additional_property"
  | "server_rejected";

export interface SchemaValidationIssue {
  readonly pointer: string;
  readonly code: SchemaValidationIssueCode;
  readonly message: string;
}

export type SchemaValidationResult =
  | {
      readonly ok: true;
      readonly input: JsonInputObject;
    }
  | {
      readonly ok: false;
      readonly issues: readonly SchemaValidationIssue[];
    };

interface ValidationState {
  readonly issues: SchemaValidationIssue[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    return false;
  const prototype = Object.getPrototypeOf(value) as unknown;
  return prototype === Object.prototype || prototype === null;
}

function addIssue(
  state: ValidationState,
  pointer: string,
  code: SchemaValidationIssueCode,
  message: string,
): void {
  if (state.issues.length >= MAX_ISSUES) return;
  state.issues.push(Object.freeze({ pointer, code, message }));
}

function numericValue(value: unknown, integer: boolean): number | null {
  if (typeof value === "number") {
    if (!Number.isFinite(value) || (integer && !Number.isSafeInteger(value))) {
      return null;
    }
    return value;
  }
  if (typeof value !== "string") return null;
  const expression = integer ? INTEGER_DRAFT : NUMBER_DRAFT;
  if (!expression.test(value)) return null;
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || (integer && !Number.isSafeInteger(parsed))) {
    return null;
  }
  return parsed;
}

function validDate(value: string): boolean {
  const match = DATE_VALUE.exec(value);
  if (match === null) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(0, 0, 0, 0);
  return (
    date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day
  );
}

function validUri(value: string): boolean {
  if (!URI_SCHEME.test(value)) return false;
  try {
    const parsed = new URL(value);
    return parsed.protocol.length > 1;
  } catch {
    return false;
  }
}

function validFormat(schema: CompiledStringSchema, value: string): boolean {
  switch (schema.format) {
    case null:
      return true;
    case "date":
      return validDate(value);
    case "date-time":
      return (
        DATE_TIME_VALUE.test(value) &&
        validDate(value.slice(0, 10)) &&
        Number.isFinite(Date.parse(value))
      );
    case "uri":
      return validUri(value);
    case "email":
      return EMAIL_VALUE.test(value);
  }
}

function isBlankNumericDraft(schema: CompiledSchema, value: unknown): boolean {
  return (
    (schema.kind === "number" || schema.kind === "integer") &&
    typeof value === "string" &&
    value.trim().length === 0
  );
}

function validateString(
  schema: CompiledStringSchema,
  value: unknown,
  pointer: string,
  state: ValidationState,
): string | undefined {
  if (typeof value !== "string") {
    addIssue(state, pointer, "type", "Enter a text value.");
    return undefined;
  }
  const length = Array.from(value).length;
  if (length < schema.minLength) {
    addIssue(
      state,
      pointer,
      "min_length",
      "This value is shorter than allowed.",
    );
  }
  if (length > schema.maxLength) {
    addIssue(
      state,
      pointer,
      "max_length",
      "This value is longer than allowed.",
    );
  }
  if (schema.enumValues !== null && !schema.enumValues.includes(value)) {
    addIssue(state, pointer, "enum", "Choose one of the allowed values.");
  }
  if (schema.pattern !== null && !new RegExp(schema.pattern, "u").test(value)) {
    addIssue(
      state,
      pointer,
      "pattern",
      "This value does not match the required pattern.",
    );
  }
  if (!validFormat(schema, value)) {
    addIssue(state, pointer, "format", "Enter a value in the required format.");
  }
  return value;
}

function validateNumber(
  schema: Extract<CompiledSchema, { kind: "number" | "integer" }>,
  value: unknown,
  pointer: string,
  state: ValidationState,
): number | undefined {
  const parsed = numericValue(value, schema.kind === "integer");
  if (parsed === null) {
    addIssue(state, pointer, "type", "Enter a valid number.");
    return undefined;
  }
  if (schema.enumValues !== null && !schema.enumValues.includes(parsed)) {
    addIssue(state, pointer, "enum", "Choose one of the allowed values.");
  }
  if (
    schema.minimum !== null &&
    (schema.exclusiveMinimum
      ? parsed <= schema.minimum
      : parsed < schema.minimum)
  ) {
    addIssue(
      state,
      pointer,
      "minimum",
      "This number is below the allowed range.",
    );
  }
  if (
    schema.maximum !== null &&
    (schema.exclusiveMaximum
      ? parsed >= schema.maximum
      : parsed > schema.maximum)
  ) {
    addIssue(
      state,
      pointer,
      "maximum",
      "This number is above the allowed range.",
    );
  }
  return parsed;
}

function validateObject(
  schema: CompiledObjectSchema,
  value: unknown,
  pointer: string,
  state: ValidationState,
): JsonInputObject | undefined {
  if (!isRecord(value)) {
    addIssue(state, pointer, "type", "Enter values for this group.");
    return undefined;
  }

  const allowed = new Set(schema.properties.map((property) => property.name));
  if (Object.keys(value).some((name) => !allowed.has(name))) {
    addIssue(
      state,
      pointer,
      "additional_property",
      "This group contains an unsupported field.",
    );
  }

  const result: Record<string, JsonInputValue> = {};
  for (const property of schema.properties) {
    const childPointer = `${pointer}/${property.name}`;
    const raw = value[property.name];
    const missing = !Object.hasOwn(value, property.name) || raw === undefined;
    if (missing || isBlankNumericDraft(property.schema, raw)) {
      if (property.required) {
        addIssue(state, childPointer, "required", "This field is required.");
      }
      continue;
    }
    const blankString = property.schema.kind === "string" && raw === "";
    if (blankString) {
      if (!property.required) continue;
      addIssue(state, childPointer, "required", "This field is required.");
    }
    const validated = validateNode(property.schema, raw, childPointer, state);
    if (validated !== undefined) result[property.name] = validated;
  }
  return Object.freeze(result);
}

function validateArray(
  schema: CompiledArraySchema,
  value: unknown,
  pointer: string,
  state: ValidationState,
): readonly JsonInputValue[] | undefined {
  if (!Array.isArray(value)) {
    addIssue(state, pointer, "type", "Enter a list of values.");
    return undefined;
  }
  if (value.length < schema.minItems) {
    addIssue(state, pointer, "min_items", "This list has too few items.");
  }
  if (value.length > schema.maxItems) {
    addIssue(state, pointer, "max_items", "This list has too many items.");
  }

  const result: JsonInputValue[] = [];
  const inspectedLength = Math.min(value.length, schema.maxItems);
  for (let index = 0; index < inspectedLength; index += 1) {
    const validated = validateNode(
      schema.items,
      value[index],
      `${pointer}/${String(index)}`,
      state,
    );
    if (validated !== undefined) result.push(validated);
  }
  return Object.freeze(result);
}

function validateNode(
  schema: CompiledSchema,
  value: unknown,
  pointer: string,
  state: ValidationState,
): JsonInputValue | undefined {
  switch (schema.kind) {
    case "object":
      return validateObject(schema, value, pointer, state);
    case "string":
      return validateString(schema, value, pointer, state);
    case "number":
    case "integer":
      return validateNumber(schema, value, pointer, state);
    case "boolean":
      if (typeof value !== "boolean") {
        addIssue(state, pointer, "type", "Choose true or false.");
        return undefined;
      }
      if (schema.enumValues !== null && !schema.enumValues.includes(value)) {
        addIssue(state, pointer, "enum", "Choose one of the allowed values.");
      }
      return value;
    case "array":
      return validateArray(schema, value, pointer, state);
  }
}

export function validateSchemaInput(
  schema: CompiledObjectSchema,
  draft: unknown,
): SchemaValidationResult {
  const state: ValidationState = { issues: [] };
  const input = validateObject(schema, draft, INPUT_POINTER, state);
  if (state.issues.length > 0 || input === undefined) {
    return Object.freeze({ ok: false, issues: Object.freeze(state.issues) });
  }
  return Object.freeze({ ok: true, input });
}

// WEB-04 maps only schema-addressable /input JSON pointers to form errors.
import {
  INPUT_POINTER,
  type CompiledObjectSchema,
  type CompiledProperty,
  type CompiledSchema,
} from "./schemaModel";
import type { SchemaValidationIssue } from "./schemaValidation";

const SAFE_SEGMENT = /^[A-Za-z0-9_.-]{1,100}$/u;
const ARRAY_INDEX = /^(?:0|[1-9][0-9]*)$/u;
const UNSAFE_SEGMENTS = new Set(["__proto__", "prototype", "constructor"]);
const MAX_SERVER_ERRORS = 64;
const MAX_POINTER_LENGTH = 1_000;
const MAX_POINTER_SEGMENTS = 64;

export interface DryRunServerFieldError {
  readonly pointer: string;
  readonly code: string;
  readonly message: string;
}

function addressedSchema(
  root: CompiledObjectSchema,
  pointer: string,
): CompiledSchema | null {
  if (pointer === INPUT_POINTER) return root;
  if (
    pointer.length > MAX_POINTER_LENGTH ||
    !pointer.startsWith(`${INPUT_POINTER}/`)
  ) {
    return null;
  }
  const segments = pointer.slice(INPUT_POINTER.length + 1).split("/");
  if (segments.length === 0 || segments.length > MAX_POINTER_SEGMENTS)
    return null;

  let current: CompiledSchema = root;
  for (const segment of segments) {
    if (!SAFE_SEGMENT.test(segment) || UNSAFE_SEGMENTS.has(segment))
      return null;
    if (current.kind === "object") {
      const property: CompiledProperty | undefined = current.properties.find(
        (candidate) => candidate.name === segment,
      );
      if (property === undefined) return null;
      current = property.schema;
      continue;
    }
    if (current.kind === "array") {
      if (!ARRAY_INDEX.test(segment)) return null;
      const index = Number(segment);
      if (!Number.isSafeInteger(index) || index >= current.maxItems)
        return null;
      current = current.items;
      continue;
    }
    return null;
  }
  return current;
}

export function mapDryRunFieldErrors(
  schema: CompiledObjectSchema,
  fieldErrors: readonly DryRunServerFieldError[],
): readonly SchemaValidationIssue[] {
  const mapped: SchemaValidationIssue[] = [];
  const seen = new Set<string>();
  for (const fieldError of fieldErrors.slice(0, MAX_SERVER_ERRORS)) {
    if (
      typeof fieldError.pointer !== "string" ||
      addressedSchema(schema, fieldError.pointer) === null ||
      seen.has(fieldError.pointer)
    ) {
      continue;
    }
    seen.add(fieldError.pointer);
    mapped.push(
      Object.freeze({
        pointer: fieldError.pointer,
        code: "server_rejected",
        message: "The server rejected this field.",
      }),
    );
  }
  return Object.freeze(mapped);
}

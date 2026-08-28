import type { CompiledSchema } from "./schemaModel";
import { createSchemaDefaults } from "./schemaDefaults";

export function valueForNewArrayItem(schema: CompiledSchema): unknown {
  if (schema.hasDefault) return structuredClone(schema.defaultValue);
  switch (schema.kind) {
    case "object":
      return createSchemaDefaults(schema);
    case "array":
      return [];
    case "boolean":
      return false;
    case "integer":
    case "number":
    case "string":
      return "";
  }
}

export function asDraftObject(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function asDraftArray(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : [];
}

// WEB-04 creates fresh drafts from declared schema defaults only.
import type {
  CompiledObjectSchema,
  CompiledSchema,
  JsonInputObject,
  JsonInputValue,
  SchemaDraftObject,
} from "./schemaModel";

interface CollectedDefault {
  readonly present: boolean;
  readonly value?: unknown;
}

function cloneJsonValue(value: JsonInputValue): unknown {
  if (Array.isArray(value)) {
    const array = value as readonly JsonInputValue[];
    return array.map((item) => cloneJsonValue(item));
  }
  if (typeof value === "object") {
    const clone: SchemaDraftObject = {};
    for (const [key, child] of Object.entries(value as JsonInputObject)) {
      clone[key] = cloneJsonValue(child);
    }
    return clone;
  }
  return value;
}

function collectDefault(schema: CompiledSchema): CollectedDefault {
  if (schema.hasDefault && schema.defaultValue !== undefined) {
    return { present: true, value: cloneJsonValue(schema.defaultValue) };
  }
  if (schema.kind !== "object") return { present: false };

  const object: SchemaDraftObject = {};
  let present = false;
  for (const property of schema.properties) {
    const child = collectDefault(property.schema);
    if (!child.present) continue;
    object[property.name] = child.value;
    present = true;
  }
  return present ? { present: true, value: object } : { present: false };
}

export function createSchemaDefaults(
  schema: CompiledObjectSchema,
): SchemaDraftObject {
  const collected = collectDefault(schema);
  if (!collected.present) return {};
  return collected.value as SchemaDraftObject;
}

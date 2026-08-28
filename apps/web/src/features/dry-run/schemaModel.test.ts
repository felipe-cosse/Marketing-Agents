// WEB-04 locks the supported schema subset and its fail-closed boundaries.
import { describe, expect, it } from "vitest";

import {
  InputSchemaCompileError,
  compileInputSchema,
  type CompiledProperty,
} from "./schemaModel";

function propertyNamed(
  properties: readonly CompiledProperty[],
  name: string,
): CompiledProperty {
  const property = properties.find((candidate) => candidate.name === name);
  expect(property).toBeDefined();
  if (property === undefined) throw new Error("Expected compiled property.");
  return property;
}

function expectCompileCode(schema: unknown, code: string): void {
  try {
    compileInputSchema(schema);
    throw new Error("Expected schema compilation to fail.");
  } catch (error) {
    expect(error).toBeInstanceOf(InputSchemaCompileError);
    if (error instanceof InputSchemaCompileError) expect(error.code).toBe(code);
  }
}

describe("WEB-04 input-schema compiler", () => {
  it("compiles nested fields, enums, bounded arrays, annotations, and stable pointers", () => {
    const schema = compileInputSchema({
      $schema: "https://json-schema.org/draft/2020-12/schema",
      $id: "urn:test:web-04",
      type: "object",
      title: "Dry-run input",
      additionalProperties: false,
      required: ["summary", "rows"],
      properties: {
        summary: {
          type: "string",
          minLength: 2,
          maxLength: 40,
          pattern: "^[A-Za-z ]+$",
          default: "Ready",
          examples: ["Example"],
          "x-sensitive": true,
          "x-ui": { control: "textarea", order: 2, help: "Short summary" },
        },
        mode: {
          type: "string",
          enum: ["preview", "execute"],
          default: "preview",
          "x-ui": { order: 1 },
        },
        settings: {
          type: "object",
          additionalProperties: false,
          "x-sensitive": true,
          properties: {
            enabled: { type: "boolean", default: false },
          },
        },
        rows: {
          type: "array",
          minItems: 1,
          maxItems: 3,
          items: {
            type: "object",
            additionalProperties: false,
            required: ["label"],
            properties: {
              label: { type: "string", minLength: 1, maxLength: 12 },
              weight: {
                type: "number",
                exclusiveMinimum: 0,
                maximum: 10,
              },
            },
          },
        },
        created: {
          type: "string",
          maxLength: 10,
          format: "date",
          default: "2024-02-29",
        },
      },
    });

    expect(schema.properties.map((property) => property.name)).toEqual([
      "mode",
      "summary",
      "rows",
      "settings",
      "created",
    ]);
    const summary = propertyNamed(schema.properties, "summary");
    expect(summary.required).toBe(true);
    expect(summary.schema).toMatchObject({
      kind: "string",
      pointer: "/input/summary",
      sensitive: true,
      minLength: 2,
      maxLength: 40,
      pattern: "^[A-Za-z ]+$",
      hasDefault: true,
      defaultValue: "Ready",
      examples: ["Example"],
      ui: { control: "textarea", order: 2, help: "Short summary" },
    });

    const settings = propertyNamed(schema.properties, "settings").schema;
    expect(settings.kind).toBe("object");
    if (settings.kind === "object") {
      expect(settings.properties[0]?.schema.sensitive).toBe(true);
    }

    const rows = propertyNamed(schema.properties, "rows").schema;
    expect(rows.kind).toBe("array");
    if (rows.kind === "array") {
      expect(rows).toMatchObject({ minItems: 1, maxItems: 3 });
      expect(rows.items.pointer).toBe("/input/rows/*");
      expect(rows.items.kind).toBe("object");
      if (rows.items.kind === "object") {
        const weight = propertyNamed(rows.items.properties, "weight").schema;
        expect(weight).toMatchObject({
          kind: "number",
          minimum: 0,
          exclusiveMinimum: true,
          maximum: 10,
          exclusiveMaximum: false,
        });
      }
    }
    expect(Object.isFrozen(schema)).toBe(true);
    expect(Object.isFrozen(schema.properties)).toBe(true);
  });

  it.each([
    [
      "unsupported keywords",
      {
        type: "object",
        additionalProperties: false,
        properties: {},
        oneOf: [],
      },
      "unsupported_schema_keyword",
    ],
    [
      "arbitrary objects",
      { type: "object", additionalProperties: true, properties: {} },
      "unbounded_object",
    ],
    [
      "unbounded strings",
      {
        type: "object",
        additionalProperties: false,
        properties: { value: { type: "string" } },
      },
      "unbounded_string",
    ],
    [
      "unbounded arrays",
      {
        type: "object",
        additionalProperties: false,
        properties: {
          values: { type: "array", items: { type: "boolean" } },
        },
      },
      "unbounded_array",
    ],
    [
      "nested arrays",
      {
        type: "object",
        additionalProperties: false,
        properties: {
          values: {
            type: "array",
            maxItems: 2,
            items: {
              type: "array",
              maxItems: 2,
              items: { type: "boolean" },
            },
          },
        },
      },
      "unsupported_nested_array",
    ],
    [
      "unsafe property names",
      {
        type: "object",
        additionalProperties: false,
        properties: { constructor: { type: "boolean" } },
      },
      "unsafe_property_name",
    ],
    [
      "unsupported formats",
      {
        type: "object",
        additionalProperties: false,
        properties: {
          host: { type: "string", maxLength: 100, format: "hostname" },
        },
      },
      "unsupported_schema_format",
    ],
    [
      "unsupported presentation hints",
      {
        type: "object",
        additionalProperties: false,
        properties: {
          value: {
            type: "string",
            maxLength: 10,
            "x-ui": { control: "select" },
          },
        },
      },
      "unsupported_ui_annotation",
    ],
    [
      "invalid formatted defaults",
      {
        type: "object",
        additionalProperties: false,
        properties: {
          date: {
            type: "string",
            maxLength: 10,
            format: "date",
            default: "2025-02-30",
          },
        },
      },
      "invalid_schema_annotation",
    ],
  ])("rejects %s", (_label, schema, code) => {
    expectCompileCode(schema, code);
  });

  it("enforces depth, property-count, and array-size safety caps", () => {
    let nested: Record<string, unknown> = {
      type: "string",
      maxLength: 10,
    };
    for (let index = 0; index < 10; index += 1) {
      nested = {
        type: "object",
        additionalProperties: false,
        properties: { child: nested },
      };
    }
    expectCompileCode(nested, "schema_too_deep");

    const properties: Record<string, unknown> = {};
    for (let index = 0; index < 65; index += 1) {
      properties[`field_${String(index)}`] = { type: "boolean" };
    }
    expectCompileCode(
      { type: "object", additionalProperties: false, properties },
      "too_many_properties",
    );
    expectCompileCode(
      {
        type: "object",
        additionalProperties: false,
        properties: {
          values: {
            type: "array",
            maxItems: 33,
            items: { type: "boolean" },
          },
        },
      },
      "invalid_schema_bound",
    );
  });

  it.each([
    "^(a+)+$",
    "^(?:a+)+$",
    "^(?:a|aa)+$",
    "^(?:.*a)*$",
    "^(a+)\\1$",
    "^[a-z]+[a-z]+$",
  ])("rejects unsafe pattern %s before it can execute", (pattern) => {
    expectCompileCode(
      {
        type: "object",
        additionalProperties: false,
        properties: {
          value: { type: "string", maxLength: 100_000, pattern },
        },
      },
      "unsafe_schema_pattern",
    );
  });

  it("accepts the reviewed catalog and flat safe pattern subset", () => {
    const schema = compileInputSchema({
      type: "object",
      additionalProperties: false,
      properties: {
        request: {
          type: "string",
          maxLength: 80,
          pattern: "^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$",
        },
        locale: {
          type: "string",
          maxLength: 12,
          pattern: "^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$",
        },
        label: { type: "string", maxLength: 40, pattern: "^[A-Za-z ]+$" },
      },
    });

    expect(schema.properties).toHaveLength(3);
  });
});

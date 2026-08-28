// WEB-04 proves fresh defaults and value-safe client validation.
import { describe, expect, it } from "vitest";

import { createSchemaDefaults } from "./schemaDefaults";
import { compileInputSchema } from "./schemaModel";
import { validateSchemaInput } from "./schemaValidation";

function validationSchema() {
  return compileInputSchema({
    type: "object",
    additionalProperties: false,
    required: ["id", "quantity", "secret", "rows"],
    properties: {
      id: {
        type: "string",
        minLength: 2,
        maxLength: 12,
        pattern: "^[a-z0-9-]+$",
      },
      quantity: { type: "integer", minimum: 1, maximum: 9 },
      score: { type: "number", exclusiveMinimum: 0, exclusiveMaximum: 10 },
      optionalNumber: { type: "number", minimum: 0 },
      optionalLocale: { type: "string", maxLength: 12 },
      choice: { type: "string", enum: ["one", "two"], default: "one" },
      secret: {
        type: "string",
        maxLength: 20,
        pattern: "^[A-Z]+$",
        "x-sensitive": true,
      },
      date: { type: "string", maxLength: 10, format: "date" },
      timestamp: { type: "string", maxLength: 40, format: "date-time" },
      link: { type: "string", maxLength: 200, format: "uri" },
      email: { type: "string", maxLength: 200, format: "email" },
      flags: {
        type: "array",
        minItems: 1,
        maxItems: 2,
        items: { type: "boolean" },
        default: [true],
      },
      settings: {
        type: "object",
        additionalProperties: false,
        properties: {
          enabled: { type: "boolean", default: false },
          note: { type: "string", maxLength: 30 },
        },
      },
      rows: {
        type: "array",
        minItems: 1,
        maxItems: 2,
        items: {
          type: "object",
          additionalProperties: false,
          required: ["label"],
          properties: {
            label: { type: "string", minLength: 1, maxLength: 20 },
          },
        },
      },
    },
  });
}

describe("WEB-04 schema defaults", () => {
  it("returns fresh recursive drafts without inventing unspecified values", () => {
    const schema = validationSchema();
    const first = createSchemaDefaults(schema);
    const second = createSchemaDefaults(schema);

    expect(first).toEqual({
      choice: "one",
      flags: [true],
      settings: { enabled: false },
    });
    expect(first).not.toBe(second);
    expect(first.flags).not.toBe(second.flags);
    expect(first.settings).not.toBe(second.settings);
    expect(first).not.toHaveProperty("id");
    expect(first).not.toHaveProperty("settings.note");
  });
});

describe("WEB-04 schema validation", () => {
  it("normalizes numeric drafts, omits optional blanks, and returns frozen JSON", () => {
    const result = validateSchemaInput(validationSchema(), {
      id: "run-1",
      quantity: "7",
      score: ".5",
      optionalNumber: "",
      optionalLocale: "",
      choice: "two",
      secret: "PRIVATE",
      date: "2024-02-29",
      timestamp: "2024-02-29T12:30:45Z",
      link: "https://example.test/path",
      email: "owner@example.test",
      flags: [false],
      settings: { enabled: true, note: "ready" },
      rows: [{ label: "first" }],
    });

    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("Expected valid input.");
    expect(result.input).toEqual({
      id: "run-1",
      quantity: 7,
      score: 0.5,
      choice: "two",
      secret: "PRIVATE",
      date: "2024-02-29",
      timestamp: "2024-02-29T12:30:45Z",
      link: "https://example.test/path",
      email: "owner@example.test",
      flags: [false],
      settings: { enabled: true, note: "ready" },
      rows: [{ label: "first" }],
    });
    expect(Object.isFrozen(result.input)).toBe(true);
    expect(Object.isFrozen(result.input.flags)).toBe(true);
    expect(Object.isFrozen(result.input.settings)).toBe(true);
  });

  it("returns concrete pointer issues without reflecting sensitive values", () => {
    const sensitiveValue = "PRIVATE sensitive material";
    const result = validateSchemaInput(validationSchema(), {
      id: "",
      quantity: "",
      score: "10",
      choice: "other",
      secret: sensitiveValue,
      date: "2025-02-30",
      timestamp: "2025-02-30T12:30:45Z",
      link: "not-a-uri",
      email: "two@@bad",
      flags: [true, false, true],
      settings: { enabled: "yes", extra: "unsupported" },
      rows: [{}],
      extra: "unsupported",
    });

    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("Expected invalid input.");
    expect(result.issues).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          pointer: "/input",
          code: "additional_property",
        }),
        expect.objectContaining({ pointer: "/input/id", code: "required" }),
        expect.objectContaining({ pointer: "/input/id", code: "min_length" }),
        expect.objectContaining({
          pointer: "/input/quantity",
          code: "required",
        }),
        expect.objectContaining({ pointer: "/input/score", code: "maximum" }),
        expect.objectContaining({ pointer: "/input/choice", code: "enum" }),
        expect.objectContaining({ pointer: "/input/secret", code: "pattern" }),
        expect.objectContaining({ pointer: "/input/date", code: "format" }),
        expect.objectContaining({
          pointer: "/input/timestamp",
          code: "format",
        }),
        expect.objectContaining({ pointer: "/input/link", code: "format" }),
        expect.objectContaining({ pointer: "/input/email", code: "format" }),
        expect.objectContaining({ pointer: "/input/flags", code: "max_items" }),
        expect.objectContaining({
          pointer: "/input/settings",
          code: "additional_property",
        }),
        expect.objectContaining({
          pointer: "/input/settings/enabled",
          code: "type",
        }),
        expect.objectContaining({
          pointer: "/input/rows/0/label",
          code: "required",
        }),
      ]),
    );
    expect(JSON.stringify(result.issues)).not.toContain(sensitiveValue);
    expect(Object.isFrozen(result.issues)).toBe(true);
  });

  it("rejects fractional integer drafts and accepts numeric exponent drafts", () => {
    const invalid = validateSchemaInput(validationSchema(), {
      id: "run-1",
      quantity: "1.5",
      secret: "PRIVATE",
      rows: [{ label: "first" }],
    });
    expect(invalid.ok).toBe(false);
    if (!invalid.ok) {
      expect(invalid.issues).toEqual(
        expect.arrayContaining([
          expect.objectContaining({ pointer: "/input/quantity", code: "type" }),
        ]),
      );
    }

    const valid = validateSchemaInput(validationSchema(), {
      id: "run-1",
      quantity: "2",
      score: "1e0",
      secret: "PRIVATE",
      rows: [{ label: "first" }],
    });
    expect(valid.ok).toBe(true);
  });
});

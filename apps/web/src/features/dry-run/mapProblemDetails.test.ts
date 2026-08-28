// WEB-04 maps authoritative server errors only to compiled form addresses.
import { describe, expect, it } from "vitest";

import {
  mapDryRunFieldErrors,
  type DryRunServerFieldError,
} from "./mapProblemDetails";
import { compileInputSchema } from "./schemaModel";

function serverError(
  pointer: string,
  message = "rejected raw-secret",
): DryRunServerFieldError {
  return { pointer, code: "server_code", message };
}

describe("WEB-04 problem-detail field mapping", () => {
  it("maps root, field, and concrete bounded-array pointers with static messages", () => {
    const schema = compileInputSchema({
      type: "object",
      additionalProperties: false,
      properties: {
        name: { type: "string", maxLength: 30 },
        rows: {
          type: "array",
          maxItems: 3,
          items: {
            type: "object",
            additionalProperties: false,
            properties: {
              label: { type: "string", maxLength: 30 },
            },
          },
        },
      },
    });

    const mapped = mapDryRunFieldErrors(schema, [
      serverError("/input"),
      serverError("/input/name"),
      serverError("/input/rows/0/label"),
      serverError("/input/rows/2/label"),
      serverError("/input/name", "duplicate"),
    ]);

    expect(mapped).toEqual([
      {
        pointer: "/input",
        code: "server_rejected",
        message: "The server rejected this field.",
      },
      {
        pointer: "/input/name",
        code: "server_rejected",
        message: "The server rejected this field.",
      },
      {
        pointer: "/input/rows/0/label",
        code: "server_rejected",
        message: "The server rejected this field.",
      },
      {
        pointer: "/input/rows/2/label",
        code: "server_rejected",
        message: "The server rejected this field.",
      },
    ]);
    expect(JSON.stringify(mapped)).not.toContain("raw-secret");
    expect(Object.isFrozen(mapped)).toBe(true);
  });

  it("drops foreign, unknown, wildcard, escaped, unsafe, and out-of-range pointers", () => {
    const schema = compileInputSchema({
      type: "object",
      additionalProperties: false,
      properties: {
        rows: {
          type: "array",
          maxItems: 2,
          items: { type: "string", maxLength: 30 },
        },
      },
    });

    expect(
      mapDryRunFieldErrors(schema, [
        serverError("/payload/rows/0"),
        serverError("/input/unknown"),
        serverError("/input/rows/*"),
        serverError("/input/rows/00"),
        serverError("/input/rows/2"),
        serverError("/input/rows/0/extra"),
        serverError("/input/~1rows"),
        serverError("/input/constructor"),
      ]),
    ).toEqual([]);
  });
});

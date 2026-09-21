// WEB-01 keeps source-evidence annotations in the API model but out of product copy.
import { describe, expect, it } from "vitest";

import { presentPurpose, presentRuntimeStatus } from "./presentation";

describe("WEB-01 vendor-neutral purpose presentation", () => {
  it("OBJ-06 distinguishes every runtime state, including unknown versus never run", () => {
    expect(
      (
        [
          undefined,
          "never_run",
          "received",
          "validated",
          "planned",
          "awaiting_approval",
          "executing",
          "completed",
          "failed",
          "rejected",
          "cancelled",
        ] as const
      ).map(presentRuntimeStatus),
    ).toEqual([
      "Unavailable",
      "Never run",
      "Received",
      "Validated",
      "Planned",
      "Awaiting approval",
      "Executing",
      "Completed",
      "Failed",
      "Rejected",
      "Cancelled",
    ]);
  });
  it("removes only a terminal source-chart vendor annotation", () => {
    expect(
      presentPurpose(
        "Add new website signups to the configured newsletter system; the source chart names Loops.",
      ),
    ).toBe("Add new website signups to the configured newsletter system.");
  });

  it.each([
    "Handle unsubscribe requests safely.",
    "Summarize the source chart names and preserve context.",
    "Prepare a draft; never send it automatically.",
  ])("preserves ordinary purpose copy: %s", (purpose) => {
    expect(presentPurpose(purpose)).toBe(purpose);
  });
});

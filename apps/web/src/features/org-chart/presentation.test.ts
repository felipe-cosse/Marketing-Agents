// WEB-01 keeps source-evidence annotations in the API model but out of product copy.
import { describe, expect, it } from "vitest";

import {
  presentDeploymentInventory,
  presentPurpose,
  presentRuntimeStatus,
} from "./presentation";

describe("WEB-01 vendor-neutral purpose presentation", () => {
  it("AC-04 derives complete and filtered Community inventory wording", () => {
    expect(presentDeploymentInventory(14, 7)).toBe(
      "14 deployed instances · 7 reusable templates",
    );
    expect(presentDeploymentInventory(2, 1)).toBe(
      "2 deployed instances · 1 reusable template",
    );
    expect(presentDeploymentInventory(1, 1)).toBe(
      "1 deployed instance · 1 reusable template",
    );
  });
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

import { describe, expect, it } from "vitest";

import runTimelineStyles from "./run-timeline.css?raw";

describe("WEB-06 responsive timeline order", () => {
  it("keeps narrow visual order aligned with the sidebar-first DOM order", () => {
    expect(runTimelineStyles).not.toMatch(
      /\.run-page__timeline-pane\s*\{[^}]*\border\s*:/u,
    );
  });
});

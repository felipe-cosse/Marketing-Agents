import { describe, expect, it } from "vitest";

import type { RunTimelineEvent, RunTimelinePage } from "../../api/runArtifacts";
import {
  makeArtifactPage,
  makeArtifactResource,
  makeArtifactSummary,
  WEB_06_RUN_ID,
} from "../../test/runArtifactFixture";
import {
  artifactMarkdownValue,
  isAdvisoryArtifact,
  mergeArtifactPages,
} from "./artifactPresentation";
import {
  humanizeRuntimeValue,
  mergeTimelinePages,
  runRefreshInterval,
  timelineCategory,
  timelineEventTitle,
} from "./timelineModel";

function event(
  sequence: number,
  eventType = "run.transitioned",
): RunTimelineEvent {
  return {
    id: `event.web06.${String(sequence)}`,
    sequence,
    schemaVersion: 1,
    eventType,
    aggregateType: "run",
    aggregateId: "run.web06.01",
    outcome: "accepted",
    actorId: "actor-hmac-sha256-v1:" + "a".repeat(64),
    actorSource: "local_session",
    authMethod: "local_session",
    correlationId: "correlation-hmac-sha256-v1:" + "b".repeat(64),
    occurredAt: `2026-08-31T12:00:0${String(sequence)}.000000000Z`,
    stepId: null,
    actionId: null,
    approvalRequestId: null,
    artifactId: null,
    attemptedCommand: null,
    previousState: null,
    newState: null,
    reasonCode: null,
    metadata: {},
    metadataClassification: "internal",
    metadataExpiresAt: "2026-09-30T12:00:00.000000000Z",
    metadataExpired: false,
    runUrl: "/api/v1/runs/run.web06.01",
    stepUrl: null,
    actionUrl: null,
    approvalUrl: null,
    artifactUrl: null,
  };
}

function page(
  items: readonly RunTimelineEvent[],
  nextCursor: string | null = null,
  runId = "run.web06.01",
): RunTimelinePage {
  return { runId, items, nextCursor };
}

describe("WEB-06 timeline presentation model", () => {
  it("keeps persisted sequence order across pages", () => {
    const snapshot = mergeTimelinePages("run.web06.01", [
      page([event(1), event(2)], "run-timeline-v1:cursor"),
      page([event(3), event(4)]),
    ]);

    expect(snapshot.events.map((item) => item.sequence)).toEqual([1, 2, 3, 4]);
    expect(snapshot.pageCount).toBe(2);
    expect(snapshot.nextCursor).toBeNull();
  });

  it("fails closed on duplicate, regressing, or cross-run pages", () => {
    expect(() =>
      mergeTimelinePages("run.web06.01", [page([event(2), event(2)])]),
    ).toThrow("timeline sequence is not coherent");
    expect(() =>
      mergeTimelinePages("run.web06.01", [page([event(2), event(1)])]),
    ).toThrow("timeline sequence is not coherent");
    expect(() =>
      mergeTimelinePages("run.web06.01", [page([event(1)], null, "run.other")]),
    ).toThrow("does not belong");
  });

  it("uses textual categories and readable fallback labels", () => {
    expect(timelineCategory(event(1, "model.attempt.started"))).toBe("attempt");
    expect(timelineCategory(event(2, "artifact.created"))).toBe("artifact");
    expect(timelineEventTitle(event(2, "artifact.created"))).toBe(
      "Artifact created",
    );
    expect(timelineEventTitle(event(3, "future.runtime_signal.recorded"))).toBe(
      "Future Runtime Signal Recorded",
    );
    expect(humanizeRuntimeValue("awaiting_approval")).toBe("Awaiting Approval");
  });

  it("stops terminal polling and backs off after a read error", () => {
    expect(runRefreshInterval("completed", false)).toBe(false);
    expect(runRefreshInterval("failed", true)).toBe(false);
    expect(runRefreshInterval("executing", false)).toBe(2_500);
    expect(runRefreshInterval(undefined, true)).toBe(10_000);
  });

  it("keeps artifact keyset order across pages", () => {
    const first = makeArtifactSummary({
      id: "artifact.web06.01",
      createdAt: "2026-08-31T16:02:00Z",
    });
    const second = makeArtifactSummary({
      id: "artifact.web06.02",
      createdAt: "2026-08-31T16:03:00Z",
    });
    const third = makeArtifactSummary({
      id: "artifact.web06.03",
      createdAt: "2026-08-31T16:04:00Z",
    });
    const merged = mergeArtifactPages(WEB_06_RUN_ID, [
      makeArtifactPage({ items: [first, second], nextCursor: "cursor.02" }),
      makeArtifactPage({ items: [third] }),
    ]);

    expect(merged.error).toBeNull();
    expect(merged.items.map((artifact) => artifact.id)).toEqual([
      first.id,
      second.id,
      third.id,
    ]);
  });

  it("fails closed on duplicate, regressing, or cross-run artifact pages", () => {
    const first = makeArtifactSummary({
      id: "artifact.web06.01",
      createdAt: "2026-08-31T16:03:00Z",
    });
    const duplicate = makeArtifactSummary({
      id: first.id,
      createdAt: "2026-08-31T16:04:00Z",
    });
    const regressing = makeArtifactSummary({
      id: "artifact.web06.02",
      createdAt: "2026-08-31T16:02:00Z",
    });

    expect(
      mergeArtifactPages(WEB_06_RUN_ID, [
        makeArtifactPage({ items: [first] }),
        makeArtifactPage({ items: [duplicate] }),
      ]).error,
    ).toMatch("unique ascending keyset order");
    expect(
      mergeArtifactPages(WEB_06_RUN_ID, [
        makeArtifactPage({ items: [first] }),
        makeArtifactPage({ items: [regressing] }),
      ]).error,
    ).toMatch("unique ascending keyset order");
    expect(
      mergeArtifactPages(WEB_06_RUN_ID, [
        makeArtifactPage({ runId: "run.web06.other", items: [] }),
      ]).error,
    ).toMatch("does not belong");
  });

  it("enables advisory Markdown only for the exact catalog output schema", () => {
    const artifact = makeArtifactResource();
    expect(isAdvisoryArtifact(artifact)).toBe(true);
    expect(artifactMarkdownValue(artifact)).toContain("# Churn review");

    const mismatchedSchema = makeArtifactResource({
      outputSchemaId:
        "urn:marketing-agents:catalog:v1:tpl.other.template:output",
    });
    expect(isAdvisoryArtifact(mismatchedSchema)).toBe(false);
    expect(artifactMarkdownValue(mismatchedSchema)).toBeNull();

    const unknownTemplate = "tpl.unknown.attacker-controlled";
    const selfMatchedUnknown = makeArtifactResource({
      templateId: unknownTemplate,
      outputSchemaId: `urn:marketing-agents:catalog:v1:${unknownTemplate}:output`,
    });
    expect(isAdvisoryArtifact(selfMatchedUnknown)).toBe(false);
    expect(artifactMarkdownValue(selfMatchedUnknown)).toBeNull();
  });
});

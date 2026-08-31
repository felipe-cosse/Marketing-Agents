import { describe, expect, it } from "vitest";

import type { RunPage, RunSummary } from "../../api/runArtifacts";
import { mergeRunPages } from "./runListModel";

function run(id: string, createdAt: string): RunSummary {
  return {
    id,
    workItemId: `work.${id}`,
    instanceId: "instance.web06",
    workflowId: "workflow.web06",
    triggerId: "trigger.web06",
    source: "manual",
    mode: "mock_execution",
    state: "completed",
    catalogHash: `catalog-sha256-v1:${"a".repeat(64)}`,
    configurationRevision: 1,
    approvalRequired: false,
    terminalReasonCode: "run_completed",
    createdAt,
    updatedAt: createdAt,
    version: 1,
    runUrl: `/api/v1/runs/${id}`,
    timelineUrl: `/api/v1/runs/${id}/timeline`,
    artifactsUrl: `/api/v1/runs/${id}/artifacts`,
    instanceUrl: "/api/v1/agent-instances/instance.web06",
  };
}

function page(...items: readonly RunSummary[]): RunPage {
  return { items, nextCursor: null };
}

describe("WEB-06 run-list page integrity", () => {
  it("merges globally descending pages with nanosecond and ID tie-break order", () => {
    const result = mergeRunPages([
      page(
        run("run.web06.z", "2026-08-31T18:00:00.000000003Z"),
        run("run.web06.y", "2026-08-31T18:00:00.000000002Z"),
      ),
      page(
        run("run.web06.x", "2026-08-31T18:00:00.000000002Z"),
        run("run.web06.w", "2026-08-31T18:00:00.000000001Z"),
      ),
    ]);

    expect(result.error).toBeNull();
    expect(result.items.map(({ id }) => id)).toEqual([
      "run.web06.z",
      "run.web06.y",
      "run.web06.x",
      "run.web06.w",
    ]);
    expect(Object.isFrozen(result.items)).toBe(true);
  });

  it("fails closed on a duplicate run across cursor pages", () => {
    const duplicate = run(
      "run.web06.duplicate",
      "2026-08-31T18:00:00.000000002Z",
    );
    const result = mergeRunPages([
      page(duplicate),
      page({
        ...duplicate,
        createdAt: "2026-08-31T18:00:00.000000001Z",
      }),
    ]);

    expect(result.items).toEqual([]);
    expect(result.error).toBe(
      "Recent run pages failed their identity or ordering checks.",
    );
  });

  it("fails closed on a cross-page timestamp or ID ordering regression", () => {
    const timestampRegression = mergeRunPages([
      page(run("run.web06.a", "2026-08-31T18:00:00.000000001Z")),
      page(run("run.web06.b", "2026-08-31T18:00:00.000000002Z")),
    ]);
    const idRegression = mergeRunPages([
      page(run("run.web06.a", "2026-08-31T18:00:00.000000001Z")),
      page(run("run.web06.b", "2026-08-31T18:00:00.000000001Z")),
    ]);

    expect(timestampRegression.items).toEqual([]);
    expect(timestampRegression.error).not.toBeNull();
    expect(idRegression.items).toEqual([]);
    expect(idRegression.error).not.toBeNull();
  });
});

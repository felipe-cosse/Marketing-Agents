// OBJ-06: behavioral label evidence plus static React connection evidence.
// This is not a React renderer. Unit/browser gates prove interaction behavior.
import "./require-pinned-node.mjs";

import assert from "node:assert/strict";
import { realpathSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const CONNECTION_SOURCES = Object.freeze({
  page: "apps/web/src/features/org-chart/OrgChartPage.tsx",
  canvas: "apps/web/src/features/org-chart/OrgChartCanvas.tsx",
  stage: "apps/web/src/features/org-chart/HierarchyStage.tsx",
  card: "apps/web/src/features/org-chart/AgentCard.tsx",
  tree: "apps/web/src/features/org-chart/OrgTreeFallback.tsx",
  snapshot: "apps/web/src/features/runs/RunExecutionSnapshot.tsx",
  approvals: "apps/web/src/features/approvals/ApprovalQueuePage.tsx",
  detail: "apps/web/src/features/instance-detail/AgentDetailPane.tsx",
  dryRun: "apps/web/src/features/dry-run/DryRunPanel.tsx",
});

// Deliberately narrow lexical check: retain literals, remove comments, and
// reject malformed quotes/comments. It does not parse TypeScript or prove flow.
function withoutComments(source) {
  let result = "";
  for (let index = 0; index < source.length;) {
    if (source.startsWith("//", index)) {
      const end = source.indexOf("\n", index + 2);
      index = end < 0 ? source.length : end;
    } else if (source.startsWith("/*", index)) {
      const end = source.indexOf("*/", index + 2);
      assert.notEqual(end, -1, "OBJ-06 unterminated source comment");
      result += " ";
      index = end + 2;
    } else if (["'", '"', "`"].includes(source[index])) {
      const quote = source[index];
      result += source[index++];
      while (index < source.length && source[index] !== quote) {
        if (source[index] === "\\") result += source[index++];
        result += source[index++];
      }
      assert.ok(index < source.length, "OBJ-06 unterminated source literal");
      result += source[index++];
    } else {
      result += source[index++];
    }
  }
  return result;
}

// Bound checks to the selected element, never a later sibling that happens to
// have the required prop. These sources use self-closing component invocations.
function openingElement(source, name) {
  const start = source.search(new RegExp(`<${name}\\b`, "u"));
  assert.notEqual(start, -1, `OBJ-06 missing ${name} element`);
  const end = source.indexOf("/>", start);
  assert.notEqual(end, -1, `OBJ-06 missing ${name} closing marker`);
  return source.slice(start, end);
}

export async function readConnectionSources(repositoryRoot) {
  return Object.fromEntries(
    await Promise.all(
      Object.entries(CONNECTION_SOURCES).map(async ([name, path]) => [
        name,
        await readFile(resolve(repositoryRoot, path), "utf8"),
      ]),
    ),
  );
}

export function verifyControlConnections(sources) {
  const source = Object.fromEntries(
    Object.keys(CONNECTION_SOURCES).map((name) => {
      assert.equal(
        typeof sources[name],
        "string",
        `OBJ-06 missing ${name} source`,
      );
      return [name, withoutComments(sources[name])];
    }),
  );
  for (const component of ["OrgChartCanvas", "OrgTreeFallback"]) {
    assert.match(
      openingElement(source.page, component),
      /runtimeStatusByInstanceId=\{\s*statusQuery\.isError \? undefined : runtimeStatusByInstanceId\s*\}/u,
      `OBJ-06 ${component} must receive observed runtime separately and withhold failed polling`,
    );
  }
  assert.match(
    openingElement(source.canvas, "HierarchyStage"),
    /runtimeStatusByInstanceId=\{runtimeStatusByInstanceId\}/u,
    "OBJ-06 canvas must forward observed runtime to its stage",
  );
  assert.match(
    source.canvas,
    /\[hierarchy\.structuralKey\],/u,
    "OBJ-06 canvas layout remains keyed only by hierarchy structure",
  );
  assert.match(
    openingElement(source.stage, "AgentCard"),
    /runtimeStatus=\{runtimeStatusByInstanceId\?\.get\(\s*instance\.id,?\s*\)\}/u,
    "OBJ-06 stage must select observed runtime by exact instance identity",
  );
  assert.match(
    source.card,
    /presentRuntimeStatus\(runtimeStatus\?\.status\)/u,
    "OBJ-06 card must present observed runtime rather than deployment state",
  );
  assert.match(
    source.card,
    /Latest run: \{runStatus\}/u,
    "OBJ-06 card runtime must be available in its accessible description",
  );
  assert.match(
    source.card,
    /data-runtime-status=\{runtimeStatus\?\.status \?\? "unavailable"\}/u,
    "OBJ-06 unknown card runtime must remain unavailable",
  );
  assert.match(
    source.tree,
    /nodeSummary\(\s*node,\s*runtimeStatusByInstanceId\?\.get\(node\.id\),?\s*\)/u,
    "OBJ-06 tree must select observed runtime by exact node identity",
  );
  assert.match(
    source.tree,
    /Latest run: \{presentRuntimeStatus\(runtimeStatus\?\.status\)\}/u,
    "OBJ-06 tree must expose the same runtime presentation contract",
  );
  assert.ok(
    source.snapshot.includes(
      "to={`/approvals?run_id=${encodeURIComponent(run.id)}`}",
    ),
    "OBJ-06 pending-action review must retain the exact run scope",
  );
  assert.match(
    source.approvals,
    /const \[searchParams\] = useSearchParams\(\);/u,
    "OBJ-06 approval scope must subscribe to router search changes",
  );
  assert.match(
    source.approvals,
    /const requestedRunId = approvalRunIdFromSearch\(searchParams\);/u,
    "OBJ-06 approval scope must come from the subscribed router value",
  );
  assert.match(
    source.approvals,
    /<ScopedApprovalQueue\s+key=\{requestedRunId \?\? "all-runs"\}\s+requestedRunId=\{requestedRunId\}/u,
    "OBJ-06 changing run scope must reset stale approval review state",
  );
  assert.match(
    openingElement(source.detail, "DryRunPanel"),
    /onOpenRun=\{onOpenRun\}/u,
    "OBJ-06 admitted run navigation must use the guarded inspector callback",
  );
  assert.match(
    source.dryRun,
    /event\.preventDefault\(\);\s*onOpenRun\(receipt\.runId\);/u,
    "OBJ-06 primary receipt activation must use guarded client navigation",
  );
  assert.match(
    source.page,
    /const navigationBlocker = useBlocker\(\s*\(\{ currentLocation, nextLocation \}\) =>\s*\(dirtyStateRef\.current\.configuration \|\| dirtyStateRef\.current\.dryRun\) &&/u,
    "OBJ-06 navigation blocker must consume current configuration and input dirtiness",
  );
}

export function verifyRuntimePresentation(presentRuntimeStatus) {
  assert.equal(
    typeof presentRuntimeStatus,
    "function",
    "OBJ-06 runtime presentation must expose its independent status contract",
  );
  for (const [state, expected] of [
    [undefined, "Unavailable"],
    ["never_run", "Never run"],
    ["received", "Received"],
    ["validated", "Validated"],
    ["planned", "Planned"],
    ["awaiting_approval", "Awaiting approval"],
    ["executing", "Executing"],
    ["completed", "Completed"],
    ["failed", "Failed"],
    ["rejected", "Rejected"],
    ["cancelled", "Cancelled"],
  ]) {
    assert.equal(
      presentRuntimeStatus(state),
      expected,
      `OBJ-06 runtime ${String(state)} must not be confused with another state`,
    );
  }
}

if (
  process.argv[1] &&
  realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  const root = fileURLToPath(new URL("../../..", import.meta.url));
  const presentation =
    await import("../src/features/org-chart/presentation.ts");
  verifyRuntimePresentation(presentation.presentRuntimeStatus);
  verifyControlConnections(await readConnectionSources(root));
  process.stdout.write(
    "OBJ-06 dependency-free witness passed: runtime-label behavior and static graph/tree/approval connections; React interaction is verified by separate unit/browser gates.\n",
  );
}

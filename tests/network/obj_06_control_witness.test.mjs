// OBJ-06 negative controls test the bounded static witness, not React behavior.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { cp, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  CONNECTION_SOURCES,
  readConnectionSources,
  verifyControlConnections,
  verifyRuntimePresentation,
} from "../../apps/web/scripts/run-obj-06-witness.mjs";

const root = fileURLToPath(new URL("../..", import.meta.url));
const sources = await readConnectionSources(root);

test("OBJ-06 actual graph/tree/approval source connections satisfy the bounded witness", () => {
  verifyControlConnections(sources);
});

for (const [name, pattern, replacement, expected] of [
  [
    "page",
    /runtimeStatusByInstanceId=\{\s*statusQuery\.isError \? undefined : runtimeStatusByInstanceId\s*\}/u,
    "runtimeStatusByInstanceId={undefined}",
    /OrgTreeFallback|OrgChartCanvas/,
  ],
  [
    "canvas",
    "runtimeStatusByInstanceId={runtimeStatusByInstanceId}",
    "runtimeStatusByInstanceId={undefined}",
    /canvas must forward/,
  ],
  [
    "canvas",
    "[hierarchy.structuralKey],",
    "[hierarchy.structuralKey, runtimeStatusByInstanceId],",
    /layout remains keyed/,
  ],
  [
    "stage",
    /runtimeStatus=\{runtimeStatusByInstanceId\?\.get\(\s*instance\.id,?\s*\)\}/u,
    "runtimeStatus={undefined}",
    /exact instance identity/,
  ],
  [
    "card",
    "presentRuntimeStatus(runtimeStatus?.status)",
    'presentRuntimeStatus(instance.enabled ? "completed" : "never_run")',
    /rather than deployment/,
  ],
  [
    "card",
    'runtimeStatus?.status ?? "unavailable"',
    'runtimeStatus?.status ?? "never_run"',
    /remain unavailable/,
  ],
  [
    "tree",
    "runtimeStatusByInstanceId?.get(node.id)",
    "undefined",
    /exact node identity/,
  ],
  [
    "snapshot",
    "to={`/approvals?run_id=${encodeURIComponent(run.id)}`}",
    'to="/approvals"',
    /exact run scope/,
  ],
  [
    "approvals",
    "const [searchParams] = useSearchParams();",
    "const searchParams = new URLSearchParams(window.location.search);",
    /router search changes/,
  ],
  [
    "approvals",
    'key={requestedRunId ?? "all-runs"}',
    'key="unchanging"',
    /reset stale approval/,
  ],
  [
    "detail",
    /onOpenRun=\{onOpenRun\}/gu,
    "onOpenRun={undefined}",
    /guarded inspector callback/,
  ],
  [
    "dryRun",
    "onOpenRun(receipt.runId);",
    "window.location.assign(`/runs/${receipt.runId}`);",
    /guarded client navigation/,
  ],
  [
    "page",
    "(dirtyStateRef.current.configuration || dirtyStateRef.current.dryRun) &&",
    "false &&",
    /current configuration and input dirtiness/,
  ],
]) {
  test(`OBJ-06 rejects disconnected ${name}: ${expected.source}`, () => {
    const mutated = sources[name].replace(pattern, replacement);
    assert.notEqual(
      mutated,
      sources[name],
      "mutation must change the actual production boundary",
    );
    assert.throws(
      () => verifyControlConnections({ ...sources, [name]: mutated }),
      (error) =>
        error instanceof assert.AssertionError && expected.test(error.message),
    );
  });
}

test("OBJ-06 commented-out wiring does not satisfy source assertions", () => {
  const invocation = "runtimeStatusByInstanceId={runtimeStatusByInstanceId}";
  assert.throws(
    () =>
      verifyControlConnections({
        ...sources,
        canvas: sources.canvas.replace(invocation, `/* ${invocation} */`),
      }),
    /canvas must forward/,
  );
});

test("OBJ-06 behavioral label check rejects absence and an invented never-run observation", () => {
  assert.throws(
    () => verifyRuntimePresentation(undefined),
    assert.AssertionError,
  );
  assert.throws(
    () => verifyRuntimePresentation(() => "Never run"),
    /runtime undefined/,
  );
});

test("OBJ-06 witness executes with only tracked source and Node, without node_modules or a Git checkout", async () => {
  const fixture = await mkdtemp(
    join(tmpdir(), "marketing-agents-obj06-witness-"),
  );
  try {
    const files = [
      ".nvmrc",
      "apps/web/scripts/require-pinned-node.mjs",
      "apps/web/scripts/run-obj-06-witness.mjs",
      "apps/web/src/features/org-chart/presentation.ts",
      ...Object.values(CONNECTION_SOURCES),
    ];
    for (const file of files) {
      const destination = join(fixture, file);
      await mkdir(dirname(destination), { recursive: true });
      await cp(join(root, file), destination);
    }
    const command = join(fixture, "apps/web/scripts/run-obj-06-witness.mjs");
    const run = () =>
      spawnSync(process.execPath, [command], {
        cwd: fixture,
        encoding: "utf8",
        timeout: 10_000,
      });
    const feature = run();
    assert.equal(feature.error, undefined);
    assert.equal(feature.status, 0, feature.stderr);
    assert.match(
      feature.stdout,
      /runtime-label behavior and static graph\/tree\/approval connections/u,
    );

    await writeFile(
      join(fixture, CONNECTION_SOURCES.snapshot),
      sources.snapshot.replace(
        "to={`/approvals?run_id=${encodeURIComponent(run.id)}`}",
        'to="/approvals"',
      ),
    );
    const disconnected = run();
    assert.equal(disconnected.status, 1);
    assert.match(
      disconnected.stderr,
      /AssertionError.*OBJ-06 pending-action review must retain the exact run scope/u,
    );
    assert.doesNotMatch(
      disconnected.stderr,
      /ERR_MODULE_NOT_FOUND|Cannot find package/u,
    );
  } finally {
    await rm(fixture, { recursive: true, force: true });
  }
});

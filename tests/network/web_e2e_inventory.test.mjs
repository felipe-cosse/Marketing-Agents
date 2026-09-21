import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  BROWSER_RUNNERS,
  BrowserInventoryError,
  readBrowserInventory,
  validateBrowserInventory,
} from "../../apps/web/scripts/browser-evidence-inventory.mjs";

const webRoot = fileURLToPath(new URL("../../apps/web", import.meta.url));
const runner = "run-fixture-e2e.mjs";
const spec = "e2e/fixture.spec.ts";
const source = (args = ["test", spec]) =>
  `runLocalTool("playwright", ${JSON.stringify(args)}, process.env);`;
const inventory = (text = source(), specs = [spec], runners = [runner]) =>
  validateBrowserInventory(runners, specs, new Map([[runner, text]]));

test("DEL-07 inventories every existing browser spec exactly once, including ORCH-01 through WEB-01", () => {
  assert.deepEqual(readBrowserInventory(webRoot), {
    runnerCount: 16,
    specCount: 16,
  });
  assert.equal(BROWSER_RUNNERS.includes("run-orch-01-e2e.mjs"), false);
  assert.match(
    readFileSync(
      new URL("../../apps/web/e2e/web-01-org-chart.spec.ts", import.meta.url),
      "utf8",
    ),
    /test\("ORCH-01 /,
  );
});

test("ARCH-02 remains mandatory in the centralized browser inventory", () => {
  assert.ok(BROWSER_RUNNERS.includes("run-arch-02-e2e.mjs"));
  assert.throws(
    () =>
      readBrowserInventory(
        webRoot,
        BROWSER_RUNNERS.filter((name) => name !== "run-arch-02-e2e.mjs"),
      ),
    BrowserInventoryError,
  );
});

test("OBJ-06 runtime configuration is exact and cannot add filters or exclude its journey", () => {
  const nativeRunner = "run-obj-06-e2e.mjs";
  const nativeSpec = "e2e/obj-06-control-surface.spec.ts";
  const nativeConfig = "config/playwright-obj-06.config.ts";
  const check = (args) =>
    validateBrowserInventory(
      [nativeRunner],
      [nativeSpec],
      new Map([[nativeRunner, source(args)]]),
    );
  assert.deepEqual(check(["test", "--config", nativeConfig, nativeSpec]), {
    runnerCount: 1,
    specCount: 1,
  });
  for (const args of [
    ["test", nativeSpec],
    ["test", "--config", "config/unreviewed.ts", nativeSpec],
    ["test", "--config", nativeConfig, nativeSpec, "--grep", "subset"],
    ["test", "--config", nativeConfig, nativeSpec, "--pass-with-no-tests"],
    ["test", "--config", nativeConfig, nativeSpec, "--list"],
  ])
    assert.throws(() => check(args), BrowserInventoryError);
  assert.throws(
    () => inventory(source(["test", "--config", nativeConfig, spec])),
    /no filters or zero-test overrides/,
  );
  const runtimeConfig = readFileSync(
    new URL(
      "../../apps/web/config/playwright-obj-06.config.ts",
      import.meta.url,
    ),
    "utf8",
  );
  assert.match(runtimeConfig, /obj-06-control-surface\.spec\.ts/);
  assert.doesNotMatch(runtimeConfig, /testIgnore|grepInvert|test\.skip/);
});

test("DEL-07 refuses zero runners or zero discovered browser specs", () => {
  assert.throws(
    () => inventory(source(), [spec], []),
    /nonzero runners and specs/,
  );
  assert.throws(() => inventory(source(), []), /nonzero runners and specs/);
});

test("DEL-07 refuses a new unowned spec, including nested browser files", () => {
  assert.throws(
    () => inventory(source(), [spec, "e2e/new/nested.spec.ts"]),
    /Unowned browser spec/,
  );
});

test("DEL-07 refuses missing, empty, and non-executing runner declarations", () => {
  assert.throws(
    () => validateBrowserInventory([runner], [spec], new Map()),
    /Missing or empty/,
  );
  assert.throws(() => inventory(" \n"), /Missing or empty/);
  assert.throws(() => inventory(`// ${source()}`), /found 0/);
  assert.throws(() => inventory(`/* ${source()} */`), /found 0/);
  assert.throws(
    () => inventory(`console.log(${JSON.stringify(source())});`),
    /found 0/,
  );
  assert.throws(
    () => readBrowserInventory(webRoot, ["run-missing-e2e.mjs"]),
    /Cannot read browser runner/,
  );
});

test("DEL-07 refuses missing selected specs and duplicate runner/spec ownership", () => {
  assert.throws(
    () => inventory(source(["test", "e2e/missing.spec.ts"])),
    /does not exist/,
  );
  assert.throws(
    () => inventory(source(), [spec], [runner, runner]),
    /Duplicate browser runner/,
  );
  assert.throws(
    () => inventory(source(["test", spec, spec])),
    /duplicate ownership/,
  );
  assert.throws(
    () => inventory(source(), [spec, spec]),
    /Duplicate discovered/,
  );
});

test("DEL-07 refuses filtered, dynamic, and allow-zero-test runner selections", () => {
  for (const args of [
    ["test"],
    ["test", spec, "--grep", "one test"],
    ["test", spec, "--pass-with-no-tests"],
    ["test", spec, "--list"],
  ]) {
    assert.throws(
      () => inventory(source(args)),
      /no filters or zero-test overrides/,
    );
  }
  assert.throws(
    () => inventory('runLocalTool("playwright", selections);'),
    /literal array/,
  );
  assert.throws(
    () => inventory('runLocalTool("playwright", ["test", spec]);'),
    /only literal strings/,
  );
  assert.throws(() => inventory(`${source()}\n${source()}`), /exactly one/);
});

test("DEL-07 accepts the current literal unfiltered runner contract and rejects traversal", () => {
  assert.deepEqual(inventory(), { runnerCount: 1, specCount: 1 });
  assert.deepEqual(inventory(source().replace("runLocalTool", "run")), {
    runnerCount: 1,
    specCount: 1,
  });
  assert.throws(
    () =>
      inventory(source(["test", "e2e/../private.spec.ts"]), [
        "e2e/../private.spec.ts",
      ]),
    BrowserInventoryError,
  );
  assert.throws(
    () => readBrowserInventory(webRoot, ["../run-private-e2e.mjs"]),
    /Invalid browser runner filename/,
  );
});

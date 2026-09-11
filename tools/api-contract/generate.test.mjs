// DEL-07: generator failures are observable and check mode never repairs drift.
import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  renderArtifacts,
  requireLocalReferences,
  snapshotPath,
  typesPath,
  verifyOrWriteArtifacts,
} from "./generate.mjs";

const document = {
  openapi: "3.1.0",
  info: { title: "DEL-07 fixture", version: "1" },
  paths: { "/health": { get: { responses: { 200: { description: "ok" } } } } },
  components: {
    schemas: {
      Example: {
        type: "object",
        required: ["state", "items"],
        properties: {
          state: { enum: ["ready", "failed"] },
          items: { type: "array", items: { type: "integer" } },
          nullable: { anyOf: [{ type: "string" }, { type: "null" }] },
        },
      },
    },
  },
};

test("DEL-07 rejects every nonlocal reference before any resolver runs", () => {
  for (const ref of [
    "https://example.invalid/schema",
    "../schema.json",
    "file:///tmp/schema",
    42,
    null,
  ]) {
    assert.throws(
      () => requireLocalReferences({ nested: [{ $ref: ref }] }),
      /nonlocal/,
    );
  }
  assert.doesNotThrow(() =>
    requireLocalReferences({ $ref: "#/components/schemas/Example" }),
  );
});

test("DEL-07 fails empty/malformed contracts instead of accepting zero generated types", async () => {
  await assert.rejects(
    renderArtifacts({ openapi: "3.1.0" }),
    /paths and component schemas/,
  );
  await assert.rejects(
    renderArtifacts({ ...document, openapi: "3.0.0" }),
    /paths and component schemas/,
  );
});

test("DEL-07 deterministic readonly types preserve union, optional, array and null", async () => {
  const first = await renderArtifacts(document);
  assert.deepEqual(await renderArtifacts(document), first);
  assert.equal(first.size, 2);
  assert.deepEqual(JSON.parse(first.get(snapshotPath)), document);
  const types = first.get(typesPath);
  assert.match(types, /readonly state: "ready" \| "failed"/);
  assert.match(types, /readonly items: readonly number\[\]/);
  assert.match(types, /readonly nullable\?: string \| null/);
});

test("DEL-07 snapshot retains exact source numeric bounds without a JavaScript round-trip", async () => {
  const source = JSON.stringify(document).replace(
    '"type":"integer"',
    '"type":"integer","maximum":9.223372036854776e+18',
  );
  const parsed = JSON.parse(source);
  const untouched = structuredClone(parsed);
  const artifacts = await renderArtifacts(parsed, source);
  assert.match(artifacts.get(snapshotPath), /9\.223372036854776e\+?18/);
  assert.doesNotMatch(artifacts.get(snapshotPath), /9223372036854776000/);
  assert.deepEqual(parsed, untouched);
});

test("DEL-07 missing and changed artifacts fail without overwriting caller files", async () => {
  const root = await mkdtemp(
    join(tmpdir(), "marketing-agents-del07-contract-"),
  );
  try {
    const artifacts = await renderArtifacts(document);
    await assert.rejects(
      verifyOrWriteArtifacts(root, artifacts),
      /API contract drift/,
    );
    await verifyOrWriteArtifacts(root, artifacts, true);
    await verifyOrWriteArtifacts(root, artifacts);
    const changed = new Map(artifacts);
    changed.set(typesPath, "different\n");
    await assert.rejects(
      verifyOrWriteArtifacts(root, changed),
      /API contract drift/,
    );
    assert.equal(
      await readFile(join(root, typesPath), "utf8"),
      artifacts.get(typesPath),
    );
    await assert.rejects(
      verifyOrWriteArtifacts(root, new Map([["../unsafe", "x"]]), true),
      /Expected exactly/,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("DEL-07 empty, partial and symlinked output cannot masquerade as a successful generation", async () => {
  const root = await mkdtemp(
    join(tmpdir(), "marketing-agents-del07-artifacts-"),
  );
  try {
    const artifacts = await renderArtifacts(document);
    for (const incomplete of [new Map(), new Map([[typesPath, "x"]])]) {
      await assert.rejects(
        verifyOrWriteArtifacts(root, incomplete),
        /Expected exactly/,
      );
    }
    await mkdir(join(root, "owned"));
    await symlink(join(root, "owned"), join(root, "apps"));
    await assert.rejects(
      verifyOrWriteArtifacts(root, artifacts, true),
      /must not follow symlinks/,
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

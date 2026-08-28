// WEB-04 dependency-free witness executes the production schema and dry-run transport boundaries.
import "./require-pinned-node.mjs";

import assert from "node:assert/strict";
import { globSync, readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

registerHooks({
  resolve(specifier, context, nextResolve) {
    try {
      return nextResolve(specifier, context);
    } catch (error) {
      if (specifier.startsWith(".") && !specifier.endsWith(".ts")) {
        return nextResolve(`${specifier}.ts`, context);
      }
      throw error;
    }
  },
});

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(webRoot, "../..");
const [
  schemaModule,
  defaultsModule,
  validationModule,
  problemMappingModule,
  manualDryRunModule,
  localSessionModule,
] = await Promise.all([
  import("../src/features/dry-run/schemaModel.ts"),
  import("../src/features/dry-run/schemaDefaults.ts"),
  import("../src/features/dry-run/schemaValidation.ts"),
  import("../src/features/dry-run/mapProblemDetails.ts"),
  import("../src/api/manualDryRun.ts"),
  import("../src/api/localSession.ts"),
]);
const { compileInputSchema } = schemaModule;
const { createSchemaDefaults } = defaultsModule;
const { validateSchemaInput } = validationModule;
const { mapDryRunFieldErrors } = problemMappingModule;
const { createManualDryRun } = manualDryRunModule;
const { clearLocalSession } = localSessionModule;

const schemaPaths = globSync("catalog/v1/schemas/*/input.schema.json", {
  cwd: repositoryRoot,
}).toSorted();
assert.equal(schemaPaths.length, 36);

for (const relativePath of schemaPaths) {
  const raw = JSON.parse(
    readFileSync(resolve(repositoryRoot, relativePath), "utf8"),
  );
  const compiled = compileInputSchema(raw);
  assert.equal(compiled.kind, "object");
  assert.equal(compiled.additionalProperties, false);
  assert.equal(compiled.properties.length, 4);
  assert.deepEqual(
    compiled.properties.map(({ name }) => name),
    ["request_id", "source_content", "audience", "locale"],
  );
  const sourceContent = compiled.properties.find(
    ({ name }) => name === "source_content",
  );
  assert.equal(sourceContent?.schema.kind, "string");
  assert.equal(sourceContent?.schema.sensitive, true);
}

const synthetic = compileInputSchema({
  $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: "urn:marketing-agents:witness:web-04",
  type: "object",
  title: "Full supported subset",
  additionalProperties: false,
  required: ["profile", "channels"],
  properties: {
    profile: {
      type: "object",
      additionalProperties: false,
      required: ["email", "score"],
      properties: {
        email: { type: "string", format: "email", maxLength: 200 },
        score: {
          type: "number",
          minimum: 0,
          maximum: 1,
          default: 0.5,
        },
        subscribed: { type: "boolean", default: true },
      },
    },
    channels: {
      type: "array",
      minItems: 1,
      maxItems: 3,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["kind", "quota"],
        properties: {
          kind: { type: "string", enum: ["email", "social"] },
          quota: { type: "integer", minimum: 1, maximum: 10 },
        },
      },
    },
    launch_date: { type: "string", format: "date", maxLength: 10 },
    callback: {
      type: "string",
      format: "uri",
      maxLength: 2_000,
      description: "Validation does not fetch this URI.",
      "x-ui": { order: 20, help: "Optional callback." },
    },
    notes: {
      type: "string",
      maxLength: 2_000,
      "x-sensitive": true,
      "x-ui": { control: "textarea", order: 30, help: "Private notes." },
    },
  },
});
assert.equal(synthetic.properties.length, 5);
assert.equal(
  synthetic.properties.find(({ name }) => name === "profile")?.schema.kind,
  "object",
);
assert.equal(
  synthetic.properties.find(({ name }) => name === "channels")?.schema.kind,
  "array",
);

assert.throws(
  () =>
    compileInputSchema({
      type: "object",
      additionalProperties: true,
      properties: {},
    }),
  /closed|support|additional/u,
);

assert.throws(
  () =>
    compileInputSchema({
      type: "object",
      additionalProperties: false,
      properties: {
        hostile: {
          type: "string",
          maxLength: 100_000,
          pattern: "^(a+)+$",
        },
      },
    }),
  /pattern|safe/u,
);

assert.deepEqual(createSchemaDefaults(synthetic), {
  profile: { score: 0.5, subscribed: true },
});
const validInput = validateSchemaInput(synthetic, {
  profile: {
    email: "operator@example.test",
    score: "0.75",
    subscribed: true,
  },
  channels: [{ kind: "email", quota: "2" }],
  launch_date: "2026-08-28",
  callback: "https://example.test/callback",
  notes: "memory-only",
});
assert.equal(validInput.ok, true);
if (!validInput.ok) throw new Error("valid WEB-04 witness input was rejected");
assert.deepEqual(validInput.input, {
  profile: {
    email: "operator@example.test",
    score: 0.75,
    subscribed: true,
  },
  channels: [{ kind: "email", quota: 2 }],
  launch_date: "2026-08-28",
  callback: "https://example.test/callback",
  notes: "memory-only",
});

const rejectedInput = validateSchemaInput(synthetic, {
  profile: { email: "invalid", score: "2", subscribed: true },
  channels: [],
  launch_date: "2026-02-30",
  callback: "not a uri",
  notes: "sensitive-value".repeat(500),
});
assert.equal(rejectedInput.ok, false);
if (rejectedInput.ok)
  throw new Error("invalid WEB-04 witness input was accepted");
assert.ok(rejectedInput.issues.length >= 5);
assert.equal(
  rejectedInput.issues.some(({ message }) =>
    message.includes("sensitive-value"),
  ),
  false,
);

assert.deepEqual(
  mapDryRunFieldErrors(synthetic, [
    {
      pointer: "/input/profile/email",
      code: "format",
      message: "unsafe reflected server value",
    },
    {
      pointer: "/input/not_a_field",
      code: "unknown",
      message: "must remain unmapped",
    },
    {
      pointer: "/request",
      code: "request_invalid",
      message: "must remain in the request summary",
    },
  ]),
  [
    {
      pointer: "/input/profile/email",
      code: "server_rejected",
      message: "The server rejected this field.",
    },
  ],
);

const instanceId = "inst.social-media.new-content.agent-1.01";
const idempotencyKey = "web-04-witness-idempotency-key";
const correlationId = `correlation.api.${"c".repeat(32)}`;
const receipt = {
  status: "accepted",
  disposition: "created",
  eventId: `manual-event-hmac-sha256-v1:${"d".repeat(64)}`,
  workId: "work.web-04.witness",
  runId: "run.web-04.witness",
  executionMode: "dry_run",
  instanceUrl: `/api/v1/agent-instances/${instanceId}`,
  runUrl: "/api/v1/runs/run.web-04.witness",
};
const sessions = ["a".repeat(43), "b".repeat(43)];
const requests = [];
const priorFetch = globalThis.fetch;
globalThis.fetch = async (path, request) => {
  requests.push({ path, request });
  const index = requests.length;
  if (index === 1 || index === 3) {
    return new Response(
      JSON.stringify({
        actorId: "local-operator",
        roles: ["approver", "local_admin", "operator", "viewer"],
        scopes: [],
        authMode: "local",
        environment: "local",
        modelMode: "mock",
        connectorMode: "mock",
        networkPermission: false,
        warning: "Local identity — not production authentication",
        csrfToken: sessions[index === 1 ? 0 : 1],
        csrfHeaderName: "X-CSRF-Token",
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }
  if (index === 2) {
    return new Response(
      JSON.stringify({
        type: "urn:marketing-agents:problem:csrf_token_invalid",
        title: "Browser request forbidden",
        status: 403,
        detail: "The browser session token is stale.",
        instance: `urn:marketing-agents:request:${correlationId}`,
        code: "csrf_token_invalid",
        correlation_id: correlationId,
      }),
      {
        status: 403,
        headers: {
          "Content-Type": "application/problem+json",
          "X-Correlation-ID": correlationId,
        },
      },
    );
  }
  if (index === 4) {
    return new Response(JSON.stringify(receipt), {
      status: 202,
      headers: { "Content-Type": "application/json" },
    });
  }
  throw new Error("unexpected WEB-04 witness fetch");
};

clearLocalSession();
try {
  assert.deepEqual(
    await createManualDryRun({
      instanceId,
      input: { request_id: "web-04-witness", source_content: "private" },
      executionMode: "dry_run",
      idempotencyKey,
    }),
    receipt,
  );
} finally {
  globalThis.fetch = priorFetch;
  clearLocalSession();
}

assert.equal(requests.length, 4);
assert.equal(requests[0]?.path, "/api/v1/session");
assert.equal(
  requests[1]?.path,
  `/api/v1/agent-instances/${instanceId}/dry-runs`,
);
assert.equal(requests[2]?.path, "/api/v1/session");
assert.equal(requests[3]?.path, requests[1]?.path);
const firstMutation = requests[1]?.request;
const retriedMutation = requests[3]?.request;
assert.equal(firstMutation?.body, retriedMutation?.body);
assert.deepEqual(JSON.parse(String(firstMutation?.body)), {
  input: { request_id: "web-04-witness", source_content: "private" },
  executionMode: "dry_run",
});
const firstHeaders = new Headers(firstMutation?.headers);
const retriedHeaders = new Headers(retriedMutation?.headers);
assert.equal(firstHeaders.get("Idempotency-Key"), idempotencyKey);
assert.equal(retriedHeaders.get("Idempotency-Key"), idempotencyKey);
assert.equal(firstHeaders.get("X-CSRF-Token"), sessions[0]);
assert.equal(retriedHeaders.get("X-CSRF-Token"), sessions[1]);
assert.equal(firstMutation?.credentials, "same-origin");
assert.equal(firstMutation?.cache, "no-store");

process.stdout.write(
  `WEB-04 dependency-free witness passed: Node ${process.versions.node}, 36 catalog schemas, the complete bounded schema subset, safe rejection, and the strict same-origin dry-run mutation.\n`,
);

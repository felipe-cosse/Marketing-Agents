// DEL-01 orchestration controls, not live browser/product qualification.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runDel01ProductJourney } from "./del-01-product-journey.mjs";

const ORIGIN = "http://127.0.0.1:8080";
const SCENARIO = "demo.social-media.content-draft.v1";
const TEMPLATE = "tpl.social-media.new-content.linkedin-post-drafter";
const INSTANCE = "inst.social-media.new-content.linkedin-post-drafter.01";
const SCHEMA = "schema.demo.social-media.content-draft.output.v1";
const PRIVATE_SENTINEL = "untrusted-payload-or-token-must-not-appear";

function fixture() {
  const receipt = {
    status: "accepted",
    disposition: "created",
    scenarioId: SCENARIO,
    executionMode: "dry_run",
    runId: "run.del01.fixture",
    workId: "work.del01.fixture",
    instanceUrl: `/api/v1/agent-instances/${INSTANCE}`,
    runUrl: "/api/v1/runs/run.del01.fixture",
    timelineUrl: "/api/v1/runs/run.del01.fixture/timeline",
    artifactsUrl: "/api/v1/runs/run.del01.fixture/artifacts",
  };
  const summary = {
    id: "artifact.del01.fixture",
    run_id: receipt.runId,
    work_item_id: receipt.workId,
    step_id: "step.del01.fixture",
    workflow_id: SCENARIO,
    workflow_version: "1",
    template_id: TEMPLATE,
    instance_id: INSTANCE,
    output_schema_id: SCHEMA,
    output_schema_version: "v1",
    classification: "internal",
    artifact_url: "/api/v1/artifacts/artifact.del01.fixture",
    run_url: receipt.runUrl,
  };
  const run = {
    id: receipt.runId,
    work_item_id: receipt.workId,
    workflow_id: SCENARIO,
    instance_id: INSTANCE,
    mode: "dry_run",
    state: "completed",
    external_actions: [],
    pending_approvals: [],
    execution_control: { model_calls: 1, tool_calls: 0 },
    approval_required: false,
    details_expired: false,
    artifacts_truncated: false,
    artifact_summaries: [summary],
    catalog_hash: `catalog-sha256-v1:${"a".repeat(64)}`,
    configuration_revision: 1,
  };
  const artifact = {
    ...summary,
    catalog_hash: run.catalog_hash,
    instance_config_revision: 1,
    payload_digest: `artifact-hmac-sha256-v1:${"b".repeat(64)}`,
    output_schema_hash: `schema-sha256-v1:${"c".repeat(64)}`,
    parent_artifact_ids: [],
    sources: [
      {
        kind: "work_input",
        source_id: receipt.workId,
        classification: "internal",
      },
    ],
    providers: [
      { provider_kind: "llm", mode: "mock", name: "mock", version: "v1" },
    ],
    redacted_payload: {
      scenario_id: SCENARIO,
      artifact_type: "social_post_draft",
      platform: "LinkedIn",
      publication_status: "not_published",
      proposed_actions: [],
      draft_text: PRIVATE_SENTINEL,
    },
  };
  return {
    receipt,
    run,
    artifact,
    reloaded: structuredClone(artifact),
    receiptStatus: 202,
    receiptCache: "no-store",
    responseOrigin: ORIGIN,
    contentType: "application/json",
    uiFailure: null,
  };
}

function harness(data) {
  const actions = [];
  const waits = [];
  let url = `${ORIGIN}/`;
  let title = "Organization chart | Marketing Agents";
  let pending;
  let artifactReads = 0;
  const response = (path, method, body, status = 200) => ({
    url: () => `${data.responseOrigin}${path}`,
    status: () => status,
    headers: () => ({
      "content-type": data.contentType,
      "cache-control": data.receiptCache,
    }),
    request: () => ({ method: () => method }),
    json: async () => structuredClone(body),
  });
  async function emit(value, last = true) {
    assert.ok(
      pending,
      "response waiter must be installed before the UI action",
    );
    const waiter = pending;
    try {
      if (await waiter.predicate(value)) {
        pending = undefined;
        waiter.resolve(value);
      } else if (last) {
        pending = undefined;
        waiter.reject(new Error(`bounded timeout: ${PRIVATE_SENTINEL}`));
      }
    } catch (error) {
      pending = undefined;
      waiter.reject(error);
    }
  }
  function locator(key) {
    return {
      key,
      getByRole: (role, { name } = {}) =>
        locator(`${key}/role:${role}:${name ?? ""}`),
      getByText: (value) => locator(`${key}/text:${value}`),
      filter: ({ has }) => locator(`${key}/has:${has.key}`),
      async click() {
        actions.push(key);
        if (key.endsWith("role:link:Demos")) {
          url = `${ORIGIN}/demos`;
          title = "Demos | Marketing Agents";
        } else if (key.endsWith("role:button:Create draft")) {
          await emit(
            response(
              `/api/v1/demo-scenarios/${SCENARIO}/runs`,
              "POST",
              data.receipt,
              data.receiptStatus,
            ),
          );
        } else if (key.endsWith("role:link:Open accepted run")) {
          url = `${ORIGIN}/runs/run.del01.fixture`;
          title = "Run timeline | Marketing Agents";
          if (data.intermediateRun !== undefined) {
            await emit(
              response(
                "/api/v1/runs/run.del01.fixture",
                "GET",
                data.intermediateRun,
              ),
              false,
            );
          }
          await emit(
            response("/api/v1/runs/run.del01.fixture", "GET", data.run),
          );
        } else if (key.endsWith("role:link:artifact.del01.fixture")) {
          url = `${ORIGIN}/artifacts/artifact.del01.fixture`;
          title = "Artifact viewer | Marketing Agents";
          artifactReads += 1;
          await emit(
            response(
              "/api/v1/artifacts/artifact.del01.fixture",
              "GET",
              data.artifact,
            ),
          );
        } else throw new Error(`Unexpected fixture action: ${key}`);
      },
    };
  }
  const page = {
    setViewportSize: async (value) => actions.push(value),
    getByRole: (role, { name }) => locator(`role:${role}:${name}`),
    getByText: (value) => locator(`text:${value}`),
    locator,
    waitForResponse(predicate, options) {
      assert.equal(pending, undefined);
      assert.deepEqual(options, { timeout: 30_000 });
      waits.push(options);
      return new Promise((resolve, reject) => {
        pending = { predicate, resolve, reject };
      });
    },
    async reload() {
      actions.push("reload");
      artifactReads += 1;
      await emit(
        response(
          "/api/v1/artifacts/artifact.del01.fixture",
          "GET",
          data.reloaded,
        ),
      );
    },
  };
  function expect(target) {
    const check = (method, expected, options) => {
      assert.deepEqual(options, { timeout: 30_000 });
      const key = target.key ?? "page";
      if (data.uiFailure === key) throw new Error(PRIVATE_SENTINEL);
      if (method === "url") assert.equal(url, expected);
      if (method === "title") assert.equal(title, expected);
      if (method === "attribute") {
        assert.equal(expected[0], "href");
        const route = key.endsWith("Open accepted run")
          ? "/runs/run.del01.fixture"
          : "/artifacts/artifact.del01.fixture";
        assert.equal(route, expected[1]);
      }
      if (method === "text")
        assert.equal(
          data.run.state === "completed" ? "Completed" : "Executing",
          expected,
        );
      actions.push(`assert:${method}:${key}`);
    };
    return {
      toHaveURL: async (value, options) => check("url", value, options),
      toHaveTitle: async (value, options) => check("title", value, options),
      toBeVisible: async (options) => check("visible", null, options),
      toHaveAttribute: async (name, value, options) =>
        check("attribute", [name, value], options),
      toHaveText: async (value, options) => check("text", value, options),
      toContainText: async (value, options) =>
        check("contains", value, options),
    };
  }
  return { page, expect, actions, waits, artifactReads: () => artifactReads };
}

test("DEL-01 live journey orchestration reaches real-response checkpoints then reload before success", async () => {
  const controls = harness(fixture());
  const result = await runDel01ProductJourney({ ...controls, origin: ORIGIN });
  assert.deepEqual(result, [
    "del-01-live-social-submission",
    "del-01-completed-run-artifact-provenance",
    "del-01-artifact-reload-identity",
  ]);
  assert.equal(controls.artifactReads(), 2);
  assert.equal(controls.waits.length, 4);
  const clicks = controls.actions.filter(
    (item) => typeof item === "string" && !item.startsWith("assert:"),
  );
  assert.deepEqual(clicks, [
    "role:link:Demos",
    "role:form:Social draft demo preset/role:button:Create draft",
    "role:link:Open accepted run",
    ".run-artifacts/role:link:artifact.del01.fixture",
    "reload",
  ]);
  assert.equal(JSON.stringify(result).includes(PRIVATE_SENTINEL), false);
});

test("DEL-01 waits for a later completed UI response without treating a progress snapshot as success", async () => {
  const data = fixture();
  data.intermediateRun = {
    ...structuredClone(data.run),
    state: "executing",
    artifact_summaries: [],
    execution_control: { model_calls: 0, tool_calls: 0 },
  };
  const controls = harness(data);
  const result = await runDel01ProductJourney({ ...controls, origin: ORIGIN });
  assert.equal(result.length, 3);
  assert.equal(controls.artifactReads(), 2);
});

const cases = [
  [
    "nonaccepted status",
    (data) => {
      data.receiptStatus = 200;
    },
    "response_status_200",
  ],
  [
    "cached receipt",
    (data) => {
      data.receiptCache = "public";
    },
    "receipt_cache",
  ],
  [
    "nonJSON receipt",
    (data) => {
      data.contentType = "text/html";
    },
    "response_content_type",
  ],
  [
    "external response origin",
    (data) => {
      data.responseOrigin = "https://example.test";
    },
    "response_origin_or_path",
  ],
  [
    "invalid receipt object",
    (data) => {
      data.receipt = [];
    },
    "response_object",
  ],
  [
    "replayed receipt",
    (data) => {
      data.receipt.disposition = "replayed";
    },
    "receipt_identity",
  ],
  [
    "wrong scenario",
    (data) => {
      data.receipt.scenarioId = "other";
    },
    "receipt_identity",
  ],
  [
    "unsafe run id",
    (data) => {
      data.receipt.runId = "../../outside";
    },
    "receipt_identity",
  ],
  [
    "mock execution instead of read only",
    (data) => {
      data.receipt.executionMode = "mock_execute";
    },
    "receipt_identity",
  ],
  [
    "receipt resource substitution",
    (data) => {
      data.receipt.runUrl = "https://example.test/run";
    },
    "receipt_resources",
  ],
  [
    "wrong run",
    (data) => {
      data.run.id = "other";
    },
    "run_identity_or_effects",
  ],
  [
    "wrong work",
    (data) => {
      data.run.work_item_id = "other";
    },
    "run_identity_or_effects",
  ],
  [
    "external action",
    (data) => {
      data.run.external_actions = [{}];
    },
    "run_identity_or_effects",
  ],
  [
    "pending approval",
    (data) => {
      data.run.pending_approvals = [{}];
    },
    "run_identity_or_effects",
  ],
  [
    "failed run",
    (data) => {
      data.run.state = "failed";
    },
    "run_state",
  ],
  [
    "unfinished run",
    (data) => {
      data.run.state = "executing";
    },
    "completed_run",
  ],
  [
    "unexpected tool call",
    (data) => {
      data.run.execution_control.tool_calls = 1;
    },
    "completed_run_evidence",
  ],
  [
    "wrong model count",
    (data) => {
      data.run.execution_control.model_calls = 0;
    },
    "completed_run_evidence",
  ],
  [
    "truncated artifact inventory",
    (data) => {
      data.run.artifacts_truncated = true;
    },
    "completed_run_evidence",
  ],
  [
    "missing artifact",
    (data) => {
      data.run.artifact_summaries = [];
    },
    "completed_run_evidence",
  ],
  [
    "wrong artifact run",
    (data) => {
      data.artifact.run_id = "other";
    },
    "artifact_identity",
  ],
  [
    "wrong artifact template",
    (data) => {
      data.artifact.template_id = "other";
    },
    "artifact_identity",
  ],
  [
    "wrong artifact schema",
    (data) => {
      data.artifact.output_schema_id = "other";
    },
    "artifact_identity",
  ],
  [
    "wrong artifact instance",
    (data) => {
      data.artifact.instance_id = "other";
    },
    "artifact_identity",
  ],
  [
    "wrong catalog provenance",
    (data) => {
      data.artifact.catalog_hash = "other";
    },
    "artifact_provenance",
  ],
  [
    "real model provenance",
    (data) => {
      data.artifact.providers[0].mode = "real";
    },
    "artifact_provenance",
  ],
  [
    "missing work source",
    (data) => {
      data.artifact.sources = [];
    },
    "artifact_provenance",
  ],
  [
    "raw digest",
    (data) => {
      data.artifact.payload_digest = "a".repeat(64);
    },
    "artifact_provenance",
  ],
  [
    "published artifact",
    (data) => {
      data.artifact.redacted_payload.publication_status = "published";
    },
    "artifact_read_only",
  ],
  [
    "proposed action",
    (data) => {
      data.artifact.redacted_payload.proposed_actions = [{}];
    },
    "artifact_read_only",
  ],
  [
    "reload changed artifact",
    (data) => {
      data.reloaded.id = "artifact.other";
      data.reloaded.artifact_url = "/api/v1/artifacts/artifact.other";
    },
    "artifact_provenance",
  ],
  [
    "reload changed digest",
    (data) => {
      data.reloaded.payload_digest = `artifact-hmac-sha256-v1:${"d".repeat(64)}`;
    },
    "artifact_reload_identity",
  ],
  [
    "reload changed source",
    (data) => {
      data.reloaded.sources.push({
        kind: "external_observation",
        source_id: "other",
      });
    },
    "artifact_reload_identity",
  ],
  [
    "rendered timeline unavailable",
    (data) => {
      data.uiFailure =
        "role:list:Run timeline in sequence order/role:listitem:/has:text:Executing → Completed/text:run.transitioned";
    },
    "completed_run",
  ],
  [
    "artifact viewer unavailable",
    (data) => {
      data.uiFailure = "role:heading:Artifact viewer";
    },
    "artifact_view",
  ],
];

for (const [name, change, code] of cases) {
  test(`DEL-01 rejects ${name} without retaining private diagnostics`, async () => {
    const data = fixture();
    change(data);
    await assert.rejects(
      runDel01ProductJourney({ ...harness(data), origin: ORIGIN }),
      (error) => {
        assert.equal(error.message, `DEL-01 ${code}`);
        assert.equal(String(error.stack).includes(PRIVATE_SENTINEL), false);
        assert.equal(error.cause, undefined);
        return true;
      },
    );
  });
}

for (const status of [
  403,
  422,
  500,
  -1,
  600,
  Number.NaN,
  "403",
  PRIVATE_SENTINEL,
]) {
  test(`DEL-01 HTTP status diagnostic is numeric or invalid: ${typeof status === "number" ? String(status) : typeof status}`, async () => {
    const data = fixture();
    data.receiptStatus = status;
    const suffix =
      Number.isInteger(status) && status >= 100 && status <= 599
        ? String(status)
        : "invalid";
    await assert.rejects(
      runDel01ProductJourney({ ...harness(data), origin: ORIGIN }),
      { message: `DEL-01 response_status_${suffix}` },
    );
  });
}

for (const origin of [
  "https://127.0.0.1:8080",
  "http://example.test",
  "http://user@127.0.0.1",
  `${ORIGIN}/path`,
  `${ORIGIN}?token=secret`,
  "not a URL",
]) {
  test(`DEL-01 rejects invalid origin before any UI action: ${origin}`, async () => {
    const controls = harness(fixture());
    await assert.rejects(runDel01ProductJourney({ ...controls, origin }), {
      message: "DEL-01 origin_invalid",
    });
    assert.deepEqual(controls.actions, []);
  });
}

test("DEL-01 production runner retains isolation using native Fetch Metadata and strict WebSocket denial", async () => {
  const source = await readFile(
    new URL("./run-del-05-e2e.mjs", import.meta.url),
    "utf8",
  );
  assert.match(
    source,
    /import \{ installObj06NativeNetworkGuard \} from "\.\.\/config\/obj-06-native-network\.ts"/u,
  );
  assert.match(
    source,
    /const isolation = await installPlaywrightIsolation\(\{/u,
  );
  assert.match(
    source,
    /installContextGuard: \(guardedContext, allowedOrigin\) =>\s*installObj06NativeNetworkGuard\(guardedContext, allowedOrigin, \{\s*denyWebSockets: true,\s*\}\)/u,
  );
  assert.match(source, /serviceWorkers: "block"/u);
  assert.match(source, /await isolation\.assertNoExternalAttempts\(\)/u);
  assert.match(source, /isolation\.restore\(\)/u);
  assert.doesNotMatch(source, /context\.routeWebSocket\(/u);
});

test("DEL-01 production runner awaits the journey before console/isolation checks and reports its checks", async () => {
  const source = await readFile(
    new URL("./run-del-05-e2e.mjs", import.meta.url),
    "utf8",
  );
  assert.match(
    source,
    /import \{ runDel01ProductJourney \} from "\.\/del-01-product-journey\.mjs"/u,
  );
  assert.match(
    source,
    /const deliveryChecks = await runDel01ProductJourney\(\{\s*page,\s*expect,\s*origin: origin\.origin,\s*\}\)/u,
  );
  assert.match(source, /"mobile-tree",\s*\.\.\.deliveryChecks/u);
  assert.ok(
    source.indexOf("const deliveryChecks") >
      source.indexOf('"mobile document must not overflow horizontally"'),
  );
  assert.ok(
    source.indexOf("const deliveryChecks") <
      source.indexOf("await isolation.assertNoExternalAttempts()"),
  );
  assert.ok(
    source.indexOf("const deliveryChecks") <
      source.indexOf(
        '"production browser must remain healthy and same-origin"',
      ),
  );
});

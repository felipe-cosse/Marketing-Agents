// WEB-03 dependency-free witness executes the strict detail and configuration production boundaries.
import "./require-pinned-node.mjs";

import assert from "node:assert/strict";
import { registerHooks } from "node:module";

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

const [detailModule, configurationModule, fixtureModule] = await Promise.all([
  import("../src/api/agentInstanceDetail.ts"),
  import("../src/api/instanceConfiguration.ts"),
  import("../src/test/agentDetailFixture.ts"),
]);
const { fetchAgentInstanceDetail, normalizeAgentInstanceDetail } = detailModule;
const { normalizeLocalSession, serializeInstanceConfigurationPatch } =
  configurationModule;
const { AGENT_DETAIL_ETAG, makeAgentDetailPayload } = fixtureModule;

const identity = Object.freeze({
  instanceId: "inst.social-media.new-content.agent-1.01",
  templateId: "tpl.social-media.new-content.agent-1",
  departmentId: "dept.social-media",
  functionId: "func.social-media.new-content",
  sourceOrdinal: 1,
  sharedTemplateDeploymentCount: 1,
  catalogVersion: "1.0.0",
  catalogHash: `catalog-sha256-v1:${"a".repeat(64)}`,
});
const detail = normalizeAgentInstanceDetail(
  makeAgentDetailPayload({
    instanceId: identity.instanceId,
    templateId: identity.templateId,
    departmentId: identity.departmentId,
    functionId: identity.functionId,
    runtime: "completed",
  }),
  identity,
  AGENT_DETAIL_ETAG,
);
assert.equal(detail.runtimeAvailable, true);
assert.equal(detail.recentRuns.length, 1);
assert.equal(detail.recentRuns[0]?.state, "completed");
assert.equal(detail.template.allowedToolCapabilityIds.length, 1);
assert.equal(
  detail.instance.configurationEtag,
  '"instance-configuration-v1-1"',
);

const staticDetail = normalizeAgentInstanceDetail(
  makeAgentDetailPayload({
    instanceId: identity.instanceId,
    templateId: identity.templateId,
    departmentId: identity.departmentId,
    functionId: identity.functionId,
    runtime: "static",
  }),
  identity,
  AGENT_DETAIL_ETAG,
);
assert.equal(staticDetail.runtimeAvailable, false);
assert.equal(staticDetail.runtimeStatus, null);

const duplicateIdentity = Object.freeze({
  ...identity,
  instanceId: "inst.community.events.agent-1.02",
  templateId: "tpl.community.events.agent-1",
  departmentId: "dept.community",
  functionId: "func.community.events",
  sourceOrdinal: 2,
  sharedTemplateDeploymentCount: 2,
});
const duplicate = normalizeAgentInstanceDetail(
  makeAgentDetailPayload({
    instanceId: duplicateIdentity.instanceId,
    templateId: duplicateIdentity.templateId,
    departmentId: duplicateIdentity.departmentId,
    functionId: duplicateIdentity.functionId,
    sourceOrdinal: 2,
    sharedTemplateDeploymentCount: 2,
    runtime: "static",
  }),
  duplicateIdentity,
  AGENT_DETAIL_ETAG,
);
assert.equal(duplicate.instance.sourceOrdinal, 2);
assert.equal(duplicate.sharedTemplateDeploymentCount, 2);

const priorFetch = globalThis.fetch;
globalThis.fetch = async (path, request) => {
  assert.equal(path, `/api/v1/agent-instances/${identity.instanceId}`);
  assert.equal(
    new Headers(request?.headers).get("If-None-Match"),
    AGENT_DETAIL_ETAG,
  );
  return new Response(null, {
    status: 304,
    headers: { ETag: AGENT_DETAIL_ETAG },
  });
};
assert.equal(
  await fetchAgentInstanceDetail(identity, { previous: detail }),
  detail,
);
globalThis.fetch = priorFetch;

const schema = Object.freeze({
  projectionVersion: "instance-configuration-schema-v1",
  instanceId: identity.instanceId,
  templateId: identity.templateId,
  supportedTriggerTypes: Object.freeze(["manual", "webhook", "schedule"]),
  connectorFamilies: Object.freeze([
    Object.freeze({
      connectorFamily: "newsletter",
      bindingIds: Object.freeze(["mock.newsletter.default"]),
    }),
  ]),
  scheduleSupported: true,
  variantLabelMaxLength: 100,
  maxTriggerBindings: 16,
  maxConnectorBindings: 16,
});
const schedule = Object.freeze({
  cron: "0 9 * * 1",
  timezone: "America/Los_Angeles",
  misfirePolicy: "run_once",
  misfireGraceSeconds: 300,
});
const patch = serializeInstanceConfigurationPatch(
  {
    enabled: false,
    variantLabel: "West Coast",
    triggerBindings: [
      { type: "manual" },
      { type: "webhook", eventSource: "newsletter.created" },
      { type: "schedule", ...schedule },
    ],
    connectorBindings: {
      newsletter: {
        connectorFamily: "newsletter",
        bindingId: "mock.newsletter.default",
      },
    },
    schedule,
  },
  schema,
);
assert.deepEqual(Object.keys(patch), [
  "enabled",
  "variantLabel",
  "triggerBindings",
  "connectorBindings",
  "schedule",
]);
assert.throws(
  () =>
    serializeInstanceConfigurationPatch(
      {
        triggerBindings: [{ type: "schedule", ...schedule }],
        schedule: { ...schedule, timezone: "UTC" },
      },
      schema,
    ),
  /invalid/u,
);

const session = normalizeLocalSession({
  actorId: "local-operator",
  roles: ["local_admin", "viewer"],
  scopes: [],
  authMode: "local",
  environment: "local",
  modelMode: "mock",
  connectorMode: "mock",
  networkPermission: false,
  warning: "Local identity — not production authentication",
  csrfToken: "a".repeat(43),
  csrfHeaderName: "X-CSRF-Token",
});
assert.deepEqual(session.roles, ["local_admin", "viewer"]);
assert.equal(Object.hasOwn(session, "csrfToken"), false);

process.stdout.write(
  `WEB-03 dependency-free witness passed: Node ${process.versions.node}, complete static/dynamic detail, duplicate identity, conditional reuse, five-field configuration, schedule coherence, and session secrecy.\n`,
);

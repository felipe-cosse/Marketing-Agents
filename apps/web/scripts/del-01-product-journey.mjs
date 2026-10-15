// DEL-01 observes the production UI's real requests; it creates no fixture API,
// routes, direct API mutations or data. Success is returned only after reload.
const SCENARIO = "demo.social-media.content-draft.v1";
const TEMPLATE = "tpl.social-media.new-content.linkedin-post-drafter";
const INSTANCE = "inst.social-media.new-content.linkedin-post-drafter.01";
const SCHEMA = "schema.demo.social-media.content-draft.output.v1";
const RESOURCE_ID = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$/u;
const WAIT = Object.freeze({ timeout: 30_000 });
const CHECKS = Object.freeze([
  "del-01-live-social-submission",
  "del-01-completed-run-artifact-provenance",
  "del-01-artifact-reload-identity",
]);

class JourneyFailure extends Error {}

function requireThat(condition, code) {
  if (!condition) throw new JourneyFailure(`DEL-01 ${code}`);
}

function record(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function identifier(value) {
  return typeof value === "string" && RESOURCE_ID.test(value);
}

function empty(value) {
  return Array.isArray(value) && value.length === 0;
}

function originValue(value) {
  const url = new URL(value);
  requireThat(
    url.protocol === "http:" &&
      ["127.0.0.1", "localhost"].includes(url.hostname) &&
      url.pathname === "/" &&
      url.username + url.password + url.search + url.hash === "",
    "origin_invalid",
  );
  return url.origin;
}

function matches(response, path, method) {
  return (
    new URL(response.url()).pathname === path &&
    response.request().method() === method
  );
}

async function jsonResponse(response, origin, path, status) {
  requireThat(response.url() === `${origin}${path}`, "response_origin_or_path");
  const actualStatus = response.status();
  const safeStatus =
    Number.isInteger(actualStatus) && actualStatus >= 100 && actualStatus <= 599
      ? String(actualStatus)
      : "invalid";
  requireThat(actualStatus === status, `response_status_${safeStatus}`);
  requireThat(
    response.headers()["content-type"]?.includes("application/json"),
    "response_content_type",
  );
  const value = await response.json();
  requireThat(record(value), "response_object");
  return value;
}

function receiptIdentity(receipt) {
  requireThat(
    receipt.status === "accepted" &&
      receipt.disposition === "created" &&
      receipt.scenarioId === SCENARIO &&
      receipt.executionMode === "dry_run" &&
      identifier(receipt.runId) &&
      identifier(receipt.workId),
    "receipt_identity",
  );
  const runPath = `/api/v1/runs/${receipt.runId}`;
  requireThat(
    receipt.instanceUrl === `/api/v1/agent-instances/${INSTANCE}` &&
      receipt.runUrl === runPath &&
      receipt.timelineUrl === `${runPath}/timeline` &&
      receipt.artifactsUrl === `${runPath}/artifacts`,
    "receipt_resources",
  );
  return runPath;
}

function runIdentity(run, receipt) {
  requireThat(
    run.id === receipt.runId &&
      run.work_item_id === receipt.workId &&
      run.workflow_id === SCENARIO &&
      run.instance_id === INSTANCE &&
      run.mode === "dry_run" &&
      empty(run.external_actions) &&
      empty(run.pending_approvals),
    "run_identity_or_effects",
  );
  requireThat(
    ["received", "validated", "planned", "executing", "completed"].includes(
      run.state,
    ),
    "run_state",
  );
}

function completedArtifact(run, receipt) {
  requireThat(
    run.state === "completed" &&
      run.execution_control?.model_calls === 1 &&
      run.execution_control?.tool_calls === 0 &&
      /^(?:catalog-sha256-v1:)?[0-9a-f]{64}$/u.test(run.catalog_hash) &&
      Number.isSafeInteger(run.configuration_revision) &&
      run.configuration_revision >= 1 &&
      run.approval_required === false &&
      run.details_expired === false &&
      run.artifacts_truncated === false &&
      Array.isArray(run.artifact_summaries) &&
      run.artifact_summaries.length === 1,
    "completed_run_evidence",
  );
  const artifact = run.artifact_summaries[0];
  artifactIdentity(artifact, receipt);
  return artifact;
}

function artifactIdentity(artifact, receipt) {
  requireThat(
    record(artifact) &&
      identifier(artifact.id) &&
      identifier(artifact.step_id) &&
      artifact.run_id === receipt.runId &&
      artifact.work_item_id === receipt.workId &&
      artifact.workflow_id === SCENARIO &&
      artifact.workflow_version === "1" &&
      artifact.template_id === TEMPLATE &&
      artifact.instance_id === INSTANCE &&
      artifact.output_schema_id === SCHEMA &&
      artifact.output_schema_version === "v1" &&
      artifact.classification === "internal" &&
      artifact.artifact_url === `/api/v1/artifacts/${artifact.id}` &&
      artifact.run_url === receipt.runUrl,
    "artifact_identity",
  );
}

function artifactProvenance(artifact, summary, run, receipt) {
  artifactIdentity(artifact, receipt);
  requireThat(
    artifact.id === summary.id &&
      artifact.step_id === summary.step_id &&
      artifact.catalog_hash === run.catalog_hash &&
      artifact.instance_config_revision === run.configuration_revision &&
      /^artifact-hmac-sha256-v1:[0-9a-f]{64}$/u.test(artifact.payload_digest) &&
      /^schema-sha256-v1:[0-9a-f]{64}$/u.test(artifact.output_schema_hash) &&
      empty(artifact.parent_artifact_ids) &&
      Array.isArray(artifact.sources) &&
      artifact.sources.some(
        (source) =>
          source?.kind === "work_input" &&
          source.source_id === receipt.workId &&
          source.classification === "internal",
      ) &&
      Array.isArray(artifact.providers) &&
      artifact.providers.length === 1 &&
      artifact.providers[0].provider_kind === "llm" &&
      artifact.providers[0].mode === "mock" &&
      artifact.providers[0].name === "mock" &&
      artifact.providers[0].version === "v1",
    "artifact_provenance",
  );
  requireThat(
    artifact.redacted_payload?.scenario_id === SCENARIO &&
      artifact.redacted_payload?.artifact_type === "social_post_draft" &&
      artifact.redacted_payload?.platform === "LinkedIn" &&
      artifact.redacted_payload?.publication_status === "not_published" &&
      empty(artifact.redacted_payload?.proposed_actions),
    "artifact_read_only",
  );
  // Compare only immutable identity/provenance on reload, never retain payload.
  return JSON.stringify([
    artifact.id,
    artifact.run_id,
    artifact.work_item_id,
    artifact.step_id,
    artifact.workflow_id,
    artifact.workflow_version,
    artifact.template_id,
    artifact.instance_id,
    artifact.output_schema_id,
    artifact.output_schema_version,
    artifact.classification,
    artifact.output_schema_hash,
    artifact.payload_digest,
    artifact.catalog_hash,
    artifact.instance_config_revision,
    artifact.sources,
    artifact.providers,
  ]);
}

export async function runDel01ProductJourney({ page, expect, origin }) {
  let stage = "origin_invalid";
  try {
    const localOrigin = originValue(origin);
    stage = "social_navigation";
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole("link", { name: "Demos", exact: true }).click();
    await expect(page).toHaveURL(`${localOrigin}/demos`, WAIT);
    await expect(page).toHaveTitle("Demos | Marketing Agents", WAIT);
    const form = page.getByRole("form", { name: "Social draft demo preset" });
    await expect(form).toBeVisible(WAIT);

    stage = "social_submission";
    const submitPath = `/api/v1/demo-scenarios/${SCENARIO}/runs`;
    const [accepted] = await Promise.all([
      page.waitForResponse(
        (response) => matches(response, submitPath, "POST"),
        WAIT,
      ),
      form.getByRole("button", { name: "Create draft", exact: true }).click(),
    ]);
    requireThat(
      accepted.headers()["cache-control"] === "no-store",
      "receipt_cache",
    );
    const receipt = await jsonResponse(accepted, localOrigin, submitPath, 202);
    const runPath = receiptIdentity(receipt);
    await expect(
      page.getByRole("heading", { name: "Draft run accepted" }),
    ).toBeVisible(WAIT);
    const runLink = page.getByRole("link", {
      name: "Open accepted run",
      exact: true,
    });
    await expect(runLink).toHaveAttribute(
      "href",
      `/runs/${receipt.runId}`,
      WAIT,
    );

    stage = "completed_run";
    let completed;
    await Promise.all([
      page.waitForResponse(async (response) => {
        if (!matches(response, runPath, "GET")) return false;
        const run = await jsonResponse(response, localOrigin, runPath, 200);
        runIdentity(run, receipt);
        if (run.state !== "completed") return false;
        completed = run;
        return true;
      }, WAIT),
      runLink.click(),
    ]);
    const summary = completedArtifact(completed, receipt);
    await expect(page).toHaveURL(`${localOrigin}/runs/${receipt.runId}`, WAIT);
    await expect(
      page.getByRole("heading", { name: "Run timeline", exact: true }),
    ).toBeVisible(WAIT);
    await expect(page.locator(".run-snapshot > header .run-status")).toHaveText(
      "Completed",
      WAIT,
    );
    const timeline = page.getByRole("list", {
      name: "Run timeline in sequence order",
    });
    const completedEvent = timeline.getByRole("listitem").filter({
      has: page.getByText("Executing → Completed", { exact: true }),
    });
    await expect(
      completedEvent.getByText("run.transitioned", { exact: true }),
    ).toBeVisible(WAIT);
    const artifactEvent = timeline.getByRole("listitem").filter({
      has: page.getByRole("link", {
        name: `Artifact ${summary.id}`,
        exact: true,
      }),
    });
    await expect(
      artifactEvent.getByText("artifact.persisted", { exact: true }),
    ).toBeVisible(WAIT);

    stage = "artifact_view";
    const artifactLink = page
      .locator(".run-artifacts")
      .getByRole("link", { name: summary.id, exact: true });
    await expect(artifactLink).toHaveAttribute(
      "href",
      `/artifacts/${summary.id}`,
      WAIT,
    );
    const [artifactResponse] = await Promise.all([
      page.waitForResponse(
        (response) => matches(response, summary.artifact_url, "GET"),
        WAIT,
      ),
      artifactLink.click(),
    ]);
    const artifact = await jsonResponse(
      artifactResponse,
      localOrigin,
      summary.artifact_url,
      200,
    );
    const originalIdentity = artifactProvenance(
      artifact,
      summary,
      completed,
      receipt,
    );
    await expect(page).toHaveURL(
      `${localOrigin}/artifacts/${summary.id}`,
      WAIT,
    );
    await expect(
      page.getByRole("heading", { name: "Artifact viewer", exact: true }),
    ).toBeVisible(WAIT);
    await expect(page.locator(".artifact-page__facts")).toContainText(
      TEMPLATE,
      WAIT,
    );
    await expect(page.locator(".artifact-page__facts")).toContainText(
      INSTANCE,
      WAIT,
    );
    await expect(page.locator(".artifact-page__facts")).toContainText(
      SCHEMA,
      WAIT,
    );
    await expect(page.locator(".artifact-page__provenance")).toContainText(
      "mock v1",
      WAIT,
    );

    stage = "artifact_reload";
    const [reloadedResponse] = await Promise.all([
      page.waitForResponse(
        (response) => matches(response, summary.artifact_url, "GET"),
        WAIT,
      ),
      page.reload(),
    ]);
    const reloaded = await jsonResponse(
      reloadedResponse,
      localOrigin,
      summary.artifact_url,
      200,
    );
    requireThat(
      artifactProvenance(reloaded, summary, completed, receipt) ===
        originalIdentity,
      "artifact_reload_identity",
    );
    await expect(page).toHaveURL(
      `${localOrigin}/artifacts/${summary.id}`,
      WAIT,
    );
    await expect(
      page.getByRole("heading", { name: "Artifact viewer", exact: true }),
    ).toBeVisible(WAIT);
    await expect(page.locator(".artifact-page__facts")).toContainText(
      summary.id,
      WAIT,
    );
    return [...CHECKS];
  } catch (error) {
    // Playwright errors can contain DOM/API content. Keep this extra journey's
    // diagnostics bounded; neither successful nor failed output carries it.
    if (error instanceof JourneyFailure) throw error;
  }
  throw new Error(`DEL-01 ${stage}`);
}

// WEB-03 browser evidence uses the production Vite preview and real hierarchy/detail APIs.
import {
  expect,
  test,
  type Page,
  type Request,
  type Route,
  type TestInfo,
} from "@playwright/test";

interface HierarchyInstance {
  readonly id: string;
  readonly templateId: string;
  readonly displayName: string;
  readonly sourceOrdinal: number;
}

interface HierarchyFunction {
  readonly instances: readonly HierarchyInstance[];
}

interface HierarchyDepartment {
  readonly functions: readonly HierarchyFunction[];
}

interface HierarchyBody {
  readonly departments: readonly HierarchyDepartment[];
}

interface BoundaryObservation {
  hierarchyRequestCount: number;
  readonly detailConditionalHeaders: (string | null)[];
  readonly externalRequests: string[];
  readonly patches: {
    readonly headers: Record<string, string>;
    readonly body: Record<string, unknown>;
  }[];
  hierarchyBody: HierarchyBody | null;
}

interface CachedDetail {
  body: Record<string, unknown>;
  etag: string;
}

const STATUS_WATERMARK = `instance-status-sha256-v1:${"d".repeat(64)}`;
const STATUS_ETAG = `"${STATUS_WATERMARK}"`;
const UPDATED_DETAIL_ETAG = `"${"e".repeat(64)}"`;
const CONFLICT_DETAIL_ETAG = `"${"1".repeat(64)}"`;
const CORRELATION_ID = `correlation.api.${"f".repeat(32)}`;

function allInstances(body: HierarchyBody): readonly HierarchyInstance[] {
  return body.departments.flatMap(({ functions }) =>
    functions.flatMap(({ instances }) => instances),
  );
}

function statusItem(
  instanceId: string,
  index: number,
): Record<string, unknown> {
  const completed = index === 0;
  return {
    instance_id: instanceId,
    status: completed ? "completed" : "never_run",
    latest_run_id: completed ? "run.web-03.latest" : null,
    latest_run_state: completed ? "completed" : null,
    latest_run_created_at: completed ? "2026-08-28T18:00:00Z" : null,
    latest_run_updated_at: completed ? "2026-08-28T18:02:00Z" : null,
    instance_url: `/api/v1/agent-instances/${encodeURIComponent(instanceId)}`,
    latest_run_url: completed ? "/api/v1/runs/run.web-03.latest" : null,
  };
}

function addDeterministicRuntime(
  body: Record<string, unknown>,
): Record<string, unknown> {
  const result = structuredClone(body);
  delete result.runtimeWatermark;
  delete result.runtimeStatus;
  delete result.recentRuns;
  const instance = result.instance as Record<string, unknown>;
  if (instance.sourceOrdinal === 2) return result;
  return {
    ...result,
    runtimeWatermark: STATUS_WATERMARK,
    runtimeStatus: {
      status: "completed",
      latestRunId: "run.web-03.latest",
      latestRunState: "completed",
      latestRunCreatedAt: "2026-08-28T18:00:00Z",
      latestRunUpdatedAt: "2026-08-28T18:02:00Z",
      latestRunUrl: "/api/v1/runs/run.web-03.latest",
    },
    recentRuns: [
      {
        id: "run.web-03.latest",
        state: "completed",
        workflowId: "workflow.web-03.latest",
        createdAt: "2026-08-28T18:00:00Z",
        updatedAt: "2026-08-28T18:02:00Z",
        runUrl: "/api/v1/runs/run.web-03.latest",
      },
    ],
  };
}

function nullSchema(): Record<string, unknown> {
  return { type: "null" };
}

function configurationSchema(
  instanceId: string,
  templateId: string,
): Record<string, unknown> {
  return {
    projectionVersion: "instance-configuration-schema-v1",
    instanceId,
    templateId,
    configurationSchema: {
      $schema: "https://json-schema.org/draft/2020-12/schema",
      $id: `urn:marketing-agents:instance-configuration:${instanceId}:v1`,
      type: "object",
      description:
        "Structural deployment PATCH schema. The API additionally enforces registered bindings, recurrence validity, and exact trigger/schedule value consistency.",
      additionalProperties: false,
      minProperties: 1,
      properties: {
        enabled: { type: "boolean" },
        variantLabel: {
          type: ["string", "null"],
          minLength: 1,
          maxLength: 100,
        },
        triggerBindings: {
          type: "array",
          maxItems: 16,
          items: {
            oneOf: [
              {
                type: "object",
                additionalProperties: false,
                required: ["type"],
                properties: {
                  type: { const: "manual" },
                  enabled: { type: "boolean", default: true },
                  eventSource: nullSchema(),
                  cron: nullSchema(),
                  timezone: nullSchema(),
                  misfirePolicy: nullSchema(),
                  misfireGraceSeconds: nullSchema(),
                },
              },
            ],
          },
        },
        connectorBindings: {
          type: "object",
          maxProperties: 16,
          properties: {},
          additionalProperties: false,
        },
        schedule: nullSchema(),
      },
    },
  };
}

function problem(
  status: number,
  code: string,
  optional: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    type: `urn:marketing-agents:problem:${code}`,
    title: status === 409 ? "Conflict" : "Request failed",
    status,
    detail: "The request could not be completed.",
    instance: `urn:marketing-agents:request:${CORRELATION_ID}`,
    code,
    correlation_id: CORRELATION_ID,
    ...optional,
  };
}

function jsonBody(route: Route): Record<string, unknown> {
  const value: unknown = route.request().postDataJSON();
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("expected a JSON object request body");
  }
  return value as Record<string, unknown>;
}

function decodedCapture(match: RegExpExecArray, label: string): string {
  const value = match[1];
  if (value === undefined) throw new Error(`${label} did not capture an ID`);
  return decodeURIComponent(value);
}

async function installBoundary(page: Page): Promise<{
  readonly observation: BoundaryObservation;
  readonly setConflict: (value: boolean) => void;
}> {
  const observation: BoundaryObservation = {
    hierarchyRequestCount: 0,
    detailConditionalHeaders: [],
    externalRequests: [],
    patches: [],
    hierarchyBody: null,
  };
  const detailCache = new Map<string, CachedDetail>();
  let conflict = false;
  let resolveHierarchy: ((body: HierarchyBody) => void) | undefined;
  const hierarchyReady = new Promise<HierarchyBody>((resolve) => {
    resolveHierarchy = resolve;
  });

  page.on("request", (request: Request) => {
    const url = new URL(request.url());
    if (!new Set(["127.0.0.1", "localhost"]).has(url.hostname)) {
      observation.externalRequests.push(request.url());
    }
    if (url.pathname === "/api/v1/catalog/hierarchy") {
      observation.hierarchyRequestCount += 1;
    }
  });
  page.on("response", async (response) => {
    if (new URL(response.url()).pathname !== "/api/v1/catalog/hierarchy") {
      return;
    }
    const body = (await response.json()) as HierarchyBody;
    observation.hierarchyBody = body;
    resolveHierarchy?.(body);
    resolveHierarchy = undefined;
  });

  await page.route("**/api/v1/agent-instances/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === "/api/v1/agent-instances/status-summary") {
      const hierarchy = await hierarchyReady;
      const conditional = request.headers()["if-none-match"];
      const headers = {
        "Cache-Control": "private, no-cache, max-age=0",
        ETag: STATUS_ETAG,
        Vary: "Authorization",
      };
      if (conditional === STATUS_ETAG) {
        await route.fulfill({ status: 304, headers });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers,
        body: JSON.stringify({
          scope: "single-local-installation",
          runtime_watermark: STATUS_WATERMARK,
          items: allInstances(hierarchy).map(({ id }, index) =>
            statusItem(id, index),
          ),
        }),
      });
      return;
    }

    const configurationMatch =
      /^\/api\/v1\/agent-instances\/([^/]+)\/configuration$/u.exec(path);
    if (configurationMatch !== null) {
      const instanceId = decodedCapture(
        configurationMatch,
        "configuration route",
      );
      const cached = detailCache.get(instanceId);
      if (cached === undefined) throw new Error("detail must load before edit");
      const patch = jsonBody(route);
      observation.patches.push({ headers: request.headers(), body: patch });
      if (conflict) {
        const instance = cached.body.instance as Record<string, unknown>;
        instance.variantLabel = "External revision";
        instance.configurationRevision = 3;
        instance.configurationEtag = '"instance-configuration-v1-3"';
        cached.etag = CONFLICT_DETAIL_ETAG;
        await route.fulfill({
          status: 409,
          contentType: "application/problem+json",
          body: JSON.stringify(
            problem(409, "configuration_revision_conflict", {
              current_resource_version: 3,
            }),
          ),
        });
        return;
      }
      const instance = cached.body.instance as Record<string, unknown>;
      for (const [field, value] of Object.entries(patch)) {
        instance[field] = value;
      }
      instance.configurationRevision = 2;
      instance.configurationEtag = '"instance-configuration-v1-2"';
      cached.etag = UPDATED_DETAIL_ETAG;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { ETag: '"instance-configuration-v1-2"' },
        body: JSON.stringify({
          projectionVersion: "instance-configuration-v1",
          configuration: {
            instanceId,
            enabled: instance.enabled,
            variantLabel: instance.variantLabel,
            triggerBindings: instance.triggerBindings,
            connectorBindings: instance.connectorBindings,
            schedule: instance.schedule,
            configurationRevision: 2,
          },
        }),
      });
      return;
    }

    const schemaMatch =
      /^\/api\/v1\/agent-instances\/([^/]+)\/configuration-schema$/u.exec(path);
    if (schemaMatch !== null) {
      const instanceId = decodedCapture(schemaMatch, "schema route");
      const cached = detailCache.get(instanceId);
      if (cached === undefined)
        throw new Error("detail must load before schema");
      const instance = cached.body.instance as Record<string, unknown>;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          configurationSchema(instanceId, String(instance.templateId)),
        ),
      });
      return;
    }

    const detailMatch = /^\/api\/v1\/agent-instances\/([^/]+)$/u.exec(path);
    if (detailMatch === null) {
      await route.fallback();
      return;
    }
    const instanceId = decodedCapture(detailMatch, "detail route");
    const conditional = request.headers()["if-none-match"] ?? null;
    observation.detailConditionalHeaders.push(conditional);
    const cached = detailCache.get(instanceId);
    if (cached !== undefined) {
      if (conditional === cached.etag) {
        await route.fulfill({
          status: 304,
          headers: { ETag: cached.etag, "Cache-Control": "private, no-cache" },
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { ETag: cached.etag, "Cache-Control": "private, no-cache" },
        body: JSON.stringify(cached.body),
      });
      return;
    }
    const response = await route.fetch();
    expect(response.status()).toBe(200);
    const body = addDeterministicRuntime(
      (await response.json()) as Record<string, unknown>,
    );
    const etag = response.headers().etag;
    if (etag === undefined) throw new Error("detail ETag is required");
    detailCache.set(instanceId, { body, etag });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { ETag: etag, "Cache-Control": "private, no-cache" },
      body: JSON.stringify(body),
    });
  });
  return {
    observation,
    setConflict(value: boolean) {
      conflict = value;
    },
  };
}

async function waitForCatalog(page: Page): Promise<void> {
  await expect(page.locator('[data-node-kind="instance"]')).toHaveCount(43);
}

async function screenshot(
  page: Page,
  testInfo: TestInfo,
  name: string,
): Promise<void> {
  await page.screenshot({ path: testInfo.outputPath(name), fullPage: true });
}

function requireHierarchy(observation: BoundaryObservation): HierarchyBody {
  if (observation.hierarchyBody === null) {
    throw new Error("hierarchy was not observed");
  }
  return observation.hierarchyBody;
}

test("WEB-03 selection shows complete dynamic and duplicate static detail with conditional reuse", async ({
  page,
}) => {
  const { observation } = await installBoundary(page);
  await page.goto("/");
  await waitForCatalog(page);
  const hierarchy = requireHierarchy(observation);
  const instances = allInstances(hierarchy);
  const first = instances[0];
  if (first === undefined) throw new Error("catalog has no instances");

  const firstCard = page.locator(`[data-instance-id="${first.id}"]`);
  await firstCard.click();
  const inspector = page.locator("#agent-inspector");
  await expect(inspector).toBeVisible();
  await expect(
    inspector.getByRole("heading", { name: "Overview" }),
  ).toBeVisible();
  await expect(
    inspector.getByRole("heading", { name: "Deployment & configuration" }),
  ).toBeVisible();
  await expect(
    inspector.getByRole("heading", { name: "Capabilities & policies" }),
  ).toBeVisible();
  await expect(
    inspector.getByRole("heading", { name: "Schemas" }),
  ).toBeVisible();
  await expect(
    inspector.getByRole("heading", { name: "Recent runs" }),
  ).toBeVisible();
  await expect(
    inspector.getByText("run.web-03.latest", { exact: true }),
  ).toBeVisible();
  await expect(firstCard).toHaveAttribute("aria-expanded", "true");

  await inspector.locator(".agent-inspector__close").click();
  await expect(inspector).toBeHidden();
  await expect(firstCard).toBeFocused();
  await firstCard.click();
  await expect(inspector).toBeVisible();
  expect(observation.detailConditionalHeaders.slice(0, 2)).toEqual([
    null,
    expect.stringMatching(/^"[a-f0-9]{64}"$/u),
  ]);

  const counts = new Map<string, number>();
  for (const instance of instances) {
    counts.set(instance.templateId, (counts.get(instance.templateId) ?? 0) + 1);
  }
  const duplicate = instances.find(
    ({ templateId, sourceOrdinal }) =>
      counts.get(templateId) === 2 && sourceOrdinal === 2,
  );
  if (duplicate === undefined)
    throw new Error("duplicate deployment is missing");
  await page.locator(`[data-instance-id="${duplicate.id}"]`).click();
  await expect(
    inspector.getByRole("heading", {
      name: `${duplicate.displayName} · Instance 2 of 2`,
    }),
  ).toBeVisible();
  await expect(
    inspector.getByText(duplicate.id, { exact: true }).first(),
  ).toBeVisible();
  await expect(
    inspector.getByText(
      "Recent run data is unavailable for this local runtime.",
    ),
  ).toBeVisible();
  await expect(inspector.getByText("Never run", { exact: true })).toHaveCount(
    0,
  );
  expect(observation.hierarchyRequestCount).toBe(1);
  expect(observation.externalRequests).toEqual([]);
});

test("WEB-03 inspector docks at 1536 and overlays without overflow at 1280", async ({
  page,
}, testInfo) => {
  const { observation } = await installBoundary(page);
  await page.goto("/");
  await waitForCatalog(page);
  const first = allInstances(requireHierarchy(observation))[0];
  if (first === undefined) throw new Error("catalog has no instances");

  await page.locator(`[data-instance-id="${first.id}"]`).click();
  const workspace = page.locator(".chart-workspace");
  const chart = page.locator(".chart-surface");
  const inspector = page.locator("#agent-inspector");
  const wideWorkspace = await workspace.boundingBox();
  const wideChart = await chart.boundingBox();
  const wideInspector = await inspector.boundingBox();
  expect(wideWorkspace).not.toBeNull();
  expect(wideChart).not.toBeNull();
  expect(wideInspector).not.toBeNull();
  if (wideWorkspace === null || wideChart === null || wideInspector === null)
    return;
  expect(wideInspector.width).toBeGreaterThanOrEqual(329);
  expect(wideInspector.width).toBeLessThanOrEqual(333);
  expect(wideInspector.x).toBeGreaterThanOrEqual(
    wideChart.x + wideChart.width - 1,
  );
  expect(wideInspector.x + wideInspector.width).toBeLessThanOrEqual(
    wideWorkspace.x + wideWorkspace.width + 1,
  );
  await screenshot(page, testInfo, "web-03-inspector-1536x1024.png");

  await inspector.locator(".agent-inspector__close").click();
  await page.setViewportSize({ width: 1280, height: 800 });
  const compactChartBefore = await chart.boundingBox();
  await page.locator(`[data-instance-id="${first.id}"]`).click();
  const compactChartAfter = await chart.boundingBox();
  const compactInspector = await inspector.boundingBox();
  expect(compactChartBefore).not.toBeNull();
  expect(compactChartAfter).not.toBeNull();
  expect(compactInspector).not.toBeNull();
  if (
    compactChartBefore === null ||
    compactChartAfter === null ||
    compactInspector === null
  ) {
    return;
  }
  expect(
    Math.abs(compactChartAfter.width - compactChartBefore.width),
  ).toBeLessThan(2);
  expect(compactInspector.width).toBeGreaterThanOrEqual(358);
  expect(compactInspector.width).toBeLessThanOrEqual(362);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await screenshot(page, testInfo, "web-03-inspector-1280x800.png");
});

test("WEB-03 configuration edit uses revision safety, dirty warnings, and explicit conflict reload", async ({
  page,
}) => {
  const { observation, setConflict } = await installBoundary(page);
  await page.goto("/");
  await waitForCatalog(page);
  const first = allInstances(requireHierarchy(observation))[0];
  if (first === undefined) throw new Error("catalog has no instances");

  await page.locator(`[data-instance-id="${first.id}"]`).click();
  const inspector = page.locator("#agent-inspector");
  await inspector.getByRole("button", { name: "Edit" }).click();
  const form = inspector.getByRole("form", {
    name: "Deployment configuration editor",
  });
  const variant = form.getByLabel("Variant label");
  await variant.fill("WEB-03 local override");

  await inspector.locator(".agent-inspector__close").click();
  const discardDialog = page.getByRole("alertdialog", {
    name: "Discard configuration changes?",
  });
  await expect(discardDialog).toBeVisible();
  await discardDialog.getByRole("button", { name: "Keep editing" }).click();
  await expect(variant).toHaveValue("WEB-03 local override");

  await form.getByRole("button", { name: "Save configuration" }).click();
  await expect(inspector.getByText("Configuration saved.")).toBeVisible();
  expect(observation.patches).toHaveLength(1);
  const successfulPatch = observation.patches[0];
  if (successfulPatch === undefined) throw new Error("PATCH was not observed");
  expect(successfulPatch.body).toEqual({
    variantLabel: "WEB-03 local override",
  });
  expect(successfulPatch.headers["content-type"]).toBe("application/json");
  expect(successfulPatch.headers["if-match"]).toBe(
    '"instance-configuration-v1-1"',
  );
  expect(successfulPatch.headers["x-csrf-token"]).toMatch(
    /^[A-Za-z0-9_-]{32,128}$/u,
  );

  await inspector.getByRole("button", { name: "Edit" }).click();
  const conflictForm = inspector.getByRole("form", {
    name: "Deployment configuration editor",
  });
  const conflictVariant = conflictForm.getByLabel("Variant label");
  await conflictVariant.fill("Draft kept through conflict");
  setConflict(true);
  await conflictForm
    .getByRole("button", { name: "Save configuration" })
    .click();

  const conflictAlert = conflictForm.getByRole("alert");
  await expect(conflictAlert).toContainText("Current server revision: 3");
  await expect(conflictVariant).toHaveValue("Draft kept through conflict");
  expect(observation.patches).toHaveLength(2);
  expect(observation.patches[1]?.headers["if-match"]).toBe(
    '"instance-configuration-v1-2"',
  );
  await conflictAlert
    .getByRole("button", { name: "Reload current configuration" })
    .click();
  await expect(
    inspector.getByText("Configuration reloaded from the server."),
  ).toBeVisible();
  await expect(
    inspector.getByText("External revision", { exact: true }),
  ).toBeVisible();
  expect(observation.patches).toHaveLength(2);
  expect(observation.externalRequests).toEqual([]);
});

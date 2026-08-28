import { afterEach, describe, expect, it, vi } from "vitest";

import {
  clearLocalSession,
  fetchInstanceConfigurationSchema,
  fetchLocalSession,
  InstanceConfigurationRequestError,
  normalizeInstanceConfigurationSchema,
  normalizeLocalSession,
  serializeInstanceConfigurationPatch,
  updateInstanceConfiguration,
  type InstanceConfigurationPatch,
} from "./instanceConfiguration";

const INSTANCE_ID = "inst.email.newsletter.newsletter-subscriber.01";
const TEMPLATE_ID = "tpl.email.newsletter.newsletter-subscriber";
const CONFIGURATION_ETAG = '"instance-configuration-v1-1"';
const SESSION_TOKEN = "a".repeat(43);
const REFRESHED_SESSION_TOKEN = "b".repeat(43);
const CORRELATION_ID = `correlation.api.${"c".repeat(32)}`;

function sessionBody(csrfToken = SESSION_TOKEN): Record<string, unknown> {
  return {
    actorId: "local-operator",
    roles: ["approver", "local_admin", "operator", "viewer"],
    scopes: ["approvals:decide", "approvals:read"],
    authMode: "local",
    environment: "local",
    modelMode: "mock",
    connectorMode: "mock",
    networkPermission: false,
    warning: "Local identity — not production authentication",
    csrfToken,
    csrfHeaderName: "X-CSRF-Token",
  };
}

function nullSchema(): Record<string, unknown> {
  return { type: "null" };
}

function triggerProperties(
  type: "manual" | "webhook" | "schedule",
): Record<string, unknown> {
  if (type === "manual") {
    return {
      type: { const: "manual" },
      enabled: { type: "boolean", default: true },
      eventSource: nullSchema(),
      cron: nullSchema(),
      timezone: nullSchema(),
      misfirePolicy: nullSchema(),
      misfireGraceSeconds: nullSchema(),
    };
  }
  if (type === "webhook") {
    return {
      type: { const: "webhook" },
      enabled: { type: "boolean", default: true },
      eventSource: {
        type: "string",
        minLength: 1,
        maxLength: 100,
        pattern: "^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$",
      },
      cron: nullSchema(),
      timezone: nullSchema(),
      misfirePolicy: nullSchema(),
      misfireGraceSeconds: nullSchema(),
    };
  }
  return {
    type: { const: "schedule" },
    enabled: { const: true, default: true },
    eventSource: nullSchema(),
    cron: { type: "string", minLength: 1, maxLength: 100 },
    timezone: { type: "string", minLength: 1, maxLength: 100 },
    misfirePolicy: { enum: ["skip", "run_once"] },
    misfireGraceSeconds: { type: "integer", minimum: 0, maximum: 86_400 },
  };
}

function objectTriggerVariant(
  required: readonly string[],
  properties: Record<string, unknown>,
): Record<string, unknown> {
  return {
    type: "object",
    additionalProperties: false,
    required: [...required],
    properties,
  };
}

function enabledScheduleSchema(): Record<string, unknown> {
  return {
    type: "object",
    additionalProperties: false,
    required: ["cron", "timezone", "misfirePolicy", "misfireGraceSeconds"],
    properties: {
      cron: { type: "string", minLength: 1, maxLength: 100 },
      timezone: { type: "string", minLength: 1, maxLength: 100 },
      misfirePolicy: { enum: ["skip", "run_once"] },
      misfireGraceSeconds: { type: "integer", minimum: 0, maximum: 86_400 },
    },
  };
}

function schemaBody(): Record<string, unknown> {
  return {
    projectionVersion: "instance-configuration-schema-v1",
    instanceId: INSTANCE_ID,
    templateId: TEMPLATE_ID,
    configurationSchema: {
      $schema: "https://json-schema.org/draft/2020-12/schema",
      $id: `urn:marketing-agents:instance-configuration:${INSTANCE_ID}:v1`,
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
              objectTriggerVariant(["type"], triggerProperties("manual")),
              objectTriggerVariant(
                ["type", "eventSource"],
                triggerProperties("webhook"),
              ),
              objectTriggerVariant(["type", "enabled"], {
                type: { const: "schedule" },
                enabled: { const: false },
                eventSource: nullSchema(),
                cron: nullSchema(),
                timezone: nullSchema(),
                misfirePolicy: nullSchema(),
                misfireGraceSeconds: nullSchema(),
              }),
              objectTriggerVariant(
                [
                  "type",
                  "cron",
                  "timezone",
                  "misfirePolicy",
                  "misfireGraceSeconds",
                ],
                triggerProperties("schedule"),
              ),
            ],
          },
        },
        connectorBindings: {
          type: "object",
          maxProperties: 16,
          properties: {
            newsletter: {
              type: "object",
              additionalProperties: false,
              required: ["connectorFamily", "bindingId"],
              properties: {
                connectorFamily: { const: "newsletter" },
                bindingId: { enum: ["mock.newsletter.default"] },
                enabled: { type: "boolean", default: true },
              },
            },
          },
          additionalProperties: false,
        },
        schedule: {
          oneOf: [nullSchema(), enabledScheduleSchema()],
        },
      },
    },
  };
}

function successBody(revision = 1): Record<string, unknown> {
  return {
    projectionVersion: "instance-configuration-v1",
    configuration: {
      instanceId: INSTANCE_ID,
      enabled: true,
      variantLabel: null,
      triggerBindings: [
        {
          type: "manual",
          enabled: true,
          eventSource: null,
          cron: null,
          timezone: null,
          misfirePolicy: null,
          misfireGraceSeconds: null,
        },
      ],
      connectorBindings: {},
      schedule: null,
      configurationRevision: revision,
    },
  };
}

function problemBody(
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

function jsonResponse(
  value: Record<string, unknown>,
  options: { readonly status?: number; readonly etag?: string } = {},
): Response {
  const headers = new Headers({ "Content-Type": "application/json" });
  if (options.etag !== undefined) headers.set("ETag", options.etag);
  return new Response(JSON.stringify(value), {
    status: options.status ?? 200,
    headers,
  });
}

function problemResponse(
  status: number,
  code: string,
  optional: Record<string, unknown> = {},
): Response {
  return new Response(JSON.stringify(problemBody(status, code, optional)), {
    status,
    headers: { "Content-Type": "application/problem+json; charset=utf-8" },
  });
}

afterEach(() => {
  clearLocalSession();
  vi.unstubAllGlobals();
});

describe("local session contract", () => {
  it("normalizes the exact local session without exposing its CSRF token", () => {
    const session = normalizeLocalSession(sessionBody());

    expect(session).toEqual({
      actorId: "local-operator",
      roles: ["approver", "local_admin", "operator", "viewer"],
      scopes: ["approvals:decide", "approvals:read"],
      authMode: "local",
      environment: "local",
      modelMode: "mock",
      connectorMode: "mock",
      networkPermission: false,
      warning: "Local identity — not production authentication",
    });
    expect(Object.hasOwn(session, "csrfToken")).toBe(false);
    expect(JSON.stringify(session)).not.toContain(SESSION_TOKEN);
    expect(() =>
      normalizeLocalSession({ ...sessionBody(), unexpected: true }),
    ).toThrow();
    expect(() =>
      normalizeLocalSession({
        ...sessionBody(),
        roles: ["viewer", "local_admin"],
      }),
    ).toThrow();
  });

  it("fetches the session no-store and forwards the abort signal", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(sessionBody()));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    const session = await fetchLocalSession(controller.signal);

    expect(session.actorId).toBe("local-operator");
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/session", {
      method: "GET",
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
  });
});

describe("configuration schema contract", () => {
  it("strictly derives editor options bound to the expected instance and template", () => {
    const schema = normalizeInstanceConfigurationSchema(schemaBody(), {
      instanceId: INSTANCE_ID,
      templateId: TEMPLATE_ID,
    });

    expect(schema).toEqual({
      projectionVersion: "instance-configuration-schema-v1",
      instanceId: INSTANCE_ID,
      templateId: TEMPLATE_ID,
      supportedTriggerTypes: ["manual", "webhook", "schedule"],
      connectorFamilies: [
        {
          connectorFamily: "newsletter",
          bindingIds: ["mock.newsletter.default"],
        },
      ],
      scheduleSupported: true,
      variantLabelMaxLength: 100,
      maxTriggerBindings: 16,
      maxConnectorBindings: 16,
    });
    expect(Object.isFrozen(schema)).toBe(true);

    expect(() =>
      normalizeInstanceConfigurationSchema(
        { ...schemaBody(), templateId: "tpl.email.newsletter.other" },
        { instanceId: INSTANCE_ID, templateId: TEMPLATE_ID },
      ),
    ).toThrow();
    expect(() =>
      normalizeInstanceConfigurationSchema(
        { ...schemaBody(), unexpected: true },
        { instanceId: INSTANCE_ID, templateId: TEMPLATE_ID },
      ),
    ).toThrow();
  });

  it("rejects a schema whose schedule field disagrees with trigger support", () => {
    const body = schemaBody();
    const configurationSchema = body.configurationSchema as Record<
      string,
      unknown
    >;
    const properties = configurationSchema.properties as Record<
      string,
      unknown
    >;
    properties.schedule = { type: "null" };

    expect(() =>
      normalizeInstanceConfigurationSchema(body, {
        instanceId: INSTANCE_ID,
        templateId: TEMPLATE_ID,
      }),
    ).toThrow();
  });

  it("fetches and cross-binds the schema with the exact route", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(schemaBody()));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await expect(
      fetchInstanceConfigurationSchema(
        { instanceId: INSTANCE_ID, templateId: TEMPLATE_ID },
        controller.signal,
      ),
    ).resolves.toMatchObject({
      instanceId: INSTANCE_ID,
      templateId: TEMPLATE_ID,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/agent-instances/${INSTANCE_ID}/configuration-schema`,
      {
        method: "GET",
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      },
    );
  });
});

describe("configuration PATCH serializer", () => {
  it("serializes all five fields and validates schema-bound options", () => {
    const schema = normalizeInstanceConfigurationSchema(schemaBody(), {
      instanceId: INSTANCE_ID,
      templateId: TEMPLATE_ID,
    });
    const schedule = {
      cron: "0 9 * * 1",
      timezone: "America/Los_Angeles",
      misfirePolicy: "run_once" as const,
      misfireGraceSeconds: 300,
    };

    expect(
      serializeInstanceConfigurationPatch(
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
      ),
    ).toEqual({
      enabled: false,
      variantLabel: "West Coast",
      triggerBindings: [
        { type: "manual", enabled: true },
        {
          type: "webhook",
          enabled: true,
          eventSource: "newsletter.created",
        },
        { type: "schedule", enabled: true, ...schedule },
      ],
      connectorBindings: {
        newsletter: {
          connectorFamily: "newsletter",
          bindingId: "mock.newsletter.default",
          enabled: true,
        },
      },
      schedule,
    });
  });

  it.each([
    null,
    {},
    { enabled: null },
    { unknown: true },
    { triggerBindings: [{ type: "manual" }, { type: "manual" }] },
    {
      triggerBindings: [
        {
          type: "schedule",
          cron: "0 9 * * 1",
          timezone: "UTC",
          misfirePolicy: "skip",
          misfireGraceSeconds: 0,
        },
      ],
      schedule: null,
    },
  ])("rejects an invalid partial patch %#", (patch) => {
    expect(() =>
      serializeInstanceConfigurationPatch(
        patch as unknown as InstanceConfigurationPatch,
      ),
    ).toThrow(
      expect.objectContaining({
        code: "invalid_configuration_patch",
        status: 0,
      }),
    );
  });

  it("rejects connector choices that are absent from the normalized schema", () => {
    const schema = normalizeInstanceConfigurationSchema(schemaBody(), {
      instanceId: INSTANCE_ID,
      templateId: TEMPLATE_ID,
    });

    expect(() =>
      serializeInstanceConfigurationPatch(
        {
          connectorBindings: {
            newsletter: {
              connectorFamily: "newsletter",
              bindingId: "unregistered",
            },
          },
        },
        schema,
      ),
    ).toThrow(InstanceConfigurationRequestError);
  });
});

describe("WEB-03 configuration PATCH transport", () => {
  it("uses the exact configuration ETag and private CSRF header", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(
        jsonResponse(successBody(2), {
          etag: '"instance-configuration-v1-2"',
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    const result = await updateInstanceConfiguration({
      instanceId: INSTANCE_ID,
      configurationEtag: CONFIGURATION_ETAG,
      patch: { enabled: true },
      signal: controller.signal,
    });

    expect(result.configurationEtag).toBe('"instance-configuration-v1-2"');
    expect(JSON.stringify(result)).not.toContain(SESSION_TOKEN);
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `/api/v1/agent-instances/${INSTANCE_ID}/configuration`,
      {
        method: "PATCH",
        cache: "no-store",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "If-Match": CONFIGURATION_ETAG,
          "X-CSRF-Token": SESSION_TOKEN,
        },
        signal: controller.signal,
        body: JSON.stringify({ enabled: true }),
      },
    );
  });

  it("refreshes the session and retries exactly once for csrf_token_invalid", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(problemResponse(403, "csrf_token_invalid"))
      .mockResolvedValueOnce(jsonResponse(sessionBody(REFRESHED_SESSION_TOKEN)))
      .mockResolvedValueOnce(
        jsonResponse(successBody(1), { etag: CONFIGURATION_ETAG }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      updateInstanceConfiguration({
        instanceId: INSTANCE_ID,
        configurationEtag: CONFIGURATION_ETAG,
        patch: { variantLabel: null },
      }),
    ).resolves.toMatchObject({ configurationEtag: CONFIGURATION_ETAG });
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(fetchMock.mock.calls[1]?.[1]?.headers).toMatchObject({
      "X-CSRF-Token": SESSION_TOKEN,
    });
    expect(fetchMock.mock.calls[3]?.[1]?.headers).toMatchObject({
      "X-CSRF-Token": REFRESHED_SESSION_TOKEN,
    });
  });

  it("does not retry another authorization problem", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(problemResponse(403, "request_forbidden"));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      updateInstanceConfiguration({
        instanceId: INSTANCE_ID,
        configurationEtag: CONFIGURATION_ETAG,
        patch: { enabled: false },
      }),
    ).rejects.toMatchObject({ status: 403, code: "request_forbidden" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("stops after one failed CSRF refresh retry", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(problemResponse(403, "csrf_token_invalid"))
      .mockResolvedValueOnce(jsonResponse(sessionBody(REFRESHED_SESSION_TOKEN)))
      .mockResolvedValueOnce(problemResponse(403, "csrf_token_invalid"));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      updateInstanceConfiguration({
        instanceId: INSTANCE_ID,
        configurationEtag: CONFIGURATION_ETAG,
        patch: { enabled: false },
      }),
    ).rejects.toMatchObject({ status: 403, code: "csrf_token_invalid" });
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("exposes only safe 409 and field-validation metadata", async () => {
    const fieldErrors = [
      {
        pointer: "/body",
        code: "value_error",
        message: "Field is invalid.",
      },
    ];
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(sessionBody()))
      .mockResolvedValueOnce(
        problemResponse(409, "configuration_revision_conflict", {
          current_resource_version: 7,
          field_errors: fieldErrors,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const request = updateInstanceConfiguration({
      instanceId: INSTANCE_ID,
      configurationEtag: CONFIGURATION_ETAG,
      patch: { enabled: false },
    });

    await expect(request).rejects.toMatchObject({
      status: 409,
      code: "configuration_revision_conflict",
      currentResourceVersion: 7,
      fieldErrors,
    });
    await expect(request).rejects.not.toHaveProperty("correlationId");
  });

  it.each([
    [null, '"instance-configuration-v1-2"'],
    ['"instance-configuration-v1-3"', '"instance-configuration-v1-2"'],
  ])(
    "rejects a success whose ETag is missing or disagrees with its body %#",
    async (etag, bodyEtag) => {
      const response = jsonResponse(successBody(2), {
        ...(etag === null ? {} : { etag }),
      });
      const fetchMock = vi
        .fn<typeof fetch>()
        .mockResolvedValueOnce(jsonResponse(sessionBody()))
        .mockResolvedValueOnce(response);
      vi.stubGlobal("fetch", fetchMock);

      await expect(
        updateInstanceConfiguration({
          instanceId: INSTANCE_ID,
          configurationEtag: CONFIGURATION_ETAG,
          patch: { enabled: false },
        }),
      ).rejects.toMatchObject({
        status: 200,
        code: "invalid_configuration_response",
      });
      expect(bodyEtag).toBe('"instance-configuration-v1-2"');
    },
  );

  it("rejects malformed validators before making a request", async () => {
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      updateInstanceConfiguration({
        instanceId: INSTANCE_ID,
        configurationEtag: 'W/"instance-configuration-v1-1"',
        patch: { enabled: false },
      }),
    ).rejects.toMatchObject({
      status: 0,
      code: "invalid_configuration_etag",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("preserves AbortError and forwards no replacement error", async () => {
    const abortError = new DOMException("aborted", "AbortError");
    const fetchMock = vi.fn<typeof fetch>().mockRejectedValue(abortError);
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchLocalSession()).rejects.toBe(abortError);
  });
});

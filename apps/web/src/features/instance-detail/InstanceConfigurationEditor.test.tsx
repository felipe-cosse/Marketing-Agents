// OBJ-03 covers restricted configuration loading and explicit dry-run-only schedule input.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  AGENT_DETAIL_ETAG,
  makeAgentDetailPayload,
} from "../../test/agentDetailFixture";
import {
  normalizeAgentInstanceDetail,
  type AgentInstanceDetail,
  type AgentInstanceDetailIdentity,
} from "../../api/agentInstanceDetail";
import {
  fetchInstanceConfiguration,
  fetchInstanceConfigurationSchema,
  fetchLocalSession,
  InstanceConfigurationRequestError,
  updateInstanceConfiguration,
  type InstanceConfigurationResult,
  type InstanceConfigurationSchema,
  type LocalSession,
} from "../../api/instanceConfiguration";
import type * as InstanceConfigurationApi from "../../api/instanceConfiguration";
import { InstanceConfigurationEditor } from "./InstanceConfigurationEditor";

vi.mock("../../api/instanceConfiguration", async () => {
  const actual = await vi.importActual<typeof InstanceConfigurationApi>(
    "../../api/instanceConfiguration",
  );
  return {
    ...actual,
    fetchLocalSession: vi.fn(),
    fetchInstanceConfiguration: vi.fn(),
    fetchInstanceConfigurationSchema: vi.fn(),
    updateInstanceConfiguration: vi.fn(),
  };
});

const INSTANCE_ID = "inst.email.newsletter.newsletter-subscriber.01";
const TEMPLATE_ID = "tpl.email.newsletter.newsletter-subscriber";
const IDENTITY: AgentInstanceDetailIdentity = {
  instanceId: INSTANCE_ID,
  templateId: TEMPLATE_ID,
  departmentId: "dept.email",
  functionId: "func.email.newsletter",
  sourceOrdinal: 1,
  sharedTemplateDeploymentCount: 1,
  catalogVersion: "1.0.0",
  catalogHash: `catalog-sha256-v1:${"a".repeat(64)}`,
};

const ADMIN_SESSION: LocalSession = {
  actorId: "principal.local.admin",
  roles: ["local_admin", "viewer"],
  scopes: [],
  authMode: "local",
  environment: "local",
  modelMode: "mock",
  connectorMode: "mock",
  networkPermission: false,
  warning: "Local identity — not production authentication",
};

const VIEWER_SESSION: LocalSession = {
  ...ADMIN_SESSION,
  actorId: "principal.local.viewer",
  roles: ["viewer"],
};

const SCHEMA: InstanceConfigurationSchema = {
  projectionVersion: "instance-configuration-schema-v1",
  instanceId: INSTANCE_ID,
  templateId: TEMPLATE_ID,
  supportedTriggerTypes: ["manual", "webhook", "schedule"],
  connectorFamilies: [
    {
      connectorFamily: "local",
      bindingIds: ["local-catalog", "local-mail"],
    },
  ],
  scheduleSupported: true,
  variantLabelMaxLength: 100,
  maxTriggerBindings: 16,
  maxConnectorBindings: 16,
};

const RESULT = {} as InstanceConfigurationResult;

function makeDetail(): AgentInstanceDetail {
  const detail = normalizeAgentInstanceDetail(
    makeAgentDetailPayload({
      instanceId: INSTANCE_ID,
      templateId: TEMPLATE_ID,
      departmentId: IDENTITY.departmentId,
      functionId: IDENTITY.functionId,
      runtime: "completed",
    }),
    IDENTITY,
    AGENT_DETAIL_ETAG,
  );
  return {
    ...detail,
    inputSchema: {
      $schema: "https://json-schema.org/draft/2020-12/schema",
      $id: detail.template.inputSchemaId,
      type: "object",
      additionalProperties: false,
      required: ["request_id", "source_content"],
      properties: {
        request_id: {
          type: "string",
          title: "Request ID",
          minLength: 1,
          maxLength: 80,
        },
        source_content: {
          type: "string",
          title: "Source content",
          minLength: 1,
          maxLength: 12000,
          "x-sensitive": true,
        },
      },
    },
  };
}

interface RenderOptions {
  readonly detail?: AgentInstanceDetail;
  readonly onDirtyChange?: (dirty: boolean) => void;
  readonly onSaved?: () => Promise<void>;
  readonly onReload?: () => Promise<void>;
  readonly client?: QueryClient;
}

function Providers({
  children,
  client: providedClient,
}: {
  readonly children: ReactNode;
  readonly client?: QueryClient;
}): React.JSX.Element {
  const client =
    providedClient ??
    new QueryClient({
      defaultOptions: {
        queries: { retry: false, gcTime: Number.POSITIVE_INFINITY },
      },
    });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function renderEditor(options: RenderOptions = {}): ReturnType<typeof render> {
  return render(
    <Providers
      {...(options.client === undefined ? {} : { client: options.client })}
    >
      <InstanceConfigurationEditor
        detail={options.detail ?? makeDetail()}
        onDirtyChange={options.onDirtyChange ?? vi.fn()}
        onSaved={options.onSaved ?? vi.fn().mockResolvedValue(undefined)}
        onReload={options.onReload ?? vi.fn().mockResolvedValue(undefined)}
      />
    </Providers>,
  );
}

const fetchSessionMock = vi.mocked(fetchLocalSession);
const fetchSchemaMock = vi.mocked(fetchInstanceConfigurationSchema);
const fetchConfigurationMock = vi.mocked(fetchInstanceConfiguration);
const updateConfigurationMock = vi.mocked(updateInstanceConfiguration);

async function openEditor(
  user: ReturnType<typeof userEvent.setup>,
): Promise<HTMLFormElement> {
  await user.click(await screen.findByRole("button", { name: "Edit" }));
  return await screen.findByRole("form", {
    name: "Deployment configuration editor",
  });
}

describe("WEB-03 InstanceConfigurationEditor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchSessionMock.mockResolvedValue(ADMIN_SESSION);
    fetchSchemaMock.mockResolvedValue(SCHEMA);
    const detail = makeDetail();
    fetchConfigurationMock.mockResolvedValue({
      projectionVersion: "instance-configuration-v1",
      configuration: {
        ...detail.instance,
        instanceId: INSTANCE_ID,
        scheduledInput: null,
      },
      configurationEtag: detail.instance.configurationEtag,
    });
    updateConfigurationMock.mockResolvedValue(RESULT);
  });

  it("keeps viewer sessions read-only and never requests the edit schema", async () => {
    fetchSessionMock.mockResolvedValue(VIEWER_SESSION);
    renderEditor();

    expect(
      await screen.findByText(/does not include the local_admin role/u),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Edit" }),
    ).not.toBeInTheDocument();
    expect(fetchSessionMock).toHaveBeenCalledOnce();
    expect(fetchSchemaMock).not.toHaveBeenCalled();
    expect(fetchConfigurationMock).not.toHaveBeenCalled();
  });

  it("loads the edit schema only after a local admin explicitly chooses Edit", async () => {
    const user = userEvent.setup();
    renderEditor();

    const edit = await screen.findByRole("button", { name: "Edit" });
    expect(fetchSchemaMock).not.toHaveBeenCalled();
    expect(fetchConfigurationMock).not.toHaveBeenCalled();
    await user.click(edit);

    expect(
      await screen.findByRole("form", {
        name: "Deployment configuration editor",
      }),
    ).toBeVisible();
    expect(fetchSchemaMock).toHaveBeenCalledWith(
      { instanceId: INSTANCE_ID, templateId: TEMPLATE_ID },
      expect.any(AbortSignal),
    );
    expect(fetchConfigurationMock).toHaveBeenCalledWith(
      INSTANCE_ID,
      expect.any(AbortSignal),
    );
  });

  it("sends only changed top-level fields with the configuration ETag and announces success", async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn().mockResolvedValue(undefined);
    const onDirtyChange = vi.fn();
    renderEditor({ onSaved, onDirtyChange });
    const form = await openEditor(user);

    await user.click(
      within(form).getByRole("checkbox", { name: "Deployment enabled" }),
    );
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true));
    await user.click(
      within(form).getByRole("button", { name: "Save configuration" }),
    );

    await waitFor(() => {
      expect(updateConfigurationMock).toHaveBeenCalledWith({
        instanceId: INSTANCE_ID,
        configurationEtag: '"instance-configuration-v1-1"',
        patch: { enabled: false },
      });
    });
    expect(onSaved).toHaveBeenCalledOnce();
    expect(await screen.findByText("Configuration saved.")).toHaveAttribute(
      "role",
      "status",
    );
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
  });

  it("keeps an enabled schedule identical to its single schedule trigger", async () => {
    const user = userEvent.setup();
    renderEditor();
    const form = await openEditor(user);

    await user.click(
      within(form).getByRole("checkbox", {
        name: "Configure schedule trigger",
      }),
    );
    await user.click(
      within(form).getByRole("checkbox", { name: "Enable schedule trigger" }),
    );
    await user.type(
      within(form).getByLabelText("Cron expression"),
      "0 9 * * 1",
    );
    expect(
      within(form).getByRole("button", { name: "Save configuration" }),
    ).toBeDisabled();
    expect(within(form).getByText(/Saved input is required/u)).toBeVisible();
    expect(
      within(form).getByText(/Saving stores this input locally/u),
    ).toBeVisible();
    expect(
      within(form).queryByText("Sensitive value. Kept only in this open form."),
    ).not.toBeInTheDocument();
    await user.type(
      within(form).getByRole("textbox", { name: /Request ID/u }),
      "request.scheduled.explicit",
    );
    await user.type(
      within(form).getByRole("textbox", { name: /Source content/u }),
      "Operator-supplied business content",
    );
    await user.click(
      within(form).getByRole("button", { name: "Save configuration" }),
    );

    await waitFor(() => expect(updateConfigurationMock).toHaveBeenCalledOnce());
    const request = updateConfigurationMock.mock.calls[0]?.[0];
    expect(request?.patch).toEqual({
      scheduledInput: {
        input: {
          request_id: "request.scheduled.explicit",
          source_content: "Operator-supplied business content",
        },
        executionMode: "dry_run",
      },
      triggerBindings: [
        { type: "manual", enabled: true },
        {
          type: "schedule",
          enabled: true,
          cron: "0 9 * * 1",
          timezone: "UTC",
          misfirePolicy: "run_once",
          misfireGraceSeconds: 300,
        },
      ],
      schedule: {
        cron: "0 9 * * 1",
        timezone: "UTC",
        misfirePolicy: "run_once",
        misfireGraceSeconds: 300,
      },
    });
    const triggerTypes = request?.patch.triggerBindings?.map(
      (trigger) => trigger.type,
    );
    expect(new Set(triggerTypes).size).toBe(triggerTypes?.length);
  });

  it("offers only registered connector binding IDs", async () => {
    const user = userEvent.setup();
    renderEditor();
    const form = await openEditor(user);
    const binding = within(form).getByRole("combobox", {
      name: "local registered binding",
    });

    expect(
      within(binding)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["local-catalog", "local-mail"]);
    await user.selectOptions(binding, "local-mail");
    await user.click(
      within(form).getByRole("button", { name: "Save configuration" }),
    );

    await waitFor(() => {
      expect(updateConfigurationMock).toHaveBeenCalledWith({
        instanceId: INSTANCE_ID,
        configurationEtag: '"instance-configuration-v1-1"',
        patch: {
          connectorBindings: {
            local: {
              connectorFamily: "local",
              bindingId: "local-mail",
              enabled: true,
            },
          },
        },
      });
    });
  });

  it("uses a fresh restricted snapshot and its revision instead of a stale detail", async () => {
    const detail = makeDetail();
    const scheduledInput = {
      input: {
        request_id: "request.saved",
        source_content: "private-saved-canary",
      },
      executionMode: "dry_run" as const,
    };
    fetchConfigurationMock.mockResolvedValue({
      projectionVersion: "instance-configuration-v1",
      configurationEtag: '"instance-configuration-v1-9"',
      configuration: {
        ...detail.instance,
        instanceId: INSTANCE_ID,
        variantLabel: "Fresh server label",
        configurationRevision: 9,
        scheduledInput,
        triggerBindings: [
          ...detail.instance.triggerBindings,
          {
            type: "schedule",
            enabled: false,
            eventSource: null,
            cron: null,
            timezone: null,
            misfirePolicy: null,
            misfireGraceSeconds: null,
          },
        ],
      },
    });
    const user = userEvent.setup();
    renderEditor({ detail });
    expect(screen.queryByText("private-saved-canary")).not.toBeInTheDocument();
    const form = await openEditor(user);
    expect(
      within(form).getByRole("checkbox", { name: "Deployment enabled" }),
    ).toHaveFocus();
    expect(within(form).getByLabelText("Variant label")).toHaveValue(
      "Fresh server label",
    );
    expect(
      within(form).getByRole("textbox", { name: /Source content/u }),
    ).toHaveValue("private-saved-canary");
    expect(within(form).getByLabelText("Scheduled execution mode")).toHaveValue(
      "dry_run",
    );
    expect(
      within(form).queryByRole("option", { name: /Mock execution/u }),
    ).not.toBeInTheDocument();
    await user.clear(
      within(form).getByRole("textbox", { name: /Source content/u }),
    );
    await user.type(
      within(form).getByRole("textbox", { name: /Source content/u }),
      "Updated explicit content",
    );
    await user.click(
      within(form).getByRole("button", { name: "Save configuration" }),
    );
    await waitFor(() =>
      expect(updateConfigurationMock).toHaveBeenCalledWith({
        instanceId: INSTANCE_ID,
        configurationEtag: '"instance-configuration-v1-9"',
        patch: {
          scheduledInput: {
            ...scheduledInput,
            input: {
              request_id: "request.saved",
              source_content: "Updated explicit content",
            },
          },
        },
      }),
    );
  });

  it("discards restricted cache and unsaved input on close and fetches again on reopen", async () => {
    const client = new QueryClient();
    const user = userEvent.setup();
    const localStorageWrite = vi.spyOn(Storage.prototype, "setItem");
    renderEditor({ client });
    const form = await openEditor(user);
    await user.click(
      within(form).getByRole("checkbox", {
        name: "Configure schedule trigger",
      }),
    );
    await user.type(
      within(form).getByRole("textbox", { name: /Source content/u }),
      "Unsaved sensitive content",
    );
    await user.click(within(form).getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(
        client.getQueryData([
          "agent-instance",
          INSTANCE_ID,
          "restricted-configuration",
        ]),
      ).toBeUndefined(),
    );
    const reopened = await openEditor(user);
    expect(fetchConfigurationMock).toHaveBeenCalledTimes(2);
    await user.click(
      within(reopened).getByRole("checkbox", {
        name: "Configure schedule trigger",
      }),
    );
    expect(
      within(reopened).getByRole("textbox", { name: /Source content/u }),
    ).toHaveValue("");
    expect(localStorageWrite).not.toHaveBeenCalled();
    localStorageWrite.mockRestore();
  });

  it("allows explicitly clearing stored input with a disabled schedule", async () => {
    const detail = makeDetail();
    fetchConfigurationMock.mockResolvedValue({
      projectionVersion: "instance-configuration-v1",
      configurationEtag: detail.instance.configurationEtag,
      configuration: {
        ...detail.instance,
        instanceId: INSTANCE_ID,
        scheduledInput: {
          input: {
            request_id: "request.saved",
            source_content: "private-canary",
          },
          executionMode: "dry_run",
        },
        triggerBindings: [
          ...detail.instance.triggerBindings,
          {
            type: "schedule",
            enabled: false,
            eventSource: null,
            cron: null,
            timezone: null,
            misfirePolicy: null,
            misfireGraceSeconds: null,
          },
        ],
      },
    });
    const user = userEvent.setup();
    renderEditor();
    const form = await openEditor(user);
    await user.click(
      within(form).getByRole("button", { name: "Clear saved input" }),
    );
    expect(
      within(form).getByRole("textbox", { name: /Source content/u }),
    ).toHaveValue("");
    await user.click(
      within(form).getByRole("button", { name: "Save configuration" }),
    );
    await waitFor(() =>
      expect(updateConfigurationMock).toHaveBeenCalledWith(
        expect.objectContaining({ patch: { scheduledInput: null } }),
      ),
    );
  });

  it("does not render an editable fallback if the sensitive snapshot is denied", async () => {
    fetchConfigurationMock.mockRejectedValue(
      new InstanceConfigurationRequestError(
        403,
        "configuration_forbidden",
        "Administrator access required.",
      ),
    );
    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    expect(
      await screen.findByText("Saved configuration is unavailable"),
    ).toBeVisible();
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    expect(updateConfigurationMock).not.toHaveBeenCalled();
  });

  it("preserves a draft on conflict and reloads explicitly without resubmitting", async () => {
    const user = userEvent.setup();
    const onReload = vi.fn().mockResolvedValue(undefined);
    updateConfigurationMock.mockRejectedValueOnce(
      new InstanceConfigurationRequestError(
        409,
        "configuration_revision_conflict",
        "The configuration changed after this editor was opened.",
        { currentResourceVersion: 7 },
      ),
    );
    renderEditor({ onReload });
    const form = await openEditor(user);
    const label = within(form).getByLabelText("Variant label");

    await user.type(label, "Draft label");
    await user.click(
      within(form).getByRole("button", { name: "Save configuration" }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Current server revision: 7");
    expect(label).toHaveValue("Draft label");
    expect(updateConfigurationMock).toHaveBeenCalledOnce();

    await user.click(
      within(alert).getByRole("button", {
        name: "Reload current configuration",
      }),
    );
    await waitFor(() => expect(onReload).toHaveBeenCalledOnce());
    expect(updateConfigurationMock).toHaveBeenCalledOnce();
    expect(
      await screen.findByText("Configuration reloaded from the server."),
    ).toBeVisible();
  });

  it("shows server field errors and preserves the invalid draft on 422", async () => {
    const user = userEvent.setup();
    updateConfigurationMock.mockRejectedValueOnce(
      new InstanceConfigurationRequestError(
        422,
        "invalid_configuration",
        "The configuration contains invalid fields.",
        {
          fieldErrors: [
            {
              pointer: "/variantLabel",
              code: "reserved_label",
              message: "This variant label is reserved.",
            },
          ],
        },
      ),
    );
    renderEditor();
    const form = await openEditor(user);
    const label = within(form).getByLabelText("Variant label");

    await user.type(label, "Reserved");
    await user.click(
      within(form).getByRole("button", { name: "Save configuration" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "/variantLabel: This variant label is reserved.",
    );
    expect(label).toHaveValue("Reserved");
    expect(updateConfigurationMock).toHaveBeenCalledOnce();
  });

  it("clears the dirty callback when a dirty editor unmounts", async () => {
    const user = userEvent.setup();
    const onDirtyChange = vi.fn();
    const view = renderEditor({ onDirtyChange });
    const form = await openEditor(user);

    await user.type(within(form).getByLabelText("Variant label"), "Changed");
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true));
    view.unmount();

    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
  });
});

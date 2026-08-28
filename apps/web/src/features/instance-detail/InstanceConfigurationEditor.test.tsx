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
  return normalizeAgentInstanceDetail(
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
}

interface RenderOptions {
  readonly detail?: AgentInstanceDetail;
  readonly onDirtyChange?: (dirty: boolean) => void;
  readonly onSaved?: () => Promise<void>;
  readonly onReload?: () => Promise<void>;
}

function Providers({
  children,
}: {
  readonly children: ReactNode;
}): React.JSX.Element {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Number.POSITIVE_INFINITY },
    },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function renderEditor(options: RenderOptions = {}): ReturnType<typeof render> {
  return render(
    <Providers>
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
  });

  it("loads the edit schema only after a local admin explicitly chooses Edit", async () => {
    const user = userEvent.setup();
    renderEditor();

    const edit = await screen.findByRole("button", { name: "Edit" });
    expect(fetchSchemaMock).not.toHaveBeenCalled();
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
    await user.click(
      within(form).getByRole("button", { name: "Save configuration" }),
    );

    await waitFor(() => expect(updateConfigurationMock).toHaveBeenCalledOnce());
    const request = updateConfigurationMock.mock.calls[0]?.[0];
    expect(request?.patch).toEqual({
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

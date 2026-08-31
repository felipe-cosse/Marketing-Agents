import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
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
  fetchLocalSession,
  type LocalSession,
} from "../../api/instanceConfiguration";
import type * as InstanceConfigurationApi from "../../api/instanceConfiguration";
import {
  createManualDryRun,
  generateManualDryRunIdempotencyKey,
  ManualDryRunRequestError,
  type ManualDryRunReceipt,
} from "../../api/manualDryRun";
import type * as ManualDryRunApi from "../../api/manualDryRun";
import { DryRunPanel } from "./DryRunPanel";

vi.mock("../../api/instanceConfiguration", async () => {
  const actual = await vi.importActual<typeof InstanceConfigurationApi>(
    "../../api/instanceConfiguration",
  );
  return { ...actual, fetchLocalSession: vi.fn() };
});

vi.mock("../../api/manualDryRun", async () => {
  const actual = await vi.importActual<typeof ManualDryRunApi>(
    "../../api/manualDryRun",
  );
  return {
    ...actual,
    createManualDryRun: vi.fn(),
    generateManualDryRunIdempotencyKey: vi.fn(),
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

const OPERATOR_SESSION: LocalSession = {
  actorId: "principal.local.operator",
  roles: ["operator", "viewer"],
  scopes: [],
  authMode: "local",
  environment: "local",
  modelMode: "mock",
  connectorMode: "mock",
  networkPermission: false,
  warning: "Local identity — not production authentication",
};

const VIEWER_SESSION: LocalSession = {
  ...OPERATOR_SESSION,
  actorId: "principal.local.viewer",
  roles: ["viewer"],
};

const RECEIPT: ManualDryRunReceipt = {
  status: "accepted",
  disposition: "created",
  eventId: `manual-event-hmac-sha256-v1:${"a".repeat(64)}`,
  workId: "work.web04.01",
  runId: "run.web04.01",
  executionMode: "dry_run",
  instanceUrl: `/api/v1/agent-instances/${INSTANCE_ID}`,
  runUrl: "/api/v1/runs/run.web04.01",
};

function deferred<T>(): {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
  readonly reject: (reason: unknown) => void;
} {
  let resolvePromise: ((value: T) => void) | undefined;
  let rejectPromise: ((reason: unknown) => void) | undefined;
  const promise = new Promise<T>((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  return {
    promise,
    resolve: (value: T) => resolvePromise?.(value),
    reject: (reason: unknown) => rejectPromise?.(reason),
  };
}

const INPUT_SCHEMA = {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  type: "object",
  additionalProperties: false,
  required: ["source_content"],
  properties: {
    source_content: {
      type: "string",
      title: "Source content",
      minLength: 1,
      maxLength: 12_000,
      "x-sensitive": true,
    },
  },
} as const;

function makeDetail(
  options: {
    readonly enabled?: boolean;
    readonly manualSupported?: boolean;
    readonly manualEnabled?: boolean;
    readonly mutating?: boolean;
  } = {},
): AgentInstanceDetail {
  const normalized = normalizeAgentInstanceDetail(
    makeAgentDetailPayload({
      instanceId: INSTANCE_ID,
      templateId: TEMPLATE_ID,
      departmentId: IDENTITY.departmentId,
      functionId: IDENTITY.functionId,
      runtime: "never_run",
    }),
    IDENTITY,
    AGENT_DETAIL_ETAG,
  );
  const manualSupported = options.manualSupported ?? true;
  return {
    ...normalized,
    inputSchema: INPUT_SCHEMA,
    instance: {
      ...normalized.instance,
      enabled: options.enabled ?? true,
      triggerBindings: manualSupported
        ? normalized.instance.triggerBindings.map((binding) =>
            binding.type === "manual"
              ? { ...binding, enabled: options.manualEnabled ?? true }
              : binding,
          )
        : [],
    },
    template: {
      ...normalized.template,
      supportedTriggerTypes: manualSupported ? ["manual"] : ["webhook"],
      operationClassification: options.mutating ? "mutating" : "read_only",
    },
  };
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

function renderPanel(
  options: {
    readonly detail?: AgentInstanceDetail;
    readonly onDirtyChange?: (dirty: boolean) => void;
    readonly onRuntimeMayHaveChanged?: () => Promise<void>;
  } = {},
): ReturnType<typeof render> {
  return render(
    <Providers>
      <DryRunPanel
        detail={options.detail ?? makeDetail()}
        onDirtyChange={options.onDirtyChange ?? vi.fn()}
        onRuntimeMayHaveChanged={
          options.onRuntimeMayHaveChanged ??
          vi.fn().mockResolvedValue(undefined)
        }
      />
    </Providers>,
  );
}

const fetchSessionMock = vi.mocked(fetchLocalSession);
const createDryRunMock = vi.mocked(createManualDryRun);
const generateKeyMock = vi.mocked(generateManualDryRunIdempotencyKey);

describe("WEB-04 DryRunPanel", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    fetchSessionMock.mockResolvedValue(OPERATOR_SESSION);
    createDryRunMock.mockResolvedValue(RECEIPT);
    generateKeyMock.mockReturnValue("retry-key-web04-0001");
  });

  it("enforces operator, enabled-deployment, and manual-trigger gates", async () => {
    fetchSessionMock.mockResolvedValue(VIEWER_SESSION);
    const viewer = renderPanel();
    expect(
      await screen.findByText(/does not include the operator role/iu),
    ).toBeVisible();
    expect(
      screen.queryByRole("form", { name: "Manual dry-run input" }),
    ).not.toBeInTheDocument();
    viewer.unmount();

    fetchSessionMock.mockResolvedValue(OPERATOR_SESSION);
    const disabled = renderPanel({ detail: makeDetail({ enabled: false }) });
    expect(await screen.findByText(/deployment is disabled/iu)).toBeVisible();
    expect(
      screen.queryByRole("form", { name: "Manual dry-run input" }),
    ).not.toBeInTheDocument();
    disabled.unmount();

    renderPanel({ detail: makeDetail({ manualSupported: false }) });
    expect(
      await screen.findByText(/does not support manual dry runs/iu),
    ).toBeVisible();
    expect(createDryRunMock).not.toHaveBeenCalled();
  });

  it("blocks an explicitly disabled manual binding but allows an absent binding", async () => {
    const disabledBinding = renderPanel({
      detail: makeDetail({ manualEnabled: false }),
    });
    expect(
      await screen.findByText(/manual trigger is disabled/iu),
    ).toBeVisible();
    expect(
      screen.queryByRole("form", { name: "Manual dry-run input" }),
    ).not.toBeInTheDocument();
    disabledBinding.unmount();

    const detail = makeDetail();
    renderPanel({
      detail: {
        ...detail,
        instance: { ...detail.instance, triggerBindings: [] },
      },
    });
    expect(
      await screen.findByRole("form", { name: "Manual dry-run input" }),
    ).toBeVisible();
  });

  it("renders a safe explicit error for an unsupported input schema", async () => {
    const detail = makeDetail();
    renderPanel({
      detail: {
        ...detail,
        inputSchema: {
          type: "object",
          additionalProperties: true,
          properties: {},
        },
      },
    });

    expect(
      await screen.findByText("This input schema cannot be rendered safely."),
    ).toBeVisible();
    expect(screen.getByText(/unbounded_object at \/input/iu)).toBeVisible();
    expect(
      screen.queryByRole("form", { name: "Manual dry-run input" }),
    ).not.toBeInTheDocument();
  });

  it("offers mock execution only in a mock session and explains mutating approval", async () => {
    const user = userEvent.setup();
    renderPanel({ detail: makeDetail({ mutating: true }) });
    expect(
      await screen.findByText(
        /each external action still requires its configured approval/iu,
      ),
    ).toBeVisible();
    await screen.findByRole("form", { name: "Manual dry-run input" });
    await user.click(screen.getByRole("radio", { name: /mock execution/iu }));
    expect(
      screen.getByRole("button", { name: "Run with mocks" }),
    ).toBeVisible();
  });

  it("uses connector mode alone to expose mock execution", async () => {
    fetchSessionMock.mockResolvedValue({
      ...OPERATOR_SESSION,
      modelMode: "real",
    });
    const mockConnector = renderPanel();
    expect(
      await screen.findByRole("form", { name: "Manual dry-run input" }),
    ).toBeVisible();
    expect(
      screen.getByRole("radio", { name: /mock execution/iu }),
    ).toBeVisible();
    mockConnector.unmount();

    fetchSessionMock.mockResolvedValue({
      ...OPERATOR_SESSION,
      connectorMode: "real",
    });
    renderPanel();
    expect(
      await screen.findByRole("form", { name: "Manual dry-run input" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("radio", { name: /mock execution/iu }),
    ).not.toBeInTheDocument();
  });

  it("focuses client errors without calling the API", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(
      await screen.findByRole("button", { name: "Create dry run" }),
    );

    const summary = screen.getByRole("alert", {
      name: "",
    });
    await waitFor(() => expect(summary).toHaveFocus());
    expect(
      within(summary).getByRole("button", { name: "This field is required." }),
    ).toBeVisible();
    expect(createDryRunMock).not.toHaveBeenCalled();
  });

  it("retains the retry key and draft on server errors, then rotates after an edit", async () => {
    const user = userEvent.setup();
    const rejected = new ManualDryRunRequestError(
      422,
      "dry_run_input_invalid",
      "The manual dry-run input is invalid.",
      [
        {
          pointer: "/input/source_content",
          code: "server_value_invalid",
          message: "must not reflect this value",
        },
      ],
    );
    createDryRunMock
      .mockRejectedValueOnce(rejected)
      .mockRejectedValueOnce(rejected)
      .mockResolvedValueOnce(RECEIPT);
    generateKeyMock
      .mockReturnValueOnce("retry-key-web04-0001")
      .mockReturnValueOnce("retry-key-web04-0002");
    renderPanel();

    const source = await screen.findByRole("textbox", {
      name: /source content/iu,
    });
    await user.type(source, "draft value");
    await user.click(screen.getByRole("button", { name: "Create dry run" }));
    expect(
      await screen.findAllByText("The server rejected this field."),
    ).toHaveLength(2);
    expect(source).toHaveValue("draft value");

    await user.click(screen.getByRole("button", { name: "Create dry run" }));
    await waitFor(() => expect(createDryRunMock).toHaveBeenCalledTimes(2));
    expect(createDryRunMock.mock.calls[0]?.[0].idempotencyKey).toBe(
      "retry-key-web04-0001",
    );
    expect(createDryRunMock.mock.calls[1]?.[0].idempotencyKey).toBe(
      "retry-key-web04-0001",
    );

    await user.type(source, " changed");
    await user.click(screen.getByRole("button", { name: "Create dry run" }));
    await screen.findByRole("heading", { name: "Dry run accepted" });
    expect(createDryRunMock.mock.calls[2]?.[0].idempotencyKey).toBe(
      "retry-key-web04-0002",
    );
    expect(source).toHaveValue("");
  });

  it("stops waiting without claiming cancellation and safely reuses the retry key", async () => {
    const user = userEvent.setup();
    const onRuntimeMayHaveChanged = vi.fn().mockResolvedValue(undefined);
    createDryRunMock
      .mockImplementationOnce(
        ({ signal }) =>
          new Promise<ManualDryRunReceipt>((_resolve, reject) => {
            signal?.addEventListener(
              "abort",
              () => reject(new DOMException("Aborted", "AbortError")),
              { once: true },
            );
          }),
      )
      .mockResolvedValueOnce(RECEIPT);
    renderPanel({ onRuntimeMayHaveChanged });

    await user.type(
      await screen.findByRole("textbox", { name: /source content/iu }),
      "safe input",
    );
    await user.click(screen.getByRole("button", { name: "Create dry run" }));
    await user.click(
      await screen.findByRole("button", { name: "Stop waiting" }),
    );
    const notice = await screen.findByText(
      /server may already have accepted/iu,
    );
    expect(notice).not.toHaveTextContent(/cancel/iu);
    expect(onRuntimeMayHaveChanged).toHaveBeenCalledOnce();

    await user.click(screen.getByRole("button", { name: "Create dry run" }));
    await screen.findByRole("heading", { name: "Dry run accepted" });
    expect(createDryRunMock.mock.calls[0]?.[0].idempotencyKey).toBe(
      createDryRunMock.mock.calls[1]?.[0].idempotencyKey,
    );
  });

  it("shows an accepted receipt before a deferred runtime refresh finishes", async () => {
    const user = userEvent.setup();
    const refresh = deferred<undefined>();
    const onRuntimeMayHaveChanged = vi.fn(() => refresh.promise);
    renderPanel({ onRuntimeMayHaveChanged });

    const source = await screen.findByRole("textbox", {
      name: /source content/iu,
    });
    await user.type(source, "accepted before refresh");
    await user.click(screen.getByRole("button", { name: "Create dry run" }));

    expect(
      await screen.findByRole("heading", { name: "Dry run accepted" }),
    ).toBeVisible();
    expect(source).toHaveValue("");
    expect(onRuntimeMayHaveChanged).toHaveBeenCalledOnce();

    act(() => {
      refresh.reject(new Error("runtime refresh unavailable"));
    });
    expect(
      await screen.findByText(/refreshed inspector status is not available/iu),
    ).toBeVisible();
  });

  it("shows the ambiguous-abort notice before a deferred runtime refresh finishes", async () => {
    const user = userEvent.setup();
    const refresh = deferred<undefined>();
    const onRuntimeMayHaveChanged = vi.fn(() => refresh.promise);
    createDryRunMock.mockImplementationOnce(
      ({ signal }) =>
        new Promise<ManualDryRunReceipt>((_resolve, reject) => {
          signal?.addEventListener(
            "abort",
            () => reject(new DOMException("Aborted", "AbortError")),
            { once: true },
          );
        }),
    );
    renderPanel({ onRuntimeMayHaveChanged });

    await user.type(
      await screen.findByRole("textbox", { name: /source content/iu }),
      "ambiguous request",
    );
    await user.click(screen.getByRole("button", { name: "Create dry run" }));
    await user.click(
      await screen.findByRole("button", { name: "Stop waiting" }),
    );

    expect(
      await screen.findByText(/server may already have accepted/iu),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Create dry run" }),
    ).toBeEnabled();
    expect(onRuntimeMayHaveChanged).toHaveBeenCalledOnce();

    await act(async () => {
      refresh.resolve(undefined);
      await refresh.promise;
    });
  });

  it("shows only an accepted receipt, clears sensitive input, and announces clean state", async () => {
    const user = userEvent.setup();
    const onDirtyChange = vi.fn();
    const onRuntimeMayHaveChanged = vi.fn().mockResolvedValue(undefined);
    renderPanel({ onDirtyChange, onRuntimeMayHaveChanged });
    const form = await screen.findByRole("form", {
      name: "Manual dry-run input",
    });
    const source = within(form).getByRole("textbox", {
      name: /source content/iu,
    });
    await user.type(source, "sensitive draft");
    await user.click(
      within(form).getByRole("button", { name: "Create dry run" }),
    );

    const receipt = await screen.findByRole("heading", {
      name: "Dry run accepted",
    });
    expect(receipt).toBeVisible();
    expect(screen.getByText("work.web04.01")).toBeVisible();
    expect(screen.getByText("run.web04.01")).toBeVisible();
    expect(
      screen.getByRole("link", { name: "Open accepted run resource" }),
    ).toHaveAttribute("href", `/runs/${encodeURIComponent(RECEIPT.runId)}`);
    expect(
      screen.queryByText(/published|sent|completed/iu),
    ).not.toBeInTheDocument();
    expect(source).toHaveValue("");
    expect(onRuntimeMayHaveChanged).toHaveBeenCalledOnce();
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false));
  });

  it("preserves the draft and reports retry-key generation failure", async () => {
    const user = userEvent.setup();
    generateKeyMock.mockImplementationOnce(() => {
      throw new ManualDryRunRequestError(
        0,
        "idempotency_key_unavailable",
        "A secure manual dry-run retry key could not be generated.",
      );
    });
    renderPanel();
    const source = await screen.findByRole("textbox", {
      name: /source content/iu,
    });
    await user.type(source, "preserved input");
    await user.click(screen.getByRole("button", { name: "Create dry run" }));

    expect(
      await screen.findByText(
        "A secure manual dry-run retry key could not be generated.",
      ),
    ).toBeVisible();
    expect(source).toHaveValue("preserved input");
    expect(createDryRunMock).not.toHaveBeenCalled();
  });
});

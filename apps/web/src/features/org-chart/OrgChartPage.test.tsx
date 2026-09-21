import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import {
  createMemoryRouter,
  Link,
  RouterProvider,
  useLocation,
  useNavigate,
} from "react-router-dom";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type Mock,
} from "vitest";

import {
  AGENT_DETAIL_ETAG,
  makeAgentDetailPayload,
  makeViewerSessionPayload,
} from "../../test/agentDetailFixture";
import {
  fetchInstanceConfiguration,
  fetchInstanceConfigurationSchema,
  fetchLocalSession,
  type InstanceConfigurationSchema,
  type InstanceConfigurationResult,
  type LocalSession,
} from "../../api/instanceConfiguration";
import type * as InstanceConfigurationApi from "../../api/instanceConfiguration";
import {
  createManualDryRun,
  generateManualDryRunIdempotencyKey,
  type ManualDryRunReceipt,
} from "../../api/manualDryRun";
import type * as ManualDryRunApi from "../../api/manualDryRun";
import { makeHierarchyPayload } from "../../test/hierarchyFixture";
import { HIERARCHY_VIEW_MEDIA_QUERY } from "./hierarchyViewMode";
import { OrgChartPage } from "./OrgChartPage";

vi.mock("../../api/instanceConfiguration", async () => {
  const actual = await vi.importActual<typeof InstanceConfigurationApi>(
    "../../api/instanceConfiguration",
  );
  return {
    ...actual,
    fetchLocalSession: vi.fn(),
    fetchInstanceConfiguration: vi.fn(),
    fetchInstanceConfigurationSchema: vi.fn(),
  };
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

const VIEWER_SESSION: LocalSession = {
  actorId: "principal.local.viewer",
  roles: ["viewer"],
  scopes: [],
  authMode: "local",
  environment: "local",
  modelMode: "mock",
  connectorMode: "mock",
  networkPermission: false,
  warning: "Local identity — not production authentication",
};

const ADMIN_SESSION: LocalSession = {
  ...VIEWER_SESSION,
  actorId: "principal.local.admin",
  roles: ["local_admin", "viewer"],
};

const fetchSessionMock = vi.mocked(fetchLocalSession);
const fetchSchemaMock = vi.mocked(fetchInstanceConfigurationSchema);
const fetchConfigurationMock = vi.mocked(fetchInstanceConfiguration);
const createDryRunMock = vi.mocked(createManualDryRun);

interface MatchMediaController {
  readonly matchMedia: (query: string) => MediaQueryList;
  readonly setMatches: (matches: boolean) => void;
}

function installHierarchyMatchMedia(
  initialMatches: boolean,
): MatchMediaController {
  let matches = initialMatches;
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  const mediaQuery = {
    get matches() {
      return matches;
    },
    media: HIERARCHY_VIEW_MEDIA_QUERY,
    onchange: null,
    addEventListener: (
      type: string,
      listener: (event: MediaQueryListEvent) => void,
    ) => {
      if (type === "change") listeners.add(listener);
    },
    removeEventListener: (
      type: string,
      listener: (event: MediaQueryListEvent) => void,
    ) => {
      if (type === "change") listeners.delete(listener);
    },
  } as unknown as MediaQueryList;
  const controller = {
    matchMedia: vi.fn(() => mediaQuery),
    setMatches(nextMatches: boolean) {
      matches = nextMatches;
      const event = {
        matches,
        media: HIERARCHY_VIEW_MEDIA_QUERY,
      } as MediaQueryListEvent;
      for (const listener of listeners) listener(event);
    },
  };
  vi.stubGlobal("matchMedia", controller.matchMedia);
  return controller;
}

function configurationSchema(
  instanceId: string,
  templateId: string,
): InstanceConfigurationSchema {
  return {
    projectionVersion: "instance-configuration-schema-v1",
    instanceId,
    templateId,
    supportedTriggerTypes: ["manual"],
    connectorFamilies: [
      { connectorFamily: "local", bindingIds: ["local-catalog"] },
    ],
    scheduleSupported: false,
    variantLabelMaxLength: 100,
    maxTriggerBindings: 16,
    maxConnectorBindings: 16,
  };
}

function resetConfigurationMocks(): void {
  createDryRunMock.mockReset();
  vi.mocked(generateManualDryRunIdempotencyKey).mockReturnValue(
    "obj06-chart-retry-key",
  );
  fetchSessionMock.mockReset();
  fetchSchemaMock.mockReset();
  fetchConfigurationMock.mockReset();
  fetchConfigurationMock.mockImplementation((instanceId) =>
    Promise.resolve({
      projectionVersion: "instance-configuration-v1",
      configurationEtag: '"instance-configuration-v1-1"',
      configuration: {
        ...(fixtureDetail(instanceId)
          .instance as InstanceConfigurationResult["configuration"]),
        instanceId,
        scheduledInput: null,
      },
    }),
  );
  fetchSessionMock.mockResolvedValue(VIEWER_SESSION);
  fetchSchemaMock.mockImplementation(({ instanceId, templateId }) =>
    Promise.resolve(configurationSchema(instanceId, templateId)),
  );
}

function enableConfigurationEditing(): void {
  fetchSessionMock.mockResolvedValue(ADMIN_SESSION);
}

interface FixtureInstance {
  readonly id: string;
  readonly templateId: string;
  readonly displayName: string;
  readonly purpose: string;
  readonly sourceOrdinal: number;
}

interface FixtureFunction {
  readonly id: string;
  readonly displayName: string;
  readonly instances: readonly FixtureInstance[];
}

interface FixtureDepartment {
  readonly id: string;
  readonly displayName: string;
  readonly functions: readonly FixtureFunction[];
}

function fixtureInstanceIds(): string[] {
  const payload = makeHierarchyPayload();
  const departments = payload.departments as {
    functions: { instances: FixtureInstance[] }[];
  }[];
  return departments.flatMap(({ functions }) =>
    functions.flatMap(({ instances }) => instances.map(({ id }) => id)),
  );
}

function statusPayload(completedId?: string): Record<string, unknown> {
  return {
    scope: "single-local-installation",
    runtime_watermark: `instance-status-sha256-v1:${"b".repeat(64)}`,
    items: fixtureInstanceIds().map((instanceId, index) => {
      const completed = instanceId === completedId;
      const runId = `run.web-02.${String(index + 1)}`;
      return {
        instance_id: instanceId,
        status: completed ? "completed" : "never_run",
        latest_run_id: completed ? runId : null,
        latest_run_state: completed ? "completed" : null,
        latest_run_created_at: completed ? "2026-08-28T12:00:00Z" : null,
        latest_run_updated_at: completed ? "2026-08-28T12:01:00Z" : null,
        instance_url: `/api/v1/agent-instances/${instanceId}`,
        latest_run_url: completed ? `/api/v1/runs/${runId}` : null,
      };
    }),
  };
}

function LocationProbe(): React.JSX.Element {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <div>
      <output aria-label="Current pathname">{location.pathname}</output>
      <output aria-label="Current search">{location.search}</output>
      <Link to="/runs">Global runs navigation</Link>
      <button type="button" onClick={() => void navigate(-1)}>
        Back
      </button>
      <button type="button" onClick={() => void navigate(1)}>
        Forward
      </button>
    </div>
  );
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

function renderPage(
  initialEntry = "/",
  historyEntries?: readonly string[],
): ReturnType<typeof render> {
  const entries = historyEntries ?? [initialEntry];
  const router = createMemoryRouter(
    [
      {
        path: "*",
        element: (
          <>
            <LocationProbe />
            <OrgChartPage />
          </>
        ),
      },
    ],
    { initialEntries: [...entries], initialIndex: entries.length - 1 },
  );
  return render(
    <Providers>
      <RouterProvider router={router} />
    </Providers>,
  );
}

function fixtureDetail(instanceId: string): Record<string, unknown> {
  const payload = makeHierarchyPayload();
  const departments = payload.departments as FixtureDepartment[];
  for (const department of departments) {
    for (const agentFunction of department.functions) {
      const instance = agentFunction.instances.find(
        (candidate) => candidate.id === instanceId,
      );
      if (instance === undefined) continue;
      const sharedTemplateDeploymentCount = departments
        .flatMap(({ functions }) => functions)
        .flatMap(({ instances }) => instances)
        .filter(({ templateId }) => templateId === instance.templateId).length;
      const detail = makeAgentDetailPayload({
        instanceId,
        templateId: instance.templateId,
        departmentId: department.id,
        functionId: agentFunction.id,
        sourceOrdinal: instance.sourceOrdinal,
        sharedTemplateDeploymentCount,
        displayName: instance.displayName,
        purpose: instance.purpose,
        runtime: instanceId.endsWith(".02") ? "static" : "completed",
      });
      return {
        ...detail,
        inputSchema: {
          ...(detail.inputSchema as Record<string, unknown>),
          properties: { request_id: { type: "string", maxLength: 100 } },
        },
      };
    }
  }
  throw new Error(`Unknown fixture instance: ${instanceId}`);
}

function requestHeader(
  init: RequestInit | undefined,
  name: string,
): string | null {
  return new Headers(init?.headers).get(name);
}

function requestUrl(input: RequestInfo | URL): string {
  return typeof input === "string"
    ? input
    : input instanceof URL
      ? input.href
      : input.url;
}

function installFetch(completedId?: string): Mock<typeof fetch> {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    if (url === "/api/v1/catalog/hierarchy") {
      return Promise.resolve(Response.json(makeHierarchyPayload()));
    }
    if (url === "/api/v1/agent-instances/status-summary") {
      return Promise.resolve(
        Response.json(statusPayload(completedId), {
          headers: {
            ETag: `"instance-status-sha256-v1:${"b".repeat(64)}"`,
          },
        }),
      );
    }
    if (url === "/api/v1/session") {
      return Promise.resolve(Response.json(makeViewerSessionPayload()));
    }
    const detailMatch = /^\/api\/v1\/agent-instances\/([^/]+)$/u.exec(url);
    if (detailMatch !== null) {
      if (requestHeader(init, "If-None-Match") === AGENT_DETAIL_ETAG) {
        return Promise.resolve(
          new Response(null, {
            status: 304,
            headers: { ETag: AGENT_DETAIL_ETAG },
          }),
        );
      }
      const encodedInstanceId = detailMatch[1];
      if (encodedInstanceId === undefined) {
        return Promise.reject(new Error("Detail route did not capture an ID"));
      }
      return Promise.resolve(
        Response.json(fixtureDetail(decodeURIComponent(encodedInstanceId)), {
          headers: { ETag: AGENT_DETAIL_ETAG },
        }),
      );
    }
    return Promise.reject(new Error(`Unexpected request: ${url}`));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function loaded(): Promise<void> {
  await screen.findByRole("search", { name: "Catalog search and filters" });
}

async function openFilters(
  user: ReturnType<typeof userEvent.setup>,
): Promise<void> {
  await user.click(screen.getByRole("button", { name: /^Filters/ }));
  await screen.findByRole("dialog", { name: "Catalog filters" });
}

const FIRST_DETAIL_ID = "inst.social-media.new-content.agent-1.01";
const SECOND_DETAIL_ID = "inst.social-media.new-content.agent-2.01";

const OBJ06_RECEIPT: ManualDryRunReceipt = {
  status: "accepted",
  disposition: "created",
  eventId: `manual-event-hmac-sha256-v1:${"6".repeat(64)}`,
  workId: "work.obj06.accepted",
  runId: "run.obj06.accepted",
  executionMode: "dry_run",
  instanceUrl: `/api/v1/agent-instances/${FIRST_DETAIL_ID}`,
  runUrl: "/api/v1/runs/run.obj06.accepted",
};

async function submitObj06DryRun(
  user: ReturnType<typeof userEvent.setup>,
): Promise<void> {
  const form = await screen.findByRole("form", {
    name: "Manual dry-run input",
  });
  await user.type(
    within(form).getByRole("textbox", { name: /request id/iu }),
    "obj06 request",
  );
  await user.click(
    within(form).getByRole("button", { name: "Create dry run" }),
  );
}

function cardById(instanceId: string): HTMLButtonElement {
  const card = document.querySelector<HTMLButtonElement>(
    `[data-instance-id="${instanceId}"]`,
  );
  if (card === null) throw new Error(`Expected card ${instanceId}`);
  return card;
}

function itemByNodeId(nodeId: string): HTMLButtonElement {
  const item = document.querySelector<HTMLButtonElement>(
    `[role="treeitem"][data-node-id="${nodeId}"]`,
  );
  if (item === null) throw new Error(`Expected tree item ${nodeId}`);
  return item;
}

async function makeConfigurationDirty(
  user: ReturnType<typeof userEvent.setup>,
  value = "Unsaved local override",
): Promise<{
  readonly inspector: HTMLElement;
  readonly form: HTMLElement;
  readonly variant: HTMLInputElement;
}> {
  const inspector = await screen.findByRole("complementary", {
    name: "Agent 1",
  });
  await user.click(within(inspector).getByRole("button", { name: "Edit" }));
  const form = await within(inspector).findByRole("form", {
    name: "Deployment configuration editor",
  });
  const variant =
    within(form).getByLabelText<HTMLInputElement>("Variant label");
  await user.type(variant, value);
  await waitFor(() =>
    expect(
      within(form).getByRole("button", { name: "Save configuration" }),
    ).toBeEnabled(),
  );
  return { inspector, form, variant };
}

describe("WEB-02 OrgChartPage integration", () => {
  beforeEach(() => {
    vi.useRealTimers();
    resetConfigurationMocks();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("canonicalizes invalid and repeated URL state and fetches each resource once", async () => {
    const fetchMock = installFetch();
    renderPage(
      "/?q=one&q=two&department=dept.email&function=func.community.events&unknown=x",
    );
    await loaded();
    await waitFor(() =>
      expect(screen.getByLabelText("Current search")).toHaveTextContent(
        "?department=dept.email",
      ),
    );
    expect(document.querySelectorAll(".department-group")).toHaveLength(1);
    expect(document.querySelectorAll(".function-group")).toHaveLength(2);
    expect(
      fetchMock.mock.calls.filter(([url]) =>
        requestUrl(url).includes("catalog/hierarchy"),
      ),
    ).toHaveLength(1);
    expect(
      fetchMock.mock.calls.filter(([url]) =>
        requestUrl(url).includes("status-summary"),
      ),
    ).toHaveLength(1);
  });

  it("wires search and constrained filters without refetching hierarchy, then clears all", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetch();
    renderPage();
    await loaded();
    await waitFor(() =>
      expect(document.querySelectorAll(".agent-card")).toHaveLength(43),
    );

    const search = screen.getByRole("searchbox", { name: "Search agents" });
    await user.type(search, "inst.community.events.agent-2.02");
    await waitFor(() =>
      expect(document.querySelectorAll(".agent-card")).toHaveLength(1),
    );
    expect(document.querySelectorAll(".department-group")).toHaveLength(1);
    expect(document.querySelectorAll(".function-group")).toHaveLength(1);

    await user.clear(search);
    await openFilters(user);
    const department = screen.getByRole("combobox", { name: "Department" });
    const agentFunction = screen.getByRole("combobox", { name: "Function" });
    expect(agentFunction).toBeDisabled();
    await user.selectOptions(department, "dept.community");
    expect(agentFunction).toBeEnabled();
    expect(
      within(agentFunction)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["All functions", "Events", "Education", "Discussion"]);
    await user.selectOptions(agentFunction, "func.community.education");
    await waitFor(() =>
      expect(document.querySelectorAll(".agent-card")).toHaveLength(6),
    );
    expect(
      [...document.querySelectorAll<HTMLElement>(".agent-card")].map(
        (node) => node.dataset.instanceId,
      ),
    ).toEqual([
      "inst.community.education.agent-1.01",
      "inst.community.education.agent-1.02",
      "inst.community.education.agent-2.01",
      "inst.community.education.agent-2.02",
      "inst.community.education.agent-3.01",
      "inst.community.education.agent-3.02",
    ]);

    const clearButtons = screen.getAllByRole("button", { name: "Clear all" });
    await user.click(clearButtons[0] as HTMLButtonElement);
    await waitFor(() =>
      expect(document.querySelectorAll(".agent-card")).toHaveLength(43),
    );
    expect(screen.getByLabelText("Current search")).toHaveTextContent("");
    expect(
      fetchMock.mock.calls.filter(([url]) =>
        requestUrl(url).includes("catalog/hierarchy"),
      ),
    ).toHaveLength(1);
  });

  it("joins runtime status for run filtering and recovers from an empty result", async () => {
    const completedId = "inst.social-media.new-content.agent-2.01";
    const user = userEvent.setup();
    installFetch(completedId);
    renderPage();
    await loaded();
    await waitFor(() =>
      expect(document.querySelectorAll(".agent-card")).toHaveLength(43),
    );
    await openFilters(user);
    const recentRun = screen.getByRole("combobox", {
      name: "Recent run state",
    });
    await waitFor(() => expect(recentRun).toBeEnabled());
    await user.selectOptions(recentRun, "completed");
    await waitFor(() =>
      expect(document.querySelectorAll(".agent-card")).toHaveLength(1),
    );
    expect(
      document.querySelector<HTMLElement>(".agent-card")?.dataset.instanceId,
    ).toBe(completedId);

    await user.type(
      screen.getByRole("searchbox", { name: "Search agents" }),
      "no such agent",
    );
    expect(
      await screen.findByText("No agents match your search and filters."),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Clear search and filters" }),
    );
    await waitFor(() =>
      expect(document.querySelectorAll(".agent-card")).toHaveLength(43),
    );
  });

  it("moves focus to the nearest retained ancestor when a selected card disappears", async () => {
    const user = userEvent.setup();
    installFetch();
    renderPage();
    await loaded();
    await waitFor(() =>
      expect(document.querySelectorAll(".agent-card")).toHaveLength(43),
    );
    const first = document.querySelector<HTMLButtonElement>(
      '[data-instance-id="inst.social-media.new-content.agent-1.01"]',
    );
    if (first === null) throw new Error("expected first card");
    await user.click(first);
    expect(first).toHaveFocus();
    await user.type(
      screen.getByRole("searchbox", { name: "Search agents" }),
      "inst.social-media.new-content.agent-2.01",
    );
    await waitFor(() =>
      expect(
        screen.getByRole("searchbox", { name: "Search agents" }),
      ).toHaveFocus(),
    );
    expect(
      document.querySelector('.agent-card[aria-pressed="true"]'),
    ).toBeNull();
  });

  it("restores pushed filter state through history back and forward", async () => {
    const user = userEvent.setup();
    installFetch();
    renderPage();
    await loaded();
    await waitFor(() =>
      expect(document.querySelectorAll(".agent-card")).toHaveLength(43),
    );
    await openFilters(user);
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Department" }),
      "dept.email",
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Current search")).toHaveTextContent(
        "?department=dept.email",
      ),
    );
    await user.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Current search")).toHaveTextContent(""),
    );
    expect(document.querySelectorAll(".agent-card")).toHaveLength(43);
    await user.click(screen.getByRole("button", { name: "Forward" }));
    await waitFor(() =>
      expect(document.querySelectorAll(".agent-card")).toHaveLength(5),
    );
  });
});

describe("WEB-03 OrgChartPage detail integration", () => {
  beforeEach(() => {
    vi.useRealTimers();
    resetConfigurationMocks();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("closes immediately on Escape from the selected card and restores its focus", async () => {
    const user = userEvent.setup();
    installFetch();
    renderPage();
    await loaded();
    const card = cardById(FIRST_DETAIL_ID);

    await user.click(card);
    await screen.findByRole("complementary", { name: "Agent 1" });
    expect(card).toHaveFocus();
    await user.keyboard("{Escape}");

    await waitFor(() =>
      expect(
        screen.queryByRole("complementary", { name: "Agent 1" }),
      ).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(card).toHaveFocus());
  });

  it("closes only the topmost filter dialog on Escape", async () => {
    const user = userEvent.setup();
    installFetch();
    renderPage();
    await loaded();
    const card = cardById(FIRST_DETAIL_ID);

    await user.click(card);
    const inspector = await screen.findByRole("complementary", {
      name: "Agent 1",
    });
    await openFilters(user);
    const dialog = screen.getByRole("dialog", { name: "Catalog filters" });
    within(dialog).getByRole("combobox", { name: "Department" }).focus();
    await user.keyboard("{Escape}");

    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(inspector).toBeVisible();
    expect(card).toHaveAttribute("aria-pressed", "true");
  });

  it("keeps a dirty selection in place or discards it before focusing the destination card", async () => {
    const user = userEvent.setup();
    enableConfigurationEditing();
    installFetch();
    renderPage();
    await loaded();
    const first = cardById(FIRST_DETAIL_ID);
    const second = cardById(SECOND_DETAIL_ID);

    await user.click(first);
    const { inspector, form, variant } = await makeConfigurationDirty(user);
    await user.click(second);

    let dialog = await screen.findByRole("alertdialog", {
      name: "Discard configuration changes?",
    });
    expect(first).toHaveAttribute("aria-pressed", "true");
    expect(second).toHaveAttribute("aria-pressed", "false");
    await user.click(
      within(dialog).getByRole("button", { name: "Keep editing" }),
    );
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(inspector).toBeVisible();
    expect(variant).toHaveValue("Unsaved local override");
    await waitFor(() =>
      expect(
        within(form).getByRole("checkbox", { name: "Deployment enabled" }),
      ).toHaveFocus(),
    );

    await user.click(second);
    dialog = await screen.findByRole("alertdialog", {
      name: "Discard configuration changes?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Discard changes" }),
    );

    await screen.findByRole("complementary", { name: "Agent 2" });
    await waitFor(() => expect(second).toHaveFocus());
    expect(first).toHaveAttribute("aria-pressed", "false");
    expect(second).toHaveAttribute("aria-pressed", "true");
  });

  it("guards recent-run navigation until dirty configuration is kept or discarded", async () => {
    const user = userEvent.setup();
    enableConfigurationEditing();
    installFetch();
    renderPage();
    await loaded();
    await user.click(cardById(FIRST_DETAIL_ID));
    const { inspector, form, variant } = await makeConfigurationDirty(user);
    const runLink = within(inspector).getByRole("link", {
      name: "run.web-03.latest",
    });

    await user.click(runLink);
    let dialog = await screen.findByRole("alertdialog", {
      name: "Discard configuration changes?",
    });
    expect(screen.getByLabelText("Current pathname")).toHaveTextContent(
      /^\/$/u,
    );
    expect(dialog).toHaveTextContent(
      "will be lost before you open run run.web-03.latest",
    );

    await user.click(
      within(dialog).getByRole("button", { name: "Keep editing" }),
    );
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(variant).toHaveValue("Unsaved local override");
    expect(screen.getByLabelText("Current pathname")).toHaveTextContent(
      /^\/$/u,
    );
    await waitFor(() =>
      expect(
        within(form).getByRole("checkbox", { name: "Deployment enabled" }),
      ).toHaveFocus(),
    );

    await user.click(runLink);
    dialog = await screen.findByRole("alertdialog", {
      name: "Discard configuration changes?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Discard changes" }),
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Current pathname")).toHaveTextContent(
        "/runs/run.web-03.latest",
      ),
    );
  });

  it("OBJ-06 navigates after clean admission without a stale draft warning", async () => {
    const user = userEvent.setup();
    fetchSessionMock.mockResolvedValue({
      ...ADMIN_SESSION,
      roles: ["viewer", "local_admin", "operator"],
    });
    createDryRunMock.mockResolvedValue(OBJ06_RECEIPT);
    installFetch();
    renderPage();
    await loaded();
    await user.click(cardById(FIRST_DETAIL_ID));
    await submitObj06DryRun(user);
    await waitFor(() =>
      expect(screen.getByLabelText("Current pathname")).toHaveTextContent(
        /^\/runs\/run.obj06.accepted$/u,
      ),
    );
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(createDryRunMock).toHaveBeenCalledOnce();
  });

  it.each(["before submission", "while admission is pending"])(
    "OBJ-06 keeps configuration changed %s across accepted-run navigation",
    async (editTiming) => {
      const user = userEvent.setup();
      fetchSessionMock.mockResolvedValue({
        ...ADMIN_SESSION,
        roles: ["viewer", "local_admin", "operator"],
      });
      let accept: ((receipt: ManualDryRunReceipt) => void) | undefined;
      createDryRunMock.mockReturnValue(
        new Promise((resolve) => {
          accept = resolve;
        }),
      );
      const fetchMock = installFetch();
      renderPage();
      await loaded();
      await user.click(cardById(FIRST_DETAIL_ID));
      let variant: HTMLInputElement;
      if (editTiming === "before submission") {
        ({ variant } = await makeConfigurationDirty(user));
        await submitObj06DryRun(user);
      } else {
        await submitObj06DryRun(user);
        ({ variant } = await makeConfigurationDirty(user));
      }
      expect(screen.getByLabelText("Current pathname")).toHaveTextContent(
        /^\/$/u,
      );
      act(() => {
        accept?.(OBJ06_RECEIPT);
      });
      let dialog = await screen.findByRole("alertdialog", {
        name: "Discard configuration changes?",
      });
      expect(dialog).toHaveTextContent("open run run.obj06.accepted");
      await user.click(
        within(dialog).getByRole("button", { name: "Keep editing" }),
      );
      expect(variant).toHaveValue("Unsaved local override");
      expect(
        await screen.findByRole("heading", { name: "Dry run accepted" }),
      ).toBeVisible();
      expect(screen.getByRole("textbox", { name: /request id/iu })).toHaveValue(
        "",
      );
      await user.click(
        screen.getByRole("link", { name: "Open accepted run resource" }),
      );
      dialog = await screen.findByRole("alertdialog", {
        name: "Discard configuration changes?",
      });
      await user.click(
        within(dialog).getByRole("button", { name: "Discard changes" }),
      );
      await waitFor(() =>
        expect(screen.getByLabelText("Current pathname")).toHaveTextContent(
          /^\/runs\/run.obj06.accepted$/u,
        ),
      );
      expect(createDryRunMock).toHaveBeenCalledOnce();
      expect(
        fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH"),
      ).toHaveLength(0);
    },
  );

  it.each(["status", "detail"])(
    "OBJ-06 preserves acceptance and configuration when post-admission %s refresh fails",
    async (failedResource) => {
      const user = userEvent.setup();
      fetchSessionMock.mockResolvedValue({
        ...ADMIN_SESSION,
        roles: ["viewer", "local_admin", "operator"],
      });
      const fetchMock = installFetch();
      const originalFetch = fetchMock.getMockImplementation();
      if (originalFetch === undefined) throw new Error("Missing fixture fetch");
      let accepted = false;
      fetchMock.mockImplementation((input, init) => {
        if (
          accepted &&
          input ===
            (failedResource === "status"
              ? "/api/v1/agent-instances/status-summary"
              : `/api/v1/agent-instances/${FIRST_DETAIL_ID}`)
        ) {
          return Promise.reject(new Error("OBJ-06 refresh unavailable"));
        }
        return originalFetch(input, init);
      });
      createDryRunMock.mockImplementation(() => {
        accepted = true;
        return Promise.resolve(OBJ06_RECEIPT);
      });
      renderPage();
      await loaded();
      await user.click(cardById(FIRST_DETAIL_ID));
      const { variant } = await makeConfigurationDirty(user);
      await submitObj06DryRun(user);
      const dialog = await screen.findByRole("alertdialog", {
        name: "Discard configuration changes?",
      });
      await user.click(
        within(dialog).getByRole("button", { name: "Keep editing" }),
      );
      expect(
        await screen.findByText(
          /request was accepted, but refreshed inspector status is not available/iu,
        ),
      ).toBeVisible();
      expect(
        screen.getByRole("heading", { name: "Dry run accepted" }),
      ).toBeVisible();
      expect(variant).toHaveValue("Unsaved local override");
      expect(screen.getByLabelText("Current pathname")).toHaveTextContent(
        /^\/$/u,
      );
      expect(createDryRunMock).toHaveBeenCalledOnce();
    },
  );

  it("blocks global, history, and unload navigation while editor state is dirty", async () => {
    const user = userEvent.setup();
    enableConfigurationEditing();
    installFetch();
    renderPage("/", ["/runs", "/"]);
    await loaded();
    await user.click(cardById(FIRST_DETAIL_ID));
    const { variant } = await makeConfigurationDirty(user);
    const beforeUnload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(beforeUnload);
    expect(beforeUnload.defaultPrevented).toBe(true);

    await user.click(screen.getByRole("button", { name: "Back" }));
    let dialog = await screen.findByRole("alertdialog", {
      name: "Discard configuration changes?",
    });
    expect(dialog).toHaveTextContent(
      "will be lost before you open Runs & audit",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Keep editing" }),
    );
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(variant).toHaveValue("Unsaved local override");
    expect(screen.getByLabelText("Current pathname")).toHaveTextContent(
      /^\/$/u,
    );

    await user.click(
      screen.getByRole("link", { name: "Global runs navigation" }),
    );
    dialog = await screen.findByRole("alertdialog", {
      name: "Discard configuration changes?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Discard changes" }),
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Current pathname")).toHaveTextContent(
        "/runs",
      ),
    );
  });

  it("keeps a filtered-out dirty editor usable and can discard on the next filter transition", async () => {
    const user = userEvent.setup();
    enableConfigurationEditing();
    installFetch();
    renderPage();
    await loaded();
    await user.click(cardById(FIRST_DETAIL_ID));
    const { inspector, form, variant } = await makeConfigurationDirty(user);
    const search = screen.getByRole("searchbox", { name: "Search agents" });

    fireEvent.change(search, { target: { value: SECOND_DETAIL_ID } });
    const dialog = await screen.findByRole("alertdialog", {
      name: "Discard configuration changes?",
    });
    await user.click(
      within(dialog).getByRole("button", { name: "Keep editing" }),
    );

    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(search).toHaveValue(SECOND_DETAIL_ID);
    expect(
      document.querySelector(`[data-instance-id="${FIRST_DETAIL_ID}"]`),
    ).toBeNull();
    expect(inspector).toBeVisible();
    expect(variant).toHaveValue("Unsaved local override");
    await waitFor(() =>
      expect(
        within(form).getByRole("checkbox", { name: "Deployment enabled" }),
      ).toHaveFocus(),
    );

    fireEvent.change(search, { target: { value: "no matching agent" } });
    const discardDialog = await screen.findByRole("alertdialog", {
      name: "Discard configuration changes?",
    });
    await user.click(
      within(discardDialog).getByRole("button", { name: "Discard changes" }),
    );

    await waitFor(() => expect(inspector).not.toBeInTheDocument());
    expect(search).toHaveValue("no matching agent");
    await waitFor(() => expect(search).toHaveFocus());
    expect(
      document.querySelector('.agent-card[aria-pressed="true"]'),
    ).toBeNull();
  });

  it("loads one complete selected detail, restores focus on close, and conditionally reuses it", async () => {
    const user = userEvent.setup();
    const fetchMock = installFetch();
    renderPage();
    await loaded();
    const card = document.querySelector<HTMLButtonElement>(
      '[data-instance-id="inst.social-media.new-content.agent-1.01"]',
    );
    if (card === null) throw new Error("expected detail fixture card");

    await user.click(card);
    const inspector = await screen.findByRole("complementary", {
      name: "Agent 1",
    });
    expect(card).toHaveAttribute("aria-expanded", "true");
    expect(within(inspector).getByText("Overview")).toBeVisible();
    expect(
      within(inspector).getByText("Deployment & configuration"),
    ).toBeVisible();
    expect(
      within(inspector).getByText("Capabilities & policies"),
    ).toBeVisible();
    expect(within(inspector).getByText("Recent runs")).toBeVisible();
    expect(within(inspector).getAllByText(/run\.web-03\.latest/u)).toHaveLength(
      2,
    );

    await user.click(
      within(inspector).getByRole("button", {
        name: "Close details for Agent 1",
      }),
    );
    expect(screen.queryByRole("complementary", { name: "Agent 1" })).toBeNull();
    await waitFor(() => expect(card).toHaveFocus());

    await user.click(card);
    await screen.findByRole("complementary", { name: "Agent 1" });
    const detailCalls = fetchMock.mock.calls.filter(([input]) =>
      requestUrl(input).includes(
        "/api/v1/agent-instances/inst.social-media.new-content.agent-1.01",
      ),
    );
    expect(detailCalls).toHaveLength(2);
    expect(requestHeader(detailCalls[1]?.[1], "If-None-Match")).toBe(
      AGENT_DETAIL_ETAG,
    );
  });

  it("identifies the second shared-template deployment and never fabricates a run state", async () => {
    const user = userEvent.setup();
    installFetch();
    renderPage();
    await loaded();
    const card = document.querySelector<HTMLButtonElement>(
      '[data-instance-id="inst.community.events.agent-1.02"]',
    );
    if (card === null) throw new Error("expected duplicate fixture card");

    await user.click(card);
    const inspector = await screen.findByRole("complementary", {
      name: "Agent 1 · Instance 2 of 2",
    });
    expect(
      within(inspector).getAllByText("inst.community.events.agent-1.02"),
    ).toHaveLength(2);
    expect(
      within(inspector).getByText(
        "Recent run data is unavailable for this local runtime.",
      ),
    ).toBeVisible();
    expect(within(inspector).queryByText("Never run")).toBeNull();
  });
});

describe("WEB-07 responsive organization hierarchy", () => {
  beforeEach(() => {
    vi.useRealTimers();
    resetConfigurationMocks();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("WEB-07 defaults narrow to tree and wide to graph with one mounted representation", async () => {
    const viewport = installHierarchyMatchMedia(true);
    installFetch();
    renderPage();
    await loaded();

    expect(
      await screen.findByRole("tree", {
        name: "Marketing Agents organization tree",
      }),
    ).toBeVisible();
    expect(screen.queryByTestId("org-chart-viewport")).toBeNull();
    expect(screen.getByRole("button", { name: "Tree view" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    act(() => viewport.setMatches(false));
    await waitFor(() =>
      expect(screen.getByTestId("org-chart-viewport")).toBeVisible(),
    );
    expect(
      screen.queryByRole("tree", {
        name: "Marketing Agents organization tree",
      }),
    ).toBeNull();

    act(() => viewport.setMatches(true));
    await waitFor(() =>
      expect(
        screen.getByRole("tree", {
          name: "Marketing Agents organization tree",
        }),
      ).toBeVisible(),
    );
    expect(screen.queryByTestId("org-chart-viewport")).toBeNull();
  });

  it("WEB-07 preserves selection and viable focus across explicit graph and tree changes", async () => {
    const viewport = installHierarchyMatchMedia(true);
    const user = userEvent.setup();
    installFetch();
    renderPage();
    await loaded();

    await user.click(itemByNodeId("func.social-media.new-content"));
    const treeInstance = itemByNodeId(FIRST_DETAIL_ID);
    await user.click(treeInstance);
    const inspector = await screen.findByRole("dialog", { name: "Agent 1" });
    expect(inspector).toHaveAttribute("aria-modal", "true");
    expect(treeInstance).toHaveAttribute("aria-selected", "true");

    await user.click(screen.getByRole("button", { name: "Graph view" }));
    await waitFor(() =>
      expect(screen.getByTestId("org-chart-viewport")).toBeVisible(),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Graph view" })).toHaveFocus(),
    );
    expect(cardById(FIRST_DETAIL_ID)).toHaveAttribute("aria-pressed", "true");
    expect(
      screen.queryByRole("tree", {
        name: "Marketing Agents organization tree",
      }),
    ).toBeNull();

    await user.click(screen.getByRole("button", { name: "Tree view" }));
    await waitFor(() =>
      expect(
        screen.getByRole("tree", {
          name: "Marketing Agents organization tree",
        }),
      ).toBeVisible(),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Tree view" })).toHaveFocus(),
    );
    expect(itemByNodeId(FIRST_DETAIL_ID)).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.queryByTestId("org-chart-viewport")).toBeNull();

    act(() => viewport.setMatches(false));
    expect(
      screen.getByRole("tree", {
        name: "Marketing Agents organization tree",
      }),
    ).toBeVisible();
  });

  it("WEB-07 restores a safe focus target across automatic representation changes", async () => {
    const viewport = installHierarchyMatchMedia(false);
    installFetch();
    renderPage();
    await loaded();

    const graphInstance = cardById(FIRST_DETAIL_ID);
    graphInstance.focus();
    expect(graphInstance).toHaveFocus();

    act(() => viewport.setMatches(true));
    await waitFor(() =>
      expect(
        screen.getByRole("tree", {
          name: "Marketing Agents organization tree",
        }),
      ).toBeVisible(),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("searchbox", { name: "Search agents" }),
      ).toHaveFocus(),
    );

    act(() => viewport.setMatches(false));
    await waitFor(() =>
      expect(screen.getByTestId("org-chart-viewport")).toBeVisible(),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("searchbox", { name: "Search agents" }),
      ).toHaveFocus(),
    );
  });
});

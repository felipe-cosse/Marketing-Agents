import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { makeHierarchyPayload } from "../../test/hierarchyFixture";
import { OrgChartPage } from "./OrgChartPage";

interface FixtureInstance {
  readonly id: string;
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
      <output aria-label="Current search">{location.search}</output>
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

function renderPage(initialEntry = "/"): ReturnType<typeof render> {
  return render(
    <Providers>
      <MemoryRouter initialEntries={[initialEntry]}>
        <LocationProbe />
        <OrgChartPage />
      </MemoryRouter>
    </Providers>,
  );
}

function installFetch(completedId?: string): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
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

describe("WEB-02 OrgChartPage integration", () => {
  beforeEach(() => vi.useRealTimers());
  afterEach(() => vi.unstubAllGlobals());

  it("canonicalizes invalid and repeated URL state and fetches each resource once", async () => {
    const fetchMock = installFetch();
    renderPage(
      "/?q=one&q=two&department=dept.email&function=func.community.events&unknown=x",
    );
    await loaded();
    expect(screen.getByLabelText("Current search")).toHaveTextContent(
      "?department=dept.email",
    );
    expect(document.querySelectorAll(".department-group")).toHaveLength(1);
    expect(document.querySelectorAll(".function-group")).toHaveLength(2);
    expect(
      fetchMock.mock.calls.filter(([url]) =>
        String(url).includes("catalog/hierarchy"),
      ),
    ).toHaveLength(1);
    expect(
      fetchMock.mock.calls.filter(([url]) =>
        String(url).includes("status-summary"),
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
        String(url).includes("catalog/hierarchy"),
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
    expect(document.querySelector('[aria-pressed="true"]')).toBeNull();
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

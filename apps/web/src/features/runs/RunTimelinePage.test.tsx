import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchRunArtifactsPage,
  fetchRunResource,
  fetchRunTimelinePage,
  RunArtifactsRequestError,
} from "../../api/runArtifacts";
import type * as RunArtifactsApi from "../../api/runArtifacts";
import {
  makeArtifactPage,
  makeRunResource,
  makeTimelineEvent,
  makeTimelinePage,
  WEB_06_ACTION_ID,
  WEB_06_ARTIFACT_ID,
  WEB_06_RUN_ID,
  WEB_06_STEP_ID,
} from "../../test/runArtifactFixture";
import { NO_EXTERNAL_DELIVERY_LABEL } from "../artifacts";
import { RunTimelinePage } from "./RunTimelinePage";

vi.mock("../../api/runArtifacts", async () => {
  const actual = await vi.importActual<typeof RunArtifactsApi>(
    "../../api/runArtifacts",
  );
  return {
    ...actual,
    fetchRunArtifactsPage: vi.fn(),
    fetchRunResource: vi.fn(),
    fetchRunTimelinePage: vi.fn(),
  };
});

const fetchRunMock = vi.mocked(fetchRunResource);
const fetchTimelineMock = vi.mocked(fetchRunTimelinePage);
const fetchArtifactsMock = vi.mocked(fetchRunArtifactsPage);

function Providers({
  children,
}: {
  readonly children: ReactNode;
}): React.JSX.Element {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: Number.POSITIVE_INFINITY,
      },
    },
  });
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

function renderRunPage(
  initialEntry = `/runs/${WEB_06_RUN_ID}`,
): ReturnType<typeof render> {
  return render(
    <Providers>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/runs/:runId" element={<RunTimelinePage />} />
          <Route path="/runs" element={<p>Runs index</p>} />
          <Route path="/approvals" element={<p>Approval queue</p>} />
          <Route path="/artifacts/:artifactId" element={<p>Artifact page</p>} />
        </Routes>
      </MemoryRouter>
    </Providers>,
  );
}

function fact(scope: HTMLElement, label: string): HTMLElement {
  const term = within(scope).getByText(label, { selector: "dt" });
  const row = term.parentElement;
  if (row === null) throw new Error(`Missing fact row for ${label}`);
  return row;
}

function sectionForHeading(name: string): HTMLElement {
  const section = screen.getByRole("heading", { name }).closest("section");
  if (section === null) throw new Error(`Missing section for ${name}`);
  return section;
}

beforeEach(() => {
  vi.resetAllMocks();
  fetchRunMock.mockResolvedValue(makeRunResource());
  fetchTimelineMock.mockResolvedValue(
    makeTimelinePage([
      makeTimelineEvent({
        sequence: 1,
        eventType: "run.received",
        newState: "received",
        occurredAt: "2026-08-31T16:05:00Z",
      }),
    ]),
  );
  fetchArtifactsMock.mockResolvedValue(makeArtifactPage());
});

describe("WEB-06 RunTimelinePage", () => {
  it("presents persisted sequence order, all event families, sealed policy facts, and truthful mock receipt labels", async () => {
    fetchTimelineMock.mockResolvedValue(
      makeTimelinePage([
        makeTimelineEvent({
          sequence: 1,
          eventType: "run.received",
          previousState: null,
          newState: "received",
          occurredAt: "2026-08-31T16:06:00Z",
        }),
        makeTimelineEvent({
          sequence: 2,
          eventType: "plan.sealed",
          occurredAt: "2026-08-31T16:05:00Z",
        }),
        makeTimelineEvent({
          sequence: 3,
          eventType: "model.attempt.started",
          stepId: WEB_06_STEP_ID,
          attemptedCommand: "start_attempt",
          occurredAt: "2026-08-31T16:04:00Z",
        }),
        makeTimelineEvent({
          sequence: 4,
          eventType: "approval.requested",
          stepId: WEB_06_STEP_ID,
          approvalRequestId: "approval.web06.01",
          occurredAt: "2026-08-31T16:03:00Z",
        }),
        makeTimelineEvent({
          sequence: 5,
          eventType: "action.succeeded",
          stepId: WEB_06_STEP_ID,
          actionId: WEB_06_ACTION_ID,
          newState: "succeeded",
          occurredAt: "2026-08-31T16:02:00Z",
        }),
        makeTimelineEvent({
          sequence: 6,
          eventType: "artifact.created",
          stepId: WEB_06_STEP_ID,
          artifactId: WEB_06_ARTIFACT_ID,
          occurredAt: "2026-08-31T16:01:00Z",
        }),
      ]),
    );

    const user = userEvent.setup();
    renderRunPage();

    expect(
      await screen.findByRole("heading", { level: 1, name: "Run timeline" }),
    ).toBeVisible();
    await waitFor(() =>
      expect(fetchRunMock).toHaveBeenCalledWith(
        WEB_06_RUN_ID,
        expect.any(AbortSignal),
      ),
    );
    expect(
      screen.getByText("Terminal snapshot — polling stopped"),
    ).toBeVisible();

    const timeline = screen.getByRole("list", {
      name: "Run timeline in sequence order",
    });
    const events = within(timeline).getAllByRole("listitem");
    expect(events.map((event) => event.dataset.sequence)).toEqual([
      "1",
      "2",
      "3",
      "4",
      "5",
      "6",
    ]);
    expect(within(timeline).getByText("Run state")).toBeVisible();
    expect(within(timeline).getByText("Plan snapshot")).toBeVisible();
    expect(within(timeline).getByText("Provider attempt")).toBeVisible();
    expect(within(timeline).getByText("Approval")).toBeVisible();
    expect(within(timeline).getByText("External action")).toBeVisible();
    expect(within(timeline).getByText("Artifact")).toBeVisible();
    expect(within(timeline).getByText("Attempted Start Attempt")).toBeVisible();

    const runDetails = screen.getByRole("complementary", {
      name: "Run details",
    });
    expect(fact(runDetails, "Configuration revision")).toHaveTextContent("7");
    const plan = sectionForHeading("Selected plan and policy");
    expect(fact(plan, "Workflow version")).toHaveTextContent("3");
    expect(fact(plan, "Approval policy")).toHaveTextContent(
      "Approval required",
    );
    expect(fact(plan, "Run limits")).toHaveTextContent(
      "4 steps · 2 model calls · 3 tool calls · 180s",
    );
    const selectedAgents = sectionForHeading("Selected agents");
    expect(selectedAgents).toHaveTextContent("configuration revision 7");
    expect(selectedAgents).toHaveTextContent("Target");
    const steps = sectionForHeading("Steps and attempt policy");
    expect(fact(steps, "Provider attempt")).toHaveTextContent(
      "Tool; up to 2 attempts",
    );
    expect(fact(steps, "Configuration snapshot")).toHaveTextContent(
      "revision 7",
    );
    expect(fact(steps, "Approval policy")).toHaveTextContent(
      "policy.human-external-write",
    );

    const actions = sectionForHeading("External actions and receipts");
    expect(screen.getByLabelText(NO_EXTERNAL_DELIVERY_LABEL)).toHaveTextContent(
      NO_EXTERNAL_DELIVERY_LABEL,
    );
    expect(fact(actions, "Idempotency")).toHaveTextContent(
      "Required support; the protected key is not exposed by this read API",
    );
    expect(fact(actions, "Receipt identity")).toHaveTextContent(
      "receipt.web06.mock.01",
    );
    expect(fact(actions, "Result")).toHaveTextContent("Accepted");
    expect(fact(actions, "Completed").querySelector("time")).toHaveAttribute(
      "datetime",
      "2026-08-31T16:02:00Z",
    );
    await waitFor(() => expect(fetchArtifactsMock).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "Refresh now" }));
    await waitFor(() => expect(fetchArtifactsMock).toHaveBeenCalledTimes(2));
  });

  it("focuses an asynchronously rendered fragment target", async () => {
    const previousScrollDescriptor = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "scrollIntoView",
    );
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });
    try {
      renderRunPage(
        `/runs/${WEB_06_RUN_ID}#action-${encodeURIComponent(WEB_06_ACTION_ID)}`,
      );

      const target = await waitFor(() => {
        const element = document.getElementById(`action-${WEB_06_ACTION_ID}`);
        if (element === null) throw new Error("Missing action fragment target");
        return element;
      });
      await waitFor(() => expect(target).toHaveFocus());
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "center" });
    } finally {
      if (previousScrollDescriptor === undefined) {
        delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
      } else {
        Object.defineProperty(
          HTMLElement.prototype,
          "scrollIntoView",
          previousScrollDescriptor,
        );
      }
    }
  });

  it("focuses a timeline-event fragment after its page resolves", async () => {
    const previousScrollDescriptor = Object.getOwnPropertyDescriptor(
      HTMLElement.prototype,
      "scrollIntoView",
    );
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });
    try {
      fetchRunMock.mockResolvedValue(makeRunResource({ state: "executing" }));
      fetchTimelineMock
        .mockResolvedValueOnce(
          makeTimelinePage([
            makeTimelineEvent({ sequence: 1, eventType: "run.received" }),
          ]),
        )
        .mockResolvedValueOnce(
          makeTimelinePage([
            makeTimelineEvent({ sequence: 1, eventType: "run.received" }),
            makeTimelineEvent({ sequence: 2, eventType: "run.validated" }),
          ]),
        );
      const user = userEvent.setup();
      renderRunPage(`/runs/${WEB_06_RUN_ID}#timeline-event-1`);

      const target = await waitFor(() => {
        const element = document.getElementById("timeline-event-1");
        if (element === null)
          throw new Error("Missing timeline fragment target");
        return element;
      });
      await waitFor(() => expect(target).toHaveFocus());
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "center" });

      const refresh = screen.getByRole("button", { name: "Refresh now" });
      await user.click(refresh);
      expect(
        await screen.findByText("2 events loaded across 1 page."),
      ).toBeVisible();
      await waitFor(() => expect(refresh).toHaveFocus());
      expect(target).not.toHaveFocus();
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
    } finally {
      if (previousScrollDescriptor === undefined) {
        delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
      } else {
        Object.defineProperty(
          HTMLElement.prototype,
          "scrollIntoView",
          previousScrollDescriptor,
        );
      }
    }
  });

  it("labels an expired pending approval and withholds its review action", async () => {
    fetchRunMock.mockResolvedValue(
      makeRunResource({
        pendingApprovals: Object.freeze([
          Object.freeze({
            id: "approval.web06.expired.01",
            actionId: WEB_06_ACTION_ID,
            stepId: WEB_06_STEP_ID,
            status: "pending" as const,
            destinationSummary: "Expired mock CRM write",
            requestedAt: "2026-08-31T16:00:00Z",
            expiresAt: "2026-08-31T16:01:00Z",
            isExpired: true,
            approvalUrl: "/api/v1/approvals/approval.web06.expired.01",
            actionUrl: `/api/v1/external-actions/${WEB_06_ACTION_ID}`,
            stepUrl: `/api/v1/runs/${WEB_06_RUN_ID}/steps/${WEB_06_STEP_ID}`,
          }),
        ]),
      }),
    );

    renderRunPage();

    const approvals = await waitFor(() =>
      sectionForHeading("Pending approvals"),
    );
    const expiredItem = within(approvals)
      .getByText("Expired mock CRM write")
      .closest("li");
    if (expiredItem === null) throw new Error("Missing expired approval row");
    expect(
      within(expiredItem).getByText(/^Expired /u, { selector: "div > span" }),
    ).toBeVisible();
    expect(expiredItem).toHaveTextContent("Expired — no decision available");
    expect(
      within(approvals).queryByRole("link", { name: "Review approval" }),
    ).not.toBeInTheDocument();
  });

  it("loads timeline pages only through the bounded cursor request and appends their persisted order", async () => {
    const cursor = "run-timeline-v1.web06-next";
    const firstPage = makeTimelinePage(
      [
        makeTimelineEvent({ sequence: 1, eventType: "run.received" }),
        makeTimelineEvent({ sequence: 2, eventType: "run.validated" }),
      ],
      cursor,
    );
    const secondPage = makeTimelinePage([
      makeTimelineEvent({ sequence: 3, eventType: "run.planned" }),
    ]);
    fetchRunMock.mockResolvedValue(makeRunResource({ state: "executing" }));
    fetchTimelineMock.mockImplementation((_runId, query) =>
      Promise.resolve(query?.cursor === cursor ? secondPage : firstPage),
    );
    const user = userEvent.setup();

    renderRunPage();

    expect(
      await screen.findByText(
        "More persisted events exist after the loaded cursor. Load them before treating this as the complete run history.",
      ),
    ).toBeVisible();
    expect(fetchTimelineMock).toHaveBeenCalledWith(
      WEB_06_RUN_ID,
      { limit: 100 },
      expect.any(AbortSignal),
    );
    await user.click(screen.getByRole("button", { name: "Load more events" }));
    await waitFor(() =>
      expect(fetchTimelineMock).toHaveBeenCalledWith(
        WEB_06_RUN_ID,
        { limit: 100, cursor },
        expect.any(AbortSignal),
      ),
    );
    expect(
      await screen.findByText("3 events loaded across 2 pages."),
    ).toBeVisible();
    const timeline = screen.getByRole("list", {
      name: "Run timeline in sequence order",
    });
    expect(
      within(timeline)
        .getAllByRole("listitem")
        .map((event) => event.dataset.sequence),
    ).toEqual(["1", "2", "3"]);
    expect(
      screen.queryByRole("button", { name: "Load more events" }),
    ).not.toBeInTheDocument();
  });

  it("surfaces a bounded run failure and retries only after the operator asks", async () => {
    fetchRunMock
      .mockRejectedValueOnce(
        new RunArtifactsRequestError(
          503,
          "run_temporarily_unavailable",
          "The run is temporarily unavailable.",
        ),
      )
      .mockResolvedValueOnce(makeRunResource());
    const user = userEvent.setup();

    renderRunPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The run is unavailable");
    expect(alert).toHaveTextContent("The run is temporarily unavailable.");
    expect(fetchRunMock).toHaveBeenCalledTimes(1);
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "Run timeline" }),
    ).toBeVisible();
    expect(fetchRunMock).toHaveBeenCalledTimes(2);
  });

  it("keeps a timeline error distinct from the run snapshot and supports explicit retry", async () => {
    fetchRunMock.mockResolvedValue(makeRunResource({ state: "executing" }));
    fetchTimelineMock
      .mockRejectedValueOnce(
        new RunArtifactsRequestError(
          503,
          "timeline_temporarily_unavailable",
          "The run timeline is temporarily unavailable.",
        ),
      )
      .mockResolvedValueOnce(
        makeTimelinePage([
          makeTimelineEvent({ sequence: 1, eventType: "run.received" }),
        ]),
      );
    const user = userEvent.setup();

    renderRunPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The timeline is unavailable");
    expect(alert).toHaveTextContent(
      "The run timeline is temporarily unavailable.",
    );
    expect(
      screen.getByRole("heading", { name: "Execution snapshot" }),
    ).toBeVisible();
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("list", {
        name: "Run timeline in sequence order",
      }),
    ).toBeVisible();
    expect(fetchTimelineMock).toHaveBeenCalledTimes(2);
  });
});

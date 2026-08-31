import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApprovalRequestError,
  decideApproval,
  fetchApprovalDetail,
  fetchApprovalPage,
} from "../../api/approvals";
import type * as ApprovalsApi from "../../api/approvals";
import { fetchApprovalRunSafety } from "../../api/approvalRunSafety";
import type * as ApprovalRunSafetyApi from "../../api/approvalRunSafety";
import { fetchCatalogHierarchy } from "../../api/catalogHierarchy";
import type * as CatalogApi from "../../api/catalogHierarchy";
import {
  ApprovalPendingCountBadge,
  ApprovalQueuePage,
} from "./ApprovalQueuePage";
import {
  APPROVAL_HIERARCHY,
  APPROVAL_ONE_ID,
  makeApprovalDetail,
  makePartiallyApprovedRunSafety,
  makeApprovalRunSafety,
  makeApprovalSummary,
  makeSecondApprovalSummary,
} from "./approvalTestFixtures";
import { verifiedEmailRunState } from "./verifiedEmailRunState";

vi.mock("../../api/approvals", async () => {
  const actual = await vi.importActual<typeof ApprovalsApi>(
    "../../api/approvals",
  );
  return {
    ...actual,
    decideApproval: vi.fn(),
    fetchApprovalDetail: vi.fn(),
    fetchApprovalPage: vi.fn(),
  };
});

vi.mock("../../api/approvalRunSafety", async () => {
  const actual = await vi.importActual<typeof ApprovalRunSafetyApi>(
    "../../api/approvalRunSafety",
  );
  return { ...actual, fetchApprovalRunSafety: vi.fn() };
});

vi.mock("../../api/catalogHierarchy", async () => {
  const actual = await vi.importActual<typeof CatalogApi>(
    "../../api/catalogHierarchy",
  );
  return { ...actual, fetchCatalogHierarchy: vi.fn() };
});

const FIRST_SUMMARY = makeApprovalSummary();
const SECOND_SUMMARY = makeSecondApprovalSummary();
const PENDING_PAGE = Object.freeze({
  items: Object.freeze([FIRST_SUMMARY, SECOND_SUMMARY]),
  nextCursor: null,
});

const PARTIALLY_APPROVED_PAGE = Object.freeze({
  items: Object.freeze([
    makeApprovalSummary({ status: "approved" }),
    SECOND_SUMMARY,
  ]),
  nextCursor: null,
});
const ACTIVE_NOW_MS = Date.parse("2099-08-31T21:00:00Z");

function Providers({
  children,
}: {
  readonly children: ReactNode;
}): React.JSX.Element {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Number.POSITIVE_INFINITY },
      mutations: { retry: false },
    },
  });
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

function renderPage(): ReturnType<typeof render> {
  return render(
    <Providers>
      <ApprovalQueuePage />
    </Providers>,
  );
}

function deferred<T>(): {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
} {
  let resolvePromise: ((value: T) => void) | undefined;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  return {
    promise,
    resolve: (value) => resolvePromise?.(value),
  };
}

const fetchPageMock = vi.mocked(fetchApprovalPage);
const fetchDetailMock = vi.mocked(fetchApprovalDetail);
const decideMock = vi.mocked(decideApproval);
const fetchRunSafetyMock = vi.mocked(fetchApprovalRunSafety);
const fetchHierarchyMock = vi.mocked(fetchCatalogHierarchy);

describe("WEB-05 ApprovalQueuePage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    fetchPageMock.mockResolvedValue(PENDING_PAGE);
    fetchDetailMock.mockResolvedValue(makeApprovalDetail());
    fetchRunSafetyMock.mockResolvedValue(makeApprovalRunSafety());
    fetchHierarchyMock.mockResolvedValue(APPROVAL_HIERARCHY);
  });

  it("defaults to pending and applies truthful raw loaded-page filters", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(
      await screen.findByRole("heading", { level: 1, name: "Approval queue" }),
    ).toBeVisible();
    const status = screen.getByLabelText("Approval status");
    expect(status).toHaveValue("pending");
    await waitFor(() =>
      expect(fetchPageMock).toHaveBeenCalledWith(
        { status: "pending", limit: 25 },
        expect.any(AbortSignal),
      ),
    );
    expect(document.querySelectorAll("article[data-approval-id]")).toHaveLength(
      2,
    );
    expect(
      screen.getByText(/filters apply only to pages already loaded/iu),
    ).toBeVisible();

    const department = screen.getByLabelText("Department");
    await user.selectOptions(department, "email");
    expect(department).toHaveValue("email");
    await waitFor(() =>
      expect(fetchPageMock).toHaveBeenCalledWith(
        { runId: "run.web05.email", limit: 100 },
        expect.any(AbortSignal),
      ),
    );
    expect(
      await screen.findByText(
        "0 mock connector calls until both approvals are approved.",
      ),
    ).toBeVisible();

    const actionType = screen.getByLabelText("Action type");
    await user.selectOptions(actionType, "newsletter.subscribe");
    expect(actionType).toHaveValue("newsletter.subscribe");
    expect(document.querySelectorAll("article[data-approval-id]")).toHaveLength(
      1,
    );
    expect(
      document.querySelector(`article[data-approval-id="${APPROVAL_ONE_ID}"]`),
    ).not.toBeNull();
  });

  it("labels truncation and loads another server page without overstating local filters", async () => {
    const cursor = "approval-page-v1.web05-cursor";
    fetchPageMock
      .mockResolvedValueOnce(
        Object.freeze({
          items: Object.freeze([FIRST_SUMMARY]),
          nextCursor: cursor,
        }),
      )
      .mockResolvedValueOnce(
        Object.freeze({
          items: Object.freeze([SECOND_SUMMARY]),
          nextCursor: null,
        }),
      );
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText(/queue is truncated/iu)).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Load more approvals" }),
    );
    await waitFor(() =>
      expect(fetchPageMock).toHaveBeenCalledWith(
        { status: "pending", limit: 25, cursor },
        expect.any(AbortSignal),
      ),
    );
    expect(
      await screen.findByText(/server returned no next cursor/iu),
    ).toBeVisible();
    expect(document.querySelectorAll("article[data-approval-id]")).toHaveLength(
      2,
    );
  });

  it("distinguishes loading, empty, unavailable, and lower-bound pending-count states", async () => {
    const pageRequest =
      deferred<Awaited<ReturnType<typeof fetchApprovalPage>>>();
    fetchPageMock.mockReturnValue(pageRequest.promise);
    const page = renderPage();

    expect(await screen.findByText("Loading approval requests")).toBeVisible();
    pageRequest.resolve(
      Object.freeze({ items: Object.freeze([]), nextCursor: null }),
    );
    expect(
      await screen.findByText("No loaded approvals match these filters."),
    ).toBeVisible();
    page.unmount();

    fetchPageMock.mockRejectedValue(
      new ApprovalRequestError(
        503,
        "approval_service_unavailable",
        "Approval service unavailable.",
      ),
    );
    const unavailablePage = renderPage();
    expect(
      await screen.findByText("The approval queue is unavailable"),
    ).toBeVisible();
    expect(screen.getByText("Approval service unavailable.")).toBeVisible();
    unavailablePage.unmount();

    fetchPageMock.mockResolvedValue(
      Object.freeze({
        items: Object.freeze([FIRST_SUMMARY]),
        nextCursor: "approval-page-v1.more-pending",
      }),
    );
    render(
      <Providers>
        <ApprovalPendingCountBadge />
      </Providers>,
    );
    expect(
      await screen.findByRole("status", {
        name: "1 or more pending approvals",
      }),
    ).toHaveTextContent("1+");
  });

  it("refetches exact detail after a conflict and never presents optimistic success", async () => {
    const refreshed = makeApprovalDetail({
      generation: 2,
      payloadHash: "d".repeat(64),
    });
    let detailCalls = 0;
    fetchDetailMock.mockImplementation(() => {
      detailCalls += 1;
      return Promise.resolve(
        detailCalls === 1 ? makeApprovalDetail() : refreshed,
      );
    });
    decideMock.mockRejectedValue(
      new ApprovalRequestError(
        409,
        "approval_decision_conflict",
        "Approval changed.",
        2,
      ),
    );
    const user = userEvent.setup();
    renderPage();

    await user.click(
      await screen.findByRole("button", {
        name: `Review approval ${APPROVAL_ONE_ID}`,
      }),
    );
    const panel = await screen.findByRole("complementary");
    expect(within(panel).getByText("a".repeat(64))).toBeVisible();
    expect(
      within(panel).getAllByText("newsletter.subscribe").length,
    ).toBeGreaterThan(0);
    expect(
      within(panel).getByLabelText("Redacted payload JSON"),
    ).toHaveTextContent('"email": "[REDACTED]"');

    await user.click(within(panel).getByRole("button", { name: "Approve" }));
    await user.click(
      screen.getByRole("button", { name: "Approve exact action" }),
    );

    expect(
      await screen.findByRole("alert", {
        name: "",
      }),
    ).toHaveTextContent(/authoritative details were refetched/iu);
    expect(within(panel).getByText("d".repeat(64))).toBeVisible();
    expect(within(panel).getAllByText("Pending").length).toBeGreaterThan(0);
    expect(
      screen.queryByText(/approval recorded as/iu),
    ).not.toBeInTheDocument();
    expect(decideMock).toHaveBeenCalledWith({
      approvalId: APPROVAL_ONE_ID,
      decision: "approve",
      expectedGeneration: 1,
      expectedPayloadHash: "a".repeat(64),
      expectedActionId: "action.web05.email.newsletter",
      expectedRunId: "run.web05.email",
    });
    expect(detailCalls).toBeGreaterThanOrEqual(2);
  });

  it("waits for the mutation and authoritative refetch before reporting a recorded decision", async () => {
    const decision = deferred<Awaited<ReturnType<typeof decideApproval>>>();
    decideMock.mockReturnValue(decision.promise);
    let detailCalls = 0;
    fetchDetailMock.mockImplementation(() => {
      detailCalls += 1;
      if (detailCalls === 1) return Promise.resolve(makeApprovalDetail());
      if (detailCalls === 2) {
        return Promise.resolve(
          makeApprovalDetail({ status: "approved", isActionable: false }),
        );
      }
      return Promise.resolve(
        makeApprovalDetail({
          status: "consumed",
          generation: 2,
          payloadHash: "d".repeat(64),
        }),
      );
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(
      await screen.findByRole("button", {
        name: `Review approval ${APPROVAL_ONE_ID}`,
      }),
    );
    const panel = await screen.findByRole("complementary");
    await user.click(within(panel).getByRole("button", { name: "Approve" }));
    await user.click(
      screen.getByRole("button", { name: "Approve exact action" }),
    );
    expect(
      screen.getByText(/recording the decision with the server/iu),
    ).toBeVisible();
    expect(
      screen.queryByText(/approval recorded as/iu),
    ).not.toBeInTheDocument();

    decision.resolve({
      approvalId: APPROVAL_ONE_ID,
      decisionId: "decision.web05.01",
      actionId: "action.web05.email.newsletter",
      runId: "run.web05.email",
      status: "approved",
      approval: null,
    });
    expect(
      await screen.findByText(/approval recorded as approved/iu),
    ).toBeVisible();
    expect(screen.getByText(/completion is not reported/iu)).toBeVisible();
    expect(detailCalls).toBe(2);
    await waitFor(() => expect(panel).toHaveFocus());
  });

  it("WEB-08 restores focus to the status filter when a decided row leaves the queue", async () => {
    fetchPageMock.mockResolvedValueOnce(PENDING_PAGE).mockResolvedValue(
      Object.freeze({
        items: Object.freeze([SECOND_SUMMARY]),
        nextCursor: null,
      }),
    );
    decideMock.mockResolvedValue({
      approvalId: APPROVAL_ONE_ID,
      decisionId: "decision.web05.01",
      actionId: "action.web05.email.newsletter",
      runId: "run.web05.email",
      status: "approved",
      approval: null,
    });
    let detailCalls = 0;
    fetchDetailMock.mockImplementation(() => {
      detailCalls += 1;
      return Promise.resolve(
        detailCalls === 1
          ? makeApprovalDetail()
          : makeApprovalDetail({ status: "approved", isActionable: false }),
      );
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(
      await screen.findByRole("button", {
        name: `Review approval ${APPROVAL_ONE_ID}`,
      }),
    );
    const panel = await screen.findByRole("complementary");
    await user.click(within(panel).getByRole("button", { name: "Approve" }));
    await user.click(
      screen.getByRole("button", { name: "Approve exact action" }),
    );
    await screen.findByText(/approval recorded as approved/iu);
    await waitFor(() =>
      expect(
        screen.queryByRole("button", {
          name: `Review approval ${APPROVAL_ONE_ID}`,
        }),
      ).not.toBeInTheDocument(),
    );

    await user.click(
      within(panel).getByRole("button", { name: "Close approval review" }),
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Approval status")).toHaveFocus(),
    );
  });

  it("withholds success when the authoritative refetch drifts from the exact reviewed action", async () => {
    decideMock.mockResolvedValue({
      approvalId: APPROVAL_ONE_ID,
      decisionId: "decision.web05.01",
      actionId: "action.web05.email.newsletter",
      runId: "run.web05.email",
      status: "approved",
      approval: null,
    });
    let detailCalls = 0;
    fetchDetailMock.mockImplementation(() => {
      detailCalls += 1;
      return Promise.resolve(
        detailCalls === 1
          ? makeApprovalDetail()
          : makeApprovalDetail({
              status: "approved",
              isActionable: false,
              generation: 2,
              payloadHash: "d".repeat(64),
            }),
      );
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(
      await screen.findByRole("button", {
        name: `Review approval ${APPROVAL_ONE_ID}`,
      }),
    );
    const panel = await screen.findByRole("complementary");
    await user.click(within(panel).getByRole("button", { name: "Approve" }));
    await user.click(
      screen.getByRole("button", { name: "Approve exact action" }),
    );

    expect(
      await screen.findByText(/identity or state did not confirm/iu),
    ).toBeVisible();
    expect(
      screen.queryByText(/approval recorded as/iu),
    ).not.toBeInTheDocument();
    expect(within(panel).getByText("d".repeat(64))).toBeVisible();
    await waitFor(() => expect(panel).toHaveFocus());
  });

  it("withholds the Email zero-call claim when authoritative run evidence is false", async () => {
    fetchRunSafetyMock.mockResolvedValue(makeApprovalRunSafety(false));
    const user = userEvent.setup();
    renderPage();
    await waitFor(() =>
      expect(screen.getByRole("option", { name: "Email" })).toBeVisible(),
    );
    await user.selectOptions(screen.getByLabelText("Department"), "email");

    expect(
      await screen.findByRole("heading", {
        name: "Email run safety unavailable",
      }),
    ).toBeVisible();
    expect(
      screen.queryByText(/0 mock connector calls/iu),
    ).not.toBeInTheDocument();
  });

  it("keeps the exact two-action barrier after one approval and rejects semantic drift", () => {
    const partiallyApproved = makePartiallyApprovedRunSafety();
    expect(
      verifiedEmailRunState(
        partiallyApproved,
        PARTIALLY_APPROVED_PAGE,
        false,
        ACTIVE_NOW_MS,
      ),
    ).toMatchObject({
      status: "confirmed-zero",
      approvals: [
        { actionType: "crm.upsert-contact", status: "pending" },
        { actionType: "newsletter.subscribe", status: "approved" },
      ],
    });

    const base = makeApprovalRunSafety();
    const firstAction = base.externalActions[0];
    const secondAction = base.externalActions[1];
    const firstPending = base.pendingApprovals[0];
    const secondPending = base.pendingApprovals[1];
    if (
      firstAction === undefined ||
      secondAction === undefined ||
      firstPending === undefined ||
      secondPending === undefined
    ) {
      throw new Error("WEB-05 semantic-drift fixture is incomplete");
    }
    const variants = [
      Object.freeze({ ...base, approvalRequired: false }),
      Object.freeze({ ...base, state: "executing" as const }),
      Object.freeze({
        ...base,
        externalActions: Object.freeze([
          Object.freeze({ ...firstAction, actionType: "social.publish" }),
          secondAction,
        ]),
      }),
      Object.freeze({
        ...base,
        externalActions: Object.freeze([
          Object.freeze({ ...firstAction, state: "succeeded" as const }),
          secondAction,
        ]),
      }),
      Object.freeze({
        ...base,
        pendingApprovals: Object.freeze([
          Object.freeze({ ...firstPending, isExpired: true }),
          secondPending,
        ]),
      }),
      Object.freeze({
        ...base,
        pendingApprovals: Object.freeze([
          Object.freeze({
            ...firstPending,
            actionId: SECOND_SUMMARY.actionId,
          }),
          secondPending,
        ]),
      }),
      Object.freeze({
        ...base,
        externalActions: Object.freeze([
          Object.freeze({
            ...firstAction,
            receiptId: "receipt.web05.unexpected",
          }),
          secondAction,
        ]),
      }),
    ];
    for (const safety of variants) {
      expect(
        verifiedEmailRunState(safety, PENDING_PAGE, false, ACTIVE_NOW_MS),
      ).toEqual({ status: "unavailable" });
    }
    expect(
      verifiedEmailRunState(
        base,
        Object.freeze({
          items: PENDING_PAGE.items,
          nextCursor: "approval-page-v1.truncated",
        }),
        false,
        ACTIVE_NOW_MS,
      ),
    ).toEqual({ status: "unavailable" });
    expect(
      verifiedEmailRunState(
        base,
        PENDING_PAGE,
        false,
        Date.parse("2099-08-31T22:00:00Z"),
      ),
    ).toEqual({ status: "unavailable" });
  });
});

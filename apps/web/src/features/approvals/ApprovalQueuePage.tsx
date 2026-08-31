import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useMemo, useRef, useState } from "react";

import {
  approvalDetailQueryKey,
  APPROVAL_LIST_QUERY_ROOT,
  approvalListQueryKey,
  APPROVAL_STATUSES,
  APPROVALS_QUERY_ROOT,
  ApprovalRequestError,
  decideApproval,
  fetchApprovalDetail,
  fetchApprovalPage,
  type ApprovalDecisionKind,
  type ApprovalDecisionResult,
  type ApprovalDetail,
  type ApprovalStatus,
  type ApprovalSummary,
} from "../../api/approvals";
import {
  approvalRunSafetyQueryKey,
  APPROVAL_RUN_SAFETY_QUERY_ROOT,
  fetchApprovalRunSafety,
} from "../../api/approvalRunSafety";
import {
  CATALOG_HIERARCHY_QUERY_KEY,
  fetchCatalogHierarchy,
} from "../../api/catalogHierarchy";
import { useMediaQuery } from "../../accessibility/useMediaQuery";
import { ApprovalDecisionDialog } from "./ApprovalDecisionDialog";
import { ApprovalQueue } from "./ApprovalQueue";
import {
  ApprovalEmailRunSafety,
  ApprovalReviewPanel,
} from "./ApprovalReviewPanel";
import {
  APPROVAL_STATUS_LABELS,
  approvalDecisionDisabledReason,
  approvalDepartmentLookup,
  isEmailApproval,
  sortApprovalsPendingFirst,
} from "./approvalView";
import { usePendingApprovalCount } from "./usePendingApprovalCount";
import { useExpiryClock } from "./useExpiryClock";
import { verifiedEmailRunState } from "./verifiedEmailRunState";
import "./approval-queue.css";

const PAGE_SIZE = 25;
const EMAIL_RUN_APPROVAL_LIMIT = 100;
const APPROVAL_REVIEW_SHEET_MEDIA_QUERY = "(max-width: 900px)";

interface DecisionRequest {
  readonly approval: ApprovalDetail;
  readonly decision: ApprovalDecisionKind;
}

type DecisionFeedback =
  | Readonly<{ kind: "success"; message: string }>
  | Readonly<{ kind: "conflict"; message: string }>
  | Readonly<{ kind: "error"; message: string }>;

function uniqueApprovalItems(
  items: readonly ApprovalSummary[],
): readonly ApprovalSummary[] {
  const byId = new Map<string, ApprovalSummary>();
  for (const item of items) byId.set(item.id, item);
  return [...byId.values()];
}

function authoritativeDecisionMatches(
  reviewed: ApprovalDetail,
  decision: ApprovalDecisionKind,
  result: ApprovalDecisionResult,
  authoritative: ApprovalDetail,
): boolean {
  const expectedStatus = decision === "approve" ? "approved" : "rejected";
  const statusMatches =
    decision === "approve"
      ? authoritative.status === "approved" ||
        authoritative.status === "consumed"
      : authoritative.status === "rejected";
  return (
    result.approvalId === reviewed.id &&
    result.actionId === reviewed.actionId &&
    result.runId === reviewed.runId &&
    result.status === expectedStatus &&
    authoritative.id === reviewed.id &&
    authoritative.actionId === reviewed.actionId &&
    authoritative.runId === reviewed.runId &&
    authoritative.generation === reviewed.generation &&
    authoritative.payloadHash === reviewed.payloadHash &&
    authoritative.decisionId === result.decisionId &&
    authoritative.decisionKind === decision &&
    statusMatches
  );
}

export function ApprovalPendingCountBadge(): React.JSX.Element | null {
  const state = usePendingApprovalCount();
  if (state.isError) return null;
  return (
    <span
      className="approval-pending-count-badge"
      role="status"
      aria-label={state.isPending ? "Loading pending approvals" : state.label}
    >
      {state.isPending
        ? "…"
        : state.truncated
          ? `${String(state.count)}+`
          : state.count}
    </span>
  );
}

export function ApprovalQueuePage(): React.JSX.Element {
  const queryClient = useQueryClient();
  const [statusFilter, setStatusFilter] = useState<ApprovalStatus | "">(
    "pending",
  );
  const [departmentFilter, setDepartmentFilter] = useState("");
  const [actionTypeFilter, setActionTypeFilter] = useState("");
  const [selectedApprovalId, setSelectedApprovalId] = useState<string | null>(
    null,
  );
  const [decisionRequest, setDecisionRequest] =
    useState<DecisionRequest | null>(null);
  const [decisionPending, setDecisionPending] = useState(false);
  const [feedback, setFeedback] = useState<DecisionFeedback | null>(null);
  const reviewTriggerIdRef = useRef<string | null>(null);
  const statusFilterRef = useRef<HTMLSelectElement>(null);
  const reviewIsModal = useMediaQuery(APPROVAL_REVIEW_SHEET_MEDIA_QUERY);

  const listBaseQuery = useMemo(
    () => ({
      ...(statusFilter === "" ? {} : { status: statusFilter }),
      limit: PAGE_SIZE,
    }),
    [statusFilter],
  );
  const approvalsQuery = useInfiniteQuery({
    queryKey: approvalListQueryKey(listBaseQuery),
    queryFn: ({ pageParam, signal }) =>
      fetchApprovalPage(
        {
          ...listBaseQuery,
          ...(pageParam === null ? {} : { cursor: pageParam }),
        },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.nextCursor,
    retry: false,
    refetchOnWindowFocus: false,
  });
  const hierarchyQuery = useQuery({
    queryKey: CATALOG_HIERARCHY_QUERY_KEY,
    queryFn: ({ signal }) => fetchCatalogHierarchy(signal),
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: Number.POSITIVE_INFINITY,
    refetchOnWindowFocus: false,
  });
  const detailQuery = useQuery({
    queryKey:
      selectedApprovalId === null
        ? [...APPROVALS_QUERY_ROOT, "detail", "none"]
        : approvalDetailQueryKey(selectedApprovalId),
    queryFn: ({ signal }) => {
      if (selectedApprovalId === null) {
        throw new Error("An approval is required to load review details.");
      }
      return fetchApprovalDetail(selectedApprovalId, signal);
    },
    enabled: selectedApprovalId !== null,
    retry: false,
    refetchOnWindowFocus: false,
  });

  const departments = useMemo(
    () => approvalDepartmentLookup(hierarchyQuery.data),
    [hierarchyQuery.data],
  );
  const loadedItems = useMemo(
    () =>
      sortApprovalsPendingFirst(
        uniqueApprovalItems(
          approvalsQuery.data?.pages.flatMap((page) => page.items) ?? [],
        ),
      ),
    [approvalsQuery.data],
  );
  const departmentOptions = useMemo(() => {
    const values = new Map<string, string>();
    for (const item of loadedItems) {
      const department = departments.get(item.instanceId);
      if (department !== undefined) {
        values.set(department.filterValue, department.displayName);
      }
    }
    return [...values].sort((left, right) => left[1].localeCompare(right[1]));
  }, [departments, loadedItems]);
  const actionTypeOptions = useMemo(
    () => [...new Set(loadedItems.map((item) => item.actionType))].sort(),
    [loadedItems],
  );
  const visibleItems = useMemo(
    () =>
      loadedItems.filter((item) => {
        const department = departments.get(item.instanceId);
        return (
          (departmentFilter === "" ||
            department?.filterValue === departmentFilter) &&
          (actionTypeFilter === "" || item.actionType === actionTypeFilter)
        );
      }),
    [actionTypeFilter, departmentFilter, departments, loadedItems],
  );
  const selectedSummary = loadedItems.find(
    (item) => item.id === selectedApprovalId,
  );
  const selectedApproval = detailQuery.data ?? selectedSummary;
  const emailRunId = useMemo(() => {
    if (
      selectedApproval !== undefined &&
      isEmailApproval(selectedApproval, departments)
    ) {
      return selectedApproval.runId;
    }
    if (departmentFilter !== "email") return null;
    const runIds = new Set(
      loadedItems
        .filter((item) => isEmailApproval(item, departments))
        .map((item) => item.runId),
    );
    return runIds.size === 1 ? ([...runIds][0] ?? null) : null;
  }, [departmentFilter, departments, loadedItems, selectedApproval]);
  const runApprovalSetQuery = useQuery({
    queryKey:
      emailRunId === null
        ? [...APPROVALS_QUERY_ROOT, "email-run", "none"]
        : approvalListQueryKey({
            runId: emailRunId,
            limit: EMAIL_RUN_APPROVAL_LIMIT,
          }),
    queryFn: ({ signal }) => {
      if (emailRunId === null) {
        throw new Error("An Email run is required to load its approval set.");
      }
      return fetchApprovalPage(
        { runId: emailRunId, limit: EMAIL_RUN_APPROVAL_LIMIT },
        signal,
      );
    },
    enabled: emailRunId !== null,
    retry: false,
    refetchOnWindowFocus: false,
  });
  const runSafetyQuery = useQuery({
    queryKey:
      emailRunId === null
        ? [...APPROVAL_RUN_SAFETY_QUERY_ROOT, "none"]
        : approvalRunSafetyQueryKey(emailRunId),
    queryFn: ({ signal }) => {
      if (emailRunId === null) {
        throw new Error("An Email run is required to load approval safety.");
      }
      return fetchApprovalRunSafety(emailRunId, signal);
    },
    enabled: emailRunId !== null,
    retry: false,
    refetchOnWindowFocus: false,
  });
  const emailExpiryNowMs = useExpiryClock(
    runApprovalSetQuery.data?.items.map((approval) => approval.expiresAt) ?? [],
  );
  const emailRunSafety =
    emailRunId === null
      ? null
      : verifiedEmailRunState(
          runSafetyQuery.data,
          runApprovalSetQuery.data,
          runSafetyQuery.isPending || runApprovalSetQuery.isPending,
          emailExpiryNowMs,
        );

  const resetLocalSelection = (): void => {
    setSelectedApprovalId(null);
    setDecisionRequest(null);
    setFeedback(null);
  };

  const closeReview = (): void => {
    const approvalId = reviewTriggerIdRef.current;
    setSelectedApprovalId(null);
    setDecisionRequest(null);
    setFeedback(null);
    requestAnimationFrame(() => {
      const trigger =
        approvalId === null
          ? null
          : document.getElementById(`approval-review-trigger-${approvalId}`);
      if (trigger instanceof HTMLElement) {
        trigger.focus();
      } else {
        statusFilterRef.current?.focus();
      }
    });
  };

  const recordDecision = async (): Promise<void> => {
    if (decisionRequest === null || decisionPending) return;
    const { approval, decision } = decisionRequest;
    const disabledReason = approvalDecisionDisabledReason(approval, Date.now());
    if (disabledReason !== null) {
      setDecisionRequest(null);
      setFeedback({
        kind: "error",
        message: `The decision was not sent. ${disabledReason} Refresh the authoritative request before deciding.`,
      });
      return;
    }
    setDecisionPending(true);
    setFeedback(null);
    try {
      const result = await decideApproval({
        approvalId: approval.id,
        decision,
        expectedGeneration: approval.generation,
        expectedPayloadHash: approval.payloadHash,
        expectedActionId: approval.actionId,
        expectedRunId: approval.runId,
      });
      const authoritative = await fetchApprovalDetail(approval.id);
      queryClient.setQueryData(
        approvalDetailQueryKey(approval.id),
        authoritative,
      );
      await queryClient.invalidateQueries({
        queryKey: APPROVAL_LIST_QUERY_ROOT,
      });
      await queryClient.invalidateQueries({
        queryKey: APPROVAL_RUN_SAFETY_QUERY_ROOT,
      });
      if (
        !authoritativeDecisionMatches(approval, decision, result, authoritative)
      ) {
        setFeedback({
          kind: "error",
          message:
            "The decision response returned, but the refetched authoritative approval identity or state did not confirm the exact reviewed action. Review the current state before trying again.",
        });
      } else {
        setFeedback({
          kind: "success",
          message: `Approval recorded as ${APPROVAL_STATUS_LABELS[authoritative.status].toLocaleLowerCase()} in the refetched authoritative state. Connector action completion is not reported here.`,
        });
      }
    } catch (error) {
      const conflict =
        error instanceof ApprovalRequestError && error.status === 409;
      let refetched = false;
      try {
        const authoritative = await fetchApprovalDetail(approval.id);
        queryClient.setQueryData(
          approvalDetailQueryKey(approval.id),
          authoritative,
        );
        refetched = true;
      } catch {
        // The decision error remains the primary, user-visible result.
      }
      await queryClient.invalidateQueries({
        queryKey: APPROVAL_LIST_QUERY_ROOT,
      });
      await queryClient.invalidateQueries({
        queryKey: APPROVAL_RUN_SAFETY_QUERY_ROOT,
      });
      if (conflict) {
        setFeedback({
          kind: "conflict",
          message: refetched
            ? "Approval changed before this decision was recorded. Authoritative details were refetched; review the refreshed exact action before deciding again."
            : "Approval changed before this decision was recorded, and authoritative details could not be refetched. Reload the queue before deciding.",
        });
      } else {
        setFeedback({
          kind: "error",
          message: refetched
            ? "The decision was not confirmed. Authoritative details were refetched; review the current approval state before trying again."
            : "The decision was not confirmed, and authoritative details could not be refetched. Reload the queue before trying again.",
        });
      }
    } finally {
      setDecisionRequest(null);
      setDecisionPending(false);
    }
  };

  return (
    <main className="approval-page">
      <header className="approval-page__heading">
        <div>
          <h1>Approval queue</h1>
          <p>
            Review the server-provided safe projection of each immutable action.
            Recording a decision never implies that a connector completed it.
          </p>
        </div>
        <div className="approval-page__count" aria-live="polite">
          <strong>{visibleItems.length}</strong>
          <span>Matching loaded approvals</span>
        </div>
      </header>

      <section className="approval-filters" aria-label="Approval filters">
        <label>
          Approval status
          <select
            ref={statusFilterRef}
            value={statusFilter}
            onChange={(event) => {
              setStatusFilter(event.currentTarget.value as ApprovalStatus | "");
              setDepartmentFilter("");
              setActionTypeFilter("");
              resetLocalSelection();
            }}
          >
            <option value="">All statuses</option>
            {APPROVAL_STATUSES.map((status) => (
              <option key={status} value={status}>
                {APPROVAL_STATUS_LABELS[status]}
              </option>
            ))}
          </select>
        </label>
        <label>
          Department
          <select
            value={departmentFilter}
            onChange={(event) => {
              setDepartmentFilter(event.currentTarget.value);
              resetLocalSelection();
            }}
          >
            <option value="">All loaded departments</option>
            {departmentOptions.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Action type
          <select
            value={actionTypeFilter}
            onChange={(event) => {
              setActionTypeFilter(event.currentTarget.value);
              resetLocalSelection();
            }}
          >
            <option value="">All loaded action types</option>
            {actionTypeOptions.map((actionType) => (
              <option key={actionType} value={actionType}>
                {actionType}
              </option>
            ))}
          </select>
        </label>
        <p className="approval-filters__scope">
          Department and action-type filters apply only to pages already loaded
          in this browser. Status is requested from the server.
        </p>
      </section>

      {feedback === null ? null : (
        <p
          className={`approval-page__feedback is-${feedback.kind}`}
          role={feedback.kind === "conflict" ? "alert" : "status"}
        >
          {feedback.message}
        </p>
      )}

      <section className="approval-page__body" aria-label="Approval workspace">
        <div className="approval-page__list-pane">
          <p className="approval-page__loaded-summary" aria-live="polite">
            {String(visibleItems.length)} matching of{" "}
            {String(loadedItems.length)} approval requests loaded across{" "}
            {String(approvalsQuery.data?.pages.length ?? 0)}{" "}
            {approvalsQuery.data?.pages.length === 1 ? "page" : "pages"}.
          </p>

          {emailRunSafety === null || selectedApprovalId !== null ? null : (
            <ApprovalEmailRunSafety state={emailRunSafety} />
          )}

          {approvalsQuery.isPending ? (
            <section className="approval-page__state" aria-live="polite">
              <strong>Loading approval requests</strong>
              <p>The pending queue is the default server-side view.</p>
            </section>
          ) : null}
          {approvalsQuery.isError ? (
            <section className="approval-page__state is-error" role="alert">
              <strong>The approval queue is unavailable</strong>
              <p>{approvalsQuery.error.message}</p>
              <button
                type="button"
                className="approval-page__retry"
                onClick={() => void approvalsQuery.refetch()}
              >
                Try again
              </button>
            </section>
          ) : null}
          {approvalsQuery.data === undefined ? null : (
            <ApprovalQueue
              items={visibleItems}
              departments={departments}
              selectedApprovalId={selectedApprovalId}
              onReview={(approvalId) => {
                reviewTriggerIdRef.current = approvalId;
                setSelectedApprovalId(approvalId);
                setDecisionRequest(null);
                setFeedback(null);
              }}
            />
          )}

          {approvalsQuery.data === undefined ? null : (
            <footer className="approval-page__pagination">
              {approvalsQuery.hasNextPage ? (
                <>
                  <p>
                    This queue is truncated. Load the next server page before
                    treating loaded-page filters as a complete result.
                  </p>
                  <button
                    type="button"
                    className="approval-page__load-more"
                    disabled={approvalsQuery.isFetchingNextPage}
                    onClick={() => void approvalsQuery.fetchNextPage()}
                  >
                    {approvalsQuery.isFetchingNextPage
                      ? "Loading next page…"
                      : "Load more approvals"}
                  </button>
                </>
              ) : (
                <p>
                  The server returned no next cursor. All pages for the selected
                  status are loaded.
                </p>
              )}
            </footer>
          )}
        </div>

        {selectedApprovalId === null ? (
          <section className="approval-review-placeholder">
            <strong>Select an approval to review its exact action.</strong>
            <p>
              Full details are loaded separately so list summaries are never
              treated as decision evidence.
            </p>
          </section>
        ) : detailQuery.isPending ? (
          <section className="approval-review-placeholder" aria-live="polite">
            <strong>Loading exact action details</strong>
          </section>
        ) : detailQuery.isError ? (
          <section
            className="approval-review-placeholder is-error"
            role="alert"
          >
            <strong>Approval details are unavailable</strong>
            <p>{detailQuery.error.message}</p>
            <button
              type="button"
              className="approval-page__retry"
              onClick={() => void detailQuery.refetch()}
            >
              Try again
            </button>
          </section>
        ) : (
          <ApprovalReviewPanel
            approval={detailQuery.data}
            department={departments.get(detailQuery.data.instanceId)}
            emailRunSafety={
              isEmailApproval(detailQuery.data, departments)
                ? (emailRunSafety ?? Object.freeze({ status: "unavailable" }))
                : null
            }
            modal={reviewIsModal}
            onClose={closeReview}
            onRequestDecision={(decision) => {
              setDecisionRequest({ approval: detailQuery.data, decision });
              setFeedback(null);
            }}
          />
        )}
      </section>

      {decisionRequest === null ? null : (
        <ApprovalDecisionDialog
          approval={decisionRequest.approval}
          decision={decisionRequest.decision}
          pending={decisionPending}
          fallbackFocusId="approval-review-panel"
          onCancel={() => setDecisionRequest(null)}
          onConfirm={() => void recordDecision()}
        />
      )}
    </main>
  );
}

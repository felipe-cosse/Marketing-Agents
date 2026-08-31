import { useQuery } from "@tanstack/react-query";

import { approvalListQueryKey, fetchApprovalPage } from "../../api/approvals";
import { pendingCountLabel } from "./approvalView";

const PENDING_COUNT_LIMIT = 100;

export interface PendingApprovalCountState {
  readonly count: number;
  readonly truncated: boolean;
  readonly label: string;
  readonly isPending: boolean;
  readonly isError: boolean;
}

export function usePendingApprovalCount(): PendingApprovalCountState {
  const query = useQuery({
    queryKey: approvalListQueryKey({
      status: "pending",
      limit: PENDING_COUNT_LIMIT,
    }),
    queryFn: ({ signal }) =>
      fetchApprovalPage(
        { status: "pending", limit: PENDING_COUNT_LIMIT },
        signal,
      ),
    retry: false,
    refetchOnWindowFocus: false,
  });
  const count = query.data?.items.length ?? 0;
  const truncated = query.data !== undefined && query.data.nextCursor !== null;
  return Object.freeze({
    count,
    truncated,
    label: pendingCountLabel(count, truncated),
    isPending: query.isPending,
    isError: query.isError,
  });
}

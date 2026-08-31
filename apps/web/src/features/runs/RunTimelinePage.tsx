import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import {
  fetchRunResource,
  fetchRunTimelinePage,
  runResourceQueryKey,
  runTimelineQueryKey,
  type RunTimelinePage,
} from "../../api/runArtifacts";
import { RunArtifactsPanel } from "./RunArtifactsPanel";
import { RunExecutionSnapshot } from "./RunExecutionSnapshot";
import { RunTimeline } from "./RunTimeline";
import {
  isTerminalRunState,
  humanizeRuntimeValue,
  mergeTimelinePages,
  runRefreshInterval,
  type TimelineSnapshot,
} from "./timelineModel";
import "./run-timeline.css";

const TIMELINE_PAGE_SIZE = 100;
const RESOURCE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$/u;

interface SnapshotResult {
  readonly snapshot: TimelineSnapshot | null;
  readonly error: string | null;
}

function timelineSnapshot(
  runId: string,
  pages: readonly RunTimelinePage[] | undefined,
): SnapshotResult {
  if (pages === undefined) return { snapshot: null, error: null };
  try {
    return { snapshot: mergeTimelinePages(runId, pages), error: null };
  } catch {
    return {
      snapshot: null,
      error:
        "The local API returned timeline pages with an incoherent run binding or sequence.",
    };
  }
}

export function RunTimelinePage(): React.JSX.Element {
  const queryClient = useQueryClient();
  const { hash } = useLocation();
  const { runId: routeRunId } = useParams<{ readonly runId: string }>();
  const runId = routeRunId ?? "";
  const validRunId = RESOURCE_ID_PATTERN.test(runId);
  const runQuery = useQuery({
    queryKey: runResourceQueryKey(validRunId ? runId : "invalid"),
    queryFn: ({ signal }) => fetchRunResource(runId, signal),
    enabled: validRunId,
    retry: false,
    refetchInterval: (query) =>
      runRefreshInterval(query.state.data?.state, query.state.error !== null),
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
  });
  const timelineBaseQuery = { limit: TIMELINE_PAGE_SIZE } as const;
  const timelineQuery = useInfiniteQuery({
    queryKey: runTimelineQueryKey(
      validRunId ? runId : "invalid",
      timelineBaseQuery,
    ),
    queryFn: ({ pageParam, signal }) =>
      fetchRunTimelinePage(
        runId,
        {
          ...timelineBaseQuery,
          ...(pageParam === null ? {} : { cursor: pageParam }),
        },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.nextCursor,
    enabled: validRunId,
    retry: false,
    refetchInterval: (query) =>
      runRefreshInterval(runQuery.data?.state, query.state.error !== null),
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
  });
  const priorStateRef = useRef(runQuery.data?.state);
  const artifactQueryPrefix = useMemo(
    () => ["runs", "artifacts", runId] as const,
    [runId],
  );
  const fragmentKey = `${runId}|${hash}`;
  const fragmentFocusRef = useRef({ key: "", complete: false });
  useEffect(() => {
    const current = runQuery.data?.state;
    const previous = priorStateRef.current;
    priorStateRef.current = current;
    if (
      current !== undefined &&
      isTerminalRunState(current) &&
      (previous === undefined || !isTerminalRunState(previous))
    ) {
      void timelineQuery.refetch();
      void queryClient.refetchQueries({
        queryKey: artifactQueryPrefix,
        type: "active",
      });
    }
  }, [artifactQueryPrefix, queryClient, runQuery.data?.state, timelineQuery]);
  useEffect(() => {
    if (fragmentFocusRef.current.key !== fragmentKey) {
      fragmentFocusRef.current = { key: fragmentKey, complete: false };
    }
    if (fragmentFocusRef.current.complete) return;
    if (runQuery.data === undefined || hash.length < 2) return;
    let targetId: string;
    try {
      targetId = decodeURIComponent(hash.slice(1));
    } catch {
      return;
    }
    const target = document.getElementById(targetId);
    if (target === null) return;
    target.scrollIntoView({ block: "center" });
    target.focus({ preventScroll: true });
    fragmentFocusRef.current = { key: fragmentKey, complete: true };
  }, [fragmentKey, hash, runQuery.data, timelineQuery.data]);
  const merged = useMemo(
    () => timelineSnapshot(runId, timelineQuery.data?.pages),
    [runId, timelineQuery.data?.pages],
  );

  if (!validRunId) {
    return (
      <main className="run-page">
        <section className="run-workspace__state is-error" role="alert">
          <strong>The run link is invalid</strong>
          <p>Open a bounded run resource ID from the runs index.</p>
          <Link to="/runs">Return to runs</Link>
        </section>
      </main>
    );
  }

  if (runQuery.isPending) {
    return (
      <main className="run-page">
        <section className="run-workspace__state" aria-live="polite">
          <strong>Loading run {runId}</strong>
          <p>Reading the coherent private runtime projection.</p>
        </section>
      </main>
    );
  }

  if (runQuery.isError) {
    return (
      <main className="run-page">
        <section className="run-workspace__state is-error" role="alert">
          <strong>The run is unavailable</strong>
          <p>{runQuery.error.message}</p>
          <button type="button" onClick={() => void runQuery.refetch()}>
            Try again
          </button>
          <p>
            <Link to="/runs">Return to runs</Link>
          </p>
        </section>
      </main>
    );
  }

  const run = runQuery.data;
  const terminal = isTerminalRunState(run.state);
  return (
    <main className="run-page">
      <header className="run-page__heading">
        <div>
          <span className="run-section-kicker">Run {run.id}</span>
          <h1>Run timeline</h1>
          <p>
            Events are presented in the API&apos;s persisted run-sequence order,
            never reconstructed from timestamps. Generated metadata and
            artifacts remain untrusted and inert in this interface.
          </p>
          <p>
            <Link to="/runs">← All runs</Link>
          </p>
        </div>
        <div className="run-page__live-state">
          <strong aria-live="polite">
            {terminal
              ? "Terminal snapshot — polling stopped"
              : `Run ${humanizeRuntimeValue(run.state)} — live monitoring active`}
          </strong>
          <span>
            {terminal
              ? "Use Refresh if you need to re-read the immutable projection."
              : timelineQuery.isFetching
                ? "Refreshing durable events."
                : "Refreshes while this tab is visible; errors use bounded backoff."}
          </span>
          <button
            type="button"
            onClick={() => {
              void runQuery.refetch();
              void timelineQuery.refetch();
              void queryClient.refetchQueries({
                queryKey: artifactQueryPrefix,
                type: "active",
              });
            }}
          >
            Refresh now
          </button>
        </div>
      </header>

      <div className="run-page__layout">
        <aside className="run-page__sidebar" aria-label="Run details">
          <RunExecutionSnapshot run={run} />
          <RunArtifactsPanel run={run} />
        </aside>

        <section
          className="run-page__timeline-pane"
          aria-labelledby="timeline-title"
        >
          <header className="run-page__timeline-heading">
            <div>
              <span className="run-section-kicker">Durable audit sequence</span>
              <h2 id="timeline-title" tabIndex={-1}>
                Timeline
              </h2>
              <p>
                {merged.snapshot === null
                  ? "Loading the first bounded event page."
                  : `${String(merged.snapshot.events.length)} events loaded across ${String(merged.snapshot.pageCount)} ${merged.snapshot.pageCount === 1 ? "page" : "pages"}.`}
              </p>
            </div>
          </header>

          {timelineQuery.isPending ? (
            <section className="run-workspace__state" aria-live="polite">
              <strong>Loading the run timeline</strong>
              <p>The initial page is bounded to 100 persisted events.</p>
            </section>
          ) : null}
          {timelineQuery.isError ? (
            <section className="run-workspace__state is-error" role="alert">
              <strong>The timeline is unavailable</strong>
              <p>{timelineQuery.error.message}</p>
              <button
                type="button"
                onClick={() => void timelineQuery.refetch()}
              >
                Try again
              </button>
            </section>
          ) : null}
          {merged.error === null ? null : (
            <section className="run-workspace__state is-error" role="alert">
              <strong>The timeline failed its sequence check</strong>
              <p>{merged.error}</p>
            </section>
          )}
          {merged.snapshot === null ? null : (
            <RunTimeline events={merged.snapshot.events} />
          )}

          {timelineQuery.hasNextPage ? (
            <footer className="run-page__pagination">
              <p>
                More persisted events exist after the loaded cursor. Load them
                before treating this as the complete run history.
              </p>
              <button
                disabled={timelineQuery.isFetchingNextPage}
                type="button"
                onClick={() => void timelineQuery.fetchNextPage()}
              >
                {timelineQuery.isFetchingNextPage
                  ? "Loading…"
                  : "Load more events"}
              </button>
            </footer>
          ) : null}
        </section>
      </div>
    </main>
  );
}

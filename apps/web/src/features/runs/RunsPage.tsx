import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { fetchRunPage, runListQueryKey } from "../../api/runArtifacts";
import { mergeRunPages } from "./runListModel";
import { formatRuntimeTimestamp, humanizeRuntimeValue } from "./timelineModel";
import "./run-timeline.css";

const PAGE_SIZE = 25;
const RESOURCE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$/u;
const RUN_LOOKUP_ERROR_ID = "run-lookup-error";

export function RunsPage(): React.JSX.Element {
  const navigate = useNavigate();
  const [runId, setRunId] = useState("");
  const [lookupError, setLookupError] = useState<string | null>(null);
  const baseQuery = { limit: PAGE_SIZE } as const;
  const runsQuery = useInfiniteQuery({
    queryKey: runListQueryKey(baseQuery),
    queryFn: ({ pageParam, signal }) =>
      fetchRunPage(
        {
          ...baseQuery,
          ...(pageParam === null ? {} : { cursor: pageParam }),
        },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.nextCursor,
    retry: false,
    refetchOnWindowFocus: false,
  });
  const mergedRuns = mergeRunPages(runsQuery.data?.pages ?? []);
  const runs = mergedRuns.items;

  return (
    <main className="runs-page">
      <header className="runs-page__heading">
        <div>
          <span className="run-section-kicker">Operational inspection</span>
          <h1>Runs &amp; audit</h1>
          <p>
            Inspect durable run state, sequence-ordered events, sealed execution
            policy, external-action receipts, and authorized artifacts. These
            views report persisted facts; they do not initiate work.
          </p>
        </div>
      </header>

      <section className="runs-page__workspace" aria-labelledby="runs-title">
        <h2 id="runs-title">Open a run</h2>
        <form
          className="runs-page__lookup"
          onSubmit={(event) => {
            event.preventDefault();
            const normalized = runId.trim();
            if (!RESOURCE_ID_PATTERN.test(normalized)) {
              setLookupError("Enter a valid run resource ID.");
              return;
            }
            setLookupError(null);
            void navigate(`/runs/${encodeURIComponent(normalized)}`);
          }}
        >
          <label>
            Run ID
            <input
              aria-describedby={
                lookupError === null ? undefined : RUN_LOOKUP_ERROR_ID
              }
              aria-invalid={lookupError === null ? undefined : "true"}
              autoComplete="off"
              maxLength={240}
              placeholder="run.…"
              spellCheck={false}
              value={runId}
              onChange={(event) => {
                setRunId(event.currentTarget.value);
                setLookupError(null);
              }}
            />
          </label>
          <button type="submit">Open run timeline</button>
        </form>
        {lookupError === null ? null : (
          <p
            id={RUN_LOOKUP_ERROR_ID}
            className="runs-page__lookup-error"
            role="alert"
          >
            {lookupError}
          </p>
        )}

        <h2>Recent runs</h2>
        {runsQuery.isPending ? (
          <section className="run-workspace__state" aria-live="polite">
            <strong>Loading recent runs</strong>
            <p>Reading the bounded private run index from the local API.</p>
          </section>
        ) : null}
        {runsQuery.isError ? (
          <section className="run-workspace__state is-error" role="alert">
            <strong>Recent runs are unavailable</strong>
            <p>{runsQuery.error.message}</p>
            <button type="button" onClick={() => void runsQuery.refetch()}>
              Try again
            </button>
          </section>
        ) : null}
        {!runsQuery.isError && mergedRuns.error !== null ? (
          <section className="run-workspace__state is-error" role="alert">
            <strong>Recent runs are unavailable</strong>
            <p>{mergedRuns.error}</p>
            <button type="button" onClick={() => void runsQuery.refetch()}>
              Try again
            </button>
          </section>
        ) : null}
        {runsQuery.data !== undefined && mergedRuns.error === null ? (
          <>
            <p className="runs-page__loaded" aria-live="polite">
              {String(runs.length)} runs loaded across{" "}
              {String(runsQuery.data.pages.length)}{" "}
              {runsQuery.data.pages.length === 1 ? "page" : "pages"}.
            </p>
            {runs.length === 0 ? (
              <section className="run-workspace__state">
                <strong>No runs have been recorded</strong>
                <p>
                  Create a dry run from an agent detail view, then return here.
                </p>
              </section>
            ) : (
              <ol className="runs-list">
                {runs.map((run) => (
                  <li key={run.id}>
                    <header>
                      <div>
                        <Link to={`/runs/${encodeURIComponent(run.id)}`}>
                          <strong>{run.id}</strong>
                        </Link>
                        <p>
                          {run.workflowId} · {run.instanceId}
                        </p>
                      </div>
                      <span className={`run-status is-${run.state}`}>
                        {humanizeRuntimeValue(run.state)}
                      </span>
                    </header>
                    <p>
                      {humanizeRuntimeValue(run.mode)} · updated{" "}
                      {formatRuntimeTimestamp(run.updatedAt)}
                    </p>
                  </li>
                ))}
              </ol>
            )}
            {runsQuery.hasNextPage ? (
              <footer className="run-page__pagination">
                <p>The recent-run index has another bounded server page.</p>
                <button
                  disabled={runsQuery.isFetchingNextPage}
                  type="button"
                  onClick={() => void runsQuery.fetchNextPage()}
                >
                  {runsQuery.isFetchingNextPage ? "Loading…" : "Load more runs"}
                </button>
              </footer>
            ) : null}
          </>
        ) : null}
      </section>
    </main>
  );
}

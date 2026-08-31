import { useInfiniteQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import {
  fetchRunArtifactsPage,
  runArtifactsQueryKey,
  type RunResource,
} from "../../api/runArtifacts";
import { AdvisoryArtifactBanner } from "../artifacts";
import {
  artifactRoute,
  isAdvisoryArtifact,
  mergeArtifactPages,
} from "./artifactPresentation";
import {
  formatRuntimeTimestamp,
  humanizeRuntimeValue,
  runRefreshInterval,
} from "./timelineModel";

const ARTIFACT_PAGE_SIZE = 25;

export function RunArtifactsPanel({
  run,
}: {
  readonly run: RunResource;
}): React.JSX.Element {
  const baseQuery = { limit: ARTIFACT_PAGE_SIZE } as const;
  const artifactsQuery = useInfiniteQuery({
    queryKey: runArtifactsQueryKey(run.id, baseQuery),
    queryFn: ({ pageParam, signal }) =>
      fetchRunArtifactsPage(
        run.id,
        {
          ...baseQuery,
          ...(pageParam === null ? {} : { cursor: pageParam }),
        },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.nextCursor,
    retry: false,
    refetchInterval: (query) =>
      runRefreshInterval(run.state, query.state.error !== null),
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: false,
  });
  const merged = mergeArtifactPages(run.id, artifactsQuery.data?.pages ?? []);
  const artifacts =
    artifactsQuery.data === undefined ? run.artifactSummaries : merged.items;

  return (
    <section className="run-artifacts" aria-labelledby="run-artifacts-title">
      <header>
        <div>
          <span className="run-section-kicker">Authorized outputs</span>
          <h2 id="run-artifacts-title">Artifacts &amp; provenance</h2>
          <p>
            Artifact details contain a server-redacted projection and an
            authorized keyed digest, never the raw stored payload.
          </p>
        </div>
        <span className="run-status">{String(artifacts.length)} loaded</span>
      </header>

      {artifactsQuery.isPending && run.artifactSummaries.length === 0 ? (
        <section className="run-workspace__state" aria-live="polite">
          <strong>Loading artifacts</strong>
          <p>Reading the first bounded artifact metadata page.</p>
        </section>
      ) : null}
      {artifactsQuery.isError ? (
        <section className="run-workspace__state is-error" role="alert">
          <strong>The complete artifact list is unavailable</strong>
          <p>
            {run.artifactSummaries.length > 0
              ? "The embedded run preview remains below, but it may be truncated."
              : artifactsQuery.error.message}
          </p>
          <button type="button" onClick={() => void artifactsQuery.refetch()}>
            Try again
          </button>
        </section>
      ) : null}
      {merged.error === null ? null : (
        <section className="run-workspace__state is-error" role="alert">
          <strong>The artifact list failed its keyset check</strong>
          <p>{merged.error}</p>
        </section>
      )}

      {merged.error !== null ? null : artifacts.length === 0 ? (
        <section className="run-workspace__state">
          <strong>No artifacts are available yet</strong>
          <p>This run has not exposed an authorized artifact projection.</p>
        </section>
      ) : (
        <ol className="run-artifacts__list">
          {artifacts.map((artifact) => (
            <li key={artifact.id}>
              <header>
                <div>
                  <Link to={artifactRoute(artifact.id)}>
                    <strong>{artifact.id}</strong>
                  </Link>
                  <p>{artifact.outputSchemaId}</p>
                </div>
                <span className="run-status">
                  {humanizeRuntimeValue(artifact.classification)}
                </span>
              </header>
              {isAdvisoryArtifact(artifact) ? <AdvisoryArtifactBanner /> : null}
              <p>
                Producer {artifact.templateId} · {artifact.instanceId}
              </p>
              <p>
                Created {formatRuntimeTimestamp(artifact.createdAt)} · schema
                version {artifact.outputSchemaVersion}
              </p>
            </li>
          ))}
        </ol>
      )}

      {merged.error === null && artifactsQuery.hasNextPage ? (
        <footer className="run-page__pagination">
          <p>
            More artifact metadata exists beyond the loaded server pages. The
            list is not complete until the next cursor is exhausted.
          </p>
          <button
            disabled={artifactsQuery.isFetchingNextPage}
            type="button"
            onClick={() => void artifactsQuery.fetchNextPage()}
          >
            {artifactsQuery.isFetchingNextPage
              ? "Loading…"
              : "Load more artifacts"}
          </button>
        </footer>
      ) : null}
    </section>
  );
}

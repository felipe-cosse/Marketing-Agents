import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  artifactResourceQueryKey,
  fetchArtifactResource,
} from "../../api/runArtifacts";
import {
  AdvisoryArtifactBanner,
  ArtifactPayloadView,
  MockReceiptNotice,
  RestrictedArtifactMarkdown,
} from "../artifacts";
import {
  artifactMarkdownValue,
  artifactRoute,
  isAdvisoryArtifact,
} from "./artifactPresentation";
import { formatRuntimeTimestamp, humanizeRuntimeValue } from "./timelineModel";
import "./run-timeline.css";

const RESOURCE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$/u;

export function ArtifactViewerPage(): React.JSX.Element {
  const { artifactId: routeArtifactId } = useParams<{
    readonly artifactId: string;
  }>();
  const artifactId = routeArtifactId ?? "";
  const validArtifactId = RESOURCE_ID_PATTERN.test(artifactId);
  const [payloadView, setPayloadView] = useState<"structured" | "json">(
    "structured",
  );
  const artifactQuery = useQuery({
    queryKey: artifactResourceQueryKey(
      validArtifactId ? artifactId : "invalid",
    ),
    queryFn: ({ signal }) => fetchArtifactResource(artifactId, signal),
    enabled: validArtifactId,
    retry: false,
    refetchOnWindowFocus: false,
  });

  if (!validArtifactId) {
    return (
      <main className="artifact-page">
        <section className="run-workspace__state is-error" role="alert">
          <strong>The artifact link is invalid</strong>
          <p>Open an authorized artifact from a run timeline.</p>
          <Link to="/runs">Return to runs</Link>
        </section>
      </main>
    );
  }
  if (artifactQuery.isPending) {
    return (
      <main className="artifact-page">
        <section className="run-workspace__state" aria-live="polite">
          <strong>Loading artifact {artifactId}</strong>
          <p>Reading the authorized server-redacted projection.</p>
        </section>
      </main>
    );
  }
  if (artifactQuery.isError) {
    return (
      <main className="artifact-page">
        <section className="run-workspace__state is-error" role="alert">
          <strong>The artifact is unavailable</strong>
          <p>{artifactQuery.error.message}</p>
          <button type="button" onClick={() => void artifactQuery.refetch()}>
            Try again
          </button>
          <p>
            <Link to="/runs">Return to runs</Link>
          </p>
        </section>
      </main>
    );
  }

  const artifact = artifactQuery.data;
  const markdown = artifactMarkdownValue(artifact);
  const hasMockConnectorReceipt = artifact.providers.some(
    (provider) =>
      provider.providerKind === "connector" && provider.mode === "mock",
  );
  return (
    <main className="artifact-page">
      <header className="artifact-page__heading">
        <div>
          <span className="run-section-kicker">
            Authorized artifact {artifact.id}
          </span>
          <h1>Artifact viewer</h1>
          <p>
            This viewer renders only the API&apos;s redacted JSON projection. It
            does not expose or reconstruct the stored payload, raw hash, or a
            downloadable binary.
          </p>
          <p>
            <Link to={`/runs/${encodeURIComponent(artifact.runId)}`}>
              ← Return to run {artifact.runId}
            </Link>
          </p>
        </div>
        <span className="run-status">
          {humanizeRuntimeValue(artifact.classification)}
        </span>
      </header>

      <article className="artifact-page__workspace">
        {isAdvisoryArtifact(artifact) ? <AdvisoryArtifactBanner /> : null}
        {hasMockConnectorReceipt ? <MockReceiptNotice /> : null}

        <section aria-labelledby="artifact-identity-title">
          <span className="run-section-kicker">Immutable identity</span>
          <h2 id="artifact-identity-title">Schema, digest &amp; producer</h2>
          <dl className="run-fact-grid artifact-page__facts">
            <div>
              <dt>Artifact ID</dt>
              <dd>{artifact.id}</dd>
            </div>
            <div>
              <dt>Output schema</dt>
              <dd>{artifact.outputSchemaId}</dd>
            </div>
            <div>
              <dt>Schema version</dt>
              <dd>{artifact.outputSchemaVersion}</dd>
            </div>
            <div>
              <dt>Schema hash</dt>
              <dd>
                <code>{artifact.outputSchemaHash}</code>
              </dd>
            </div>
            <div>
              <dt>Authorized payload digest</dt>
              <dd>
                <code>{artifact.payloadDigest}</code>
              </dd>
            </div>
            <div>
              <dt>Catalog hash</dt>
              <dd>
                <code>{artifact.catalogHash}</code>
              </dd>
            </div>
            <div>
              <dt>Sensitivity</dt>
              <dd>{humanizeRuntimeValue(artifact.classification)}</dd>
            </div>
            <div>
              <dt>Producer template</dt>
              <dd>{artifact.templateId}</dd>
            </div>
            <div>
              <dt>Producer instance</dt>
              <dd className="artifact-page__producer">
                <span>{artifact.instanceId}</span>
                <span>
                  Configuration revision{" "}
                  {String(artifact.instanceConfigRevision)}
                </span>
              </dd>
            </div>
            <div>
              <dt>Producer step</dt>
              <dd>{artifact.stepId}</dd>
            </div>
            <div>
              <dt>Created</dt>
              <dd>
                <time dateTime={artifact.createdAt}>
                  {formatRuntimeTimestamp(artifact.createdAt)}
                </time>
              </dd>
            </div>
          </dl>
        </section>

        <section
          className="artifact-page__provenance"
          aria-labelledby="artifact-provenance-title"
        >
          <span className="run-section-kicker">Lineage</span>
          <h2 id="artifact-provenance-title">
            Sources, parents &amp; providers
          </h2>
          <div className="artifact-page__lineage-grid">
            <section aria-labelledby="artifact-sources-title">
              <h3 id="artifact-sources-title">Sources</h3>
              <ul>
                {artifact.sources.map((source, index) => (
                  <li
                    key={`${source.kind}-${source.sourceId}-${String(index)}`}
                  >
                    <strong>{humanizeRuntimeValue(source.kind)}</strong>
                    <span>{source.sourceId}</span>
                    <em>{humanizeRuntimeValue(source.classification)}</em>
                  </li>
                ))}
              </ul>
            </section>
            <section aria-labelledby="artifact-parents-title">
              <h3 id="artifact-parents-title">Parent artifacts</h3>
              {artifact.parentArtifactIds.length === 0 ? (
                <p>No parent artifact is recorded.</p>
              ) : (
                <ul>
                  {artifact.parentArtifactIds.map((parentId) => (
                    <li key={parentId}>
                      <Link to={artifactRoute(parentId)}>{parentId}</Link>
                    </li>
                  ))}
                </ul>
              )}
            </section>
            <section aria-labelledby="artifact-providers-title">
              <h3 id="artifact-providers-title">Providers</h3>
              <ul>
                {artifact.providers.map((provider, index) => (
                  <li
                    key={`${provider.providerKind}-${provider.name}-${provider.version}-${String(index)}`}
                  >
                    <strong>
                      {humanizeRuntimeValue(provider.providerKind)}
                    </strong>
                    <span>
                      {provider.name} {provider.version}
                    </span>
                    <em>{humanizeRuntimeValue(provider.mode)}</em>
                  </li>
                ))}
              </ul>
            </section>
          </div>
        </section>

        {markdown === null ? null : (
          <section
            className="artifact-page__content"
            aria-labelledby="artifact-content-title"
          >
            <span className="run-section-kicker">Restricted presentation</span>
            <h2 id="artifact-content-title">Primary artifact content</h2>
            <p>
              Markdown is enabled only for the catalog output&apos;s explicit
              <code> artifact </code> string. Raw HTML, embeds, images, and
              dangerous URLs remain inert or are removed.
            </p>
            <RestrictedArtifactMarkdown source={markdown} />
          </section>
        )}

        <section
          className="artifact-page__payload"
          aria-labelledby="artifact-payload-title"
        >
          <span className="run-section-kicker">Server-redacted projection</span>
          <h2 id="artifact-payload-title">Authorized payload (bounded view)</h2>
          <div
            className="artifact-page__view-switcher"
            role="group"
            aria-label="Artifact payload view"
          >
            <button
              aria-pressed={payloadView === "structured"}
              type="button"
              onClick={() => setPayloadView("structured")}
            >
              Structured view
            </button>
            <button
              aria-pressed={payloadView === "json"}
              type="button"
              onClick={() => setPayloadView("json")}
            >
              Escaped JSON
            </button>
          </div>
          <ArtifactPayloadView
            label="Authorized redacted artifact payload"
            presentation={payloadView}
            value={artifact.redactedPayload}
          />
        </section>
      </article>
    </main>
  );
}

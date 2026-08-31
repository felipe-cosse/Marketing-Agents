import { useMemo } from "react";

import {
  artifactNodeToJson,
  artifactOmissionText,
  prepareArtifactValue,
  tokenizeArtifactJson,
  type ArtifactRenderNode,
} from "./artifactPayload";
import "./artifact-renderer.css";
import {
  ADVISORY_ARTIFACT_LABEL,
  NO_EXTERNAL_DELIVERY_LABEL,
} from "./artifactLabels";
import { RestrictedArtifactMarkdown } from "./restrictedMarkdown";

export type ArtifactPresentation = "json" | "markdown" | "structured";

export interface ArtifactPayloadViewProps {
  readonly value: unknown;
  /** Select "markdown" only from trusted presentation metadata, never payload content. */
  readonly presentation?: ArtifactPresentation;
  readonly label?: string;
}

function StructuredArtifactNode({
  node,
}: {
  readonly node: ArtifactRenderNode;
}): React.JSX.Element {
  if (node.kind === "omitted") {
    return (
      <span className="artifact-content-omission">
        {artifactOmissionText(node.reason)}
      </span>
    );
  }
  if (node.kind === "scalar") {
    if (node.value === null) {
      return <span className="artifact-value artifact-value--null">null</span>;
    }
    return (
      <span className={`artifact-value artifact-value--${typeof node.value}`}>
        {String(node.value)}
      </span>
    );
  }
  if (node.kind === "array") {
    if (node.items.length === 0 && node.omission === null) {
      return <span className="artifact-empty-value">Empty array</span>;
    }
    return (
      <ol className="artifact-structured-list artifact-structured-list--array">
        {node.items.map((item, index) => (
          <li key={index}>
            <StructuredArtifactNode node={item} />
          </li>
        ))}
        {node.omission === null ? null : (
          <li>
            <StructuredArtifactNode node={node.omission} />
          </li>
        )}
      </ol>
    );
  }
  if (node.entries.length === 0 && node.omission === null) {
    return <span className="artifact-empty-value">Empty object</span>;
  }
  return (
    <dl className="artifact-structured-list artifact-structured-list--object">
      {node.entries.map((entry, index) => (
        <div
          key={`${String(index)}-${entry.key}`}
          className="artifact-object-entry"
        >
          <dt>{entry.key}</dt>
          <dd>
            <StructuredArtifactNode node={entry.value} />
          </dd>
        </div>
      ))}
      {node.omission === null ? null : (
        <div className="artifact-object-entry artifact-object-entry--omission">
          <dt>Additional fields</dt>
          <dd>
            <StructuredArtifactNode node={node.omission} />
          </dd>
        </div>
      )}
    </dl>
  );
}

function EscapedArtifactJson({
  node,
}: {
  readonly node: ArtifactRenderNode;
}): React.JSX.Element {
  const tokens = tokenizeArtifactJson(artifactNodeToJson(node));
  return (
    <pre
      className="artifact-json"
      aria-label="Escaped artifact JSON"
      tabIndex={0}
    >
      <code>
        {tokens.map((token, index) => (
          <span
            key={`${String(index)}-${token.kind}`}
            className={`artifact-json__${token.kind}`}
          >
            {token.text}
          </span>
        ))}
      </code>
    </pre>
  );
}

export function ArtifactPayloadView({
  value,
  presentation = "structured",
  label = "Artifact payload",
}: ArtifactPayloadViewProps): React.JSX.Element {
  const prepared = useMemo(() => prepareArtifactValue(value), [value]);
  const markdownSource =
    prepared.root.kind === "scalar" && typeof prepared.root.value === "string"
      ? prepared.root.value
      : null;

  return (
    <section className="artifact-payload" aria-label={label}>
      {presentation === "json" ? (
        <EscapedArtifactJson node={prepared.root} />
      ) : presentation === "markdown" && markdownSource !== null ? (
        <RestrictedArtifactMarkdown source={markdownSource} />
      ) : (
        <StructuredArtifactNode node={prepared.root} />
      )}
    </section>
  );
}

export function AdvisoryArtifactBanner(): React.JSX.Element {
  return (
    <aside
      className="artifact-safety-label artifact-safety-label--advisory"
      aria-label={ADVISORY_ARTIFACT_LABEL}
    >
      <strong>{ADVISORY_ARTIFACT_LABEL}</strong>
    </aside>
  );
}

export function MockReceiptNotice(): React.JSX.Element {
  return (
    <aside
      className="artifact-safety-label artifact-safety-label--mock"
      aria-label={NO_EXTERNAL_DELIVERY_LABEL}
    >
      <strong>{NO_EXTERNAL_DELIVERY_LABEL}</strong>
    </aside>
  );
}

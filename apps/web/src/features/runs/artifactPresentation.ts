import {
  compareRunTimestamps,
  type ArtifactPage,
  type ArtifactResource,
  type ArtifactSummary,
} from "../../api/runArtifacts";

function catalogOutputSchemaId(templateId: string): string {
  return `urn:marketing-agents:catalog:v1:${templateId}:output`;
}

const ADVISORY_OUTPUT_SCHEMAS = new Map([
  [
    "tpl.email.lifecycle-marketing.churned-user-monitor",
    catalogOutputSchemaId("tpl.email.lifecycle-marketing.churned-user-monitor"),
  ],
  [
    "tpl.partnerships.implementation-partners.partner-application-reviewer",
    catalogOutputSchemaId(
      "tpl.partnerships.implementation-partners.partner-application-reviewer",
    ),
  ],
]);

const MARKDOWN_OUTPUT_SCHEMAS = new Map([
  [
    "tpl.email.lifecycle-marketing.churned-user-monitor",
    catalogOutputSchemaId("tpl.email.lifecycle-marketing.churned-user-monitor"),
  ],
]);

export interface ArtifactPageMerge {
  readonly items: readonly ArtifactSummary[];
  readonly error: string | null;
}

export function mergeArtifactPages(
  runId: string,
  pages: readonly ArtifactPage[],
): ArtifactPageMerge {
  const items: ArtifactSummary[] = [];
  const ids = new Set<string>();
  let previous: ArtifactSummary | undefined;
  for (const page of pages) {
    if (page.runId !== runId) {
      return {
        items: [],
        error: "An artifact page does not belong to this run.",
      };
    }
    for (const artifact of page.items) {
      const order =
        previous === undefined
          ? -1
          : compareRunTimestamps(previous.createdAt, artifact.createdAt);
      if (
        ids.has(artifact.id) ||
        order > 0 ||
        (order === 0 && previous !== undefined && previous.id >= artifact.id)
      ) {
        return {
          items: [],
          error:
            "Artifact pages do not preserve unique ascending keyset order.",
        };
      }
      ids.add(artifact.id);
      items.push(artifact);
      previous = artifact;
    }
  }
  return { items: Object.freeze(items), error: null };
}

export function isAdvisoryArtifact(
  artifact: Pick<ArtifactSummary, "templateId" | "outputSchemaId">,
): boolean {
  return (
    ADVISORY_OUTPUT_SCHEMAS.get(artifact.templateId) === artifact.outputSchemaId
  );
}

export function artifactMarkdownValue(
  artifact: ArtifactResource,
): string | null {
  if (
    MARKDOWN_OUTPUT_SCHEMAS.get(artifact.templateId) !== artifact.outputSchemaId
  ) {
    return null;
  }
  const value = artifact.redactedPayload.artifact;
  return typeof value === "string" ? value : null;
}

export function artifactRoute(artifactId: string): string {
  return `/artifacts/${encodeURIComponent(artifactId)}`;
}

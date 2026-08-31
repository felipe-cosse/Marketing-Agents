import {
  compareRunTimestamps,
  type RunPage,
  type RunSummary,
} from "../../api/runArtifacts";

const EMPTY_RUNS: readonly RunSummary[] = Object.freeze([]);
const PAGE_INTEGRITY_ERROR =
  "Recent run pages failed their identity or ordering checks.";

export interface RunListMergeResult {
  readonly items: readonly RunSummary[];
  readonly error: string | null;
}

export function mergeRunPages(pages: readonly RunPage[]): RunListMergeResult {
  const items: RunSummary[] = [];
  const seenIds = new Set<string>();
  let previous: RunSummary | undefined;

  for (const page of pages) {
    for (const run of page.items) {
      if (seenIds.has(run.id)) {
        return Object.freeze({
          items: EMPTY_RUNS,
          error: PAGE_INTEGRITY_ERROR,
        });
      }
      if (previous !== undefined) {
        const order = compareRunTimestamps(previous.createdAt, run.createdAt);
        if (order < 0 || (order === 0 && previous.id <= run.id)) {
          return Object.freeze({
            items: EMPTY_RUNS,
            error: PAGE_INTEGRITY_ERROR,
          });
        }
      }
      seenIds.add(run.id);
      items.push(run);
      previous = run;
    }
  }

  return Object.freeze({ items: Object.freeze(items), error: null });
}

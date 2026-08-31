import type {
  RunResource,
  RunTimelineEvent,
  RunTimelinePage,
} from "../../api/runArtifacts";

const TERMINAL_RUN_STATES = new Set<RunResource["state"]>([
  "completed",
  "failed",
  "rejected",
  "cancelled",
]);

const EVENT_LABELS: Readonly<Record<string, string>> = Object.freeze({
  "run.received": "Run received",
  "run.validated": "Run validated",
  "run.planned": "Run planned",
  "run.awaiting_approval": "Run awaiting approval",
  "run.executing": "Run executing",
  "run.completed": "Run completed",
  "run.failed": "Run failed",
  "run.rejected": "Run rejected",
  "run.cancelled": "Run cancelled",
  "step.ready": "Step ready",
  "step.started": "Step attempt started",
  "step.succeeded": "Step succeeded",
  "step.failed": "Step failed",
  "approval.requested": "Approval requested",
  "approval.decided": "Approval decided",
  "approval.expired": "Approval expired",
  "approval.consumed": "Approval consumed",
  "action.proposed": "External action proposed",
  "action.dispatch_reserved": "External action dispatch reserved",
  "action.dispatch_started": "External action dispatch started",
  "action.succeeded": "External action succeeded",
  "action.failed": "External action failed",
  "action.rejected": "External action rejected",
  "action.cancelled": "External action cancelled",
  "action.receipt_reconciled": "Connector receipt reconciled",
  "artifact.created": "Artifact created",
});

export type TimelineCategory =
  | "run"
  | "plan"
  | "step"
  | "attempt"
  | "approval"
  | "action"
  | "artifact"
  | "system";

export interface TimelineSnapshot {
  readonly events: readonly RunTimelineEvent[];
  readonly nextCursor: string | null;
  readonly pageCount: number;
}

export function isTerminalRunState(state: RunResource["state"]): boolean {
  return TERMINAL_RUN_STATES.has(state);
}

export function runRefreshInterval(
  state: RunResource["state"] | undefined,
  hasError: boolean,
): number | false {
  if (state !== undefined && isTerminalRunState(state)) return false;
  return hasError ? 10_000 : 2_500;
}

export function humanizeRuntimeValue(value: string): string {
  return value
    .replaceAll(/[._-]+/gu, " ")
    .replaceAll(/\b\p{L}/gu, (character) => character.toLocaleUpperCase());
}

export function formatRuntimeTimestamp(value: string): string {
  const instant = new Date(value);
  return Number.isNaN(instant.getTime())
    ? "Timestamp unavailable"
    : instant.toLocaleString();
}

export function timelineCategory(event: RunTimelineEvent): TimelineCategory {
  const prefix = event.eventType.split(".", 1)[0];
  if (prefix === "run") return "run";
  if (prefix === "plan" || prefix === "routing") return "plan";
  if (prefix === "step") return "step";
  if (
    prefix === "model" ||
    prefix === "tool" ||
    prefix === "connector" ||
    event.eventType.includes("attempt")
  ) {
    return "attempt";
  }
  if (prefix === "approval") return "approval";
  if (prefix === "action") return "action";
  if (prefix === "artifact") return "artifact";
  return "system";
}

export function timelineCategoryLabel(category: TimelineCategory): string {
  const labels: Readonly<Record<TimelineCategory, string>> = {
    run: "Run state",
    plan: "Plan snapshot",
    step: "Step",
    attempt: "Provider attempt",
    approval: "Approval",
    action: "External action",
    artifact: "Artifact",
    system: "Runtime event",
  };
  return labels[category];
}

export function timelineEventIcon(category: TimelineCategory): string {
  const icons: Readonly<Record<TimelineCategory, string>> = {
    run: "◆",
    plan: "▦",
    step: "✓",
    attempt: "↻",
    approval: "⌁",
    action: "→",
    artifact: "◇",
    system: "•",
  };
  return icons[category];
}

export function timelineEventTitle(event: RunTimelineEvent): string {
  return EVENT_LABELS[event.eventType] ?? humanizeRuntimeValue(event.eventType);
}

export function timelineStateSummary(event: RunTimelineEvent): string | null {
  if (event.previousState !== null && event.newState !== null) {
    return `${humanizeRuntimeValue(event.previousState)} → ${humanizeRuntimeValue(event.newState)}`;
  }
  if (event.newState !== null) {
    return humanizeRuntimeValue(event.newState);
  }
  if (event.attemptedCommand !== null) {
    return `Attempted ${humanizeRuntimeValue(event.attemptedCommand)}`;
  }
  if (event.reasonCode !== null) {
    return humanizeRuntimeValue(event.reasonCode);
  }
  return event.outcome.length > 0 ? humanizeRuntimeValue(event.outcome) : null;
}

export function mergeTimelinePages(
  runId: string,
  pages: readonly RunTimelinePage[],
): TimelineSnapshot {
  const events: RunTimelineEvent[] = [];
  const eventIds = new Set<string>();
  let priorSequence = 0;
  for (const page of pages) {
    if (page.runId !== runId) {
      throw new Error("The timeline page does not belong to this run.");
    }
    for (const event of page.items) {
      if (eventIds.has(event.id) || event.sequence <= priorSequence) {
        throw new Error("The timeline sequence is not coherent.");
      }
      eventIds.add(event.id);
      priorSequence = event.sequence;
      events.push(event);
    }
  }
  return Object.freeze({
    events: Object.freeze(events),
    nextCursor: pages.at(-1)?.nextCursor ?? null,
    pageCount: pages.length,
  });
}

export function eventAnchorId(event: RunTimelineEvent): string {
  return `timeline-event-${String(event.sequence)}`;
}

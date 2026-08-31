import { Link } from "react-router-dom";

import type { RunTimelineEvent } from "../../api/runArtifacts";
import { ArtifactPayloadView } from "../artifacts";
import {
  eventAnchorId,
  formatRuntimeTimestamp,
  humanizeRuntimeValue,
  timelineCategory,
  timelineCategoryLabel,
  timelineEventIcon,
  timelineEventTitle,
  timelineStateSummary,
} from "./timelineModel";

export interface RunTimelineProps {
  readonly events: readonly RunTimelineEvent[];
}

function EventLinks({ event }: { readonly event: RunTimelineEvent }) {
  return (
    <div
      className="run-event__links"
      role="group"
      aria-label={`Related resources for sequence ${String(event.sequence)}`}
    >
      {event.stepId === null ? null : (
        <a href={`#step-${encodeURIComponent(event.stepId)}`}>
          Step {event.stepId}
        </a>
      )}
      {event.actionId === null ? null : (
        <a href={`#action-${encodeURIComponent(event.actionId)}`}>
          External action {event.actionId}
        </a>
      )}
      {event.approvalRequestId === null ? null : (
        <Link to="/approvals">Approval request {event.approvalRequestId}</Link>
      )}
      {event.artifactId === null ? null : (
        <Link to={`/artifacts/${encodeURIComponent(event.artifactId)}`}>
          Artifact {event.artifactId}
        </Link>
      )}
    </div>
  );
}

function TimelineEventCard({ event }: { readonly event: RunTimelineEvent }) {
  const category = timelineCategory(event);
  const stateSummary = timelineStateSummary(event);
  const metadataEntries = Object.keys(event.metadata).length;
  return (
    <li
      className={`run-event is-${category}`}
      id={eventAnchorId(event)}
      data-sequence={event.sequence}
      tabIndex={-1}
    >
      <span className="run-event__rail" aria-hidden="true">
        <span className="run-event__icon">{timelineEventIcon(category)}</span>
      </span>
      <article aria-labelledby={`${eventAnchorId(event)}-title`}>
        <header className="run-event__heading">
          <div>
            <span className="run-event__category">
              {timelineCategoryLabel(category)}
            </span>
            <h3 id={`${eventAnchorId(event)}-title`}>
              {timelineEventTitle(event)}
            </h3>
          </div>
          <div className="run-event__sequence">
            <strong>Sequence {String(event.sequence)}</strong>
            <time dateTime={event.occurredAt}>
              {formatRuntimeTimestamp(event.occurredAt)}
            </time>
          </div>
        </header>
        {stateSummary === null ? null : (
          <p className="run-event__state">{stateSummary}</p>
        )}
        <dl className="run-event__facts">
          <div>
            <dt>Event type</dt>
            <dd>
              <code>{event.eventType}</code>
            </dd>
          </div>
          <div>
            <dt>Outcome</dt>
            <dd>{humanizeRuntimeValue(event.outcome)}</dd>
          </div>
          <div>
            <dt>Actor source</dt>
            <dd>{humanizeRuntimeValue(event.actorSource)}</dd>
          </div>
          <div>
            <dt>Metadata sensitivity</dt>
            <dd>{humanizeRuntimeValue(event.metadataClassification)}</dd>
          </div>
        </dl>
        <EventLinks event={event} />
        {event.metadataExpired ? (
          <p className="run-event__expired">
            Event metadata expired under its retention policy. The durable
            sequence and state facts remain visible.
          </p>
        ) : metadataEntries > 0 ? (
          <details className="run-event__metadata">
            <summary>Safe event metadata ({String(metadataEntries)})</summary>
            <ArtifactPayloadView
              label={`Safe metadata for sequence ${String(event.sequence)}`}
              presentation="json"
              value={event.metadata}
            />
          </details>
        ) : (
          <p className="run-event__metadata-empty">
            No retained metadata is available for this event.
          </p>
        )}
      </article>
    </li>
  );
}

export function RunTimeline({ events }: RunTimelineProps): React.JSX.Element {
  if (events.length === 0) {
    return (
      <section className="run-workspace__state" aria-live="polite">
        <strong>No timeline events are available yet</strong>
        <p>
          The run exists, but its durable event sequence is currently empty.
        </p>
      </section>
    );
  }
  return (
    <ol className="run-timeline" aria-label="Run timeline in sequence order">
      {events.map((event) => (
        <TimelineEventCard event={event} key={event.id} />
      ))}
    </ol>
  );
}

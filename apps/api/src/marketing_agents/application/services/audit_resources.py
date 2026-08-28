"""Read-only, high-watermark-bound projections of the global audit feed."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from marketing_agents.application.policies.runtime_resource_authorization import (
    RuntimeResourceAuthorizationError,
    authorize_runtime_resource_reader,
)
from marketing_agents.application.ports.repositories import AuditFeedPage
from marketing_agents.application.ports.unit_of_work import UnitOfWorkFactory
from marketing_agents.domain.audit import AuditEvent
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.domain.validation import require_id, require_utc

DEFAULT_AUDIT_PAGE_SIZE = 25
MAX_AUDIT_PAGE_SIZE = 100
MAX_AUDIT_CURSOR_LENGTH = 1_024
AUDIT_FEED_ENDPOINT_VERSION = "audit-events-v1"
_CURSOR_PREFIX = "audit-feed-v1."
_FILTER_DOMAIN = b"marketing-agents:audit-feed-filter:v1\x00"


class AuditResourceServiceError(ValueError):
    """Stable non-sensitive failure raised by the audit query seam."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AuditListQuery:
    run_id: str | None = None
    step_id: str | None = None
    action_id: str | None = None
    approval_id: str | None = None
    event_type: str | None = None
    occurred_at_from: datetime | None = None
    occurred_at_to: datetime | None = None
    cursor: str | None = field(default=None, repr=False)
    limit: int = DEFAULT_AUDIT_PAGE_SIZE

    def __post_init__(self) -> None:
        for identifier, name in (
            (self.run_id, "audit run filter"),
            (self.step_id, "audit step filter"),
            (self.action_id, "audit action filter"),
            (self.approval_id, "audit approval filter"),
            (self.event_type, "audit event-type filter"),
        ):
            if identifier is not None:
                require_id(identifier, name)
        for timestamp, name in (
            (self.occurred_at_from, "audit lower time bound"),
            (self.occurred_at_to, "audit upper time bound"),
        ):
            if timestamp is not None:
                require_utc(timestamp, name)
        if (
            self.occurred_at_from is not None
            and self.occurred_at_to is not None
            and self.occurred_at_from > self.occurred_at_to
        ):
            raise ValueError("audit lower time bound cannot follow upper bound")
        if type(self.limit) is not int or not 1 <= self.limit <= MAX_AUDIT_PAGE_SIZE:
            raise ValueError("audit page limit is outside the supported range")
        if self.cursor is not None and (
            type(self.cursor) is not str
            or not self.cursor
            or len(self.cursor) > MAX_AUDIT_CURSOR_LENGTH
        ):
            raise ValueError("audit page cursor is invalid")


@dataclass(frozen=True, slots=True)
class AuditResource:
    event_id: str
    schema_version: int
    feed_sequence: int
    run_sequence: int | None
    run_id: str | None
    schedule_id: str | None
    occurrence_id: str | None
    event_type: str
    aggregate_type: str
    aggregate_id: str
    outcome: str
    actor_id: str
    actor_source: str
    auth_method: str
    correlation_id: str
    occurred_at: datetime
    step_id: str | None
    action_id: str | None
    action_attempt_number: int | None
    receipt_id: str | None
    approval_request_id: str | None
    approval_decision_id: str | None
    artifact_id: str | None
    attempt_id: str | None
    attempted_command: str | None
    expected_version: int | None
    observed_version: int | None
    observed_state: str | None
    requested_state: str | None
    mutation_version: int | None
    transition_sequence: int | None
    previous_state: str | None
    new_state: str | None
    reason_code: str | None
    metadata: Mapping[str, Any] = field(repr=False)
    metadata_classification: str
    metadata_expires_at: datetime
    metadata_expired: bool
    run_url: str | None
    step_url: str | None
    action_url: str | None
    approval_url: str | None
    artifact_url: str | None


@dataclass(frozen=True, slots=True)
class AuditPage:
    endpoint_version: str
    high_watermark: int
    items: tuple[AuditResource, ...]
    next_cursor: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _AuditCursorBoundary:
    high_watermark: int
    before_feed_sequence: int


def _filter_fingerprint(query: AuditListQuery) -> str:
    return hashlib.sha256(
        _FILTER_DOMAIN
        + canonical_json_bytes(
            {
                "action_id": query.action_id,
                "approval_id": query.approval_id,
                "event_type": query.event_type,
                "occurred_at_from": _iso(query.occurred_at_from),
                "occurred_at_to": _iso(query.occurred_at_to),
                "run_id": query.run_id,
                "step_id": query.step_id,
            }
        )
    ).hexdigest()


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat(timespec="microseconds")


def _encode_cursor(
    *,
    query: AuditListQuery,
    high_watermark: int,
    before_feed_sequence: int,
) -> str:
    payload = canonical_json_bytes(
        {
            "before_feed_sequence": before_feed_sequence,
            "endpoint": AUDIT_FEED_ENDPOINT_VERSION,
            "filter": _filter_fingerprint(query),
            "high_watermark": high_watermark,
            "version": 1,
        }
    )
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{_CURSOR_PREFIX}{token}"


def _decode_cursor(query: AuditListQuery) -> _AuditCursorBoundary | None:
    if query.cursor is None:
        return None
    if not query.cursor.startswith(_CURSOR_PREFIX):
        raise _cursor_error()
    encoded = query.cursor[len(_CURSOR_PREFIX) :]
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        decoded = json.loads(raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise _cursor_error() from None
    if (
        type(decoded) is not dict
        or set(decoded)
        != {
            "before_feed_sequence",
            "endpoint",
            "filter",
            "high_watermark",
            "version",
        }
        or decoded.get("version") != 1
        or decoded.get("endpoint") != AUDIT_FEED_ENDPOINT_VERSION
        or type(decoded.get("filter")) is not str
        or type(decoded.get("high_watermark")) is not int
        or type(decoded.get("before_feed_sequence")) is not int
    ):
        raise _cursor_error()
    high_watermark = decoded["high_watermark"]
    before_feed_sequence = decoded["before_feed_sequence"]
    if (
        isinstance(high_watermark, bool)
        or isinstance(before_feed_sequence, bool)
        or high_watermark < 1
        or not 1 <= before_feed_sequence <= high_watermark
    ):
        raise _cursor_error()
    expected_filter = _filter_fingerprint(query)
    try:
        if not hmac.compare_digest(decoded["filter"], expected_filter):
            raise ValueError("audit cursor filters changed")
    except (TypeError, ValueError):
        raise _cursor_error() from None
    canonical = _CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if not hmac.compare_digest(canonical, query.cursor):
        raise _cursor_error()
    return _AuditCursorBoundary(
        high_watermark=high_watermark,
        before_feed_sequence=before_feed_sequence,
    )


def _cursor_error() -> AuditResourceServiceError:
    return AuditResourceServiceError(
        "audit_cursor_invalid",
        "audit feed cursor is invalid",
    )


def _plain_mapping(value: object) -> Mapping[str, Any]:
    decoded = json.loads(canonical_json_bytes(value))
    if type(decoded) is not dict:
        raise AuditResourceServiceError(
            "audit_record_corrupt",
            "audit resources could not be validated",
        )
    return cast(dict[str, Any], decoded)


def project_audit_event(event: AuditEvent, *, now: datetime) -> AuditResource:
    """Project an integrity-valid event without its internal database identity."""

    if type(event) is not AuditEvent:
        raise AuditResourceServiceError(
            "audit_record_corrupt",
            "audit resources could not be validated",
        )
    require_utc(now, "audit projection time")
    try:
        event.draft.verify_integrity()
        event.safe_metadata.verify_integrity()
    except (TypeError, ValueError):
        raise AuditResourceServiceError(
            "audit_record_corrupt",
            "audit resources could not be validated",
        ) from None
    feed_sequence = event.feed_sequence
    if type(feed_sequence) is not int or feed_sequence < 1:
        raise AuditResourceServiceError(
            "audit_record_corrupt",
            "audit resources could not be validated",
        )
    expired = now >= event.safe_metadata.expires_at
    metadata = {} if expired else _plain_mapping(event.safe_metadata.values)
    run_url = None if event.run_id is None else f"/api/v1/runs/{event.run_id}"
    step_url = (
        None
        if event.run_id is None or event.step_id is None
        else f"/api/v1/runs/{event.run_id}/steps/{event.step_id}"
    )
    return AuditResource(
        event_id=event.id,
        schema_version=event.schema_version,
        feed_sequence=feed_sequence,
        run_sequence=event.run_sequence,
        run_id=event.run_id,
        schedule_id=event.schedule_id,
        occurrence_id=event.occurrence_id,
        event_type=event.event_type,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        outcome=event.outcome.value,
        actor_id=event.actor_id,
        actor_source=event.actor_source.value,
        auth_method=event.auth_method,
        correlation_id=event.correlation_id,
        occurred_at=event.occurred_at,
        step_id=event.step_id,
        action_id=event.action_id,
        action_attempt_number=event.action_attempt_number,
        receipt_id=event.receipt_id,
        approval_request_id=event.approval_request_id,
        approval_decision_id=event.approval_decision_id,
        artifact_id=event.artifact_id,
        attempt_id=event.attempt_id,
        attempted_command=event.attempted_command,
        expected_version=event.expected_version,
        observed_version=event.observed_version,
        observed_state=event.observed_state,
        requested_state=event.requested_state,
        mutation_version=event.mutation_version,
        transition_sequence=event.transition_sequence,
        previous_state=event.previous_state,
        new_state=event.new_state,
        reason_code=event.reason_code,
        metadata=metadata,
        metadata_classification=event.safe_metadata.classification.value,
        metadata_expires_at=event.safe_metadata.expires_at,
        metadata_expired=expired,
        run_url=run_url,
        step_url=step_url,
        action_url=(
            None if event.action_id is None else f"/api/v1/external-actions/{event.action_id}"
        ),
        approval_url=(
            None
            if event.approval_request_id is None
            else f"/api/v1/approvals/{event.approval_request_id}"
        ),
        artifact_url=(
            None if event.artifact_id is None else f"/api/v1/artifacts/{event.artifact_id}"
        ),
    )


class AuditResourceService:
    """Authorize and query the immutable installation-wide public audit order."""

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        *,
        utc_now: Callable[[], datetime],
    ) -> None:
        if not callable(unit_of_work) or not callable(utc_now):
            raise ValueError("audit resources require callable dependencies")
        self._unit_of_work = unit_of_work
        self._utc_now = utc_now

    async def list(
        self,
        query: AuditListQuery,
        *,
        principal: AuthenticatedPrincipal,
    ) -> AuditPage:
        self._authorize(principal)
        if type(query) is not AuditListQuery:
            raise AuditResourceServiceError(
                "audit_query_invalid",
                "audit list query is invalid",
            )
        boundary = _decode_cursor(query)
        try:
            async with self._unit_of_work() as unit_of_work:
                persisted = await unit_of_work.audits.list_feed(
                    high_watermark=(None if boundary is None else boundary.high_watermark),
                    before_feed_sequence=(
                        None if boundary is None else boundary.before_feed_sequence
                    ),
                    run_id=query.run_id,
                    step_id=query.step_id,
                    action_id=query.action_id,
                    approval_id=query.approval_id,
                    event_type=query.event_type,
                    occurred_at_from=query.occurred_at_from,
                    occurred_at_to=query.occurred_at_to,
                    limit=query.limit + 1,
                )
        except AuditResourceServiceError:
            raise
        except (TypeError, ValueError, RuntimeError):
            raise AuditResourceServiceError(
                "audit_record_corrupt",
                "audit resources could not be validated",
            ) from None
        if type(persisted) is not AuditFeedPage:
            raise AuditResourceServiceError(
                "audit_record_corrupt",
                "audit resources could not be validated",
            )
        if boundary is not None and persisted.high_watermark != boundary.high_watermark:
            raise AuditResourceServiceError(
                "audit_record_corrupt",
                "audit resources could not be validated",
            )
        now = self._utc_now()
        try:
            require_utc(now, "audit projection time")
            page_events = persisted.events[: query.limit]
            items = tuple(project_audit_event(event, now=now) for event in page_events)
        except AuditResourceServiceError:
            raise
        except (TypeError, ValueError):
            raise AuditResourceServiceError(
                "audit_record_corrupt",
                "audit resources could not be validated",
            ) from None
        next_cursor = None
        if len(persisted.events) > query.limit and items:
            next_cursor = _encode_cursor(
                query=query,
                high_watermark=persisted.high_watermark,
                before_feed_sequence=items[-1].feed_sequence,
            )
        return AuditPage(
            endpoint_version=AUDIT_FEED_ENDPOINT_VERSION,
            high_watermark=persisted.high_watermark,
            items=items,
            next_cursor=next_cursor,
        )

    @staticmethod
    def _authorize(principal: AuthenticatedPrincipal) -> None:
        try:
            authorize_runtime_resource_reader(principal)
        except RuntimeResourceAuthorizationError as exc:
            raise AuditResourceServiceError(exc.code, str(exc)) from None


__all__ = [
    "AUDIT_FEED_ENDPOINT_VERSION",
    "DEFAULT_AUDIT_PAGE_SIZE",
    "MAX_AUDIT_PAGE_SIZE",
    "AuditListQuery",
    "AuditPage",
    "AuditResource",
    "AuditResourceService",
    "AuditResourceServiceError",
    "project_audit_event",
]

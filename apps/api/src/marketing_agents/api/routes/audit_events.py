"""Authenticated, bounded inspection of the installation-wide audit feed."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from marketing_agents.api.dependencies import (
    AuditResourceExecutor,
    get_audit_resource_executor,
    require_runtime_resource_reader_principal,
)
from marketing_agents.api.schemas.audit_events import (
    AuditEventListResponse,
    AuditEventView,
    AuditHttpError,
    AuditPlainHttpError,
)
from marketing_agents.application.services.audit_resources import (
    AUDIT_FEED_ENDPOINT_VERSION,
    DEFAULT_AUDIT_PAGE_SIZE,
    MAX_AUDIT_CURSOR_LENGTH,
    MAX_AUDIT_PAGE_SIZE,
    AuditListQuery,
    AuditPage,
    AuditResource,
    AuditResourceServiceError,
)
from marketing_agents.domain.identity import AuthenticatedPrincipal

_RESOURCE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$"
_EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_.-]{0,119}$"
_PRIVATE_HEADERS = {
    "Cache-Control": {"schema": {"type": "string", "const": "no-store"}},
    "Vary": {"schema": {"type": "string"}},
    "X-Content-Type-Options": {"schema": {"type": "string", "const": "nosniff"}},
}
_ERROR_MODEL = AuditHttpError | AuditPlainHttpError


def _responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {
        code: {
            "model": _ERROR_MODEL,
            "description": "A fixed non-reflective audit-feed error.",
            "headers": _PRIVATE_HEADERS,
        }
        for code in codes
    }


router = APIRouter(prefix="/api/v1/audit-events", tags=["audit"])


def _raise_unavailable() -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "audit_service_unavailable",
            "message": "audit resources are unavailable",
        },
    ) from None


def _raise_service_problem(error: AuditResourceServiceError) -> NoReturn:
    if error.code in {"runtime_human_required", "runtime_read_role_missing"}:
        code = "audit_forbidden"
        message = "audit read is forbidden"
        status_code = status.HTTP_403_FORBIDDEN
    elif error.code in {"audit_cursor_invalid", "audit_query_invalid"}:
        code = "audit_query_invalid"
        message = "audit query is invalid"
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    else:
        _raise_unavailable()
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    ) from None


def _expected_links(item: AuditResource) -> dict[str, str | None]:
    return {
        "run_url": None if item.run_id is None else f"/api/v1/runs/{item.run_id}",
        "step_url": (
            None
            if item.run_id is None or item.step_id is None
            else f"/api/v1/runs/{item.run_id}/steps/{item.step_id}"
        ),
        "action_url": (
            None if item.action_id is None else f"/api/v1/external-actions/{item.action_id}"
        ),
        "approval_url": (
            None
            if item.approval_request_id is None
            else f"/api/v1/approvals/{item.approval_request_id}"
        ),
        "artifact_url": (
            None if item.artifact_id is None else f"/api/v1/artifacts/{item.artifact_id}"
        ),
    }


def _event_view(item: AuditResource) -> AuditEventView:
    if type(item) is not AuditResource:
        _raise_unavailable()
    expected_links = _expected_links(item)
    if any(getattr(item, name, None) != value for name, value in expected_links.items()):
        _raise_unavailable()
    if (
        (item.run_id is None) != (item.run_sequence is None)
        or (item.step_id is not None and item.run_id is None)
        or (item.metadata_expired and item.metadata)
        or not isinstance(item.metadata, Mapping)
    ):
        _raise_unavailable()
    try:
        return AuditEventView(
            id=item.event_id,
            schema_version=item.schema_version,
            sequence=item.feed_sequence,
            run_sequence=item.run_sequence,
            run_id=item.run_id,
            schedule_id=item.schedule_id,
            occurrence_id=item.occurrence_id,
            event_type=item.event_type,
            aggregate_type=item.aggregate_type,
            aggregate_id=item.aggregate_id,
            outcome=item.outcome,
            actor_id=item.actor_id,
            actor_source=item.actor_source,
            auth_method=item.auth_method,
            correlation_id=item.correlation_id,
            occurred_at=item.occurred_at,
            step_id=item.step_id,
            action_id=item.action_id,
            action_attempt_number=item.action_attempt_number,
            receipt_id=item.receipt_id,
            approval_request_id=item.approval_request_id,
            approval_decision_id=item.approval_decision_id,
            artifact_id=item.artifact_id,
            attempt_id=item.attempt_id,
            attempted_command=item.attempted_command,
            expected_version=item.expected_version,
            observed_version=item.observed_version,
            observed_state=item.observed_state,
            requested_state=item.requested_state,
            mutation_version=item.mutation_version,
            transition_sequence=item.transition_sequence,
            previous_state=item.previous_state,
            new_state=item.new_state,
            reason_code=item.reason_code,
            metadata=dict(item.metadata),
            metadata_classification=item.metadata_classification,
            metadata_expires_at=item.metadata_expires_at,
            metadata_expired=item.metadata_expired,
            run_url=expected_links["run_url"],
            step_url=expected_links["step_url"],
            action_url=expected_links["action_url"],
            approval_url=expected_links["approval_url"],
            artifact_url=expected_links["artifact_url"],
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


@router.get(
    "",
    response_model=AuditEventListResponse,
    operation_id="listAuditEvents",
    responses={
        status.HTTP_200_OK: {
            "model": AuditEventListResponse,
            "description": "A high-watermark-bound page of redacted audit events.",
            "headers": _PRIVATE_HEADERS,
        },
        **_responses(400, 401, 403, 422, 503),
    },
)
async def list_audit_events(
    response: Response,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_runtime_resource_reader_principal),
    ],
    executor: Annotated[AuditResourceExecutor, Depends(get_audit_resource_executor)],
    run_id: Annotated[str | None, Query(pattern=_RESOURCE_ID_PATTERN)] = None,
    step_id: Annotated[str | None, Query(pattern=_RESOURCE_ID_PATTERN)] = None,
    action_id: Annotated[str | None, Query(pattern=_RESOURCE_ID_PATTERN)] = None,
    approval_id: Annotated[str | None, Query(pattern=_RESOURCE_ID_PATTERN)] = None,
    event_type: Annotated[str | None, Query(pattern=_EVENT_TYPE_PATTERN)] = None,
    occurred_at_from: datetime | None = None,
    occurred_at_to: datetime | None = None,
    cursor: Annotated[str | None, Query(max_length=MAX_AUDIT_CURSOR_LENGTH)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_AUDIT_PAGE_SIZE)] = DEFAULT_AUDIT_PAGE_SIZE,
) -> AuditEventListResponse:
    try:
        query = AuditListQuery(
            run_id=run_id,
            step_id=step_id,
            action_id=action_id,
            approval_id=approval_id,
            event_type=event_type,
            occurred_at_from=occurred_at_from,
            occurred_at_to=occurred_at_to,
            cursor=cursor,
            limit=limit,
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "audit_query_invalid", "message": "audit query is invalid"},
        ) from None
    try:
        page = await executor.list(query, principal=principal)
    except AuditResourceServiceError as error:
        _raise_service_problem(error)
    except Exception:
        _raise_unavailable()
    if (
        type(page) is not AuditPage
        or page.endpoint_version != AUDIT_FEED_ENDPOINT_VERSION
        or type(page.high_watermark) is not int
        or page.high_watermark < 0
        or type(page.items) is not tuple
        or len(page.items) > limit
        or any(type(item) is not AuditResource for item in page.items)
        or any(
            previous.feed_sequence <= current.feed_sequence
            for previous, current in zip(page.items, page.items[1:], strict=False)
        )
        or any(item.feed_sequence > page.high_watermark for item in page.items)
        or any(run_id is not None and item.run_id != run_id for item in page.items)
        or any(step_id is not None and item.step_id != step_id for item in page.items)
        or any(action_id is not None and item.action_id != action_id for item in page.items)
        or any(
            approval_id is not None and item.approval_request_id != approval_id
            for item in page.items
        )
        or any(event_type is not None and item.event_type != event_type for item in page.items)
        or any(
            occurred_at_from is not None and item.occurred_at < occurred_at_from
            for item in page.items
        )
        or any(
            occurred_at_to is not None and item.occurred_at > occurred_at_to for item in page.items
        )
        or (
            page.next_cursor is not None
            and (
                type(page.next_cursor) is not str
                or not page.next_cursor
                or len(page.next_cursor) > MAX_AUDIT_CURSOR_LENGTH
            )
        )
    ):
        _raise_unavailable()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Authorization"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return AuditEventListResponse(
        endpoint_version=page.endpoint_version,
        high_watermark=page.high_watermark,
        items=tuple(_event_view(item) for item in page.items),
        next_cursor=page.next_cursor,
    )


__all__ = ["router"]

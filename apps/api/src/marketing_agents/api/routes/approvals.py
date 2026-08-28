"""Authenticated approval inspection, request, approve, and reject routes."""

from __future__ import annotations

import re
import secrets
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from marketing_agents.api.correlation import request_correlation_id
from marketing_agents.api.dependencies import (
    ApprovalDecisionExecutor,
    ApprovalResourceExecutor,
    get_approval_decision_executor,
    get_approval_resource_executor,
    get_optional_approval_resource_executor,
    require_approval_principal,
    require_approval_reader_principal,
    require_approval_requester_principal,
)
from marketing_agents.api.schemas.approvals import (
    ApprovalDecisionInput,
    ApprovalDecisionResourceResponse,
    ApprovalDecisionResponse,
    ApprovalHttpError,
    ApprovalListResponse,
    ApprovalPlainHttpError,
    ApprovalRequestInput,
    ApprovalRequestResponse,
    ApprovalRequestValidationError,
    ApprovalResourceView,
    ApprovalSummaryView,
)
from marketing_agents.api.strict_json import (
    StrictJsonTransportError,
    strict_json_route_path,
    strict_json_transport_headers_are_valid,
    validate_strict_json_body,
)
from marketing_agents.application.services.approval_decisions import (
    ApprovalDecisionCommand,
    ApprovalDecisionServiceError,
    AuthorizedApprovalDecision,
)
from marketing_agents.application.services.approval_resources import (
    DEFAULT_APPROVAL_PAGE_SIZE,
    MAX_APPROVAL_CURSOR_LENGTH,
    MAX_APPROVAL_PAGE_SIZE,
    ApprovalListQuery,
    ApprovalPage,
    ApprovalRequestCommand,
    ApprovalRequestDisposition,
    ApprovalRequestResult,
    ApprovalResource,
    ApprovalResourceServiceError,
)
from marketing_agents.domain.enums import (
    ApprovalDecisionKind,
    ApprovalStatus,
    ExternalActionState,
)
from marketing_agents.domain.identity import AuthenticatedPrincipal

_RESOURCE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$"
_APPROVAL_REQUEST_PATH_PATTERN = re.compile(r"^/api/v1/external-actions/[^/]+/approval-requests/?$")
_MAX_APPROVAL_REQUEST_BYTES = 8_192
_MAX_APPROVAL_JSON_DEPTH = 16
_PRIVATE_RESPONSE_HEADERS = {
    "Cache-Control": {"schema": {"type": "string", "const": "no-store"}},
    "Vary": {"schema": {"type": "string"}},
}
_PRIVATE_LOCATION_HEADERS = {
    **_PRIVATE_RESPONSE_HEADERS,
    "Location": {"schema": {"type": "string"}},
}
_APPROVAL_ERROR_MODEL = ApprovalHttpError | ApprovalPlainHttpError | ApprovalRequestValidationError


def _error_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    return {
        status_code: {
            "model": _APPROVAL_ERROR_MODEL,
            "description": "A fixed non-reflective approval error.",
            "headers": _PRIVATE_RESPONSE_HEADERS,
        }
        for status_code in status_codes
    }


_READ_RESPONSES = _error_responses(400, 401, 403, 404, 422, 503)
_MUTATION_RESPONSES = _error_responses(400, 401, 403, 404, 409, 413, 415, 422, 503)
_FORBIDDEN_CODES = frozenset(
    {
        "human_approval_required",
        "approval_role_missing",
        "approval_scope_missing",
        "self_approval_forbidden",
    }
)
_NOT_FOUND_CODES = frozenset({"approval_request_missing", "approval_action_missing"})
_INPUT_CODES = frozenset({"approval_command_invalid"})
_SAFE_CONFLICT_CODES = frozenset(
    {
        "approval_generation_conflict",
        "approval_decision_conflict",
        "approval_hash_mismatch",
        "approval_action_conflict",
        "approval_expired",
    }
)
_RESOURCE_FORBIDDEN_CODES = frozenset(
    {
        "approval_human_required",
        "approval_read_role_missing",
        "approval_read_scope_missing",
        "approval_request_role_missing",
        "approval_request_scope_missing",
    }
)
_RESOURCE_INPUT_CODES = frozenset({"approval_query_invalid", "approval_cursor_invalid"})
_RESOURCE_UNAVAILABLE_CODES = frozenset({"approval_record_corrupt", "approval_service_unavailable"})
_RESOURCE_SAFE_CONFLICT_CODES = frozenset(
    {
        "approval_generation_conflict",
        "approval_request_conflict",
        "approval_hash_mismatch",
        "approval_expired",
        "approval_not_expired",
        "approval_renewal_conflict",
        "approval_expiration_conflict",
        "expected_hash_mismatch",
        "full_set_epoch_required",
    }
)

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])
action_router = APIRouter(prefix="/api/v1/external-actions", tags=["approvals"])


class ApprovalPrivateResponseMiddleware:
    """Bound approval bodies and make every approval response private."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = strict_json_route_path(scope)
        if scope["type"] != "http" or not (
            path == "/api/v1/approvals"
            or path.startswith("/api/v1/approvals/")
            or _APPROVAL_REQUEST_PATH_PATTERN.fullmatch(path) is not None
        ):
            await self._app(scope, receive, send)
            return

        response_started = False

        async def private_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["Vary"] = self._vary_with_authorization(headers.get("Vary"))
            await send(message)

        try:
            if scope.get("method") == "POST":
                if not strict_json_transport_headers_are_valid(scope):
                    await self._reject(
                        scope,
                        receive,
                        private_send,
                        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                        code="approval_json_required",
                        message="approval mutations require application/json",
                    )
                    return
                declared = self._declared_length(scope)
                if declared == "invalid":
                    await self._reject(
                        scope,
                        receive,
                        private_send,
                        status_code=status.HTTP_400_BAD_REQUEST,
                        code="approval_transport_invalid",
                        message="approval request transport is malformed",
                    )
                    return
                if declared == "too_large":
                    await self._reject_too_large(scope, receive, private_send)
                    return
                buffered = bytearray()
                while True:
                    message = await receive()
                    if message["type"] != "http.request":
                        await self._reject(
                            scope,
                            receive,
                            private_send,
                            status_code=status.HTTP_400_BAD_REQUEST,
                            code="approval_transport_invalid",
                            message="approval request transport is malformed",
                        )
                        return
                    chunk = message.get("body", b"")
                    if (
                        type(chunk) is not bytes
                        or len(buffered) + len(chunk) > _MAX_APPROVAL_REQUEST_BYTES
                    ):
                        await self._reject_too_large(scope, receive, private_send)
                        return
                    buffered.extend(chunk)
                    if not message.get("more_body", False):
                        break
                try:
                    validate_strict_json_body(
                        bytes(buffered),
                        max_depth=_MAX_APPROVAL_JSON_DEPTH,
                    )
                except StrictJsonTransportError:
                    await self._reject(
                        scope,
                        receive,
                        private_send,
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        code="approval_input_invalid",
                        message="approval request input is invalid",
                    )
                    return
                delivered = False

                async def bounded_receive() -> Message:
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {
                            "type": "http.request",
                            "body": bytes(buffered),
                            "more_body": False,
                        }
                    return await receive()

                await self._app(scope, bounded_receive, private_send)
                return
            await self._app(scope, receive, private_send)
        except Exception:
            if response_started:
                raise
            await self._reject(
                scope,
                receive,
                private_send,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="approval_service_unavailable",
                message="approval service is unavailable",
            )

    @staticmethod
    def _vary_with_authorization(existing: str | None) -> str:
        values = [] if existing is None else [item.strip() for item in existing.split(",")]
        if not any(item.casefold() == "authorization" for item in values):
            values.append("Authorization")
        return ", ".join(item for item in values if item)

    @staticmethod
    def _declared_length(scope: Scope) -> str | None:
        values = [
            value for name, value in scope.get("headers", ()) if name.lower() == b"content-length"
        ]
        if not values:
            return None
        if len(values) != 1:
            return "invalid"
        try:
            raw = values[0].decode("ascii")
            parsed = int(raw)
        except (UnicodeDecodeError, ValueError):
            return "invalid"
        if str(parsed) != raw or parsed < 0:
            return "invalid"
        return "too_large" if parsed > _MAX_APPROVAL_REQUEST_BYTES else None

    @staticmethod
    async def _reject_too_large(scope: Scope, receive: Receive, send: Send) -> None:
        await ApprovalPrivateResponseMiddleware._reject(
            scope,
            receive,
            send,
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            code="approval_body_too_large",
            message="approval request body exceeds the allowed size",
        )

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        code: str,
        message: str,
    ) -> None:
        response = JSONResponse(
            status_code=status_code,
            content={"detail": {"code": code, "message": message}},
        )
        await response(scope, receive, send)


def _private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = ApprovalPrivateResponseMiddleware._vary_with_authorization(
        response.headers.get("Vary")
    )


def _require_json_transport(request: Request) -> None:
    if not strict_json_transport_headers_are_valid(request.scope):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "code": "approval_json_required",
                "message": "approval mutations require application/json",
            },
        )


def _raise_safe_service_problem(
    error: ApprovalDecisionServiceError,
    *,
    current: ApprovalResourceView | None = None,
) -> NoReturn:
    if error.code in _FORBIDDEN_CODES:
        status_code = status.HTTP_403_FORBIDDEN
        code = error.code
        message = "approval decision is forbidden"
    elif error.code in _NOT_FOUND_CODES:
        status_code = status.HTTP_404_NOT_FOUND
        code = "approval_not_found"
        message = "approval request was not found"
    elif error.code in _INPUT_CODES:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        code = "approval_input_invalid"
        message = "approval decision input is invalid"
    else:
        status_code = status.HTTP_409_CONFLICT
        code = error.code if error.code in _SAFE_CONFLICT_CODES else "approval_conflict"
        message = "approval request could not be decided"
    detail: dict[str, object] = {"code": code, "message": message}
    if status_code == status.HTTP_409_CONFLICT and current is not None:
        detail["current_status"] = current.status
        detail["current_resource_version"] = current.resource_version
    raise HTTPException(
        status_code=status_code,
        detail=detail,
    ) from None


def _raise_resource_problem(error: ApprovalResourceServiceError) -> NoReturn:
    if error.code in _RESOURCE_FORBIDDEN_CODES:
        status_code = status.HTTP_403_FORBIDDEN
        code = "approval_forbidden"
        message = "approval operation is forbidden"
    elif error.code in _NOT_FOUND_CODES:
        status_code = status.HTTP_404_NOT_FOUND
        code = "approval_not_found"
        message = "approval resource was not found"
    elif error.code in _RESOURCE_INPUT_CODES:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        code = "approval_input_invalid"
        message = "approval request input is invalid"
    elif error.code in _RESOURCE_UNAVAILABLE_CODES:
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        code = "approval_service_unavailable"
        message = "approval service is unavailable"
    else:
        status_code = status.HTTP_409_CONFLICT
        code = (
            error.code
            if error.code in _RESOURCE_SAFE_CONFLICT_CODES
            else "approval_request_conflict"
        )
        message = "approval request could not be created"
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    ) from None


def _raise_unavailable() -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "approval_service_unavailable",
            "message": "approval service is unavailable",
        },
    ) from None


def _raise_decision_contract_conflict() -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "approval_conflict",
            "message": "approval request could not be decided",
        },
    ) from None


def _resource_view(resource: ApprovalResource) -> ApprovalResourceView:
    if type(resource) is not ApprovalResource:
        _raise_unavailable()
    expected_urls = {
        "approval_url": f"/api/v1/approvals/{resource.approval_id}",
        "action_url": f"/api/v1/external-actions/{resource.action_id}",
        "run_url": f"/api/v1/runs/{resource.run_id}",
        "step_url": f"/api/v1/runs/{resource.run_id}/steps/{resource.step_id}",
        "template_url": f"/api/v1/agent-templates/{resource.template_id}",
        "instance_url": f"/api/v1/agent-instances/{resource.instance_id}",
    }
    if any(getattr(resource, name, None) != value for name, value in expected_urls.items()):
        _raise_unavailable()
    try:
        return ApprovalResourceView(
            id=resource.approval_id,
            status=resource.status.value,
            resource_version=resource.resource_version,
            generation=resource.generation,
            one_time_use_state=resource.one_time_use_state,
            action_id=resource.action_id,
            action_type=resource.action_type,
            capability_id=resource.capability_id,
            connector_family=resource.connector_family,
            binding_id=resource.binding_id,
            destination_summary=resource.destination_summary,
            redacted_payload=dict(resource.redacted_payload),
            payload_hash=resource.payload_hash,
            run_id=resource.run_id,
            step_id=resource.step_id,
            template_id=resource.template_id,
            instance_id=resource.instance_id,
            policy_id=resource.policy_id,
            required_roles=resource.required_roles,
            required_scopes=resource.required_scopes,
            allow_self_approval=resource.allow_self_approval,
            requested_by=resource.requested_by,
            requested_at=resource.requested_at,
            expires_at=resource.expires_at,
            updated_at=resource.updated_at,
            is_expired=resource.is_expired,
            is_actionable=resource.is_actionable,
            decision_id=resource.decision_id,
            decision_kind=resource.decision_kind,
            decision_actor_id=resource.decision_actor_id,
            decision_reason_code=resource.decision_reason_code,
            decision_reason=resource.decision_reason,
            decided_at=resource.decided_at,
            expired_at=resource.expired_at,
            replacement_approval_id=resource.replacement_approval_id,
            renewed_at=resource.renewed_at,
            superseded_at=resource.superseded_at,
            superseded_reason_code=resource.superseded_reason_code,
            consumed_at=resource.consumed_at,
            **expected_urls,
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


def _summary_view(resource: ApprovalResource) -> ApprovalSummaryView:
    full = _resource_view(resource)
    return ApprovalSummaryView(
        id=full.id,
        status=full.status,
        resource_version=full.resource_version,
        generation=full.generation,
        action_id=full.action_id,
        action_type=full.action_type,
        destination_summary=full.destination_summary,
        run_id=full.run_id,
        template_id=full.template_id,
        instance_id=full.instance_id,
        requested_at=full.requested_at,
        expires_at=full.expires_at,
        is_expired=full.is_expired,
        is_actionable=full.is_actionable,
        approval_url=full.approval_url,
        action_url=full.action_url,
        run_url=full.run_url,
    )


@router.get(
    "",
    response_model=ApprovalListResponse,
    operation_id="listApprovals",
    responses={
        status.HTTP_200_OK: {
            "model": ApprovalListResponse,
            "description": "A bounded page of approval summaries.",
            "headers": _PRIVATE_RESPONSE_HEADERS,
        },
        **_READ_RESPONSES,
    },
)
async def list_approvals(
    request: Request,
    response: Response,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_approval_reader_principal),
    ],
    executor: Annotated[
        ApprovalResourceExecutor,
        Depends(get_approval_resource_executor),
    ],
    approval_status: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
    run_id: Annotated[str | None, Query(pattern=_RESOURCE_ID_PATTERN)] = None,
    action_id: Annotated[str | None, Query(pattern=_RESOURCE_ID_PATTERN)] = None,
    cursor: Annotated[str | None, Query(max_length=MAX_APPROVAL_CURSOR_LENGTH)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_APPROVAL_PAGE_SIZE)] = DEFAULT_APPROVAL_PAGE_SIZE,
) -> ApprovalListResponse:
    for name in ("status", "run_id", "action_id", "cursor", "limit"):
        if len(request.query_params.getlist(name)) > 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "approval_query_ambiguous",
                    "message": "approval query parameters must be unique",
                },
            )
    try:
        query = ApprovalListQuery(
            status=approval_status,
            run_id=run_id,
            action_id=action_id,
            cursor=cursor,
            limit=limit,
        )
        page = await executor.list(query, principal=principal)
    except ApprovalResourceServiceError as error:
        _raise_resource_problem(error)
    except Exception:
        _raise_unavailable()
    if (
        type(page) is not ApprovalPage
        or type(page.items) is not tuple
        or len(page.items) > limit
        or any(type(item) is not ApprovalResource for item in page.items)
        or (
            page.next_cursor is not None
            and (
                type(page.next_cursor) is not str
                or not page.next_cursor
                or len(page.next_cursor) > MAX_APPROVAL_CURSOR_LENGTH
            )
        )
    ):
        _raise_unavailable()
    _private_response(response)
    return ApprovalListResponse(
        items=tuple(_summary_view(item) for item in page.items),
        next_cursor=page.next_cursor,
    )


@router.get(
    "/{approval_id}",
    response_model=ApprovalResourceView,
    operation_id="getApproval",
    responses={
        status.HTTP_200_OK: {
            "model": ApprovalResourceView,
            "description": "The authorized safe approval resource.",
            "headers": _PRIVATE_RESPONSE_HEADERS,
        },
        **_READ_RESPONSES,
    },
)
async def get_approval(
    approval_id: Annotated[str, Path(pattern=_RESOURCE_ID_PATTERN)],
    response: Response,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_approval_reader_principal),
    ],
    executor: Annotated[
        ApprovalResourceExecutor,
        Depends(get_approval_resource_executor),
    ],
) -> ApprovalResourceView:
    try:
        resource = await executor.read(approval_id, principal=principal)
    except ApprovalResourceServiceError as error:
        _raise_resource_problem(error)
    except Exception:
        _raise_unavailable()
    if type(resource) is not ApprovalResource or resource.approval_id != approval_id:
        _raise_unavailable()
    _private_response(response)
    return _resource_view(resource)


@action_router.post(
    "/{action_id}/approval-requests",
    response_model=ApprovalRequestResponse,
    status_code=status.HTTP_200_OK,
    operation_id="createApprovalRequest",
    responses={
        status.HTTP_200_OK: {
            "model": ApprovalRequestResponse,
            "description": "The current exact approval already exists.",
            "headers": _PRIVATE_LOCATION_HEADERS,
        },
        status.HTTP_201_CREATED: {
            "model": ApprovalRequestResponse,
            "description": "An exact expired approval was renewed.",
            "headers": _PRIVATE_LOCATION_HEADERS,
        },
        **_MUTATION_RESPONSES,
    },
)
async def create_approval_request(
    action_id: Annotated[str, Path(pattern=_RESOURCE_ID_PATTERN)],
    request: Request,
    response: Response,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_approval_requester_principal),
    ],
    executor: Annotated[
        ApprovalResourceExecutor,
        Depends(get_approval_resource_executor),
    ],
    _transport: Annotated[None, Depends(_require_json_transport)],
    body: ApprovalRequestInput,
) -> ApprovalRequestResponse:
    correlation_id = request_correlation_id(request)
    try:
        command = ApprovalRequestCommand(
            action_id=action_id,
            expected_generation=body.expected_generation,
            expected_action_hash=body.expected_payload_hash,
            correlation_id=correlation_id,
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "approval_input_invalid",
                "message": "approval request input is invalid",
            },
        ) from None
    try:
        result = await executor.request(command, principal=principal)
    except ApprovalResourceServiceError as error:
        _raise_resource_problem(error)
    except Exception:
        _raise_unavailable()
    if (
        type(result) is not ApprovalRequestResult
        or type(result.disposition) is not ApprovalRequestDisposition
        or type(result.approval) is not ApprovalResource
    ):
        _raise_unavailable()
    approval_view = _resource_view(result.approval)
    generation_is_valid = (
        approval_view.generation >= 1
        if body.expected_generation == 0
        else approval_view.generation == body.expected_generation + 1
    )
    pristine_pending = (
        approval_view.status == ApprovalStatus.PENDING.value
        and approval_view.one_time_use_state == "unused"
        and not approval_view.is_expired
        and approval_view.is_actionable
        and all(
            value is None
            for value in (
                approval_view.decision_id,
                approval_view.decision_kind,
                approval_view.decision_actor_id,
                approval_view.decision_reason_code,
                approval_view.decision_reason,
                approval_view.decided_at,
                approval_view.expired_at,
                approval_view.replacement_approval_id,
                approval_view.renewed_at,
                approval_view.superseded_at,
                approval_view.superseded_reason_code,
                approval_view.consumed_at,
            )
        )
    )
    reusable_existing = (
        approval_view.status
        in {
            ApprovalStatus.PENDING.value,
            ApprovalStatus.APPROVED.value,
        }
        and approval_view.one_time_use_state == "unused"
        and not approval_view.is_expired
        and approval_view.is_actionable == (approval_view.status == ApprovalStatus.PENDING.value)
    )
    if (
        not generation_is_valid
        or approval_view.action_id != action_id
        or not secrets.compare_digest(approval_view.payload_hash, body.expected_payload_hash)
        or (
            body.expected_generation == 0
            and (
                result.disposition is not ApprovalRequestDisposition.EXISTING
                or not reusable_existing
            )
        )
        or (body.expected_generation > 0 and not pristine_pending)
        or (
            result.disposition is ApprovalRequestDisposition.RENEWED
            and approval_view.requested_by != principal.actor_id
        )
    ):
        _raise_unavailable()
    _private_response(response)
    response.headers["Location"] = approval_view.approval_url
    if result.disposition is ApprovalRequestDisposition.RENEWED:
        response.status_code = status.HTTP_201_CREATED
    return ApprovalRequestResponse(
        disposition=result.disposition.value,
        approval=approval_view,
    )


async def _decide(
    *,
    approval_id: str,
    request: Request,
    body: ApprovalDecisionInput,
    decision: ApprovalDecisionKind,
    principal: AuthenticatedPrincipal,
    executor: ApprovalDecisionExecutor,
    resource_executor: ApprovalResourceExecutor | None,
    response: Response,
) -> ApprovalDecisionResourceResponse | ApprovalDecisionResponse:
    correlation_id = request_correlation_id(request)
    try:
        command = ApprovalDecisionCommand(
            request_id=approval_id,
            expected_generation=body.expected_generation,
            expected_action_hash=body.expected_payload_hash,
            decision=decision,
            correlation_id=correlation_id,
            reason=body.reason_text(),
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "approval_input_invalid",
                "message": "approval decision input is invalid",
            },
        ) from None
    try:
        result = await executor.decide(command, principal=principal)
    except ApprovalDecisionServiceError as error:
        current: ApprovalResourceView | None = None
        if error.code == "approval_decision_conflict" and resource_executor is not None:
            try:
                current = _resource_view(
                    await resource_executor.read(
                        approval_id,
                        principal=principal,
                    )
                )
            except Exception:
                current = None
        _raise_safe_service_problem(error, current=current)
    except Exception:
        _raise_unavailable()
    expected_status = (
        ApprovalStatus.APPROVED
        if decision is ApprovalDecisionKind.APPROVE
        else ApprovalStatus.REJECTED
    )
    expected_action_state = (
        ExternalActionState.APPROVED
        if decision is ApprovalDecisionKind.APPROVE
        else ExternalActionState.REJECTED
    )
    if type(result) is not AuthorizedApprovalDecision:
        _raise_decision_contract_conflict()
    try:
        decided_request = result.request.request
        recorded = result.decision
        action = result.action
        contract_matches = (
            decided_request.id == approval_id
            and decided_request.generation == body.expected_generation
            and result.request.status is expected_status
            and result.request.decision == recorded
            and action.state is expected_action_state
            and action.id == decided_request.action_id
            and secrets.compare_digest(action.action_hash, decided_request.action_hash)
            and secrets.compare_digest(
                decided_request.action_hash,
                body.expected_payload_hash,
            )
            and action.run_id == decided_request.run_id
            and recorded.decision is decision
            and recorded.request_id == decided_request.id
            and recorded.action_id == action.id
            and secrets.compare_digest(recorded.action_hash, action.action_hash)
            and recorded.run_id == decided_request.run_id
            and recorded.actor_id == principal.actor_id
            and recorded.authentication_method == principal.authentication_method.value
            and recorded.correlation_id == correlation_id
            and recorded.reason == body.reason_text()
        )
    except (AttributeError, TypeError, ValueError):
        contract_matches = False
    if not contract_matches:
        _raise_decision_contract_conflict()

    authoritative: ApprovalResourceView | None = None
    if resource_executor is not None:
        try:
            authoritative_resource = await resource_executor.read(
                approval_id,
                principal=principal,
            )
            candidate = _resource_view(authoritative_resource)
            allowed_statuses = (
                {ApprovalStatus.APPROVED.value, ApprovalStatus.CONSUMED.value}
                if decision is ApprovalDecisionKind.APPROVE
                else {ApprovalStatus.REJECTED.value}
            )
            expected_reason_code = (
                "approval_granted"
                if decision is ApprovalDecisionKind.APPROVE
                else "approval_rejected"
            )
            if (
                candidate.id == approval_id
                and candidate.action_id == action.id
                and candidate.run_id == decided_request.run_id
                and candidate.generation == decided_request.generation
                and secrets.compare_digest(candidate.payload_hash, action.action_hash)
                and candidate.resource_version >= result.request.version
                and candidate.status in allowed_statuses
                and candidate.decision_id == recorded.id
                and candidate.decision_kind == decision.value
                and candidate.decision_actor_id == principal.actor_id
                and candidate.decision_reason_code == expected_reason_code
                and candidate.decision_reason == recorded.reason
            ):
                authoritative = candidate
        except Exception:
            authoritative = None
    _private_response(response)
    response.headers["Location"] = f"/api/v1/approvals/{approval_id}"
    response_fields = {
        "approval_id": decided_request.id,
        "decision_id": recorded.id,
        "action_id": action.id,
        "run_id": decided_request.run_id,
        "status": result.request.status.value,
    }
    if authoritative is not None:
        return ApprovalDecisionResourceResponse(
            **response_fields,
            approval=authoritative,
        )
    return ApprovalDecisionResponse(**response_fields)


@router.post(
    "/{approval_id}/approve",
    response_model=ApprovalDecisionResourceResponse | ApprovalDecisionResponse,
    operation_id="approveApproval",
    responses={
        status.HTTP_200_OK: {
            "description": "The approval was decided without inline connector execution.",
            "headers": _PRIVATE_LOCATION_HEADERS,
        },
        **_MUTATION_RESPONSES,
    },
)
async def approve_approval(
    approval_id: Annotated[str, Path(pattern=_RESOURCE_ID_PATTERN)],
    request: Request,
    response: Response,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_approval_principal),
    ],
    executor: Annotated[
        ApprovalDecisionExecutor,
        Depends(get_approval_decision_executor),
    ],
    resource_executor: Annotated[
        ApprovalResourceExecutor | None,
        Depends(get_optional_approval_resource_executor),
    ],
    _transport: Annotated[None, Depends(_require_json_transport)],
    body: ApprovalDecisionInput,
) -> ApprovalDecisionResourceResponse | ApprovalDecisionResponse:
    return await _decide(
        approval_id=approval_id,
        request=request,
        body=body,
        decision=ApprovalDecisionKind.APPROVE,
        principal=principal,
        executor=executor,
        resource_executor=resource_executor,
        response=response,
    )


@router.post(
    "/{approval_id}/reject",
    response_model=ApprovalDecisionResourceResponse | ApprovalDecisionResponse,
    operation_id="rejectApproval",
    responses={
        status.HTTP_200_OK: {
            "description": "The approval was decided without inline connector execution.",
            "headers": _PRIVATE_LOCATION_HEADERS,
        },
        **_MUTATION_RESPONSES,
    },
)
async def reject_approval(
    approval_id: Annotated[str, Path(pattern=_RESOURCE_ID_PATTERN)],
    request: Request,
    response: Response,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_approval_principal),
    ],
    executor: Annotated[
        ApprovalDecisionExecutor,
        Depends(get_approval_decision_executor),
    ],
    resource_executor: Annotated[
        ApprovalResourceExecutor | None,
        Depends(get_optional_approval_resource_executor),
    ],
    _transport: Annotated[None, Depends(_require_json_transport)],
    body: ApprovalDecisionInput,
) -> ApprovalDecisionResourceResponse | ApprovalDecisionResponse:
    return await _decide(
        approval_id=approval_id,
        request=request,
        body=body,
        decision=ApprovalDecisionKind.REJECT,
        principal=principal,
        executor=executor,
        resource_executor=resource_executor,
        response=response,
    )


__all__ = ["ApprovalPrivateResponseMiddleware", "action_router", "router"]

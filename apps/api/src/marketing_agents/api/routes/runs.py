"""Authenticated runtime inspection routes and private-response boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, NoReturn, cast

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from marketing_agents.api.dependencies import (
    RunResourceExecutor,
    get_run_resource_executor,
    require_runtime_resource_reader_principal,
)
from marketing_agents.api.schemas.artifacts import ArtifactSummaryView
from marketing_agents.api.schemas.runs import (
    ExternalActionView,
    InstanceRuntimeStatusView,
    InstanceStatusSummaryResponse,
    PendingApprovalSummaryView,
    RunExecutionControlView,
    RunHttpError,
    RunListResponse,
    RunPlainHttpError,
    RunPlanView,
    RunResourceView,
    RunRoutingAssignmentView,
    RunRuntimePolicyView,
    RunSelectedInstanceView,
    RunStepTransitionView,
    RunStepView,
    RunSummaryView,
    RunTerminalErrorView,
    RunTimelineEventView,
    RunTimelineResponse,
    RunTransitionView,
    StepRuntimePolicyView,
)
from marketing_agents.application.services.artifact_resources import ArtifactSummary
from marketing_agents.application.services.run_resources import (
    DEFAULT_RUN_PAGE_SIZE,
    DEFAULT_TIMELINE_PAGE_SIZE,
    MAX_RUN_CURSOR_LENGTH,
    MAX_RUN_PAGE_SIZE,
    MAX_TIMELINE_PAGE_SIZE,
    ExternalActionResource,
    InstanceRuntimeStatus,
    InstanceStatusSummary,
    PendingApprovalSummary,
    RunExecutionControlResource,
    RunListQuery,
    RunPage,
    RunPlanResource,
    RunPlanSelectedInstanceResource,
    RunResource,
    RunResourceServiceError,
    RunRoutingAssignmentResource,
    RunStepResource,
    RunStepTransitionResource,
    RunTerminalErrorResource,
    RunTimelineEvent,
    RunTimelinePage,
    RunTimelineQuery,
    RunTransitionResource,
)
from marketing_agents.domain.enums import RunState
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.security.redaction import redact

_RUN_PATH = re.compile(r"^/api/v1/runs(?:/.*)?$")
_ARTIFACT_PATH = re.compile(r"^/api/v1/artifacts/[^/]+/?$")
_ACTION_PATH = re.compile(r"^/api/v1/external-actions/[^/]+/?$")
_AUDIT_PATH = re.compile(r"^/api/v1/audit-events/?$")
_STATUS_SUMMARY_PATH = "/api/v1/agent-instances/status-summary"
_INSTANCE_DETAIL_PATH = re.compile(
    r"^/api/v1/agent-instances/"
    r"inst\.[a-z0-9-]+\.[a-z0-9-]+\.[a-z0-9-]+\.[0-9]{2}/?$"
)
_STATUS_CACHE_CONTROL = "private, no-cache, max-age=0"
_RUNTIME_DETAIL_CACHE_CONTROL = "private, no-cache"
_RESOURCE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$"
_STATUS_ETAG_PATTERN = re.compile(r'^"instance-status-sha256-v1:[0-9a-f]{64}"$')
_MAX_IF_NONE_MATCH_LENGTH = 8_192
_PRIVATE_HEADERS = {
    "Cache-Control": {"schema": {"type": "string", "const": "no-store"}},
    "Vary": {"schema": {"type": "string"}},
    "X-Content-Type-Options": {"schema": {"type": "string", "const": "nosniff"}},
}
_STATUS_HEADERS = {
    "Cache-Control": {"schema": {"type": "string", "const": _STATUS_CACHE_CONTROL}},
    "ETag": {
        "schema": {
            "type": "string",
            "pattern": '^"instance-status-sha256-v1:[0-9a-f]{64}"$',
        }
    },
    "Vary": {"schema": {"type": "string"}},
    "X-Content-Type-Options": {"schema": {"type": "string", "const": "nosniff"}},
}
_ERROR_MODEL = RunHttpError | RunPlainHttpError

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])
external_action_router = APIRouter(prefix="/api/v1/external-actions", tags=["runs"])
instance_status_router = APIRouter(prefix="/api/v1/agent-instances", tags=["runs"])


class Api07PrivateResponseMiddleware:
    """Make API-07 responses private, inert JSON even on outer middleware failures."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if scope["type"] != "http" or not self._matches(scope, path):
            await self._app(scope, receive, send)
            return
        response_started = False
        runtime_instance_detail = self._runtime_instance_detail_is_configured(scope, path)

        async def private_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(scope=message)
                response_status = int(message.get("status", 0))
                if path == _STATUS_SUMMARY_PATH and response_status in {
                    status.HTTP_200_OK,
                    status.HTTP_304_NOT_MODIFIED,
                }:
                    if headers.get("Cache-Control") != _STATUS_CACHE_CONTROL:
                        headers["Cache-Control"] = _STATUS_CACHE_CONTROL
                elif runtime_instance_detail and response_status in {
                    status.HTTP_200_OK,
                    status.HTTP_304_NOT_MODIFIED,
                }:
                    if headers.get("Cache-Control") != _RUNTIME_DETAIL_CACHE_CONTROL:
                        headers["Cache-Control"] = _RUNTIME_DETAIL_CACHE_CONTROL
                else:
                    headers["Cache-Control"] = "no-store"
                headers["Vary"] = self._vary_with_authorization(headers.get("Vary"))
                headers["X-Content-Type-Options"] = "nosniff"
            await send(message)

        try:
            await self._app(scope, receive, private_send)
        except Exception:
            if response_started:
                raise
            response = JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "detail": {
                        "code": "runtime_service_unavailable",
                        "message": "runtime resources are unavailable",
                    }
                },
            )
            await response(scope, receive, private_send)

    @classmethod
    def _matches(cls, scope: Scope, path: str) -> bool:
        return (
            _RUN_PATH.fullmatch(path) is not None
            or _ARTIFACT_PATH.fullmatch(path) is not None
            or _ACTION_PATH.fullmatch(path) is not None
            or _AUDIT_PATH.fullmatch(path) is not None
            or path.rstrip("/") == _STATUS_SUMMARY_PATH
            or cls._runtime_instance_detail_is_configured(scope, path)
        )

    @staticmethod
    def _runtime_instance_detail_is_configured(scope: Scope, path: str) -> bool:
        if _INSTANCE_DETAIL_PATH.fullmatch(path) is None:
            return False
        try:
            application = scope["app"]
            return getattr(application.state, "run_resource_service", None) is not None
        except Exception:
            return True

    @staticmethod
    def _vary_with_authorization(existing: str | None) -> str:
        values = [] if existing is None else [item.strip() for item in existing.split(",")]
        if not any(item.casefold() == "authorization" for item in values):
            values.append("Authorization")
        return ", ".join(item for item in values if item)


def _responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {
        code: {
            "model": _ERROR_MODEL,
            "description": "A fixed non-reflective runtime-resource error.",
            "headers": _PRIVATE_HEADERS,
        }
        for code in codes
    }


def _private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = Api07PrivateResponseMiddleware._vary_with_authorization(
        response.headers.get("Vary")
    )
    response.headers["X-Content-Type-Options"] = "nosniff"


def _raise_unavailable() -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "runtime_service_unavailable",
            "message": "runtime resources are unavailable",
        },
    ) from None


def _raise_service_problem(error: RunResourceServiceError) -> NoReturn:
    if error.code in {"runtime_human_required", "runtime_read_role_missing"}:
        status_code = status.HTTP_403_FORBIDDEN
        code = "runtime_read_forbidden"
        message = "runtime resource read is forbidden"
    elif error.code in {
        "run_not_found",
        "run_step_not_found",
        "external_action_not_found",
        "agent_instance_not_found",
    }:
        status_code = status.HTTP_404_NOT_FOUND
        code = error.code
        message = {
            "run_not_found": "run was not found",
            "run_step_not_found": "run step was not found",
            "external_action_not_found": "external action was not found",
            "agent_instance_not_found": "agent instance was not found",
        }[error.code]
    elif error.code in {
        "run_cursor_invalid",
        "run_query_invalid",
        "run_timeline_cursor_invalid",
        "run_timeline_query_invalid",
        "run_id_invalid",
        "run_step_id_invalid",
        "external_action_id_invalid",
        "recent_run_limit_invalid",
    }:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        code = "runtime_query_invalid"
        message = "runtime query is invalid"
    else:
        _raise_unavailable()
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    ) from None


def _run_links(item: RunResource) -> dict[str, str]:
    return {
        "run_url": f"/api/v1/runs/{item.run_id}",
        "timeline_url": f"/api/v1/runs/{item.run_id}/timeline",
        "artifacts_url": f"/api/v1/runs/{item.run_id}/artifacts",
        "instance_url": f"/api/v1/agent-instances/{item.instance_id}",
    }


def _summary_view(item: RunResource) -> RunSummaryView:
    if type(item) is not RunResource:
        _raise_unavailable()
    links = _run_links(item)
    if any(getattr(item, name, None) != value for name, value in links.items()):
        _raise_unavailable()
    try:
        return RunSummaryView(
            id=item.run_id,
            work_item_id=item.work_item_id,
            instance_id=item.instance_id,
            workflow_id=item.workflow_id,
            trigger_id=item.trigger_id,
            source=item.source,
            mode=cast(Any, item.mode),
            state=cast(Any, item.state),
            catalog_hash=item.catalog_hash,
            configuration_revision=item.configuration_revision,
            approval_required=item.approval_required,
            terminal_reason_code=item.terminal_reason_code,
            created_at=item.created_at,
            updated_at=item.updated_at,
            version=item.version,
            **links,
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


def _transition_view(item: RunTransitionResource) -> RunTransitionView:
    if type(item) is not RunTransitionResource:
        _raise_unavailable()
    try:
        return RunTransitionView(
            sequence=item.sequence,
            command=item.command,
            previous_state=cast(Any, item.previous_state),
            new_state=cast(Any, item.new_state),
            reason_code=item.reason_code,
            occurred_at=item.occurred_at,
            expected_version=item.expected_version,
            resulting_version=item.resulting_version,
            completed_effect_count=item.completed_effect_count,
            outcome_unknown_effect_count=item.outcome_unknown_effect_count,
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


def _step_view(item: RunStepResource) -> RunStepView:
    if (
        type(item) is not RunStepResource
        or not isinstance(item.runtime_policy, Mapping)
        or type(item.transitions) is not tuple
        or any(type(value) is not RunStepTransitionResource for value in item.transitions)
        or (
            item.transitions
            and (
                tuple(value.sequence for value in item.transitions)
                != tuple(range(1, len(item.transitions) + 1))
                or item.transitions[-1].new_state != item.state
                or item.transitions[-1].resulting_version != item.version
            )
        )
    ):
        _raise_unavailable()
    links = {
        "step_url": f"/api/v1/runs/{item.run_id}/steps/{item.step_id}",
        "run_url": f"/api/v1/runs/{item.run_id}",
        "instance_url": f"/api/v1/agent-instances/{item.selected_instance_id}",
        "template_url": f"/api/v1/agent-templates/{item.template_id}",
    }
    if any(getattr(item, name, None) != value for name, value in links.items()):
        _raise_unavailable()
    try:
        return RunStepView(
            id=item.step_id,
            run_id=item.run_id,
            key=item.key,
            kind=item.kind,
            selected_instance_id=item.selected_instance_id,
            template_id=item.template_id,
            dependency_keys=item.dependency_keys,
            capability_id=item.capability_id,
            effect=cast(Any, item.effect),
            state=cast(Any, item.state),
            ordinal=item.ordinal,
            source_order=item.source_order,
            configuration_revision=item.configuration_revision,
            connector_family=item.connector_family,
            routing_slot_key=item.routing_slot_key,
            binding_id=item.binding_id,
            binding_configuration_revision=item.binding_configuration_revision,
            request_schema_id=item.request_schema_id,
            result_schema_id=item.result_schema_id,
            result_schema_hash=item.result_schema_hash,
            data_classification=cast(Any, item.data_classification),
            idempotency_support=cast(Any, item.idempotency_support),
            timeout_seconds=item.timeout_seconds,
            runtime_policy=StepRuntimePolicyView.model_validate(dict(item.runtime_policy)),
            approval_policy_id=item.approval_policy_id,
            approval_required_roles=item.approval_required_roles,
            approval_required_scopes=item.approval_required_scopes,
            approval_expires_after_seconds=item.approval_expires_after_seconds,
            approval_allow_self_approval=item.approval_allow_self_approval,
            terminal_result=item.terminal_result,
            created_at=item.created_at,
            updated_at=item.updated_at,
            version=item.version,
            terminal_reason_code=item.terminal_reason_code,
            transitions=tuple(
                RunStepTransitionView(
                    sequence=value.sequence,
                    command=value.command,
                    previous_state=cast(Any, value.previous_state),
                    new_state=cast(Any, value.new_state),
                    reason_code=value.reason_code,
                    occurred_at=value.occurred_at,
                    expected_version=value.expected_version,
                    resulting_version=value.resulting_version,
                )
                for value in item.transitions
            ),
            **links,
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


def _selected_instance_view(
    item: RunPlanSelectedInstanceResource,
) -> RunSelectedInstanceView:
    if type(item) is not RunPlanSelectedInstanceResource:
        _raise_unavailable()
    expected = {
        "instance_url": f"/api/v1/agent-instances/{item.instance_id}",
        "template_url": f"/api/v1/agent-templates/{item.template_id}",
    }
    if any(getattr(item, name, None) != value for name, value in expected.items()):
        _raise_unavailable()
    try:
        return RunSelectedInstanceView(
            instance_id=item.instance_id,
            template_id=item.template_id,
            configuration_revision=item.configuration_revision,
            display_order=item.display_order,
            source_ordinal=item.source_ordinal,
            selection_order=item.selection_order,
            target=item.target,
            **expected,
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


def _routing_assignment_view(
    item: RunRoutingAssignmentResource,
) -> RunRoutingAssignmentView:
    if type(item) is not RunRoutingAssignmentResource:
        _raise_unavailable()
    expected = {
        "instance_url": f"/api/v1/agent-instances/{item.instance_id}",
        "template_url": f"/api/v1/agent-templates/{item.template_id}",
    }
    if any(getattr(item, name, None) != value for name, value in expected.items()):
        _raise_unavailable()
    try:
        return RunRoutingAssignmentView(
            slot_key=item.slot_key,
            instance_id=item.instance_id,
            template_id=item.template_id,
            required_capability_ids=item.required_capability_ids,
            assignment_order=item.assignment_order,
            **expected,
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


def _plan_view(item: RunPlanResource) -> RunPlanView:
    if (
        type(item) is not RunPlanResource
        or type(item.selected_instances) is not tuple
        or type(item.routing_assignments) is not tuple
        or type(item.steps) is not tuple
        or not isinstance(item.runtime_policy, Mapping)
        or len(item.steps) != item.step_count
        or tuple(value.selection_order for value in item.selected_instances)
        != tuple(range(1, len(item.selected_instances) + 1))
        or tuple(value.assignment_order for value in item.routing_assignments)
        != tuple(range(1, len(item.routing_assignments) + 1))
        or tuple(value.ordinal for value in item.steps) != tuple(range(1, len(item.steps) + 1))
    ):
        _raise_unavailable()
    try:
        return RunPlanView(
            plan_hash=item.plan_hash,
            workflow_id=item.workflow_id,
            workflow_version=item.workflow_version,
            workflow_definition_hash=item.workflow_definition_hash,
            catalog_content_hash=item.catalog_content_hash,
            graph_hash=item.graph_hash,
            routing_hash=item.routing_hash,
            approval_required=item.approval_required,
            step_count=item.step_count,
            runtime_policy=RunRuntimePolicyView.model_validate(dict(item.runtime_policy)),
            created_at=item.created_at,
            selected_instances=tuple(
                _selected_instance_view(value) for value in item.selected_instances
            ),
            routing_assignments=tuple(
                _routing_assignment_view(value) for value in item.routing_assignments
            ),
            steps=tuple(_step_view(value) for value in item.steps),
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


def _action_view(item: ExternalActionResource) -> ExternalActionView:
    if (
        type(item) is not ExternalActionResource
        or not isinstance(item.redacted_payload, Mapping)
        or item.result_safe_metadata is not None
    ):
        _raise_unavailable()
    links = {
        "action_url": f"/api/v1/external-actions/{item.action_id}",
        "run_url": f"/api/v1/runs/{item.run_id}",
        "step_url": f"/api/v1/runs/{item.run_id}/steps/{item.step_id}",
        "instance_url": f"/api/v1/agent-instances/{item.instance_id}",
        "template_url": f"/api/v1/agent-templates/{item.template_id}",
    }
    if any(getattr(item, name, None) != value for name, value in links.items()):
        _raise_unavailable()
    projected_payload = redact(item.redacted_payload)
    if type(projected_payload) is not dict:
        _raise_unavailable()
    try:
        return ExternalActionView(
            id=item.action_id,
            run_id=item.run_id,
            step_id=item.step_id,
            step_key=item.step_key,
            template_id=item.template_id,
            instance_id=item.instance_id,
            proposal_revision=item.proposal_revision,
            action_type=item.action_type,
            capability_id=item.capability_id,
            connector_family=item.connector_family,
            binding_id=item.binding_id,
            destination_summary=item.destination_summary,
            redacted_payload=cast(dict[str, Any], projected_payload),
            payload_schema_id=item.payload_schema_id,
            state=cast(Any, item.state),
            created_at=item.created_at,
            updated_at=item.updated_at,
            version=item.version,
            delivery_attempt_count=item.delivery_attempt_count,
            delivery_attempt_limit=item.delivery_attempt_limit,
            approval_policy_id=item.approval_policy_id,
            approval_required_roles=item.approval_required_roles,
            approval_required_scopes=item.approval_required_scopes,
            approval_expires_after_seconds=item.approval_expires_after_seconds,
            approval_allow_self_approval=item.approval_allow_self_approval,
            terminal_reason_code=item.terminal_reason_code,
            superseded_by_action_id=item.superseded_by_action_id,
            superseded_at=item.superseded_at,
            receipt_id=item.receipt_id,
            result_status=item.result_status,
            result_safe_metadata=None,
            completed_at=item.completed_at,
            **links,
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


def _execution_control_view(
    item: RunExecutionControlResource,
) -> RunExecutionControlView:
    if (
        type(item) is not RunExecutionControlResource
        or item.model_calls + item.remaining_model_calls != item.max_model_calls
        or item.tool_calls + item.remaining_tool_calls != item.max_tool_calls
        or (item.started_at is None) != (item.deadline_at is None)
    ):
        _raise_unavailable()
    try:
        return RunExecutionControlView(
            run_timeout_seconds=item.run_timeout_seconds,
            max_model_calls=item.max_model_calls,
            max_tool_calls=item.max_tool_calls,
            model_calls=item.model_calls,
            tool_calls=item.tool_calls,
            remaining_model_calls=item.remaining_model_calls,
            remaining_tool_calls=item.remaining_tool_calls,
            started_at=item.started_at,
            deadline_at=item.deadline_at,
            cancel_requested_at=item.cancel_requested_at,
            created_at=item.created_at,
            updated_at=item.updated_at,
            version=item.version,
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


def _pending_approval_view(
    run_id: str,
    item: PendingApprovalSummary,
) -> PendingApprovalSummaryView:
    if type(item) is not PendingApprovalSummary:
        _raise_unavailable()
    expected = {
        "approval_url": f"/api/v1/approvals/{item.approval_id}",
        "action_url": f"/api/v1/external-actions/{item.action_id}",
        "step_url": f"/api/v1/runs/{run_id}/steps/{item.step_id}",
    }
    if any(getattr(item, name, None) != value for name, value in expected.items()):
        _raise_unavailable()
    try:
        return PendingApprovalSummaryView(
            id=item.approval_id,
            action_id=item.action_id,
            step_id=item.step_id,
            status=cast(Any, item.status),
            destination_summary=item.destination_summary,
            requested_at=item.requested_at,
            expires_at=item.expires_at,
            is_expired=item.is_expired,
            **expected,
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


def _artifact_summary_view(item: ArtifactSummary) -> ArtifactSummaryView:
    if type(item) is not ArtifactSummary:
        _raise_unavailable()
    expected = {
        "artifact_url": f"/api/v1/artifacts/{item.artifact_id}",
        "run_url": f"/api/v1/runs/{item.run_id}",
        "step_url": f"/api/v1/runs/{item.run_id}/steps/{item.step_id}",
        "template_url": f"/api/v1/agent-templates/{item.template_id}",
        "instance_url": f"/api/v1/agent-instances/{item.instance_id}",
    }
    if any(getattr(item, name, None) != value for name, value in expected.items()):
        _raise_unavailable()
    try:
        return ArtifactSummaryView(
            id=item.artifact_id,
            work_item_id=item.work_item_id,
            run_id=item.run_id,
            step_id=item.step_id,
            workflow_id=item.workflow_id,
            workflow_version=item.workflow_version,
            template_id=item.template_id,
            instance_id=item.instance_id,
            output_schema_id=item.output_schema_id,
            output_schema_version=item.output_schema_version,
            classification=cast(Any, item.classification),
            created_at=item.created_at,
            **expected,
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


def _resource_view(item: RunResource) -> RunResourceView:
    if (
        type(item) is not RunResource
        or type(item.transitions) is not tuple
        or type(item.external_actions) is not tuple
        or type(item.pending_approvals) is not tuple
        or type(item.artifact_summaries) is not tuple
        or type(item.artifacts_truncated) is not bool
        or len(item.artifact_summaries) > 10
        or tuple(value.sequence for value in item.transitions)
        != tuple(range(1, len(item.transitions) + 1))
        or not item.transitions
        or item.transitions[-1].new_state != item.state
        or item.transitions[-1].resulting_version != item.version
        or any(value.run_id != item.run_id for value in item.external_actions)
        or any(value.run_id != item.run_id for value in item.artifact_summaries)
        or (item.plan is not None and item.plan.workflow_id != item.workflow_id)
    ):
        _raise_unavailable()
    summary = _summary_view(item)
    try:
        return RunResourceView(
            **summary.model_dump(),
            transitions=tuple(_transition_view(value) for value in item.transitions),
            plan=None if item.plan is None else _plan_view(item.plan),
            execution_control=(
                None
                if item.execution_control is None
                else _execution_control_view(item.execution_control)
            ),
            pending_approvals=tuple(
                _pending_approval_view(item.run_id, value) for value in item.pending_approvals
            ),
            artifact_summaries=tuple(
                _artifact_summary_view(value) for value in item.artifact_summaries
            ),
            artifacts_truncated=item.artifacts_truncated,
            external_actions=tuple(_action_view(value) for value in item.external_actions),
            terminal_error=_terminal_error_view(item.run_id, item.terminal_error),
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


def _terminal_error_view(
    run_id: str,
    item: RunTerminalErrorResource | None,
) -> RunTerminalErrorView | None:
    if item is None:
        return None
    if type(item) is not RunTerminalErrorResource:
        _raise_unavailable()
    expected_step_url = (
        None if item.step_id is None else f"/api/v1/runs/{run_id}/steps/{item.step_id}"
    )
    expected_action_url = (
        None if item.action_id is None else f"/api/v1/external-actions/{item.action_id}"
    )
    if (
        item.retryable is not False
        or item.step_url != expected_step_url
        or item.action_url != expected_action_url
        or (item.source == "run" and (item.step_id is not None or item.action_id is not None))
        or (
            item.source in {"step", "read_attempt"}
            and (item.step_id is None or item.action_id is not None)
        )
        or (item.source == "external_action" and (item.step_id is None or item.action_id is None))
    ):
        _raise_unavailable()
    try:
        return RunTerminalErrorView(
            code=item.code,
            cause_code=item.cause_code,
            source=cast(Any, item.source),
            step_id=item.step_id,
            action_id=item.action_id,
            outcome=item.outcome,
            final_attempt_number=item.final_attempt_number,
            retryable=False,
            call_deadline_at=item.call_deadline_at,
            run_deadline_at=item.run_deadline_at,
            occurred_at=item.occurred_at,
            step_url=item.step_url,
            action_url=item.action_url,
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


def _timeline_view(run_id: str, item: RunTimelineEvent) -> RunTimelineEventView:
    if (
        type(item) is not RunTimelineEvent
        or not isinstance(item.metadata, Mapping)
        or (item.metadata_expired and item.metadata)
    ):
        _raise_unavailable()
    links = {
        "run_url": f"/api/v1/runs/{run_id}",
        "step_url": (
            None if item.step_id is None else f"/api/v1/runs/{run_id}/steps/{item.step_id}"
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
    if any(getattr(item, name, None) != value for name, value in links.items()):
        _raise_unavailable()
    try:
        return RunTimelineEventView(
            id=item.event_id,
            sequence=item.sequence,
            schema_version=item.schema_version,
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
            approval_request_id=item.approval_request_id,
            artifact_id=item.artifact_id,
            attempted_command=item.attempted_command,
            previous_state=item.previous_state,
            new_state=item.new_state,
            reason_code=item.reason_code,
            metadata=dict(item.metadata),
            metadata_classification=cast(Any, item.metadata_classification),
            metadata_expires_at=item.metadata_expires_at,
            metadata_expired=item.metadata_expired,
            run_url=cast(str, links["run_url"]),
            step_url=links["step_url"],
            action_url=links["action_url"],
            approval_url=links["approval_url"],
            artifact_url=links["artifact_url"],
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


def _status_item_view(item: InstanceRuntimeStatus) -> InstanceRuntimeStatusView:
    if type(item) is not InstanceRuntimeStatus:
        _raise_unavailable()
    expected_instance_url = f"/api/v1/agent-instances/{item.instance_id}"
    expected_run_url = None if item.latest_run_id is None else f"/api/v1/runs/{item.latest_run_id}"
    no_run = item.latest_run_id is None
    if (
        item.instance_url != expected_instance_url
        or item.latest_run_url != expected_run_url
        or no_run
        != all(
            value is None
            for value in (
                item.latest_run_state,
                item.latest_run_created_at,
                item.latest_run_updated_at,
                item.latest_run_url,
            )
        )
        or (no_run and item.status != "never_run")
        or (not no_run and item.status != item.latest_run_state)
    ):
        _raise_unavailable()
    try:
        return InstanceRuntimeStatusView(
            instance_id=item.instance_id,
            status=cast(Any, item.status),
            latest_run_id=item.latest_run_id,
            latest_run_state=cast(Any, item.latest_run_state),
            latest_run_created_at=item.latest_run_created_at,
            latest_run_updated_at=item.latest_run_updated_at,
            instance_url=item.instance_url,
            latest_run_url=item.latest_run_url,
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


@router.get(
    "",
    response_model=RunListResponse,
    operation_id="listRuns",
    responses={
        status.HTTP_200_OK: {
            "model": RunListResponse,
            "description": "A bounded deterministic page of Run summaries.",
            "headers": _PRIVATE_HEADERS,
        },
        **_responses(400, 401, 403, 422, 503),
    },
)
async def list_runs(
    response: Response,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_runtime_resource_reader_principal),
    ],
    executor: Annotated[RunResourceExecutor, Depends(get_run_resource_executor)],
    run_state: Annotated[RunState | None, Query(alias="state")] = None,
    instance_id: Annotated[str | None, Query(pattern=_RESOURCE_ID_PATTERN)] = None,
    workflow_id: Annotated[str | None, Query(pattern=_RESOURCE_ID_PATTERN)] = None,
    created_at_from: datetime | None = None,
    created_at_to: datetime | None = None,
    cursor: Annotated[str | None, Query(max_length=MAX_RUN_CURSOR_LENGTH)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_RUN_PAGE_SIZE)] = DEFAULT_RUN_PAGE_SIZE,
) -> RunListResponse:
    try:
        query = RunListQuery(
            state=run_state,
            instance_id=instance_id,
            workflow_id=workflow_id,
            created_at_from=created_at_from,
            created_at_to=created_at_to,
            cursor=cursor,
            limit=limit,
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "runtime_query_invalid", "message": "runtime query is invalid"},
        ) from None
    try:
        page = await executor.list(query, principal=principal)
    except RunResourceServiceError as error:
        _raise_service_problem(error)
    except Exception:
        _raise_unavailable()
    if (
        type(page) is not RunPage
        or type(page.items) is not tuple
        or len(page.items) > limit
        or any(type(item) is not RunResource for item in page.items)
        or any(
            (previous.created_at, previous.run_id) <= (current.created_at, current.run_id)
            for previous, current in zip(page.items, page.items[1:], strict=False)
        )
        or any(run_state is not None and item.state != run_state.value for item in page.items)
        or any(instance_id is not None and item.instance_id != instance_id for item in page.items)
        or any(workflow_id is not None and item.workflow_id != workflow_id for item in page.items)
        or any(
            created_at_from is not None and item.created_at < created_at_from for item in page.items
        )
        or any(created_at_to is not None and item.created_at > created_at_to for item in page.items)
        or (
            page.next_cursor is not None
            and (
                type(page.next_cursor) is not str
                or not page.next_cursor
                or len(page.next_cursor) > MAX_RUN_CURSOR_LENGTH
            )
        )
    ):
        _raise_unavailable()
    _private_response(response)
    return RunListResponse(
        items=tuple(_summary_view(item) for item in page.items),
        next_cursor=page.next_cursor,
    )


@router.get(
    "/{run_id}",
    response_model=RunResourceView,
    operation_id="getRun",
    responses={
        status.HTTP_200_OK: {
            "model": RunResourceView,
            "description": "A coherent historical Run projection.",
            "headers": _PRIVATE_HEADERS,
        },
        **_responses(400, 401, 403, 404, 422, 503),
    },
)
async def get_run(
    run_id: Annotated[str, Path(pattern=_RESOURCE_ID_PATTERN)],
    response: Response,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_runtime_resource_reader_principal),
    ],
    executor: Annotated[RunResourceExecutor, Depends(get_run_resource_executor)],
) -> RunResourceView:
    try:
        resource = await executor.read(run_id, principal=principal)
    except RunResourceServiceError as error:
        _raise_service_problem(error)
    except Exception:
        _raise_unavailable()
    if type(resource) is not RunResource or resource.run_id != run_id:
        _raise_unavailable()
    _private_response(response)
    return _resource_view(resource)


@router.get(
    "/{run_id}/timeline",
    response_model=RunTimelineResponse,
    operation_id="getRunTimeline",
    responses={
        status.HTTP_200_OK: {
            "model": RunTimelineResponse,
            "description": "A per-Run sequence-ordered timeline page.",
            "headers": _PRIVATE_HEADERS,
        },
        **_responses(400, 401, 403, 404, 422, 503),
    },
)
async def get_run_timeline(
    run_id: Annotated[str, Path(pattern=_RESOURCE_ID_PATTERN)],
    response: Response,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_runtime_resource_reader_principal),
    ],
    executor: Annotated[RunResourceExecutor, Depends(get_run_resource_executor)],
    cursor: Annotated[str | None, Query(max_length=MAX_RUN_CURSOR_LENGTH)] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_TIMELINE_PAGE_SIZE),
    ] = DEFAULT_TIMELINE_PAGE_SIZE,
) -> RunTimelineResponse:
    try:
        query = RunTimelineQuery(cursor=cursor, limit=limit)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "runtime_query_invalid", "message": "runtime query is invalid"},
        ) from None
    try:
        page = await executor.read_timeline(run_id, query, principal=principal)
    except RunResourceServiceError as error:
        _raise_service_problem(error)
    except Exception:
        _raise_unavailable()
    if (
        type(page) is not RunTimelinePage
        or page.run_id != run_id
        or type(page.items) is not tuple
        or len(page.items) > limit
        or any(type(item) is not RunTimelineEvent for item in page.items)
        or any(
            previous.sequence >= current.sequence
            for previous, current in zip(page.items, page.items[1:], strict=False)
        )
        or (
            page.next_cursor is not None
            and (
                type(page.next_cursor) is not str
                or not page.next_cursor
                or len(page.next_cursor) > MAX_RUN_CURSOR_LENGTH
            )
        )
    ):
        _raise_unavailable()
    _private_response(response)
    return RunTimelineResponse(
        run_id=run_id,
        items=tuple(_timeline_view(run_id, item) for item in page.items),
        next_cursor=page.next_cursor,
    )


@router.get(
    "/{run_id}/steps/{step_id}",
    response_model=RunStepView,
    operation_id="getRunStep",
    responses={
        status.HTTP_200_OK: {
            "model": RunStepView,
            "description": "A persisted safe Run-step projection.",
            "headers": _PRIVATE_HEADERS,
        },
        **_responses(400, 401, 403, 404, 422, 503),
    },
)
async def get_run_step(
    run_id: Annotated[str, Path(pattern=_RESOURCE_ID_PATTERN)],
    step_id: Annotated[str, Path(pattern=_RESOURCE_ID_PATTERN)],
    response: Response,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_runtime_resource_reader_principal),
    ],
    executor: Annotated[RunResourceExecutor, Depends(get_run_resource_executor)],
) -> RunStepView:
    try:
        resource = await executor.read_step(run_id, step_id, principal=principal)
    except RunResourceServiceError as error:
        _raise_service_problem(error)
    except Exception:
        _raise_unavailable()
    if (
        type(resource) is not RunStepResource
        or resource.step_id != step_id
        or resource.run_id != run_id
    ):
        _raise_unavailable()
    _private_response(response)
    return _step_view(resource)


@external_action_router.get(
    "/{action_id}",
    response_model=ExternalActionView,
    operation_id="getExternalAction",
    responses={
        status.HTTP_200_OK: {
            "model": ExternalActionView,
            "description": "A safe external-action state projection without its envelope.",
            "headers": _PRIVATE_HEADERS,
        },
        **_responses(400, 401, 403, 404, 422, 503),
    },
)
async def get_external_action(
    action_id: Annotated[str, Path(pattern=_RESOURCE_ID_PATTERN)],
    response: Response,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_runtime_resource_reader_principal),
    ],
    executor: Annotated[RunResourceExecutor, Depends(get_run_resource_executor)],
) -> ExternalActionView:
    try:
        resource = await executor.read_external_action(action_id, principal=principal)
    except RunResourceServiceError as error:
        _raise_service_problem(error)
    except Exception:
        _raise_unavailable()
    if type(resource) is not ExternalActionResource or resource.action_id != action_id:
        _raise_unavailable()
    _private_response(response)
    return _action_view(resource)


def _if_none_match_matches(value: str | None, etag: str) -> bool:
    if value is None or len(value) > _MAX_IF_NONE_MATCH_LENGTH:
        return False
    candidates = tuple(item.strip() for item in value.split(","))
    return "*" in candidates or any(
        candidate.removeprefix("W/") == etag for candidate in candidates
    )


@instance_status_router.get(
    "/status-summary",
    response_model=InstanceStatusSummaryResponse,
    operation_id="getAgentInstanceStatusSummary",
    responses={
        status.HTTP_200_OK: {
            "model": InstanceStatusSummaryResponse,
            "description": "A separately revalidated instance runtime-status projection.",
            "headers": _STATUS_HEADERS,
        },
        status.HTTP_304_NOT_MODIFIED: {
            "description": "The runtime-status representation has not changed.",
            "headers": _STATUS_HEADERS,
        },
        **_responses(400, 401, 403, 422, 503),
    },
)
async def get_agent_instance_status_summary(
    request: Request,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_runtime_resource_reader_principal),
    ],
    executor: Annotated[RunResourceExecutor, Depends(get_run_resource_executor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    if len(request.headers.getlist("if-none-match")) > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "runtime_header_ambiguous",
                "message": "runtime request headers must be unique",
            },
        )
    try:
        summary = await executor.read_instance_status_summary(principal=principal)
    except RunResourceServiceError as error:
        _raise_service_problem(error)
    except Exception:
        _raise_unavailable()
    if (
        type(summary) is not InstanceStatusSummary
        or summary.scope != "single-local-installation"
        or type(summary.items) is not tuple
        or len(summary.items) > 100
        or any(type(item) is not InstanceRuntimeStatus for item in summary.items)
        or len({item.instance_id for item in summary.items}) != len(summary.items)
        or type(summary.etag) is not str
        or _STATUS_ETAG_PATTERN.fullmatch(summary.etag) is None
    ):
        _raise_unavailable()
    headers = {
        "Cache-Control": _STATUS_CACHE_CONTROL,
        "ETag": summary.etag,
        "Vary": "Authorization",
        "X-Content-Type-Options": "nosniff",
    }
    if _if_none_match_matches(if_none_match, summary.etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    model = InstanceStatusSummaryResponse(
        scope=cast(Any, summary.scope),
        runtime_watermark=summary.etag[1:-1],
        items=tuple(_status_item_view(item) for item in summary.items),
    )
    return Response(
        status_code=status.HTTP_200_OK,
        content=model.model_dump_json(),
        media_type="application/json",
        headers=headers,
    )


__all__ = [
    "Api07PrivateResponseMiddleware",
    "external_action_router",
    "instance_status_router",
    "router",
]

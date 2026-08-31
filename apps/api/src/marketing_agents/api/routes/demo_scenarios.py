"""Safe deterministic demo discovery and asynchronous intake transport."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from marketing_agents.api.correlation import request_correlation_id
from marketing_agents.api.dependencies import (
    DemoScenarioRegistryExecutor,
    ManualDryRunExecutor,
    get_demo_scenario_registry,
    get_manual_dry_run_executor,
    require_catalog_principal,
    require_manual_work_operator_principal,
)
from marketing_agents.api.errors import problem_response
from marketing_agents.api.schemas.demo_scenarios import (
    DemoScenarioExpectedBehaviorView,
    DemoScenarioListResponse,
    DemoScenarioRunInput,
    DemoScenarioRunResponse,
    DemoScenarioSelectedAgentView,
    DemoScenarioView,
)
from marketing_agents.api.schemas.problems import ProblemDetails, ProblemFieldError
from marketing_agents.api.strict_json import (
    StrictJsonTransportError,
    strict_json_route_path,
    strict_json_transport_headers_are_valid,
    validate_strict_json_body,
)
from marketing_agents.application.policies.json_schema import (
    JsonSchemaPolicyError,
    compile_json_schema,
)
from marketing_agents.application.services.idempotent_work_receipt import (
    WorkRunReceiptDisposition,
)
from marketing_agents.application.services.manual_work_intake import (
    ManualDryRunCommand,
    ManualDryRunResult,
    ManualDryRunServiceError,
)
from marketing_agents.demos import (
    DemoScenarioDefinition,
    DemoScenarioInputError,
    DemoScenarioRegistryError,
)
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.entities import Run, WorkItem
from marketing_agents.domain.enums import RunState, WorkMode
from marketing_agents.domain.identity import AuthenticatedPrincipal
from marketing_agents.security.redaction import SecretValue

_SCENARIO_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$"
_SAFE_RESOURCE_ID_PATTERN = re.compile(_SCENARIO_ID_PATTERN)
_DEMO_RUN_PATH_PATTERN = re.compile(r"^/api/v1/demo-scenarios/[^/]+/runs$")
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[\x21-\x7e]{8,240}$")
_MAX_IDEMPOTENCY_KEY_LENGTH = 240
_MAX_DEMO_REQUEST_BYTES = 1_048_576
_MAX_DEMO_INPUT_DEPTH = 64
_MAX_DEMO_REQUEST_DEPTH = _MAX_DEMO_INPUT_DEPTH + 2
_SUBMIT_TIMEOUT_SECONDS = 5.0
_NO_STORE = "no-store"
_VARY = "Authorization"

_REPLAYABLE_RUN_VERSIONS: dict[RunState, frozenset[int]] = {
    RunState.RECEIVED: frozenset({1}),
    RunState.VALIDATED: frozenset({2}),
    RunState.PLANNED: frozenset({3}),
    RunState.AWAITING_APPROVAL: frozenset({4}),
    RunState.EXECUTING: frozenset({4, 5}),
    RunState.COMPLETED: frozenset({5, 6}),
    RunState.FAILED: frozenset({2, 3, 4, 5, 6}),
    RunState.REJECTED: frozenset({5}),
    RunState.CANCELLED: frozenset({2, 3, 4, 5, 6}),
}

_PRIVATE_HEADERS: dict[str, dict[str, object]] = {
    "Cache-Control": {"schema": {"type": "string", "const": _NO_STORE}},
    "Vary": {"schema": {"type": "string", "const": _VARY}},
}
_IDEMPOTENCY_KEY_OPENAPI_PARAMETER: dict[str, object] = {
    "name": "Idempotency-Key",
    "in": "header",
    "required": True,
    "description": "An opaque retry key bound to this exact demo admission.",
    "schema": {
        "type": "string",
        "pattern": _IDEMPOTENCY_KEY_PATTERN.pattern,
        "minLength": 8,
        "maxLength": _MAX_IDEMPOTENCY_KEY_LENGTH,
    },
}
_GET_RESPONSES: dict[int | str, dict[str, object]] = {
    status.HTTP_200_OK: {
        "description": "Safe deterministic demo presets and expected behavior.",
        "headers": _PRIVATE_HEADERS,
    },
    status.HTTP_401_UNAUTHORIZED: {"model": ProblemDetails},
    status.HTTP_403_FORBIDDEN: {"model": ProblemDetails},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ProblemDetails, "headers": _PRIVATE_HEADERS},
}
_POST_RESPONSES: dict[int | str, dict[str, object]] = {
    status.HTTP_202_ACCEPTED: {
        "description": "The deterministic demo run was accepted for asynchronous processing.",
        "headers": _PRIVATE_HEADERS,
    },
    status.HTTP_400_BAD_REQUEST: {"model": ProblemDetails, "headers": _PRIVATE_HEADERS},
    status.HTTP_401_UNAUTHORIZED: {"model": ProblemDetails, "headers": _PRIVATE_HEADERS},
    status.HTTP_403_FORBIDDEN: {"model": ProblemDetails, "headers": _PRIVATE_HEADERS},
    status.HTTP_404_NOT_FOUND: {"model": ProblemDetails, "headers": _PRIVATE_HEADERS},
    status.HTTP_409_CONFLICT: {"model": ProblemDetails, "headers": _PRIVATE_HEADERS},
    status.HTTP_413_CONTENT_TOO_LARGE: {"model": ProblemDetails, "headers": _PRIVATE_HEADERS},
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {
        "model": ProblemDetails,
        "headers": _PRIVATE_HEADERS,
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ProblemDetails,
        "headers": _PRIVATE_HEADERS,
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ProblemDetails, "headers": _PRIVATE_HEADERS},
}

_FORBIDDEN_CODES = frozenset(
    {"manual_work_forbidden", "manual_work_human_required", "manual_work_operator_role_missing"}
)
_NOT_FOUND_CODES = frozenset(
    {"demo_scenario_not_found", "demo_scenario_unknown", "instance_not_found", "instance_unknown"}
)
_IDEMPOTENCY_CONFLICT_CODES = frozenset({"idempotency_conflict", "manual_idempotency_conflict"})
_STATE_CONFLICT_CODES = frozenset(
    {"demo_scenario_disabled", "instance_disabled", "manual_trigger_unavailable"}
)
_INPUT_CODES = frozenset(
    {
        "demo_scenario_invalid",
        "input_byte_limit",
        "input_field_too_large",
        "input_invalid",
        "input_redaction_invalid",
        "input_schema_invalid",
        "input_secret_not_retainable",
        "invalid_json",
        "json_depth_limit",
        "manual_command_invalid",
        "manual_input_invalid",
        "manual_work_command_invalid",
        "work_mode_not_allowed",
    }
)

router = APIRouter(prefix="/api/v1/demo-scenarios", tags=["demo-scenarios"])


class DemoScenarioRequestBoundsMiddleware:
    """Bound and strictly parse demo JSON before FastAPI buffers it."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or _DEMO_RUN_PATH_PATTERN.fullmatch(strict_json_route_path(scope)) is None
        ):
            await self._app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        if not strict_json_transport_headers_are_valid(scope):
            await problem_response(
                request,
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                code="media_type_unsupported",
                headers={"Vary": _VARY},
            )(scope, receive, send)
            return
        if self._declared_length_is_invalid(scope):
            await problem_response(
                request,
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                code="payload_too_large",
                headers={"Vary": _VARY},
            )(scope, receive, send)
            return

        buffered = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self._invalid_body(request, scope, receive, send)
                return
            chunk = message.get("body", b"")
            if type(chunk) is not bytes or len(buffered) + len(chunk) > _MAX_DEMO_REQUEST_BYTES:
                await problem_response(
                    request,
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    code="payload_too_large",
                    headers={"Vary": _VARY},
                )(scope, receive, send)
                return
            buffered.extend(chunk)
            if not message.get("more_body", False):
                break
        try:
            validate_strict_json_body(bytes(buffered), max_depth=_MAX_DEMO_REQUEST_DEPTH)
        except StrictJsonTransportError:
            await self._invalid_body(request, scope, receive, send)
            return

        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(buffered), "more_body": False}
            return await receive()

        await self._app(scope, bounded_receive, send)

    @staticmethod
    def _declared_length_is_invalid(scope: Scope) -> bool:
        values = [
            value for name, value in scope.get("headers", ()) if name.lower() == b"content-length"
        ]
        if not values:
            return False
        if len(values) != 1:
            return True
        try:
            raw = values[0].decode("ascii")
            parsed = int(raw)
        except (UnicodeDecodeError, ValueError):
            return True
        return str(parsed) != raw or parsed < 0 or parsed > _MAX_DEMO_REQUEST_BYTES

    @staticmethod
    async def _invalid_body(
        request: Request,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        await problem_response(
            request,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="request_validation_failed",
            field_errors=(
                ProblemFieldError(
                    pointer="/body",
                    code="invalid_json",
                    message="invalid request field",
                ),
            ),
            headers={"Vary": _VARY},
        )(scope, receive, send)


def _json_value(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _scenario_view(definition: object) -> DemoScenarioView:
    if type(definition) is not DemoScenarioDefinition:
        raise TypeError("demo registry returned an invalid definition")
    if replace(definition) != definition:
        raise ValueError("demo registry definition integrity is invalid")
    input_schema = _json_value(definition.input_schema)
    preset = _json_value(definition.fixture)
    if type(input_schema) is not dict or type(preset) is not dict:
        raise TypeError("demo registry JSON projections are invalid")
    return DemoScenarioView(
        id=definition.id,
        version=definition.version,
        display_name=definition.display_name,
        description=definition.description,
        workflow_id=definition.workflow_id,
        effect=definition.effect,
        mode="deterministic_mock",
        selected_agents=tuple(
            DemoScenarioSelectedAgentView(
                template_id=agent.template_id,
                instance_id=agent.instance_id,
            )
            for agent in definition.selected_agents
        ),
        input_schema=input_schema,
        preset=preset,
        safe_submit_verb=definition.safe_submit_verb,
        expected=DemoScenarioExpectedBehaviorView(
            state_path=definition.expected_state_path,
            model_calls=definition.expected_model_calls,
            connector_calls=definition.expected_connector_calls,
            external_actions=definition.expected_external_actions,
            approvals=definition.expected_approvals,
            external_writes=definition.expected_external_actions,
        ),
    )


def _json_content_type(request: Request) -> None:
    if not strict_json_transport_headers_are_valid(request.scope):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"code": "media_type_unsupported"},
            headers={"Cache-Control": _NO_STORE, "Vary": _VARY},
        )


def _idempotency_key(request: Request) -> SecretValue:
    values = request.headers.getlist("idempotency-key")
    if (
        len(values) != 1
        or len(values[0]) > _MAX_IDEMPOTENCY_KEY_LENGTH
        or _IDEMPOTENCY_KEY_PATTERN.fullmatch(values[0]) is None
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "idempotency_key_invalid"},
            headers={"Cache-Control": _NO_STORE, "Vary": _VARY},
        )
    return SecretValue(values[0])


def _input_depth_is_bounded(value: object) -> bool:
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > _MAX_DEMO_INPUT_DEPTH:
            return False
        if type(current) is dict:
            pending.extend((item, depth + 1) for item in current.values())
        elif type(current) is list:
            pending.extend((item, depth + 1) for item in current)
    return True


def _safe_input_pointer(pointer: object) -> str:
    if type(pointer) is not str or not pointer.startswith("/"):
        return "/input"
    relative = (
        pointer.removeprefix("/input")
        if pointer == "/input" or pointer.startswith("/input/")
        else pointer
    )
    if relative in {"", "/"}:
        return "/input"
    segments = relative.removeprefix("/").split("/")
    if not 1 <= len(segments) <= 64 or any(
        re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", segment) is None for segment in segments
    ):
        return "/input"
    return "/input/" + "/".join(segments)


def _input_problem(request: Request, *, pointer: object = None) -> JSONResponse:
    return problem_response(
        request,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="demo_scenario_input_invalid",
        field_errors=(
            ProblemFieldError(
                pointer=_safe_input_pointer(pointer),
                code="demo_scenario_invalid",
                message="invalid request field",
            ),
        ),
        headers={"Vary": _VARY},
    )


def _resolved_input_is_bound(
    definition: DemoScenarioDefinition,
    resolved_input: object,
) -> bool:
    if not isinstance(resolved_input, Mapping):
        return False
    try:
        compiled = compile_json_schema(
            definition.input_schema,
            expected_schema_id=definition.input_schema_id,
        )
        compiled.validate(resolved_input, pointer_root="/input", max_depth=16)
        canonical_json_bytes(resolved_input)
    except (JsonSchemaPolicyError, TypeError, ValueError):
        return False
    return True


def _service_problem(request: Request, error: ManualDryRunServiceError) -> JSONResponse:
    if error.code in _FORBIDDEN_CODES:
        return problem_response(
            request,
            status_code=status.HTTP_403_FORBIDDEN,
            code="demo_run_forbidden",
            headers={"Vary": _VARY},
        )
    if error.code in _NOT_FOUND_CODES:
        return problem_response(
            request,
            status_code=status.HTTP_404_NOT_FOUND,
            code="demo_scenario_not_found",
            headers={"Vary": _VARY},
        )
    if error.code in _IDEMPOTENCY_CONFLICT_CODES:
        return problem_response(
            request,
            status_code=status.HTTP_409_CONFLICT,
            code="idempotency_conflict",
            headers={"Vary": _VARY},
        )
    if error.code in _STATE_CONFLICT_CODES:
        return problem_response(
            request,
            status_code=status.HTTP_409_CONFLICT,
            code="demo_run_conflict",
            headers={"Vary": _VARY},
        )
    if error.code in _INPUT_CODES:
        return _input_problem(request, pointer=error.pointer)
    return problem_response(
        request,
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="demo_run_unavailable",
        headers={"Vary": _VARY},
    )


def _result_is_bound(
    result: object,
    *,
    command: ManualDryRunCommand,
    definition: DemoScenarioDefinition,
    principal: AuthenticatedPrincipal,
) -> bool:
    if type(result) is not ManualDryRunResult:
        return False
    work_item = result.work_item
    run = result.run
    try:
        principal.verify_integrity()
        if type(work_item) is not WorkItem or type(run) is not Run:
            return False
        replace(result)
        replace(work_item)
        replace(run)
        exact_payload = canonical_json_bytes(work_item.admitted_payload) == canonical_json_bytes(
            command.input_payload
        )
    except (TypeError, ValueError):
        return False
    if result.disposition is WorkRunReceiptDisposition.CREATED:
        disposition_is_coherent = (
            run.state is RunState.RECEIVED and run.version == 1 and run.updated_at == run.created_at
        )
    elif result.disposition is WorkRunReceiptDisposition.REPLAYED:
        disposition_is_coherent = _replayed_run_is_coherent(run)
    else:  # pragma: no cover - exact enum validation above makes this defensive.
        disposition_is_coherent = False
    return (
        type(result.disposition) is WorkRunReceiptDisposition
        and result.mode is WorkMode.DRY_RUN
        and result.event_id == work_item.event_id
        and disposition_is_coherent
        and work_item.instance_id == definition.instance_id
        and work_item.workflow_id == definition.workflow_id
        and work_item.input_schema_id == definition.input_schema_id
        and work_item.source == "manual"
        and work_item.mode is WorkMode.DRY_RUN
        and work_item.brief_id is None
        and exact_payload
        and run.work_item_id == work_item.id
        and run.configuration_revision == work_item.configuration_revision
        and _SAFE_RESOURCE_ID_PATTERN.fullmatch(work_item.id) is not None
        and _SAFE_RESOURCE_ID_PATTERN.fullmatch(run.id) is not None
    )


def _replayed_run_is_coherent(run: Run) -> bool:
    """Accept only state/version pairs reachable from the durable Run lifecycle."""

    if run.version not in _REPLAYABLE_RUN_VERSIONS.get(run.state, frozenset()):
        return False
    if run.state is RunState.RECEIVED:
        return run.updated_at == run.created_at
    if run.state is RunState.EXECUTING:
        return (run.version == 4 and run.approval_required is False) or (
            run.version == 5 and run.approval_required is True
        )
    if run.state is RunState.COMPLETED:
        return (run.version == 5 and run.approval_required is False) or (
            run.version == 6 and run.approval_required is True
        )
    if run.state in {RunState.FAILED, RunState.CANCELLED}:
        if run.version in {2, 3}:
            return run.approval_required is None
        if run.version == 6:
            return run.approval_required is True
        return type(run.approval_required) is bool
    return True


@router.get(
    "",
    response_model=DemoScenarioListResponse,
    operation_id="listDemoScenarios",
    responses=_GET_RESPONSES,
)
async def list_demo_scenarios(
    _principal: Annotated[AuthenticatedPrincipal, Depends(require_catalog_principal)],
    registry: Annotated[DemoScenarioRegistryExecutor, Depends(get_demo_scenario_registry)],
) -> JSONResponse:
    del _principal
    try:
        definitions = registry.list()
        if type(definitions) is not tuple or not 1 <= len(definitions) <= 16:
            raise TypeError("demo registry returned an invalid collection")
        response = DemoScenarioListResponse(
            items=tuple(_scenario_view(item) for item in definitions)
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "demo_scenario_registry_unavailable"},
            headers={"Cache-Control": _NO_STORE, "Vary": _VARY},
        ) from None
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=response.model_dump(mode="json", by_alias=True),
        headers={"Cache-Control": _NO_STORE, "Vary": _VARY},
    )


@router.post(
    "/{scenario_id}/runs",
    response_model=DemoScenarioRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="createDemoScenarioRun",
    openapi_extra={"parameters": [_IDEMPOTENCY_KEY_OPENAPI_PARAMETER]},
    responses=_POST_RESPONSES,
)
async def create_demo_scenario_run(
    scenario_id: Annotated[str, Path(pattern=_SCENARIO_ID_PATTERN)],
    request: Request,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_manual_work_operator_principal),
    ],
    _content_type: Annotated[None, Depends(_json_content_type)],
    idempotency_key: Annotated[SecretValue, Depends(_idempotency_key)],
    registry: Annotated[DemoScenarioRegistryExecutor, Depends(get_demo_scenario_registry)],
    executor: Annotated[ManualDryRunExecutor, Depends(get_manual_dry_run_executor)],
    body: DemoScenarioRunInput,
) -> JSONResponse:
    if not _input_depth_is_bounded(body.overrides):
        return _input_problem(request)
    try:
        definition = registry.get(scenario_id)
    except DemoScenarioRegistryError:
        return problem_response(
            request,
            status_code=status.HTTP_404_NOT_FOUND,
            code="demo_scenario_not_found",
            headers={"Vary": _VARY},
        )
    except Exception:
        return problem_response(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="demo_scenario_registry_unavailable",
            headers={"Vary": _VARY},
        )
    try:
        _scenario_view(definition)
        resolved_input = registry.resolve_input(scenario_id, body.overrides)
        if not _resolved_input_is_bound(definition, resolved_input):
            raise TypeError("demo registry returned invalid resolved input")
        command = ManualDryRunCommand(
            instance_id=definition.instance_id,
            input_payload=resolved_input,
            mode=WorkMode.DRY_RUN,
            idempotency_key=idempotency_key,
            campaign_brief_id=None,
            demo_scenario_id=definition.id,
            correlation_id=request_correlation_id(request),
        )
    except DemoScenarioInputError as error:
        return _input_problem(request, pointer=error.pointer)
    except DemoScenarioRegistryError:
        return problem_response(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="demo_scenario_registry_unavailable",
            headers={"Vary": _VARY},
        )
    except (TypeError, ValueError):
        return problem_response(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="demo_scenario_registry_unavailable",
            headers={"Vary": _VARY},
        )
    try:
        result = await asyncio.wait_for(
            executor.submit(command, principal=principal),
            timeout=_SUBMIT_TIMEOUT_SECONDS,
        )
    except ManualDryRunServiceError as error:
        return _service_problem(request, error)
    except Exception:
        return problem_response(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="demo_run_unavailable",
            headers={"Vary": _VARY},
        )
    if not _result_is_bound(
        result,
        command=command,
        definition=definition,
        principal=principal,
    ):
        return problem_response(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="demo_run_unavailable",
            headers={"Vary": _VARY},
        )
    response = DemoScenarioRunResponse(
        status="accepted",
        disposition=result.disposition.value,
        scenario_id=definition.id,
        event_id=result.event_id,
        work_id=result.work_item.id,
        run_id=result.run.id,
        execution_mode="dry_run",
        instance_url=f"/api/v1/agent-instances/{definition.instance_id}",
        run_url=f"/api/v1/runs/{result.run.id}",
        timeline_url=f"/api/v1/runs/{result.run.id}/timeline",
        artifacts_url=f"/api/v1/runs/{result.run.id}/artifacts",
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=response.model_dump(mode="json", by_alias=True),
        headers={"Cache-Control": _NO_STORE, "Vary": _VARY},
    )


__all__ = ["DemoScenarioRequestBoundsMiddleware", "router"]

"""Authenticated artifact list and safe detail routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status

from marketing_agents.api.dependencies import (
    ArtifactResourceExecutor,
    get_artifact_resource_executor,
    require_runtime_resource_reader_principal,
)
from marketing_agents.api.schemas.artifacts import (
    ArtifactHttpError,
    ArtifactListResponse,
    ArtifactPlainHttpError,
    ArtifactProviderView,
    ArtifactResourceView,
    ArtifactSourceView,
    ArtifactSummaryView,
)
from marketing_agents.application.services.artifact_resources import (
    DEFAULT_ARTIFACT_PAGE_SIZE,
    MAX_ARTIFACT_CURSOR_LENGTH,
    MAX_ARTIFACT_PAGE_SIZE,
    ArtifactListQuery,
    ArtifactPage,
    ArtifactProviderResource,
    ArtifactResource,
    ArtifactResourceServiceError,
    ArtifactSourceResource,
    ArtifactSummary,
)
from marketing_agents.domain.identity import AuthenticatedPrincipal

_RESOURCE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$"
_PRIVATE_HEADERS = {
    "Cache-Control": {"schema": {"type": "string", "const": "no-store"}},
    "Vary": {"schema": {"type": "string"}},
    "X-Content-Type-Options": {"schema": {"type": "string", "const": "nosniff"}},
}
_ERROR_MODEL = ArtifactHttpError | ArtifactPlainHttpError


def _responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {
        code: {
            "model": _ERROR_MODEL,
            "description": "A fixed non-reflective artifact error.",
            "headers": _PRIVATE_HEADERS,
        }
        for code in codes
    }


run_router = APIRouter(prefix="/api/v1/runs", tags=["artifacts"])
router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])


def _raise_unavailable() -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "artifact_service_unavailable",
            "message": "artifact resources are unavailable",
        },
    ) from None


def _raise_service_problem(error: ArtifactResourceServiceError) -> NoReturn:
    if error.code in {
        "runtime_human_required",
        "runtime_read_role_missing",
    }:
        status_code = status.HTTP_403_FORBIDDEN
        code = "artifact_forbidden"
        message = "artifact read is forbidden"
    elif error.code == "artifact_not_found":
        status_code = status.HTTP_404_NOT_FOUND
        code = "artifact_not_found"
        message = "artifact was not found"
    elif error.code == "run_not_found":
        status_code = status.HTTP_404_NOT_FOUND
        code = "run_not_found"
        message = "run was not found"
    elif error.code in {
        "artifact_cursor_invalid",
        "artifact_id_invalid",
        "artifact_query_invalid",
    }:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        code = "artifact_query_invalid"
        message = "artifact query is invalid"
    else:
        _raise_unavailable()
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    ) from None


def _expected_links(item: ArtifactSummary | ArtifactResource) -> dict[str, str]:
    return {
        "artifact_url": f"/api/v1/artifacts/{item.artifact_id}",
        "run_url": f"/api/v1/runs/{item.run_id}",
        "step_url": f"/api/v1/runs/{item.run_id}/steps/{item.step_id}",
        "template_url": f"/api/v1/agent-templates/{item.template_id}",
        "instance_url": f"/api/v1/agent-instances/{item.instance_id}",
    }


def _validate_links(item: ArtifactSummary | ArtifactResource) -> dict[str, str]:
    expected = _expected_links(item)
    if any(getattr(item, name, None) != value for name, value in expected.items()):
        _raise_unavailable()
    return expected


def _summary_view(item: ArtifactSummary) -> ArtifactSummaryView:
    if type(item) is not ArtifactSummary:
        _raise_unavailable()
    links = _validate_links(item)
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
            **links,
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


def _resource_view(item: ArtifactResource) -> ArtifactResourceView:
    if type(item) is not ArtifactResource or item.classification == "secret":
        _raise_unavailable()
    links = _validate_links(item)
    if (
        type(item.sources) is not tuple
        or any(type(source) is not ArtifactSourceResource for source in item.sources)
        or type(item.providers) is not tuple
        or any(type(provider) is not ArtifactProviderResource for provider in item.providers)
        or type(item.parent_artifact_ids) is not tuple
        or not isinstance(item.redacted_payload, Mapping)
    ):
        _raise_unavailable()
    try:
        return ArtifactResourceView(
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
            catalog_hash=item.catalog_hash,
            instance_config_revision=item.instance_config_revision,
            sources=tuple(
                ArtifactSourceView(
                    kind=cast(Any, source.kind),
                    source_id=source.source_id,
                    classification=cast(Any, source.classification),
                )
                for source in item.sources
            ),
            parent_artifact_ids=item.parent_artifact_ids,
            providers=tuple(
                ArtifactProviderView(
                    provider_kind=cast(Any, provider.provider_kind),
                    mode=cast(Any, provider.mode),
                    name=provider.name,
                    version=provider.version,
                )
                for provider in item.providers
            ),
            output_schema_hash=item.output_schema_hash,
            redacted_payload=dict(item.redacted_payload),
            payload_digest=item.payload_digest,
            **links,
        )
    except (AttributeError, TypeError, ValueError):
        _raise_unavailable()


@run_router.get(
    "/{run_id}/artifacts",
    response_model=ArtifactListResponse,
    operation_id="listRunArtifacts",
    responses={
        status.HTTP_200_OK: {
            "model": ArtifactListResponse,
            "description": "A bounded page of artifact metadata without payload digests.",
            "headers": _PRIVATE_HEADERS,
        },
        **_responses(400, 401, 403, 404, 422, 503),
    },
)
async def list_run_artifacts(
    run_id: Annotated[str, Path(pattern=_RESOURCE_ID_PATTERN)],
    response: Response,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_runtime_resource_reader_principal),
    ],
    executor: Annotated[ArtifactResourceExecutor, Depends(get_artifact_resource_executor)],
    cursor: Annotated[str | None, Query(max_length=MAX_ARTIFACT_CURSOR_LENGTH)] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_ARTIFACT_PAGE_SIZE),
    ] = DEFAULT_ARTIFACT_PAGE_SIZE,
) -> ArtifactListResponse:
    try:
        query = ArtifactListQuery(run_id=run_id, cursor=cursor, limit=limit)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "artifact_query_invalid",
                "message": "artifact query is invalid",
            },
        ) from None
    try:
        page = await executor.list_for_run(query, principal=principal)
    except ArtifactResourceServiceError as error:
        _raise_service_problem(error)
    except Exception:
        _raise_unavailable()
    if (
        type(page) is not ArtifactPage
        or page.run_id != run_id
        or type(page.items) is not tuple
        or len(page.items) > limit
        or any(type(item) is not ArtifactSummary or item.run_id != run_id for item in page.items)
        or any(
            (previous.created_at, previous.artifact_id) >= (current.created_at, current.artifact_id)
            for previous, current in zip(page.items, page.items[1:], strict=False)
        )
        or (
            page.next_cursor is not None
            and (
                type(page.next_cursor) is not str
                or not page.next_cursor
                or len(page.next_cursor) > MAX_ARTIFACT_CURSOR_LENGTH
            )
        )
    ):
        _raise_unavailable()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Authorization"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return ArtifactListResponse(
        run_id=run_id,
        items=tuple(_summary_view(item) for item in page.items),
        next_cursor=page.next_cursor,
    )


@router.get(
    "/{artifact_id}",
    response_model=ArtifactResourceView,
    operation_id="getArtifact",
    responses={
        status.HTTP_200_OK: {
            "model": ArtifactResourceView,
            "description": "An authorized redacted artifact projection.",
            "headers": _PRIVATE_HEADERS,
        },
        **_responses(400, 401, 403, 404, 422, 503),
    },
)
async def get_artifact(
    artifact_id: Annotated[str, Path(pattern=_RESOURCE_ID_PATTERN)],
    response: Response,
    principal: Annotated[
        AuthenticatedPrincipal,
        Depends(require_runtime_resource_reader_principal),
    ],
    executor: Annotated[ArtifactResourceExecutor, Depends(get_artifact_resource_executor)],
) -> ArtifactResourceView:
    try:
        resource = await executor.read(artifact_id, principal=principal)
    except ArtifactResourceServiceError as error:
        _raise_service_problem(error)
    except Exception:
        _raise_unavailable()
    if type(resource) is not ArtifactResource or resource.artifact_id != artifact_id:
        _raise_unavailable()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Authorization"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return _resource_view(resource)


__all__ = ["router", "run_router"]

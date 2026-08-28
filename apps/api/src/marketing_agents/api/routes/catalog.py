"""Authenticated, conditional, read-only routes for the static catalog."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, status
from fastapi.responses import JSONResponse, Response

from marketing_agents.api.catalog_queries import (
    CatalogDocuments,
    CatalogQueryExecutor,
    CatalogQueryUnavailable,
    CatalogRepresentation,
    enrich_instance_runtime_representation,
)
from marketing_agents.api.dependencies import (
    RunResourceExecutor,
    get_catalog_query_executor,
    get_optional_run_resource_executor,
    require_catalog_principal,
)
from marketing_agents.api.schemas.catalog import (
    AgentInstanceDetailResponse,
    AgentInstanceListResponse,
    AgentInstanceRuntimeDetailResponse,
    AgentTemplateDetailResponse,
    AgentTemplateListResponse,
    ApprovalPolicyListResponse,
    CatalogApiModel,
    CatalogHierarchyResponse,
    CatalogProblem,
    CatalogResponse,
    ToolCapabilityListResponse,
)
from marketing_agents.application.policies.catalog_authorization import (
    CatalogAuthorizationError,
)
from marketing_agents.application.services.run_resources import RunResourceServiceError
from marketing_agents.domain.identity import AuthenticatedPrincipal

CATALOG_QUERY_TIMEOUT_SECONDS = 5.0
_CACHE_CONTROL = "private, no-cache"
_NO_STORE = "no-store"
_VARY = "Authorization"
_MAX_IF_NONE_MATCH_LENGTH = 8_192
_TEMPLATE_ID_PATTERN = r"^tpl\.[a-z0-9-]+\.[a-z0-9-]+\.[a-z0-9-]+$"
_INSTANCE_ID_PATTERN = r"^inst\.[a-z0-9-]+\.[a-z0-9-]+\.[a-z0-9-]+\.[0-9]{2}$"

router = APIRouter(prefix="/api/v1", tags=["catalog"])

_REPRESENTATION_HEADERS: dict[str, dict[str, object]] = {
    "ETag": {
        "description": "Strong SHA-256 validator for the exact representation bytes.",
        "schema": {"type": "string", "pattern": '^"[a-f0-9]{64}"$'},
    },
    "Cache-Control": {
        "description": "Private clients must revalidate before reuse.",
        "schema": {"type": "string", "const": _CACHE_CONTROL},
    },
    "Vary": {
        "description": "Shared caches must separate authorization contexts.",
        "schema": {"type": "string", "const": _VARY},
    },
}
_ERROR_HEADERS: dict[str, dict[str, object]] = {
    "Cache-Control": {
        "description": "Failure and absence responses must not be stored.",
        "schema": {"type": "string", "const": _NO_STORE},
    },
    "Vary": _REPRESENTATION_HEADERS["Vary"],
}
_COMMON_RESPONSES: dict[int | str, dict[str, object]] = {
    status.HTTP_200_OK: {
        "description": "A complete authorized static catalog representation.",
        "headers": _REPRESENTATION_HEADERS,
    },
    status.HTTP_304_NOT_MODIFIED: {
        "description": "The selected representation matches If-None-Match.",
        "headers": _REPRESENTATION_HEADERS,
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": CatalogProblem,
        "description": "A complete safe catalog projection is unavailable.",
        "headers": _ERROR_HEADERS,
    },
}
_DETAIL_RESPONSES: dict[int | str, dict[str, object]] = {
    **_COMMON_RESPONSES,
    status.HTTP_404_NOT_FOUND: {
        "model": CatalogProblem,
        "description": "The selected catalog resource does not exist.",
        "headers": _ERROR_HEADERS,
    },
}


def _problem(*, not_found: bool = False) -> JSONResponse:
    code = "catalog_resource_not_found" if not_found else "catalog_unavailable"
    message = (
        "catalog resource was not found" if not_found else "catalog is temporarily unavailable"
    )
    problem = CatalogProblem(code=code, message=message)
    return JSONResponse(
        status_code=(
            status.HTTP_404_NOT_FOUND if not_found else status.HTTP_503_SERVICE_UNAVAILABLE
        ),
        content=problem.model_dump(mode="json", by_alias=True),
        headers={"Cache-Control": _NO_STORE, "Vary": _VARY},
    )


async def _safe_documents(
    executor: CatalogQueryExecutor | None,
    principal: AuthenticatedPrincipal,
) -> CatalogDocuments | None:
    if executor is None:
        return None
    try:
        documents = await asyncio.wait_for(
            executor.read(principal),
            timeout=CATALOG_QUERY_TIMEOUT_SECONDS,
        )
    except CatalogAuthorizationError:
        return None
    except Exception:
        return None
    if (
        type(documents) is not CatalogDocuments
        or type(documents.template_details) is not MappingProxyType
        or type(documents.instance_details) is not MappingProxyType
    ):
        return None
    return documents


def _if_none_match_matches(value: str | None, etag: str) -> bool:
    if value is None or len(value) > _MAX_IF_NONE_MATCH_LENGTH:
        return False
    candidates = tuple(item.strip() for item in value.split(","))
    return "*" in candidates or any(
        candidate.removeprefix("W/") == etag for candidate in candidates
    )


def _representation_response(
    representation: CatalogRepresentation,
    expected_type: type[CatalogApiModel],
    expected_label: str,
    if_none_match: str | None,
) -> Response:
    if type(representation) is not CatalogRepresentation or not representation.is_valid_for(
        expected_type, expected_label
    ):
        return _problem()
    headers = {
        "Cache-Control": _CACHE_CONTROL,
        "ETag": representation.etag,
        "Vary": _VARY,
    }
    if _if_none_match_matches(if_none_match, representation.etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(
        status_code=status.HTTP_200_OK,
        content=representation.content,
        media_type="application/json",
        headers=headers,
    )


@router.get(
    "/catalog",
    response_model=CatalogResponse,
    operation_id="getCatalog",
    responses=_COMMON_RESPONSES,
)
async def get_catalog(
    principal: Annotated[AuthenticatedPrincipal, Depends(require_catalog_principal)],
    executor: Annotated[CatalogQueryExecutor | None, Depends(get_catalog_query_executor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    documents = await _safe_documents(executor, principal)
    if documents is None:
        return _problem()
    return _representation_response(
        documents.catalog,
        CatalogResponse,
        "catalog",
        if_none_match,
    )


@router.get(
    "/catalog/hierarchy",
    response_model=CatalogHierarchyResponse,
    operation_id="getCatalogHierarchy",
    responses=_COMMON_RESPONSES,
)
async def get_catalog_hierarchy(
    principal: Annotated[AuthenticatedPrincipal, Depends(require_catalog_principal)],
    executor: Annotated[CatalogQueryExecutor | None, Depends(get_catalog_query_executor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    documents = await _safe_documents(executor, principal)
    if documents is None:
        return _problem()
    return _representation_response(
        documents.hierarchy,
        CatalogHierarchyResponse,
        "catalog-hierarchy",
        if_none_match,
    )


@router.get(
    "/agent-templates",
    response_model=AgentTemplateListResponse,
    operation_id="listAgentTemplates",
    responses=_COMMON_RESPONSES,
)
async def list_agent_templates(
    principal: Annotated[AuthenticatedPrincipal, Depends(require_catalog_principal)],
    executor: Annotated[CatalogQueryExecutor | None, Depends(get_catalog_query_executor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    documents = await _safe_documents(executor, principal)
    if documents is None:
        return _problem()
    return _representation_response(
        documents.templates,
        AgentTemplateListResponse,
        "agent-templates",
        if_none_match,
    )


@router.get(
    "/agent-templates/{template_id}",
    response_model=AgentTemplateDetailResponse,
    operation_id="getAgentTemplate",
    responses=_DETAIL_RESPONSES,
)
async def get_agent_template(
    template_id: Annotated[str, Path(pattern=_TEMPLATE_ID_PATTERN)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_catalog_principal)],
    executor: Annotated[CatalogQueryExecutor | None, Depends(get_catalog_query_executor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    documents = await _safe_documents(executor, principal)
    if documents is None or not isinstance(documents.template_details, Mapping):
        return _problem()
    try:
        representation = documents.template_details.get(template_id)
    except Exception:
        return _problem()
    if representation is None:
        return _problem(not_found=True)
    return _representation_response(
        representation,
        AgentTemplateDetailResponse,
        f"agent-template:{template_id}",
        if_none_match,
    )


@router.get(
    "/tool-capabilities",
    response_model=ToolCapabilityListResponse,
    operation_id="listToolCapabilities",
    responses=_COMMON_RESPONSES,
)
async def list_tool_capabilities(
    principal: Annotated[AuthenticatedPrincipal, Depends(require_catalog_principal)],
    executor: Annotated[CatalogQueryExecutor | None, Depends(get_catalog_query_executor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    documents = await _safe_documents(executor, principal)
    if documents is None:
        return _problem()
    return _representation_response(
        documents.tool_capabilities,
        ToolCapabilityListResponse,
        "tool-capabilities",
        if_none_match,
    )


@router.get(
    "/approval-policies",
    response_model=ApprovalPolicyListResponse,
    operation_id="listApprovalPolicies",
    responses=_COMMON_RESPONSES,
)
async def list_approval_policies(
    principal: Annotated[AuthenticatedPrincipal, Depends(require_catalog_principal)],
    executor: Annotated[CatalogQueryExecutor | None, Depends(get_catalog_query_executor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    documents = await _safe_documents(executor, principal)
    if documents is None:
        return _problem()
    return _representation_response(
        documents.approval_policies,
        ApprovalPolicyListResponse,
        "approval-policies",
        if_none_match,
    )


@router.get(
    "/agent-instances",
    response_model=AgentInstanceListResponse,
    operation_id="listAgentInstances",
    responses=_COMMON_RESPONSES,
)
async def list_agent_instances(
    principal: Annotated[AuthenticatedPrincipal, Depends(require_catalog_principal)],
    executor: Annotated[CatalogQueryExecutor | None, Depends(get_catalog_query_executor)],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    documents = await _safe_documents(executor, principal)
    if documents is None:
        return _problem()
    return _representation_response(
        documents.instances,
        AgentInstanceListResponse,
        "agent-instances",
        if_none_match,
    )


@router.get(
    "/agent-instances/{instance_id}",
    response_model=AgentInstanceRuntimeDetailResponse | AgentInstanceDetailResponse,
    operation_id="getAgentInstance",
    responses=_DETAIL_RESPONSES,
)
async def get_agent_instance(
    instance_id: Annotated[str, Path(pattern=_INSTANCE_ID_PATTERN)],
    principal: Annotated[AuthenticatedPrincipal, Depends(require_catalog_principal)],
    executor: Annotated[CatalogQueryExecutor | None, Depends(get_catalog_query_executor)],
    runtime_executor: Annotated[
        RunResourceExecutor | None,
        Depends(get_optional_run_resource_executor),
    ],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    documents = await _safe_documents(executor, principal)
    if documents is None or not isinstance(documents.instance_details, Mapping):
        return _problem()
    try:
        representation = documents.instance_details.get(instance_id)
    except Exception:
        return _problem()
    if representation is None:
        return _problem(not_found=True)
    if runtime_executor is not None:
        try:
            runtime_status_before = await runtime_executor.read_instance_statuses(
                (instance_id,),
                principal=principal,
            )
            recent_runs = await runtime_executor.list_recent_instance_runs(
                instance_id,
                limit=5,
                principal=principal,
            )
            runtime_status = await runtime_executor.read_instance_statuses(
                (instance_id,),
                principal=principal,
            )
            if runtime_status != runtime_status_before:
                raise CatalogQueryUnavailable("instance runtime projection changed")
            dynamic = enrich_instance_runtime_representation(
                representation,
                instance_id=instance_id,
                status_summary=runtime_status,
                recent_runs=recent_runs,
            )
        except (CatalogQueryUnavailable, RunResourceServiceError, TypeError, ValueError):
            problem = _problem()
            problem.headers["X-Content-Type-Options"] = "nosniff"
            return problem
        except Exception:
            problem = _problem()
            problem.headers["X-Content-Type-Options"] = "nosniff"
            return problem
        result = _representation_response(
            dynamic,
            AgentInstanceRuntimeDetailResponse,
            f"agent-instance-runtime:{instance_id}",
            if_none_match,
        )
        result.headers["X-Content-Type-Options"] = "nosniff"
        return result
    return _representation_response(
        representation,
        AgentInstanceDetailResponse,
        f"agent-instance:{instance_id}",
        if_none_match,
    )
